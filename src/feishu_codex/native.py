"""Task-scoped app-server gateway for the official Codex TUI.

The daemon remains the only stdio owner. A private, temporary Unix WebSocket
endpoint translates client IDs, enforces the task lease, and routes decisions
through the same durable approval records as Feishu. No TCP listener.
"""
import asyncio
import hashlib
import json
import logging
import os
import socket
import stat
import struct
import time
import uuid
from pathlib import Path

from websockets.asyncio.server import unix_serve
from websockets.exceptions import ConnectionClosed

from .config import UserError
from .protocol import RpcError
from .service import ACTIVE, INSTRUCTIONS
from .store import encode

READ_METHODS = {
    "model/list", "modelProvider/capabilities/read", "account/read", "account/rateLimits/read",
    "account/usage/read", "configRequirements/read", "collaborationMode/list",
    "experimentalFeature/list", "mcpServerStatus/list", "app/list", "app/installed",
    "plugin/installed", "remoteControl/status/read",
}
THREAD_READ_METHODS = {
    "thread/read", "thread/turns/list", "thread/items/list", "thread/goal/get",
    "thread/queue/list", "thread/attachment/list", "thread/backgroundTerminals/list",
}


def socket_prefix(config):
    return "tui-" + hashlib.sha256(config.socket_path.encode()).hexdigest()[:8] + "-"


def clean_native_sockets(config):
    # Caller holds the daemon's socket flock. Other service configurations have
    # different prefixes even when they share the same private parent directory.
    for path in Path(config.socket_path).parent.glob(socket_prefix(config) + "*.sock"):
        info = path.lstat()
        if info.st_uid == os.getuid() and stat.S_ISSOCK(info.st_mode):
            path.unlink()


class NativeEndpoint:
    def __init__(self, service, tid, token):
        self.s, self.tid, self.token = service, tid, token
        self.thread = service.db.task(tid)["thread"]
        self.epoch = service.rpc.epoch
        self.path = Path(service.cfg.socket_path).parent / (socket_prefix(service.cfg) + uuid.uuid4().hex[:8] + ".sock")
        self.server = self.ws = None
        self.initialized = self.resumed = self.closed = False
        self.resuming = False
        self.bootstrap = []
        self.outgoing = asyncio.Queue(maxsize=2048)
        self.sent_requests = set()
        self.jobs = set()
        self.cleanup = None
        self.sandbox_policy = None
        self.model = self.service_tier = None
        self.runtime_roots = []
        self.request_ids = set()

    async def start(self):
        parent = self.path.parent.stat()
        if parent.st_uid != os.getuid() or parent.st_mode & 0o077:
            raise UserError("原生终端 socket 的父目录必须由当前用户拥有且权限为 700")
        if len(os.fsencode(self.path)) >= 104:
            raise UserError("本地 socket 路径过长，请缩短配置的 socket_path")
        self.server = await unix_serve(self.handle, str(self.path), max_size=8 * 1024 * 1024,
                                       open_timeout=5, close_timeout=1, ping_interval=10, ping_timeout=10)
        self.path.chmod(0o600)
        self.s.native[self.tid] = self
        return {"endpoint": "unix://" + str(self.path), "thread": self.thread,
                "cwd": self.s.db.task(self.tid)["cwd"], "lease_timeout": self.s.cfg.lease_timeout}

    def check(self):
        if self.closed or self.epoch != self.s.rpc.epoch or not self.s.connected:
            raise UserError("后台连接已变化，请退出后重新 attach；任务不会自动重放")
        lease = self.s.leases.get(self.tid)
        if not lease or lease[0] != self.token or lease[1] <= time.monotonic():
            raise UserError("终端控制权已失效，请重新 attach")
        return self.s.check("local", self.tid, True, self.token)

    def queue(self, message):
        if self.closed:
            return
        try:
            self.outgoing.put_nowait(self.s.db.redact(message))
        except asyncio.QueueFull:
            # A stuck terminal must never block Codex or keep the lease indefinitely.
            self.disconnect()

    def disconnect(self):
        if self.closed:
            return
        self.closed = True
        if self.s.native.get(self.tid) is self:
            self.s.native.pop(self.tid)
        self.s.release(self.tid, self.token)
        self.cleanup = asyncio.create_task(self.close())

    async def close(self):
        if self.server:
            self.server.close()
            await self.server.wait_closed()
        await asyncio.gather(*list(self.jobs), return_exceptions=True)
        self.path.unlink(missing_ok=True)

    def resolved(self, rid):
        if rid in self.sent_requests:
            self.queue({"method": "serverRequest/resolved", "params": {
                "threadId": self.thread, "requestId": "native:" + rid}})

    def request(self, row):
        rid = row["id"]
        if rid in self.sent_requests or row["status"] != "pending" or row["epoch"] != self.epoch:
            return
        params = self.s.live_requests.get(rid)
        if not params:
            return
        params = dict(params)
        if row["method"] == "item/commandExecution/requestApproval":
            params["availableDecisions"] = json.loads(row["decisions"])
        self.sent_requests.add(rid)
        self.queue({"id": "native:" + rid, "method": row["method"], "params": params})

    def publish(self, message):
        if self.closed:
            return
        p = message.get("params", {})
        thread = p.get("threadId") or p.get("thread", {}).get("id")
        if thread != self.thread:
            return
        if self.resuming:
            if len(self.bootstrap) >= 2048:
                self.disconnect()
            else:
                self.bootstrap.append(message)
            return
        if not self.resumed:
            return
        if "id" in message:
            row = self.s.db.one("SELECT * FROM requests WHERE task=? AND epoch=? AND rpc_id=?",
                                (self.tid, self.epoch, encode(message["id"])))
            if row:
                self.request(row)
        elif message["method"] != "serverRequest/resolved":
            self.queue(message)

    async def handle(self, ws):
        raw = ws.transport.get_extra_info("socket").getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
        if struct.unpack("3i", raw)[1] != os.getuid() or self.ws is not None or self.closed:
            await ws.close(code=1008, reason="Only the owning terminal may connect")
            return
        self.ws = ws
        async def sender():
            while True:
                await ws.send(encode(await self.outgoing.get()))
        writer = asyncio.create_task(sender())
        try:
            async for data in ws:
                self.check()
                msg = json.loads(data)
                if not isinstance(msg, dict):
                    raise UserError("Invalid JSON-RPC message")
                if len(self.jobs) >= 64:
                    raise UserError("Too many pending terminal requests")
                task = asyncio.create_task(self.receive(msg))
                self.jobs.add(task)
                task.add_done_callback(self.jobs.discard)
        except (ConnectionClosed, ValueError, UserError):
            pass
        finally:
            writer.cancel()
            await asyncio.gather(writer, return_exceptions=True)
            # Submitted RPCs finish in the daemon; disconnect never replays/cancels a turn.
            self.disconnect()

    async def receive(self, msg):
        rid = msg.get("id")
        method = msg.get("method")
        try:
            self.check()
            if method is None:
                await self.answer(msg)
                return
            if method == "initialized":
                return
            if "id" not in msg or not isinstance(rid, (str, int)):
                raise UserError("Missing request ID")
            if rid in self.request_ids:
                raise UserError("Duplicate request ID; not replayed")
            if len(self.request_ids) >= 10000:
                raise UserError("连接请求过多，请退出后重新 attach")
            self.request_ids.add(rid)
            result = await self.dispatch(method, msg.get("params") or {})
            if "id" in msg:
                self.queue({"id": rid, "result": result})
            if method == "thread/resume":
                self.resuming = False
                self.resumed = True
                buffered, self.bootstrap = self.bootstrap, []
                for event in buffered:
                    self.publish(event)
                for row in self.s.db.all("SELECT * FROM requests WHERE task=? AND status='pending'", (self.tid,)):
                    self.request(row)
        except Exception as e:
            if method == "thread/resume":
                self.resuming = False
                self.bootstrap.clear()
            error = str(e) if isinstance(e, (UserError, RpcError, ValueError)) else type(e).__name__
            # Do not log arbitrary wire strings, which may contain credentials.
            logging.info("native RPC rejected task=%s type=%s", self.tid, type(e).__name__)
            if method is not None and "id" in msg:
                self.queue({"id": rid, "error": {"code": -32600, "message": error}})
            else:
                # JSON-RPC responses cannot themselves receive an error response.
                # Close the UI on a rejected decision; the original request stays pending.
                self.s.db.event(self.tid, "terminal_decision_rejected", error)
                self.disconnect()

    async def dispatch(self, method, p):
        t = self.check()
        if method == "initialize":
            if self.initialized:
                raise UserError("Already initialized")
            self.initialized = True
            return self.s.rpc.initialize_result
        if not self.initialized:
            raise UserError("Initialize first")
        if not isinstance(p, dict):
            raise UserError("Invalid params")
        if p.get("threadId") and p["threadId"] != self.thread:
            raise UserError("此终端仅绑定当前任务")
        if method == "thread/loaded/list":
            return {"data": [self.thread] if self.thread in self.s.loaded else [], "nextCursor": None}
        if method == "thread/list":
            result = await self.s.rpc.call("thread/read", {"threadId": self.thread})
            thread = result["thread"]
            matches = not p.get("cursor") and not p.get("archived")
            for key in ("cwd", "parentThreadId"):
                if p.get(key) and p[key] != thread.get(key):
                    matches = False
            if p.get("ancestorThreadId"):
                matches = False  # Cross-task/subagent navigation is not exposed by this gateway.
            return {"data": [thread] if matches else [], "nextCursor": None}
        if method in READ_METHODS:
            return await self.s.rpc.call(method, p)
        if method in ("config/value/write", "config/batchWrite"):
            edits = p.get("edits", []) if method == "config/batchWrite" else [p]
            trust_key = "projects." + json.dumps(t["cwd"], ensure_ascii=False) + ".trust_level"
            if p.get("filePath") or not edits or any(
                e.get("keyPath") != trust_key or e.get("value") != "trusted" for e in edits
            ):
                raise UserError("接管只允许确认当前项目的目录信任，不允许写入其他 Codex 配置")
            if method == "config/batchWrite":
                p = {**p, "reloadUserConfig": False}
            return await self.s.rpc.call(method, p)
        if method in ("config/read", "permissionProfile/list", "skills/list", "hooks/list", "plugin/list"):
            p = dict(p)
            if method in ("skills/list", "hooks/list", "plugin/list"):
                p["cwds"] = [t["cwd"]]
            else:
                p["cwd"] = t["cwd"]
            result = await self.s.rpc.call(method, p)
            if method == "config/read" and isinstance(result.get("config"), dict):
                result["config"].update(approval_policy=self.s.cfg.approval_policy,
                                         approvals_reviewer="user", sandbox_mode=self.s.cfg.sandbox)
            return result
        if p.get("threadId") != self.thread:
            raise UserError("此终端仅绑定一个任务；切换任务请退出后 attach 对应编号")
        if method in THREAD_READ_METHODS:
            return await self.s.rpc.call(method, p)
        if method == "thread/unsubscribe":
            # Do not unsubscribe the daemon's shared stdio connection.
            return {"status": "unsubscribed"}
        if method == "thread/resume":
            async with self.s.lock:
                t = self.check()
                self.resuming = True
                cwd = self.s.cfg.workdir(t["cwd"])
                if self.thread not in self.s.loaded:
                    history = await self.s.rpc.call("thread/read", {"threadId": self.thread, "includeTurns": True})
                    if self.s.cfg.workdir(history["thread"]["cwd"]) != cwd:
                        raise UserError("会话目录与任务不一致，拒绝恢复")
                params = {k: p[k] for k in ("excludeTurns", "initialTurnsPage") if k in p}
                params.update(threadId=self.thread, cwd=cwd, approvalPolicy=self.s.cfg.approval_policy,
                              approvalsReviewer="user", sandbox=self.s.cfg.sandbox, developerInstructions=INSTRUCTIONS)
                result = await self.s.rpc.call(method, params)
                self.s.loaded.add(self.thread)
                self.sandbox_policy = result.get("sandbox")
                self.model, self.service_tier = result.get("model"), result.get("serviceTier")
                self.runtime_roots = result.get("runtimeWorkspaceRoots", [])
                return result
        if method not in ("turn/start", "turn/steer", "turn/interrupt"):
            raise UserError(f"任务接管暂不支持 {method}；可退出后使用独立 CLI，或在飞书管理任务")
        if not self.resumed:
            raise UserError("请先恢复当前任务会话")
        async with self.s.lock:
            t = self.check()
            if t["closed"]:
                raise UserError("任务已结束")
            if method == "turn/interrupt":
                if p.get("turnId") != t["turn"]:
                    raise UserError("Turn 已变化，请刷新状态")
                await self.s.interrupt(self.tid)
                return {}
            if t["status"] in ("queued", "starting", "cancelling"):
                raise UserError("任务正在排队或切换状态，请稍后再提交")
            if self.s.db.one("SELECT id FROM tasks WHERE id!=? AND status IN ('starting','running','waiting','cancelling')", (self.tid,)):
                raise UserError("另一任务正在执行；后台保持串行，请等待该任务完成")
            self.validate_turn(p, t)
            p = dict(p)
            # The TUI spells an inherited standard tier as "default".
            for key in ("serviceTier", "serviceTierForTurn"):
                if self.service_tier is None and p.get(key) == "default":
                    p.pop(key)
            if method == "turn/steer" and p.get("expectedTurnId") != t["turn"]:
                raise UserError("Turn 已变化，请刷新状态")
            running = t["status"] in ACTIVE
            if method == "turn/steer" and not running:
                raise UserError("没有活动 turn，请重新提交为后续一轮")
            if not running:
                self.s.db.update(self.tid, status="starting", turn=None, summary="")
            try:
                # Native turn/start can also steer according to the installed protocol.
                # Explicitly use turn/steer for an already-active turn.
                if running:
                    result = await self.s.rpc.call("turn/steer", {
                        "threadId": self.thread, "expectedTurnId": t["turn"], "input": p["input"]})
                    if method == "turn/start":
                        history = await self.s.rpc.call("thread/read", {"threadId": self.thread, "includeTurns": True})
                        turn = next(x for x in history["thread"]["turns"] if x["id"] == t["turn"])
                        result = {"turn": turn}
                else:
                    result = await self.s.rpc.call("turn/start", p)
                    if self.s.db.task(self.tid)["status"] == "starting":
                        self.s.db.update(self.tid, status="running", turn=result["turn"]["id"], pending=None)
                    self.s.started_at[self.tid] = time.monotonic()
                self.s.db.event(self.tid, "terminal_steered" if running else "terminal_turn", {"input": p["input"]})
                return result
            except Exception:
                if not running:
                    # Start may already have taken effect; keep the serial gate until process stops.
                    await self.s.rpc.stop()
                    self.s.connected = False
                    self.s.recover("Terminal 启动 turn 响应不确定；未重试，请检查任务后继续")
                else:
                    self.s.db.event(self.tid, "steer_uncertain", "Terminal 追加响应不确定；未自动重试")
                raise

    def validate_turn(self, p, t):
        if self.s.db.redact(p.get("input")) != p.get("input"):
            raise UserError("输入中包含疑似凭据")
        if not isinstance(p.get("input"), list) or not p["input"]:
            raise UserError("请输入任务要求")
        if p.get("cwd") and self.s.cfg.workdir(p["cwd"]) != t["cwd"]:
            raise UserError("接管时不能改变任务目录")
        if p.get("approvalPolicy") not in (None, self.s.cfg.approval_policy):
            raise UserError("接管时请沿用后台的人工审批策略")
        if p.get("approvalsReviewer") not in (None, "user"):
            raise UserError("接管时不允许自动审批")
        if p.get("sandboxPolicy") not in (None, self.sandbox_policy):
            raise UserError("接管时不能改变后台沙箱范围")
        if p.get("model") not in (None, self.model):
            raise UserError("接管时沿用当前会话模型，暂不支持更换模型")
        for key in ("serviceTier", "serviceTierForTurn"):
            if p.get(key) not in (None, self.service_tier) and not (self.service_tier is None and p.get(key) == "default"):
                raise UserError("接管时沿用当前服务配置")
        mode = p.get("collaborationMode")
        if mode and mode.get("settings", {}).get("model") not in (None, self.model):
            raise UserError("协作模式不能更换当前会话模型")
        if p.get("runtimeWorkspaceRoots") not in (None, self.runtime_roots):
            raise UserError("接管时不能改变工作根目录")
        for key in ("permissions", "environments"):
            if p.get(key) is not None:
                raise UserError(f"接管时暂不支持覆盖 {key}；沿用现有后台配置")

    async def answer(self, msg):
        wire_id = msg.get("id")
        if not isinstance(wire_id, str) or not wire_id.startswith("native:"):
            raise UserError("未知授权/问题编号")
        rid = wire_id.removeprefix("native:")
        if rid not in self.sent_requests:
            raise UserError("此请求未发送给当前终端")
        row = self.s.db.one("SELECT * FROM requests WHERE id=? AND task=?", (rid, self.tid))
        if not row or row["epoch"] != self.epoch:
            raise UserError("审批记录已失效")
        result = msg.get("result")
        if not isinstance(result, dict):
            raise UserError("未收到有效决定；保持请求未处理")
        req = {"op": "decide", "task": self.tid, "request": rid}
        if row["method"] == "item/tool/requestUserInput":
            req.update(op="answer", answers={k: v["answers"] for k, v in result["answers"].items()})
        elif row["method"] == "item/permissions/requestApproval":
            permissions = result.get("permissions", {})
            if result.get("scope", "turn") != "turn" or result.get("strictAutoReview"):
                raise UserError("仅支持当前 turn 的所请求权限或拒绝；不支持会话级授权")
            req["decision"] = "accept" if permissions else "decline"
            req["permissions"] = permissions
        else:
            req["decision"] = result.get("decision")
        await self.s.execute(req, token=self.token)


async def open_native(service, tid, token):
    async with service.lock:
        t = service.check("local", tid, True, token)
        if t["closed"]:
            raise UserError("任务已结束，请使用 status 查看历史")
        if not t["thread"]:
            raise UserError("任务尚未创建 Codex 会话，请等待排队启动后再 attach")
        if not service.connected or not service.rpc.initialize_result:
            raise UserError("app-server 尚未就绪，请稍后重试")
        if tid in service.native:
            raise UserError("此任务已有原生终端连接")
        service.cfg.workdir(t["cwd"])
        service.acquire(tid, token)
        endpoint = NativeEndpoint(service, tid, token)
        try:
            return await endpoint.start()
        except Exception:
            endpoint.disconnect()
            raise
