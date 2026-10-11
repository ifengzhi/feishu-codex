import asyncio
import json
import time
import uuid

from .presentation import card, code, markdown, notification_card, plain


def card_for(store, rid):
    r = store.one("SELECT * FROM requests WHERE id=?", (rid,))
    p = json.loads(r["params"])
    question = r["method"] == "item/tool/requestUserInput"
    title = "回答问题（非操作授权）" if question else "Codex 操作授权"
    text = f"任务：{r['task']}\n请求：{r['id']}\n状态：{r['status']}\n"
    text += json.dumps(p, ensure_ascii=False, indent=2)
    too_large = len(text.encode()) > 18000 or len(json.dumps(p, ensure_ascii=False).encode()) > 22000
    if too_large:
        text = f"任务：{r['task']}\n请求：{rid}\n操作内容超过飞书卡片容量，请 feishu-codex attach {r['task']} 查看完整详情并处理。此卡片不提供批准。\n状态：{r['status']}"
    elements = [plain(text)]
    if not too_large:
        elements = [plain(f"任务：{r['task']}\n请求：{rid} · {r['status']}"), {"tag": "hr"}]
        for key, label in (("cwd", "工作目录"), ("reason", "请求原因"), ("scope", "权限范围")):
            if p.get(key):
                elements.append(plain(f"{label}：{p[key]}"))
        if p.get("command"):
            elements += markdown("**实际操作**\n\n" + code(p["command"], "shell"), limit=24000)
        details = {k: v for k, v in p.items() if k not in ("cwd", "reason", "scope", "command")}
        if question:
            for q in details.pop("questions", []):
                lines = [f"问题 {q['id']}：{q['question']}"]
                for i, option in enumerate(q.get("options") or [], 1):
                    lines.append(f"{i}. {option['label']}：{option.get('description', '')}")
                if q.get("isSecret"):
                    lines.append("敏感问题：请通过 Terminal 回答。")
                elements.append(plain("\n".join(lines)))
        if details:
            elements += markdown("**问题内容**" if question else "**请求详情**")
            # Literal text guarantees the complete permission payload remains visible.
            elements.append(plain(json.dumps(details, ensure_ascii=False, indent=2)))
    if r["status"] == "pending":
        if question:
            text = (f'先在任务列表选中任务 {r["task"]}，然后直接发送答案；多个问题会先要求选择。'
                    '\n选项可回复编号（1、2…）、字母（A、B…）或完整选项名称。'
                    f'\n也可明确发送：answer {r["task"]} {rid} {{"问题ID":["回答"]}}\n敏感回答请在本机终端输入。')
            elements.append({"tag": "div", "text": {"tag": "plain_text", "content": text}})
        else:
            labels = {"accept": "批准本次", "decline": "拒绝", "cancel": "拒绝并中断"}
            actions = []
            for d in json.loads(r["decisions"]):
                if too_large and d == "accept":
                    continue
                label = "批准当前 turn 权限" if d == "accept" and r["method"] == "item/permissions/requestApproval" else labels[d]
                actions.append({"tag": "button", "text": {"tag": "plain_text", "content": label},
                                "type": "default", "value": {"task": r["task"], "request": rid, "decision": d}})
            if actions:
                elements.append({"tag": "action", "actions": actions})
    return card(title, elements, "orange" if r["status"] == "pending" else "grey")


class FeishuSender:
    def __init__(self, config):
        import lark_oapi as lark
        self.client = lark.Client.builder().app_id(config.feishu["app_id"]).app_secret(
            config.feishu["app_secret"]).timeout(10).log_level(lark.LogLevel.ERROR).build()

    def send(self, row, body):
        from lark_oapi.api.im.v1 import (
            CreateMessageRequest,
            CreateMessageRequestBody,
            PatchMessageRequest,
            PatchMessageRequestBody,
        )
        content = json.dumps(body, ensure_ascii=False)
        if row["message_id"] and row["kind"] == "request":
            req = PatchMessageRequest.builder().message_id(row["message_id"]).request_body(
                PatchMessageRequestBody.builder().content(content).build()).build()
            result = self.client.im.v1.message.patch(req)
        else:
            destination = row["chat"]
            receive_type = "chat_id"
            if destination.startswith("open_id:"):
                receive_type, destination = "open_id", destination.removeprefix("open_id:")
            req = CreateMessageRequest.builder().receive_id_type(receive_type).request_body(
                CreateMessageRequestBody.builder().receive_id(destination).msg_type(
                    "interactive").content(content).uuid(
                    str(uuid.uuid5(uuid.NAMESPACE_URL, row["id"]))).build()).build()
            result = self.client.im.v1.message.create(req)
        if not result.success():
            # SDK error text may echo request content. Only retain the numeric error code.
            raise RuntimeError(f"Feishu API code={result.code}")
        return row["message_id"] or result.data.message_id


class Outbox:
    def __init__(self, service, sender=None):
        self.s = service
        self.sender = sender

    async def once(self):
        if not self.sender:
            return
        s = self.s
        # Filter paused tasks before LIMIT: a large paused backlog must not starve others.
        paused = tuple(s.leases)
        clause = " AND (task IS NULL OR task NOT IN (" + ",".join("?" for _ in paused) + "))" if paused else ""
        rows = s.db.all("SELECT * FROM outbox WHERE status='pending' AND next_at<=?" + clause + " ORDER BY rowid LIMIT 50",
                        (time.time(), *paused))
        for row in rows:
            current = s.db.one("SELECT status,version FROM outbox WHERE id=?", (row["id"],))
            if current["status"] != "pending" or current["version"] != row["version"]:
                continue
            if row["task"] in s.leases:
                continue
            if row["kind"] == "progress":
                t = s.db.task(row["task"])
                newer = s.db.one("SELECT id FROM outbox WHERE task=? AND kind='progress' AND rowid>(SELECT rowid FROM outbox WHERE id=?) LIMIT 1",
                                 (row["task"], row["id"]))
                if newer or t["status"] not in ("starting", "running", "waiting", "cancelling"):
                    s.db.run("UPDATE outbox SET status='superseded' WHERE id=?", (row["id"],))
                    continue
            body = json.loads(row["body"])
            if row["kind"] == "request":
                body = card_for(s.db, body["request"])
            elif row["kind"] != "ui":
                body = notification_card(row, body, s.db.task(row["task"]) if row["task"] else None)
            try:
                message_id = await asyncio.to_thread(self.sender.send, row, body)
                # Preserve a concurrent request resolution/newer card update.
                s.db.run("UPDATE outbox SET message_id=? WHERE id=?", (message_id, row["id"]))
                s.db.run("UPDATE outbox SET status='sent',error='' WHERE id=? AND version=?", (row["id"], row["version"]))
            except Exception as e:
                attempts = row["attempts"] + 1
                state = "failed" if attempts >= s.cfg.notification_retries else "pending"
                error = str(e) if str(e).startswith("Feishu API code=") else type(e).__name__
                s.db.run("UPDATE outbox SET status=?,attempts=?,next_at=?,error=? WHERE id=? AND version=?",
                         (state, attempts, time.time() + min(2 ** attempts, 60), error, row["id"], row["version"]))

    async def run(self):
        while True:
            await self.once()
            await asyncio.sleep(0.5)
