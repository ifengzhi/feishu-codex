import asyncio
import uuid

import pytest

from feishu_codex.config import Config, Redactor
from feishu_codex.service import Service
from feishu_codex.store import Store


class FakeRPC:
    def __init__(self):
        self.epoch = "test-epoch"
        self.calls = []
        self.replies = []
        self.fail = None
        self.ready = True
        self.cwd = None
        self.stopped = False

    async def call(self, method, params):
        self.calls.append((method, params))
        if self.fail == method:
            from feishu_codex.protocol import RpcError
            raise RpcError("uncertain timeout")
        if method == "thread/start":
            self.cwd = params["cwd"]
            return {"thread": {"id": uuid.uuid4().hex, "cwd": self.cwd}}
        if method == "thread/read":
            return {"thread": {"id": params["threadId"], "cwd": self.cwd, "status": {"type": "idle"}, "turns": []}}
        if method == "thread/resume":
            return {"thread": {"id": params["threadId"]}}
        if method == "turn/start":
            return {"turn": {"id": uuid.uuid4().hex, "status": "inProgress"}}
        return {}

    async def reply(self, rid, result=None, error=None):
        self.replies.append((rid, result, error))

    async def stop(self):
        self.stopped = True

    async def start(self):
        self.epoch = uuid.uuid4().hex


@pytest.fixture
def service(tmp_path):
    cfg = Config(work_roots=[str(tmp_path / "work")], data_dir=str(tmp_path),
                 feishu={"allowed_users": ["ou_test", "ou_other"]})
    db = Store(tmp_path / "state.sqlite3", Redactor(cfg))
    s = Service(cfg, db, FakeRPC())
    s.connected = True
    yield s
    db.db.close()


async def create(s, actor="ou_test", **extra):
    return (await s.execute({"op": "create", "cwd": "new", "mkdir": True,
                            "text": "build a program", "chat": "oc_test", **extra}, actor))["task"]


async def active(s, **extra):
    tid = await create(s, **extra)
    worker = asyncio.create_task(s.worker())
    try:
        async with asyncio.timeout(3):
            while s.db.task(tid)["status"] != "running":
                await asyncio.sleep(0.01)
    finally:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
    return tid


async def request(s, tid, method="item/commandExecution/requestApproval", **params):
    t = s.db.task(tid)
    rpc_id = 123 + s.db.one("SELECT count(*) AS n FROM requests")["n"]
    await s.on_event({"id": rpc_id, "method": method, "params": {
        "threadId": t["thread"], "turnId": t["turn"], "itemId": "item-1", "startedAtMs": 1,
        "command": "touch harmless.txt", "cwd": t["cwd"], "reason": "write example",
        **params}})
    return s.db.one("SELECT * FROM requests ORDER BY rowid DESC LIMIT 1")["id"]


async def completed(s, tid, state="completed"):
    t = s.db.task(tid)
    await s.on_event({"method": "turn/completed", "params": {"threadId": t["thread"],
                        "turn": {"id": t["turn"], "status": state, "error": None}}})
