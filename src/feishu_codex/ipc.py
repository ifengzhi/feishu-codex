import asyncio
import json
import os
import re
import socket
import stat
import struct
import time
import uuid

from .commands import HELP, parse, render
from .config import UserError
from .store import encode
from .ui import Wizard

MAX_FRAME = 2 * 1024 * 1024


class IPCServer:
    def __init__(self, service):
        self.s = service
        self.server = None
        self.connections = set()
        self.wizard = Wizard(service)

    async def start(self):
        path = self.s.cfg.socket_path
        if os.path.lexists(path):
            if not stat.S_ISSOCK(os.lstat(path).st_mode):
                raise UserError("socket_path 已存在且不是 Unix socket，拒绝覆盖")
            os.unlink(path)  # daemon owns an exclusive flock before this point
        self.server = await asyncio.start_unix_server(self.client, path, limit=MAX_FRAME)
        os.chmod(path, 0o600)

    async def close(self):
        self.server.close()
        await self.server.wait_closed()
        for writer in list(self.connections):
            writer.close()
        if os.path.exists(self.s.cfg.socket_path):
            os.unlink(self.s.cfg.socket_path)

    async def client(self, reader, writer):
        self.connections.add(writer)
        token, attached = uuid.uuid4().hex, set()
        try:
            raw = writer.get_extra_info("socket").getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
            _, uid, _ = struct.unpack("3i", raw)
            if uid != os.getuid():
                return
            while line := await reader.readline():
                try:
                    req = json.loads(line)
                    if not isinstance(req, dict):
                        raise UserError("请求必须为 JSON 对象")
                    op, tid = req.get("op"), req.get("task")
                    if op == "attach":
                        result = self.s.acquire(tid, token)
                        attached.add(tid)
                    elif op == "heartbeat":
                        self.s.heartbeat(tid, token)
                        result = {"ok": True}
                    elif op == "detach":
                        self.s.release(tid, token)
                        attached.discard(tid)
                        result = {"ok": True}
                    elif op == "feishu_event":
                        result = await self.remote(req)
                    elif op == "feishu_health":
                        self.s.feishu_state = str(req.get("state", "unknown"))
                        self.s.feishu_seen = time.monotonic()
                        result = {"ok": True}
                    elif op == "identity_seen":
                        open_id = req.get("open_id", "")
                        if not re.fullmatch(r"ou_[A-Za-z0-9_]{3,128}", open_id):
                            raise UserError("无效 open_id")
                        self.s.db.run("INSERT INTO identities VALUES(?,?) ON CONFLICT(open_id) DO UPDATE SET seen=excluded.seen",
                                      (open_id, time.time()))
                        self.s.db.run("DELETE FROM identities WHERE open_id NOT IN (SELECT open_id FROM identities ORDER BY seen DESC LIMIT 100)")
                        result = {"ok": True}
                    elif op == "identities":
                        result = self.s.db.all("SELECT * FROM identities ORDER BY seen DESC")
                    else:
                        result = await self.s.execute(req, token=token)
                    response = {"ok": True, "result": result}
                except (UserError, ValueError, KeyError, TypeError) as e:
                    response = {"ok": False, "error": self.s.db.redact(str(e))}
                except Exception as e:
                    response = {"ok": False, "error": self.s.db.redact(str(e)) if type(e).__name__ == "RpcError" else type(e).__name__}
                writer.write((encode(response) + "\n").encode())
                await writer.drain()
        except (ConnectionError, ValueError, asyncio.LimitOverrunError):
            pass
        finally:
            for tid in attached:
                self.s.release(tid, token)
            self.connections.discard(writer)
            writer.close()
            try:
                await writer.wait_closed()
            except ConnectionError:
                pass

    async def remote(self, req):
        s = self.s
        actor = req.get("actor", "")
        s.check(actor)  # enforce before dedup lookup or returning any task data
        event_id = req.get("event_id")
        if not event_id or not isinstance(event_id, str):
            raise UserError("事件缺少唯一 ID")
        key = actor + ":" + event_id
        old = s.db.one("SELECT * FROM dedup WHERE id=?", (key,))
        if old:
            if old["status"] == "processing":
                return {"message": "同一事件正在处理，请查询状态"}
            return json.loads(old["result"])
        s.db.run("INSERT INTO dedup VALUES(?,'processing',NULL,?)", (key, time.time()))
        tid = None
        ui_result = None
        try:
            ui_result = await self.wizard.handle(req)
            if ui_result is not None:
                tid = ui_result["task"]
                result = {"task": tid, "stage": ui_result["stage"], "message": "已收到，请使用最新指引，无需重复点击" if ui_result.get("repeated") else "已更新任务向导"}
            elif req.get("card"):
                value = req["card"]
                tid = value["task"]
                t = s.check(actor, tid)
                if req.get("chat") != t["chat"]:
                    raise UserError("卡片与任务所在会话不匹配")
                r = s.db.one("SELECT * FROM requests WHERE id=? AND task=?", (value["request"], tid))
                if not r:
                    raise UserError("审批记录不存在")
                result = await s.execute({"op": "decide", "task": tid, "request": value["request"], "decision": value["decision"]}, actor)
            else:
                if req.get("chat_type") != "p2p":
                    raise UserError("初版仅支持机器人私聊")
                command = parse(req.get("text", ""))
                tid = command.get("task")
                if command["op"] == "help":
                    result = HELP
                else:
                    command["chat"] = req.get("chat")
                    if command["op"] == "create" and not command.get("key"):
                        command["key"] = event_id
                    result = await s.execute(command, actor)
                    tid = tid or (result.get("task") if isinstance(result, dict) else None)
        except Exception as e:
            result = {"error": s.db.redact(str(e)) if isinstance(e, (UserError, ValueError)) or type(e).__name__ == "RpcError" else type(e).__name__}
        s.db.run("UPDATE dedup SET status='done',result=? WHERE id=?", (encode(s.db.redact(result)), key))
        if ui_result is not None:
            if not ui_result.get("repeated"):
                s.db.notify("ui:" + key, None, ui_result["destination"], "ui", ui_result["card"])
        elif not req.get("card"):
            # Explicit command replies are allowed during takeover; only proactive notices pause.
            s.db.notify("reply:" + key, None, req.get("chat") or "open_id:" + actor, "reply", render(result) if not isinstance(result, str) else result)
        return result


class Client:
    def __init__(self, config):
        self.config = config
        self.reader = self.writer = None
        self.lock = asyncio.Lock()

    async def connect(self):
        self.reader, self.writer = await asyncio.open_unix_connection(self.config.socket_path, limit=MAX_FRAME)
        return self

    async def call(self, req):
        async with self.lock:
            self.writer.write((encode(req) + "\n").encode())
            await self.writer.drain()
            line = await asyncio.wait_for(self.reader.readline(), self.config.rpc_timeout + 10)
            if not line:
                raise UserError("后台连接断开；任务由后台独立管理，请重新查询")
            msg = json.loads(line)
            if not msg["ok"]:
                raise UserError(msg["error"])
            return msg["result"]

    async def close(self):
        if self.writer:
            self.writer.close()
            try:
                await self.writer.wait_closed()
            except ConnectionError:
                pass
