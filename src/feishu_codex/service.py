import asyncio
import hashlib
import json
import logging
import time
import uuid

from .config import UserError
from .protocol import AppServer, RpcError, text_input
from .store import encode

ACTIVE = ("starting", "running", "waiting", "cancelling")
INSTRUCTIONS = """在指定任务目录内工作，按要求完成源码、README 和 Git 操作。除非用户明确要求，不推送远程仓库。
不生成独立 Markdown 任务报告，不创建 reports 目录。通过对话返回结果、验证情况和后续事项。
不要读取、输出或提交凭据。保持沙箱和人工审批，不绕过权限限制。"""


class Service:
    def __init__(self, config, store, rpc=None):
        self.cfg, self.db = config, store
        self.events = asyncio.Queue()
        self.rpc = rpc or AppServer(config, self.events)
        self.lock = asyncio.Lock()
        self.leases = {}  # task -> (connection token, monotonic deadline)
        self.live_requests = {}  # raw params stay only in memory
        self.loaded = set()
        self.items = {}
        self.progress_at = {}
        self.started_at = {}
        self.interrupts = {}  # task -> (desired terminal state, monotonic deadline)
        self.connected = False
        self.closing = False
        self.feishu_state = "未配置" if not config.feishu_configured else "等待长连接"
        self.feishu_seen = 0

    def recover(self, why="后台重启；原进程的活动 turn 无法实时恢复，旧审批失效；请查询后明确继续"):
        for t in self.db.all("SELECT * FROM tasks WHERE status IN ('starting','running','waiting','cancelling')"):
            self.db.update(t["id"], status="interrupted", summary=why)
            self.db.event(t["id"], "interrupted", why)
            self.notify(t["id"], "interrupted", why, key=uuid.uuid4().hex)
        for r in self.db.all("SELECT * FROM requests WHERE status IN ('pending','responding')"):
            self.invalidate(r["id"], "invalidated")
        self.db.run("UPDATE dedup SET status='uncertain',result=? WHERE status='processing'",
                    (encode({"error": "重启前消息处理结果不确定；查询任务后使用新的明确指令"}),))
        self.loaded.clear()
        self.live_requests.clear()
        self.started_at.clear()
        self.interrupts.clear()

    def notify(self, tid, kind, body, key=None):
        t = self.db.task(tid)
        self.db.notify(key or f"{tid}:{kind}:{t['turn']}", tid, t["chat"], kind, body)

    def check(self, actor, tid=None, mutation=False, token=None):
        remote = actor != "local"
        if remote and actor not in self.cfg.feishu.get("allowed_users", []):
            raise UserError("拒绝：用户不在 allowed_users 白名单（空白名单拒绝全部远程操作）")
        t = self.db.task(tid) if tid else None
        if t and remote and t["owner"] != actor:
            raise UserError("拒绝：此任务属于其他用户或本地控制台")
        lease = self.leases.get(tid)
        if lease and lease[1] <= time.monotonic():
            self.release(tid, lease[0])
            lease = None
        if mutation and lease and (remote or token != lease[0]):
            raise UserError("任务由 Terminal 控制；飞书仅可查询，请先在终端 /detach")
        return t

    def acquire(self, tid, token):
        self.check("local", tid, True, token)
        self.leases[tid] = (token, time.monotonic() + self.cfg.lease_timeout)
        return self.status(tid)

    def heartbeat(self, tid, token):
        if tid not in self.leases or self.leases[tid][0] != token:
            raise UserError("控制权已释放或过期，请重新 attach")
        self.leases[tid] = (token, time.monotonic() + self.cfg.lease_timeout)

    def release(self, tid, token):
        if tid in self.leases and self.leases[tid][0] == token:
            del self.leases[tid]
            t = self.db.task(tid)
            self.db.notify(f"{tid}:release:{token}", tid, t["chat"], "control",
                           f"Terminal 已释放控制权。状态：{t['status']}\n{t['summary']}")
            for r in self.db.all("SELECT id FROM requests WHERE task=? AND status='pending'", (tid,)):
                self.request_notice(r["id"])

    def status(self, tid, after=0):
        t = self.db.task(tid)
        t["control"] = "terminal" if tid in self.leases else "available"
        t["requests"] = self.db.all("SELECT id,method,params,decisions,status,expires FROM requests WHERE task=? ORDER BY rowid", (tid,))
        for r in t["requests"]:
            r["params"], r["decisions"] = json.loads(r["params"]), json.loads(r["decisions"])
        # Bound IPC size and page by seq; initial history shows most recent 100 entries.
        if after:
            ev = self.db.all("SELECT * FROM events WHERE task=? AND seq>? ORDER BY seq LIMIT 100", (tid, after))
        else:
            ev = list(reversed(self.db.all("SELECT * FROM events WHERE task=? ORDER BY seq DESC LIMIT 100", (tid,))))
        for e in ev:
            e["body"] = json.loads(e["body"])
        t["events"] = ev
        t["notifications"] = self.db.all("SELECT id,kind,status,attempts,error FROM outbox WHERE task=? ORDER BY rowid DESC LIMIT 20", (tid,))
        return t

    async def execute(self, req, actor="local", token=None):
        op, tid = req.get("op"), req.get("task")
        self.check(actor)
        if op == "tasks":
            sql = "SELECT id,cwd,status,summary,updated,closed FROM tasks WHERE " + ("1=1" if req.get("all") else "closed=0")
            return self.db.all(sql + (" ORDER BY created DESC LIMIT 200" if actor == "local" else " AND owner=? ORDER BY created DESC LIMIT 200"),
                               () if actor == "local" else (actor,))
        if op == "diagnose":
            state = self.feishu_state
            if self.cfg.feishu_configured and time.monotonic() - self.feishu_seen > 35:
                state = "长连接进程未报告就绪"
            return {"app_server": "ready" if self.connected else "disconnected", "feishu": state,
                    "remote_users": len(self.cfg.feishu.get("allowed_users", [])),
                    "work_roots": self.cfg.work_roots, "data_dir": self.cfg.data_dir,
                    "socket": self.cfg.socket_path, "protocol": "codex-cli 0.160.0",
                    "remote_events_processing": self.db.one("SELECT count(*) n FROM dedup WHERE status='processing'")["n"],
                    "notifications_pending": self.db.one("SELECT count(*) n FROM outbox WHERE status='pending'")["n"],
                    "notification_failures": self.db.one("SELECT count(*) n FROM outbox WHERE status='failed'")["n"]}
        if op == "status":
            self.check(actor, tid)
            return self.status(tid, req.get("after", 0))
        async with self.lock:
            self.check(actor, tid, True, token)
            if op == "create":
                return self.create(req, actor)
            if op in ("append", "continue"):
                return await self.append(tid, req.get("text", ""))
            if op in ("cancel", "interrupt"):
                return await self.interrupt(tid, cancel=op == "cancel")
            if op == "decide":
                return await self.decide(tid, req.get("request"), req.get("decision"))
            if op == "answer":
                return await self.decide(tid, req.get("request"), answers=req.get("answers"))
            raise UserError("未知操作；使用 help 查看命令")

    def create(self, req, actor):
        prompt = req.get("text", "").strip()
        if not prompt:
            raise UserError("任务正文不能为空")
        if self.db.redact(prompt) != prompt:
            raise UserError("任务包含疑似凭据；请用安全的本机配置引用，不要把凭据发给机器人")
        key = req.get("key")
        fingerprint = hashlib.sha256(encode({k: req.get(k) for k in ("text", "cwd", "mkdir", "thread")}).encode()).hexdigest()
        if key:
            old = self.db.one("SELECT * FROM tasks WHERE owner=? AND idem=?", (actor, key))
            if old:
                if old["fingerprint"] != fingerprint:
                    raise UserError("同一幂等标识对应了不同任务内容")
                return {"task": old["id"], "status": old["status"], "duplicate": True}
        thread = req.get("thread") or None
        if thread and self.db.one("SELECT id FROM tasks WHERE thread=?", (thread,)):
            raise UserError("该会话已绑定任务，请使用对应任务编号继续")
        cwd = self.cfg.workdir(req.get("cwd"), req.get("mkdir", False))
        tid = uuid.uuid4().hex[:12]
        now = time.time()
        self.db.run("INSERT INTO tasks(id,owner,chat,cwd,prompt,pending,thread,status,created,updated,idem,fingerprint) VALUES(?,?,?,?,?,?,?,'queued',?,?,?,?)",
                    (tid, actor, req.get("chat"), cwd, prompt, prompt, thread, now, now, key, fingerprint))
        self.db.event(tid, "created", {"cwd": cwd, "text": prompt})
        return {"task": tid, "status": "queued", "cwd": cwd}

    async def append(self, tid, message):
        message = message.strip()
        if not message:
            raise UserError("继续/追加必须给出明确要求；重启后不会自动重放原任务")
        if self.db.redact(message) != message:
            raise UserError("要求中包含疑似凭据")
        t = self.db.task(tid)
        if t["closed"] or t["status"] == "cancelled":
            raise UserError("任务已取消；请创建新任务")
        if t["status"] in ("starting", "cancelling"):
            raise UserError("任务正在切换状态，请稍后查询再操作")
        if t["status"] in ("running", "waiting"):
            if not self.connected:
                raise UserError("app-server 未连接")
            try:
                await self.rpc.call("turn/steer", {"threadId": t["thread"], "expectedTurnId": t["turn"], "input": text_input(message)})
            except RpcError:
                # A timeout may mean accepted steering; do not retry or start a new turn.
                self.db.event(tid, "steer_uncertain", "追加响应失败；查询进展后再决定，未自动重发")
                raise
            self.db.event(tid, "steered", message)
        else:
            pending = (t["pending"] + "\n\n" if t["status"] == "queued" and t["pending"] else "") + message
            self.db.update(tid, pending=pending, status="queued", summary="")
            self.db.event(tid, "queued_followup", message)
        return {"task": tid, "status": self.db.task(tid)["status"]}

    async def interrupt(self, tid, cancel=False):
        t = self.db.task(tid)
        if cancel:
            self.db.update(tid, closed=1)
            self.db.run("UPDATE ui_sessions SET current_task=NULL,stage='idle',data='{}',flow='' WHERE current_task=?", (tid,))
        if t["status"] == "cancelled":
            return {"task": tid, "status": "cancelled"}
        target = "cancelled" if cancel else "interrupted"
        if t["status"] in ACTIVE:
            self.interrupts[tid] = (target, time.monotonic() + self.cfg.interrupt_timeout)
            self.db.update(tid, status="cancelling")
            try:
                await self.rpc.call("turn/interrupt", {"threadId": t["thread"], "turnId": t["turn"]})
            except Exception:
                # Stop the owning process before releasing the serial execution gate.
                await self.rpc.stop()
                self.connected = False
                self.recover("中断响应不确定，已终止所属 app-server 进程；需要人工检查工作目录")
        for r in self.db.all("SELECT id FROM requests WHERE task=? AND status='pending'", (tid,)):
            self.invalidate(r["id"], "cancelled")
        # Successful interrupt acknowledgement is not turn completion; keep gate closed.
        current = self.db.task(tid)
        if current["status"] != "cancelling":
            self.db.update(tid, status=target, pending=None, summary=current["summary"] or target)
        else:
            self.db.update(tid, pending=None)
        self.db.event(tid, target, "用户请求中断" if not cancel else "用户取消任务")
        return {"task": tid, "status": self.db.task(tid)["status"]}

    def invalidate(self, rid, state):
        self.db.run("UPDATE requests SET status=? WHERE id=? AND status IN ('pending','responding')", (state, rid))
        self.live_requests.pop(rid, None)
        self.request_notice(rid)

    def request_notice(self, rid):
        r = self.db.one("SELECT * FROM requests WHERE id=?", (rid,))
        t = self.db.task(r["task"])
        self.db.notify("request:" + rid, t["id"], t["chat"], "request", {"request": rid}, refresh=True)

    async def decide(self, tid, rid, decision=None, answers=None):
        r = self.db.one("SELECT * FROM requests WHERE id=?", (rid,))
        if not r or r["task"] != tid:
            raise UserError("审批/问题编号与任务不匹配")
        t = self.db.task(tid)
        if r["status"] != "pending" or r["epoch"] != self.rpc.epoch or rid not in self.live_requests:
            raise UserError("请求已处理或失效；不能重复决定")
        if r["expires"] <= time.time():
            await self.expire(r)
            raise UserError("请求已过期")
        p = self.live_requests[rid]
        if t["turn"] != p["turnId"] or t["status"] not in ("waiting", "running"):
            self.invalidate(rid, "invalidated")
            raise UserError("原 turn 已失效")
        if r["method"] == "item/tool/requestUserInput":
            if decision is not None or not isinstance(answers, dict):
                raise UserError("这是普通问题，请使用 answer 并提交问题 ID 到答案数组的 JSON 对象")
            ids = {q["id"] for q in p["questions"]}
            if set(answers) != ids or any(not isinstance(v, list) or not v or any(not isinstance(s, str) or not s.strip() for s in v) for v in answers.values()):
                raise UserError("回答必须覆盖且仅包含所有问题 ID，每个值为非空字符串数组")
            result = {"answers": {k: {"answers": v} for k, v in answers.items()}}
        else:
            if answers is not None or decision not in json.loads(r["decisions"]):
                raise UserError("决定不在此请求允许的范围内")
            if r["method"] == "item/permissions/requestApproval":
                result = {"permissions": p["permissions"] if decision == "accept" else {}, "scope": "turn"}
            else:
                result = {"decision": decision}
        # Persist the claim before writing; a crash never replays a decision.
        self.db.run("UPDATE requests SET status='responding',decision=? WHERE id=?", (decision or "answered", rid))
        try:
            await self.rpc.reply(json.loads(r["rpc_id"]), result)
        except Exception:
            self.invalidate(rid, "uncertain")
            raise UserError("审批响应发送失败，结果不确定；禁止重放，请中断任务并检查") from None
        self.db.run("UPDATE requests SET status='resolved' WHERE id=?", (rid,))
        self.live_requests.pop(rid, None)
        self.request_notice(rid)
        self.db.event(tid, "request_resolved", {"request": rid, "decision": decision or "answered"})
        if not self.db.one("SELECT id FROM requests WHERE task=? AND status='pending'", (tid,)) and t["status"] == "waiting":
            self.db.update(tid, status="running")
        return {"task": tid, "request": rid, "status": "resolved"}

    async def expire(self, r):
        self.invalidate(r["id"], "expired")
        self.db.event(r["task"], "request_expired", "审批/问题超时；中断 turn，不自动批准")
        await self.interrupt(r["task"])

    async def worker(self):
        while True:
            await asyncio.sleep(0.15)
            if not self.connected:
                continue
            if self.db.one("SELECT id FROM tasks WHERE status IN ('starting','running','waiting','cancelling')"):
                continue
            async with self.lock:
                t = self.db.one("SELECT * FROM tasks WHERE status='queued' ORDER BY updated LIMIT 1")
                if not t:
                    continue
                tid = t["id"]
                self.db.update(tid, status="starting")
                try:
                    cwd = self.cfg.workdir(t["cwd"])
                    params = {"cwd": cwd, "approvalPolicy": self.cfg.approval_policy,
                              "approvalsReviewer": "user", "sandbox": self.cfg.sandbox,
                              "developerInstructions": INSTRUCTIONS}
                    if not t["thread"]:
                        res = await self.rpc.call("thread/start", params)
                        t["thread"] = res["thread"]["id"]
                        self.db.update(tid, thread=t["thread"])
                    elif t["thread"] not in self.loaded:
                        # Read actual history before resuming. Never replay old turns/requests.
                        history = await self.rpc.call("thread/read", {"threadId": t["thread"], "includeTurns": True})
                        actual_cwd = history["thread"].get("cwd")
                        if actual_cwd and self.cfg.workdir(actual_cwd) != cwd:
                            raise UserError("历史会话 cwd 与指定任务目录不一致，拒绝恢复")
                        self.db.event(tid, "history_checked", {
                            "thread": t["thread"], "status": history["thread"].get("status"),
                            "turns": [{"id": x["id"], "status": x["status"]} for x in history["thread"].get("turns", [])]})
                        await self.rpc.call("thread/resume", {"threadId": t["thread"], **params})
                    self.loaded.add(t["thread"])
                    self.db.update(tid, turn=None, summary="")
                    result = await self.rpc.call("turn/start", {"threadId": t["thread"], "input": text_input(t["pending"])})
                    turn = result["turn"]["id"]
                    current = self.db.task(tid)
                    if current["status"] == "starting":
                        self.db.update(tid, turn=turn, status="running", pending=None)
                    else:
                        self.db.update(tid, pending=None)
                    self.started_at[tid] = time.monotonic()
                except Exception as e:
                    self.db.update(tid, status="interrupted", summary=self.db.redact(str(e)), pending=None)
                    self.notify(tid, "interrupted", "启动/恢复响应失败；未自动重试。" + self.db.redact(str(e)), key=uuid.uuid4().hex)
                    # The request could have taken effect. Terminate before running another task.
                    await self.rpc.stop()
                    self.connected = False

    async def consume(self):
        while True:
            epoch, msg = await self.events.get()
            if epoch != self.rpc.epoch:
                continue
            try:
                await self.on_event(msg)
            except Exception as e:
                logging.error("protocol event failed: %s", type(e).__name__)
                await self.rpc.stop()
                self.connected = False
                self.recover("协议事件处理异常，已停止 app-server；检查后明确继续")

    async def on_event(self, msg):
        method, p = msg["method"], msg.get("params", {})
        if method == "_disconnected":
            self.connected = False
            self.recover("app-server 异常断开；活动任务中断，旧请求失效，未自动重放")
            return
        thread = p.get("threadId") or p.get("thread", {}).get("id")
        t = self.db.one("SELECT * FROM tasks WHERE thread=?", (thread,)) if thread else None
        if "id" in msg:
            await self.on_request(t, msg)
            return
        if not t:
            return
        tid = t["id"]
        if method == "turn/started":
            self.db.update(tid, turn=p["turn"]["id"], status="running")
        elif method == "serverRequest/resolved":
            for r in self.db.all("SELECT id FROM requests WHERE task=? AND rpc_id=? AND epoch=? AND status='pending'",
                                 (tid, encode(p["requestId"]), self.rpc.epoch)):
                self.invalidate(r["id"], "resolved_by_server")
            if t["status"] == "waiting" and not self.db.one("SELECT id FROM requests WHERE task=? AND status='pending'", (tid,)):
                self.db.update(tid, status="running")
        elif method in ("item/started", "item/completed"):
            item = p["item"]
            self.items[(tid, item["id"])] = self.db.redact(item)
            kind = item.get("type")
            if method == "item/completed" or kind in ("commandExecution", "fileChange"):
                self.db.event(tid, method, self.summarize_item(item))
                if kind == "agentMessage" and item.get("text") and t["status"] != "cancelling":
                    self.db.update(tid, summary=item["text"][-12000:])
                if time.monotonic() - self.progress_at.get(tid, 0) >= self.cfg.progress_interval:
                    self.progress_at[tid] = time.monotonic()
                    self.notify(tid, "progress", self.summarize_item(item), key=f"{tid}:progress:{int(time.time() // self.cfg.progress_interval)}")
        elif method == "turn/completed":
            turn = p["turn"]
            if t["turn"] and t["turn"] != turn["id"]:
                return
            state = {"completed": "completed", "failed": "failed", "interrupted": "interrupted"}.get(turn["status"], "interrupted")
            if t["status"] == "cancelling":
                state = self.interrupts.get(tid, ("interrupted", 0))[0]
            summary = self.db.task(tid)["summary"]
            if turn.get("error"):
                summary = encode(self.db.redact(turn["error"]))
            self.db.update(tid, status=state, turn=turn["id"], pending=None, summary=summary)
            for r in self.db.all("SELECT id FROM requests WHERE task=? AND status='pending'", (tid,)):
                self.invalidate(r["id"], "invalidated")
            self.db.event(tid, state, {"summary": summary})
            self.notify(tid, state, summary or state)
            self.started_at.pop(tid, None)
            self.interrupts.pop(tid, None)
            self.items = {k: v for k, v in self.items.items() if k[0] != tid}
        elif method == "error":
            self.db.event(tid, "error", p.get("error", {}))
            self.notify(tid, "error", p.get("error", {}), key=uuid.uuid4().hex)

    def summarize_item(self, item):
        # No chain-of-thought or raw environment; only operational progress and final text.
        return self.db.redact({k: item[k] for k in ("id", "type", "text", "command", "cwd", "status", "exitCode", "changes") if k in item})

    async def on_request(self, t, msg):
        p, method = msg.get("params", {}), msg["method"]
        if self.db.one("SELECT id FROM requests WHERE epoch=? AND rpc_id=?", (self.rpc.epoch, encode(msg["id"]))):
            return  # server retransmission must never create a second decision record
        supported = {"item/commandExecution/requestApproval", "item/fileChange/requestApproval",
                     "item/permissions/requestApproval", "item/tool/requestUserInput"}
        if not t or method not in supported:
            await self.rpc.reply(msg["id"], error={"code": -32601, "message": "Request unsupported by feishu-codex"})
            if t:
                self.db.event(t["id"], "unsupported_request", method)
                self.notify(t["id"], "blocked", f"暂不支持 {method}；已明确拒绝并中断，请在终端查询", key=uuid.uuid4().hex)
                # Don't block the event consumer waiting for turn/completed.
                asyncio.create_task(self._unsupported_interrupt(t["id"]))
            return
        if t["turn"] != p.get("turnId") or t["status"] not in ("running", "waiting"):
            await self.rpc.reply(msg["id"], error={"code": -32600, "message": "No active matching turn"})
            return
        decisions = [] if method.endswith("requestUserInput") else ["accept", "decline", "cancel"]
        if method == "item/commandExecution/requestApproval" and p.get("availableDecisions") is not None:
            decisions = [x for x in p["availableDecisions"] if isinstance(x, str) and x in decisions]
        if method == "item/permissions/requestApproval":
            decisions = ["accept", "decline"]
        if method == "item/fileChange/requestApproval" and p.get("grantRoot"):
            # This request explicitly asks for session scope; do not label it a one-off grant.
            decisions = ["decline", "cancel"]
        rid = uuid.uuid4().hex[:16]
        display = dict(p)
        item = self.items.get((t["id"], p.get("itemId")))
        if item:
            display["operation"] = self.summarize_item(item)
        display.setdefault("cwd", t["cwd"])
        display["scope"] = "当前 turn" if method == "item/permissions/requestApproval" else "仅本次操作；不保存规则"
        if p.get("grantRoot"):
            display["scope"] = "请求会话级目录写入；本版本仅允许拒绝/取消"
        self.live_requests[rid] = p
        self.db.run("INSERT INTO requests(id,task,rpc_id,epoch,method,params,decisions,status,expires) VALUES(?,?,?,?,?,?,?,'pending',?)",
                    (rid, t["id"], encode(msg["id"]), self.rpc.epoch, method, encode(self.db.redact(display)), encode(decisions), time.time() + self.cfg.approval_timeout))
        self.db.update(t["id"], status="waiting")
        self.db.event(t["id"], "request", {"id": rid, "method": method, "params": display})
        self.request_notice(rid)

    async def _unsupported_interrupt(self, tid):
        async with self.lock:
            await self.interrupt(tid)

    async def maintain(self):
        while True:
            await asyncio.sleep(1)
            for tid, (target, deadline) in list(self.interrupts.items()):
                if self.db.task(tid)["status"] != "cancelling":
                    self.interrupts.pop(tid, None)
                elif deadline < time.monotonic():
                    async with self.lock:
                        if self.db.task(tid)["status"] == "cancelling":
                            self.connected = False
                            await self.rpc.stop()
                            reason = "中断回执超时，已终止所属 app-server；请检查任务目录中的已发生操作"
                            self.recover(reason)
                            self.db.update(tid, status=target, summary=reason)
                            self.notify(tid, target, reason)
            for tid, (token, deadline) in list(self.leases.items()):
                if deadline < time.monotonic():
                    self.release(tid, token)
            for r in self.db.all("SELECT * FROM requests WHERE status='pending' AND expires<?", (time.time(),)):
                async with self.lock:
                    if self.db.one("SELECT status FROM requests WHERE id=?", (r["id"],))["status"] == "pending":
                        await self.expire(r)
            for tid, started in list(self.started_at.items()):
                if self.db.task(tid)["status"] not in ACTIVE:
                    self.started_at.pop(tid, None)
                    continue
                if time.monotonic() - started > self.cfg.turn_timeout:
                    async with self.lock:
                        self.started_at.pop(tid, None)
                        await self.interrupt(tid)
                        self.notify(tid, "timeout", "turn 超时，已请求中断", key=uuid.uuid4().hex)

    async def connect_loop(self):
        delay = 1
        while True:
            if not self.connected:
                async with self.lock:
                    try:
                        await self.rpc.stop()
                        await self.rpc.start()
                        self.loaded.clear()
                        self.connected = True
                        delay = 1
                        logging.info("app-server initialized")
                    except Exception as e:
                        logging.warning("app-server initialization failed: %s", type(e).__name__)
                        delay = min(delay * 2, 30)
            await asyncio.sleep(delay)
