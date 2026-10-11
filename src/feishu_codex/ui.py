"""Persistent, explicitly selected task context and step-by-step Feishu cards."""
import asyncio
import json
import time
import uuid
from pathlib import Path

from .config import UserError
from .presentation import card, markdown, metadata
from .store import encode

MENU_KEYS = {"create_task": "create", "input_command": "command", "list_tasks": "list", "end_task": "end"}
TEXT_ACTIONS = {"菜单": "home", "menu": "home", "开始": "home", "创建任务": "create",
                "输入命令": "command", "任务列表": "list", "结束任务": "end",
                "取消向导": "home"}


def div(text):
    return {"tag": "div", "text": {"tag": "plain_text", "content": text}}


def button(label, action, **value):
    return {"tag": "button", "text": {"tag": "plain_text", "content": label},
            "type": "default", "value": {"ui": action, **value}}


def actions(buttons):
    return [{"tag": "action", "actions": buttons[i:i + 3]} for i in range(0, len(buttons), 3)]


class Wizard:
    def __init__(self, service):
        self.s = service
        self.locks = {}
        self.recent = {}

    def session(self, actor, chat):
        db = self.s.db
        row = db.one("SELECT * FROM ui_sessions WHERE owner=?", (actor,))
        if not row:
            row = {"owner": actor, "chat": chat or "open_id:" + actor, "current_task": None,
                   "stage": "idle", "data": "{}", "flow": uuid.uuid4().hex, "expires": time.time() + 1800}
            db.run("INSERT INTO ui_sessions VALUES(?,?,?,?,?,?,?)", tuple(row.values()))
        elif chat and row["chat"] != chat:
            if not row["chat"].startswith("open_id:"):
                raise UserError("操作不属于当前私聊上下文")
            row["chat"] = chat
            self.save(row)
        return row

    def save(self, row):
        self.s.db.run("UPDATE ui_sessions SET chat=?,current_task=?,stage=?,data=?,flow=?,expires=? WHERE owner=?",
                      tuple(row[k] for k in ("chat", "current_task", "stage", "data", "flow", "expires", "owner")))

    def stage(self, row, stage, data=None):
        row.update(stage=stage, data=encode(data or {}), flow=uuid.uuid4().hex, expires=time.time() + 1800)
        self.save(row)

    def context(self, row, mutation=False):
        if not row["current_task"]:
            raise UserError("尚未选择当前任务，请先点击“任务列表”选择，或“创建任务”")
        return self.s.check(row["owner"], row["current_task"], mutation)

    def home(self, row, note="", result=None):
        context = "尚未选择任务"
        if row["current_task"]:
            t = self.s.db.task(row["current_task"])
            context = f"当前任务：{t['id']} · {t['status']}\n目录：{t['cwd']}\n标签：{t['prompt'][:80]}"
            if t["id"] in self.s.leases:
                context += "\nTerminal 正在控制，飞书仅可查询。"
            elif not t["closed"]:
                context += "\n可直接发送消息继续此任务；等待问题时作为回答，操作授权请点审批卡片。"
        elements = [div(context)]
        if note:
            elements.append(div(note))
        if result is not None:
            elements += [{"tag": "hr"}] + markdown(result or "等待进展")
        return card("Codex 任务控制台", elements + actions([
            button("创建任务", "create"), button("输入命令", "command", task=row["current_task"]),
            button("任务列表", "list"), button("结束任务", "end", task=row["current_task"]),
            button("查询结果", "status", task=row["current_task"])]))

    def directories(self, row, offset=0):
        root = Path(self.s.cfg.work_roots[0])
        names = []
        if root.exists():
            for p in root.iterdir():
                if p.is_dir() and not p.name.startswith("."):
                    try:
                        self.s.cfg.workdir(str(p))
                        if p.resolve().is_relative_to(root.resolve()):
                            names.append(p.name)
                    except (UserError, OSError):
                        pass
        names.sort()
        offset = max(0, int(offset))
        buttons = [button(name, "directory", name=name, flow=row["flow"]) for name in names[offset:offset + 12]]
        if offset + 12 < len(names):
            buttons.append(button("下一页", "dirs_page", offset=offset + 12, flow=row["flow"]))
        if offset:
            buttons.append(button("第一页", "dirs_page", offset=0, flow=row["flow"]))
        return card("创建任务 · 1/2 选择目录", [div(
            f"默认根目录：{root}\n点击已有目录，或直接发送一个新名字（如 weather-demo），新目录将在创建任务时自动建立。\n已有目录 {len(names)} 个。发送“取消向导”退出。")] + actions(buttons))

    def task_list(self, row, offset=0, history=False):
        offset = max(0, int(offset))
        tasks = self.s.db.all("SELECT id,prompt,status FROM tasks WHERE owner=? AND closed=? ORDER BY created DESC LIMIT 13 OFFSET ?", (row["owner"], int(history), offset))
        elements = [div("已结束任务仅供查看历史，不能重新选为当前任务。" if history else "点击标签明确切换当前上下文；已结束任务请在历史中查看。")]
        for t in tasks[:12]:
            elements += [div(f"{t['id']} · {t['status']}\n{t['prompt'][:120]}")]
            elements += actions([button("查看结果" if history else "选择此任务", "history_status" if history else "select", task=t["id"])])
        if not tasks:
            elements.append(div("没有已结束任务。" if history else "没有未结束的任务，点击下方创建。"))
        navigation = [button("创建任务", "create"), button("当前任务" if history else "已结束任务", "list" if history else "history")]
        list_action = "history" if history else "list"
        if len(tasks) > 12:
            navigation.append(button("下一页", list_action, offset=offset + 12))
        if offset:
            navigation.append(button("第一页", list_action, offset=0))
        return card("已结束任务 · 历史" if history else "任务列表 · 选择上下文", elements + actions(navigation))

    def live_question(self, row, rid):
        t = self.context(row, mutation=True)
        r = self.s.db.one("SELECT * FROM requests WHERE id=? AND task=?", (rid, t["id"]))
        p = self.s.live_requests.get(rid)
        if (not r or r["method"] != "item/tool/requestUserInput" or r["status"] != "pending"
                or r["epoch"] != self.s.rpc.epoch or not p or r["expires"] <= time.time()
                or p["turnId"] != t["turn"] or t["status"] not in ("running", "waiting") or t["closed"]):
            raise UserError("问题已处理、过期或失效；本条消息未提交，请查询任务状态")
        return p

    def question_choices(self, row, note=""):
        """Never infer a question when more than one could receive the answer."""
        data = json.loads(row["data"])
        elements = [metadata(self.context(row)), div(note or "请选择要回答的问题，再发送答案。")]
        for r in self.s.db.all("SELECT id FROM requests WHERE task=? AND status='pending' AND method='item/tool/requestUserInput' ORDER BY rowid", (row["current_task"],)):
            if data.get("answers") and data.get("request") != r["id"]:
                continue
            p = self.live_question(row, r["id"])
            for q in p["questions"]:
                if r["id"] == data.get("request") and q["id"] in data.get("answers", {}):
                    continue
                elements.append(div(f"问题：{q['question']}\n请求：{r['id']} · 问题 ID：{q['id']}"))
                elements += actions([button("回答此问题", "answer_question", task=row["current_task"],
                                            request=r["id"], question=q["id"], flow=row["flow"])])
        return card("选择要回答的问题", elements)

    def question_prompt(self, row, q):
        lines = [q["question"]]
        for i, option in enumerate(q.get("options") or [], 1):
            lines.append(f"{i}. {option['label']}：{option.get('description', '')}")
        lines.append("请直接发送答案；选项可回复编号、字母或完整选项名称。回答不会批准操作。")
        return card("回答问题（非操作授权）", [metadata(self.context(row)), div("\n".join(lines))])

    async def chat(self, row, text):
        t = self.context(row, mutation=True)
        if t["closed"]:
            raise UserError("所选任务已结束，请创建或选择其他任务；本条消息未提交")
        if not text:
            raise UserError("消息不能为空")
        if self.s.db.redact(text) != text:
            raise UserError("消息中包含疑似凭据，请使用本机安全配置")
        data = json.loads(row["data"]) if row["stage"] == "answer" else {}
        requests = self.s.db.all("SELECT id,method FROM requests WHERE task=? AND status='pending' ORDER BY rowid", (t["id"],))
        questions = [r for r in requests if r["method"] == "item/tool/requestUserInput"]
        rid, qid = data.get("request"), data.get("question")
        if row["stage"] == "answer" and not questions and not rid:
            raise UserError("待回答问题已处理或失效，本条消息未提交；发送“取消向导”后可继续任务")
        if rid:
            p = self.live_question(row, rid)
        elif len(questions) == 1:
            rid = questions[0]["id"]
            p = self.live_question(row, rid)
            if len(p["questions"]) == 1:
                qid = p["questions"][0]["id"]
        if questions and not qid:
            self.stage(row, "answer", data)
            return self.question_choices(row, "有多个待回答问题，本条消息尚未提交。请先选择问题，再发送答案。")
        if rid:
            q = next((q for q in p["questions"] if q["id"] == qid), None)
            if not q:
                raise UserError("所选问题已失效，请重新选择")
            if q.get("isSecret"):
                raise UserError("这是敏感问题，请通过 Terminal 回答；本条消息未提交")
            options = q.get("options") or []
            # Only unambiguous shorthand; do not guess from sentences such as “方案 B”.
            index = int(text) - 1 if text.isascii() and text.isdigit() and len(text) < 4 else -1
            if len(text) == 1 and "A" <= text.upper() <= "Z":
                index = ord(text.upper()) - ord("A")
            labels = [option["label"] for option in options]
            answer = options[index]["label"] if text not in labels and 0 <= index < len(options) else text
            answers = {**data.get("answers", {}), qid: [answer]}
            if len(answers) < len(p["questions"]):
                self.stage(row, "answer", {"request": rid, "answers": answers})
                return self.question_choices(row, "已暂存本题回答；请选择其余问题。全部回答后才会提交给 Codex。")
            await self.s.execute({"op": "answer", "task": t["id"], "request": rid, "answers": answers}, row["owner"])
            self.stage(row, "idle")
            return self.home(row, "回答已提交给当前任务；操作授权仍需单独处理。")
        if requests:
            raise UserError("当前任务等待操作授权，请使用审批卡片批准或拒绝；聊天内容不会作为授权，本条消息未提交")
        await self.s.execute({"op": "append", "task": t["id"], "text": text}, row["owner"])
        self.stage(row, "idle")
        return self.home(row, "消息已提交给当前任务。可直接发送下一条要求。")

    async def handle(self, req):
        actor = req["actor"]
        self.s.check(actor)
        text = req.get("text", "").strip()
        value = req.get("card") or {}
        action = value.get("ui") or MENU_KEYS.get(req.get("menu")) or TEXT_ACTIONS.get(text.removeprefix("/"))
        if req.get("menu") and not action:
            raise UserError("未知菜单 event_key，请检查菜单配置")
        if not action and req.get("card"):
            return None
        if not req.get("card") and not req.get("menu") and req.get("chat_type") != "p2p":
            raise UserError("按钮向导仅支持机器人私聊")
        # Approval answers/explicit task commands must never become task-description text.
        first = text.split(maxsplit=1)[0] if text else ""
        explicit = first in {"answer", "decide", "status", "tasks", "diagnose", "help", "interrupt", "cancel"}
        explicit = explicit or text.startswith(("create --", "append ", "continue "))
        if not action and (text.startswith("/") or explicit):
            return None
        lock = self.locks.setdefault(actor, asyncio.Lock())
        if lock.locked():
            raise UserError("上一项操作正在处理，请稍候；可发送 status <任务编号> 查询，不必连续点击")
        async with lock:
            row = self.session(actor, req.get("chat"))
            signature = encode([action, value])
            recent = self.recent.get(actor)
            if action in ("create", "list", "history", "command", "end", "select") and recent:
                sig, flow, at, result = recent
                if sig == signature and flow == row["flow"] and time.monotonic() - at < 1:
                    return {**result, "repeated": True}
            if action in ("command", "end", "status") and "task" in value and value["task"] != row["current_task"]:
                raise UserError("此卡片对应的任务已不是当前任务，请在任务列表重新选择")
            if not action and row["stage"] != "idle" and row["expires"] < time.time():
                self.stage(row, "idle")
                raise UserError("向导已过期，请重新点击按钮")
            if action in ("directory", "dirs_page", "confirm_end", "answer_question"):
                if value.get("flow") != row["flow"] or row["expires"] < time.time():
                    raise UserError("此向导卡片已失效，请使用最新卡片")
            if action == "home":
                self.stage(row, "idle")
                output = self.home(row)
            elif action == "create":
                self.stage(row, "directory")
                output = self.directories(row)
            elif action == "dirs_page":
                if row["stage"] != "directory":
                    raise UserError("目录选择已结束")
                output = self.directories(row, value.get("offset", 0))
            elif action == "directory" or not action and row["stage"] == "directory":
                if row["stage"] != "directory":
                    raise UserError("目录选择已结束")
                name = value.get("name") if action else text
                if not isinstance(name, str) or name in ("", ".", "..") or len(name) > 100 or any(c in name for c in ("/", "\\", "\0", "\n", "\r")):
                    raise UserError("请只输入一个目录名字，例如 weather-demo，不要输入路径或多行文本")
                path = Path(self.s.cfg.work_roots[0]) / name
                resolved = path.resolve()
                if not resolved.is_relative_to(Path(self.s.cfg.work_roots[0])):
                    raise UserError("目录符号链接超出默认根目录")
                if path.exists() and not path.is_dir():
                    raise UserError("此名字已被文件占用")
                self.stage(row, "description", {"cwd": str(path), "mkdir": True})
                output = card("创建任务 · 2/2 输入描述", [div(
                    f"目录：{path}\n{'使用已有目录' if path.is_dir() else '将创建新目录'}。\n请直接发送任务要求和必要背景，可以多行。收到描述后立即创建并排队执行。\n发送“取消向导”退出。")])
            elif not action and row["stage"] == "description":
                if not text:
                    raise UserError("任务描述不能为空")
                data = json.loads(row["data"])
                result = await self.s.execute({"op": "create", **data, "text": text, "chat": row["chat"],
                                               "key": "wizard:" + row["flow"]}, actor)
                row["current_task"] = result["task"]
                self.stage(row, "idle")
                output = self.home(row, "任务已创建并排队。后续进展、审批和结果会通过卡片/消息通知。")
            elif action in ("list", "history"):
                self.stage(row, "idle")
                output = self.task_list(row, value.get("offset", 0), history=action == "history")
            elif action == "select":
                t = self.s.check(actor, value.get("task"))
                if t["closed"]:
                    raise UserError("该任务已结束；请在“已结束任务”查看结果，或创建新任务")
                row["current_task"] = value["task"]
                self.stage(row, "idle")
                output = self.home(row, "已切换到所选任务。直接发送消息即可继续对话。")
            elif action == "command":
                t = self.context(row, mutation=True)
                if t["closed"]:
                    raise UserError("所选任务已结束，请创建或选择其他任务")
                if row["stage"] == "answer":
                    data = json.loads(row["data"])
                    p = self.live_question(row, data["request"]) if data.get("request") else None
                    q = next((q for q in p["questions"] if q["id"] == data.get("question")), None) if p else None
                    self.stage(row, "answer", data)
                    output = self.question_prompt(row, q) if q else self.question_choices(row)
                else:
                    self.stage(row, "command", {"task": t["id"]})
                    output = card("输入命令 · 追加当前任务要求", [div(
                        f"目标任务：{t['id']}\n目录：{t['cwd']}\n标签：{t['prompt'][:80]}\n可直接发送消息，无需每次点击此按钮。运行中追加；空闲时开始后续一轮。\n等待问题时作为回答；操作授权请点审批卡片。")])
            elif action == "answer_question":
                if row["stage"] != "answer" or value.get("task") != row["current_task"]:
                    raise UserError("问题卡片不属于当前上下文，请重新选择")
                p = self.live_question(row, value.get("request"))
                q = next((q for q in p["questions"] if q["id"] == value.get("question")), None)
                if not q:
                    raise UserError("问题不存在")
                if q.get("isSecret"):
                    raise UserError("这是敏感问题，请通过 Terminal 回答")
                data = json.loads(row["data"])
                if data.get("answers") and data.get("request") != value["request"]:
                    raise UserError("请先完成当前请求的剩余问题；也可发送“取消向导”清除暂存回答")
                self.stage(row, "answer", {"request": value["request"], "question": q["id"], "answers": data.get("answers", {})})
                output = self.question_prompt(row, q)
            elif not action and row["stage"] in ("idle", "command", "answer"):
                output = await self.chat(row, text)
            elif action == "end":
                t = self.context(row, mutation=True)
                self.stage(row, "confirm_end", {"task": t["id"]})
                output = card("结束当前任务", [div(f"将结束 {t['id']}\n目录：{t['cwd']}\n会中断执行并使未处理审批失效。文件和历史会保留。")]
                              + actions([button("确认结束此任务", "confirm_end", task=t["id"], flow=row["flow"]), button("返回", "home")]))
            elif action == "confirm_end":
                if row["stage"] != "confirm_end" or json.loads(row["data"]).get("task") != value.get("task"):
                    raise UserError("当前上下文已改变，请重新选择要结束的任务")
                await self.s.execute({"op": "cancel", "task": value["task"]}, actor)
                row["current_task"] = None
                self.stage(row, "idle")
                output = self.home(row, "已请求结束任务，并移入“任务列表 → 已结束任务”；当前上下文已清除。")
            elif action == "history_status":
                t = self.s.check(actor, value.get("task"))
                output = card("历史任务结果", [metadata(t), {"tag": "hr"}] + markdown(t['summary'] or '暂无结果')
                              + actions([button("已结束任务", "history"), button("任务列表", "list")]))
            elif action == "status":
                t = self.context(row)
                status = self.s.status(t["id"])
                pending = [r["id"] for r in status["requests"] if r["status"] == "pending"]
                failed = [n for n in status["notifications"] if n["status"] == "failed"]
                note = f"待处理请求：{', '.join(pending) or '无'}"
                if failed:
                    note += f"\n通知发送失败 {len(failed)} 条；最近错误：{failed[0]['error']}"
                output = self.home(row, note, result=t['summary'])
            else:
                raise UserError("当前正在等待按钮确认；也可发送“取消向导”重新开始")
            result = {"task": row["current_task"], "stage": row["stage"], "card": output, "destination": row["chat"]}
            self.recent[actor] = (signature, row["flow"], time.monotonic(), result)
            return result
