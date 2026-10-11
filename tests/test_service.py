import asyncio
import json
import time
from pathlib import Path

import pytest
from conftest import active, completed, create, request

from feishu_codex.config import Config, UserError
from feishu_codex.ipc import Client, IPCServer
from feishu_codex.notifications import Outbox, card_for
from feishu_codex.protocol import RpcError


async def test_create_nonexistent_and_serial_execution(service):
    s = service
    a = await active(s)
    assert Path(s.db.task(a)["cwd"]).is_dir()
    assert s.rpc.calls[0][0] == "thread/start"
    assert s.rpc.calls[0][1]["cwd"] == s.db.task(a)["cwd"]
    b = await create(s, cwd="another")
    worker = asyncio.create_task(s.worker())
    await asyncio.sleep(0.25)
    assert s.db.task(b)["status"] == "queued"
    await completed(s, a)
    await asyncio.sleep(0.25)
    assert s.db.task(b)["status"] == "running"
    worker.cancel()
    await asyncio.gather(worker, return_exceptions=True)


async def test_path_validation(service, tmp_path):
    s = service
    with pytest.raises(UserError, match="不存在"):
        await create(s, mkdir=False)
    with pytest.raises(UserError, match="根目录"):
        await create(s, cwd=str(tmp_path / "outside"))
    root = Path(s.cfg.work_roots[0])
    root.mkdir()
    (root / "escape").symlink_to(tmp_path)
    with pytest.raises(UserError, match="根目录"):
        await create(s, cwd="escape/new")
    (root / "file").write_text("x")
    with pytest.raises(UserError):
        await create(s, cwd="file", mkdir=False)
    home = Path.home()
    cfg = Config(work_roots=[str(home)])
    assert cfg.workdir("~") == str(home.resolve())


async def test_idempotency_binding_and_owner(service):
    s = service
    a = await create(s, key="same")
    assert await create(s, key="same") == a
    with pytest.raises(UserError, match="不同任务"):
        await create(s, key="same", text="other")
    with pytest.raises(UserError, match="其他用户"):
        await s.execute({"op": "status", "task": a}, "ou_other")
    with pytest.raises(UserError, match="白名单"):
        await create(s, actor="ou_unknown")
    s.cfg.feishu["allowed_users"] = []
    with pytest.raises(UserError):
        await s.execute({"op": "tasks"}, "ou_test")
    assert await s.execute({"op": "tasks"})


async def test_steer_continue_cancel(service):
    s = service
    tid = await active(s)
    first = s.db.task(tid)
    await s.execute({"op": "append", "task": tid, "text": "add tests"}, "ou_test")
    method, params = s.rpc.calls[-1]
    assert method == "turn/steer" and params["expectedTurnId"] == first["turn"]
    assert params["threadId"] == first["thread"]
    await completed(s, tid)
    await s.execute({"op": "continue", "task": tid, "text": "improve"}, "ou_test")
    assert s.db.task(tid)["thread"] == first["thread"]
    await s.execute({"op": "cancel", "task": tid}, "ou_test")
    assert s.db.task(tid)["status"] == "cancelled"
    with pytest.raises(UserError, match="取消"):
        await s.execute({"op": "continue", "task": tid, "text": "again"}, "ou_test")


@pytest.mark.parametrize("decision", ["accept", "decline", "cancel"])
async def test_approval_once(service, decision):
    s = service
    tid = await active(s)
    rid = await request(s, tid)
    req = {"op": "decide", "task": tid, "request": rid, "decision": decision}
    assert s.status(tid)["status"] == "waiting"
    results = await asyncio.gather(s.execute(req, "ou_test"), s.execute(req, "ou_test"), return_exceptions=True)
    assert sum(isinstance(x, UserError) for x in results) == 1
    assert len(s.rpc.replies) == 1 and s.rpc.replies[0][1] == {"decision": decision}
    assert '"tag": "button"' not in json.dumps(card_for(s.db, rid))


async def test_approval_scope_and_expiry(service):
    s = service
    tid = await active(s)
    rid = await request(s, tid, availableDecisions=["decline", "cancel"])
    with pytest.raises(UserError, match="允许"):
        await s.execute({"op": "decide", "task": tid, "request": rid, "decision": "accept"}, "ou_test")
    other = await create(s, cwd="other")
    with pytest.raises(UserError, match="不匹配"):
        await s.execute({"op": "decide", "task": other, "request": rid, "decision": "decline"}, "ou_test")
    s.db.run("UPDATE requests SET expires=? WHERE id=?", (time.time() - 1, rid))
    with pytest.raises(UserError, match="过期"):
        await s.execute({"op": "decide", "task": tid, "request": rid, "decision": "decline"}, "ou_test")
    assert s.db.one("SELECT status FROM requests WHERE id=?", (rid,))["status"] == "expired"
    assert s.rpc.calls[-1][0] == "turn/interrupt"
    assert not s.rpc.replies


async def test_permissions_and_input(service):
    s = service
    tid = await active(s)
    p = {"network": {"enabled": True}}
    rid = await request(s, tid, method="item/permissions/requestApproval", permissions=p)
    await s.execute({"op": "decide", "task": tid, "request": rid, "decision": "accept"}, "ou_test")
    assert s.rpc.replies[-1][1] == {"permissions": p, "scope": "turn"}
    rid = await request(s, tid, method="item/tool/requestUserInput", isBlocking=True,
                        questions=[{"id": "color", "header": "选择", "question": "颜色？", "options": None}])
    with pytest.raises(UserError, match="普通问题"):
        await s.execute({"op": "decide", "task": tid, "request": rid, "decision": "accept"}, "ou_test")
    with pytest.raises(UserError, match="覆盖"):
        await s.execute({"op": "answer", "task": tid, "request": rid, "answers": {"bad": ["a"]}}, "ou_test")
    await s.execute({"op": "answer", "task": tid, "request": rid, "answers": {"color": ["blue"]}}, "ou_test")
    assert s.rpc.replies[-1][1] == {"answers": {"color": {"answers": ["blue"]}}}


async def test_recovery_never_replays_requests(service):
    s = service
    tid = await active(s)
    rid = await request(s, tid)
    thread = s.db.task(tid)["thread"]
    s.recover()
    assert s.db.task(tid)["status"] == "interrupted"
    assert s.db.task(tid)["thread"] == thread
    assert s.db.one("SELECT status FROM requests WHERE id=?", (rid,))["status"] == "invalidated"
    with pytest.raises(UserError, match="失效"):
        await s.execute({"op": "decide", "task": tid, "request": rid, "decision": "accept"}, "ou_test")
    await s.execute({"op": "continue", "task": tid, "text": "inspect state first"}, "ou_test")
    worker = asyncio.create_task(s.worker())
    await asyncio.sleep(0.3)
    worker.cancel()
    await asyncio.gather(worker, return_exceptions=True)
    methods = [x[0] for x in s.rpc.calls]
    assert methods[-3:] == ["thread/read", "thread/resume", "turn/start"]
    assert s.rpc.calls[-1][1]["input"][0]["text"] == "inspect state first"
    assert not s.rpc.replies


async def test_takeover_socket_disconnect_and_notifications(service):
    s = service
    tid = await active(s)
    rid = await request(s, tid)
    ipc = IPCServer(s)
    await ipc.start()
    client = await Client(s.cfg).connect()
    try:
        await client.call({"op": "attach", "task": tid})
        assert s.status(tid)["control"] == "terminal"
        assert (await s.execute({"op": "status", "task": tid}, "ou_test"))["status"] == "waiting"
        with pytest.raises(UserError, match="Terminal"):
            await s.execute({"op": "decide", "task": tid, "request": rid, "decision": "accept"}, "ou_test")
        sender = RecordingSender()
        out = Outbox(s, sender)
        await out.once()
        assert not sender.sent
        await client.close()  # abrupt EOF, no detach request
        await asyncio.sleep(0.05)
        assert tid not in s.leases
        assert s.db.task(tid)["status"] == "waiting"
        await out.once()
        assert len([r for r, _ in sender.sent if r["kind"] == "request"]) == 1
        await out.once()
        assert len([r for r, _ in sender.sent if r["kind"] == "request"]) == 1
        await s.execute({"op": "decide", "task": tid, "request": rid, "decision": "accept"}, "ou_test")
        await completed(s, tid)
        assert s.db.task(tid)["status"] == "completed"
    finally:
        await client.close()
        await ipc.close()


class RecordingSender:
    def __init__(self, fail=False):
        self.fail = fail
        self.sent = []

    def send(self, row, body):
        if self.fail:
            raise RuntimeError("do not log raw body")
        self.sent.append((row, body))
        return "om_test"


async def test_failure_does_not_affect_execution_or_other_tasks(service):
    s = service
    tid = await active(s)
    other = await create(s, cwd="other")
    s.acquire(tid, "terminal")
    s.notify(tid, "progress", "paused")
    s.notify(other, "test_notice", "not paused")
    sender = RecordingSender()
    out = Outbox(s, sender)
    await out.once()
    assert len(sender.sent) == 1 and sender.sent[0][0]["task"] == other
    out.sender = RecordingSender(fail=True)
    s.release(tid, "terminal")
    for _ in range(s.cfg.notification_retries):
        s.db.run("UPDATE outbox SET next_at=0")
        await out.once()
    assert any(x["status"] == "failed" for x in s.status(tid)["notifications"])
    await completed(s, tid)
    assert s.db.task(tid)["status"] == "completed"


async def test_message_dedup_and_card_ownership(service):
    s = service
    ipc = IPCServer(s)
    event = {"actor": "ou_test", "event_id": "evt-1", "chat": "oc_test", "chat_type": "p2p",
             "text": "create --workdir new --mkdir --key k -- make demo"}
    a, b = await asyncio.gather(ipc.remote(event), ipc.remote(event))
    assert a == b
    assert len(s.db.all("SELECT * FROM tasks")) == 1
    assert len(s.db.all("SELECT * FROM outbox")) == 1
    event["event_id"] = "evt-2"
    assert (await ipc.remote(event))["task"] == a["task"]
    event["event_id"] = "evt-3"
    event["actor"] = "ou_unknown"
    with pytest.raises(UserError):
        await ipc.remote(event)
    await s.execute({"op": "cancel", "task": a["task"]}, "ou_test")
    tid = await active(s, cwd="approval-task")
    rid = await request(s, tid)
    r = await ipc.remote({"actor": "ou_test", "event_id": "card-1", "chat": "wrong", "card": {
        "task": tid, "request": rid, "decision": "accept"}})
    assert "error" in r and not s.rpc.replies


async def test_redaction_never_persists_config_secret(service):
    s = service
    sentinel = "FAKE_CREDENTIAL_123456789"
    s.db.redact.add(sentinel)
    tid = await active(s)
    rid = await request(s, tid, command="echo " + sentinel)
    s.db.event(tid, "test", {"api_key": sentinel})
    s.notify(tid, "test", sentinel)
    assert sentinel not in json.dumps(s.status(tid))
    assert sentinel not in json.dumps(card_for(s.db, rid))
    assert sentinel not in "".join(s.db.db.iterdump())
    with pytest.raises(UserError, match="凭据"):
        await create(s, text="use " + sentinel)


async def test_cancel_invalidates_and_waits_for_completion(service):
    s = service
    tid = await active(s)
    rid = await request(s, tid)
    await s.execute({"op": "cancel", "task": tid}, "ou_test")
    assert s.db.task(tid)["status"] == "cancelling"
    with pytest.raises(UserError):
        await s.execute({"op": "decide", "task": tid, "request": rid, "decision": "accept"}, "ou_test")
    await completed(s, tid, "interrupted")
    assert s.db.task(tid)["status"] == "cancelled"


async def test_steer_failure_not_retried(service):
    s = service
    tid = await active(s)
    s.rpc.fail = "turn/steer"
    with pytest.raises(RpcError):
        await s.execute({"op": "append", "task": tid, "text": "new"}, "ou_test")
    assert [m for m, p in s.rpc.calls].count("turn/start") == 1


async def test_lease_timeout_and_server_resolved(service):
    s = service
    tid = await active(s)
    rid = await request(s, tid)
    s.leases[tid] = ("token", time.monotonic() - 1)
    s.check("ou_test", tid, mutation=True)
    assert tid not in s.leases
    t = s.db.task(tid)
    await s.on_event({"method": "serverRequest/resolved", "params": {"threadId": t["thread"], "requestId": 123}})
    with pytest.raises(UserError):
        await s.execute({"op": "decide", "task": tid, "request": rid, "decision": "accept"}, "ou_test")


async def test_unsupported_is_not_silent(service):
    s = service
    tid = await active(s)
    t = s.db.task(tid)
    await s.on_event({"id": 99, "method": "item/tool/call", "params": {"threadId": t["thread"], "turnId": t["turn"]}})
    await asyncio.sleep(0)
    assert s.rpc.replies[-1][2]["code"] == -32601
    assert any(e["kind"] == "unsupported_request" for e in s.status(tid)["events"])


async def test_interrupt_without_completion_stops_owner(service):
    s = service
    tid = await active(s)
    await s.execute({"op": "cancel", "task": tid}, "ou_test")
    s.interrupts[tid] = ("cancelled", time.monotonic() - 1)
    job = asyncio.create_task(s.maintain())
    try:
        await asyncio.sleep(1.1)
        assert s.rpc.stopped
        assert s.db.task(tid)["status"] == "cancelled"
        assert "回执超时" in s.db.task(tid)["summary"]
    finally:
        job.cancel()
        await asyncio.gather(job, return_exceptions=True)


async def test_duplicate_wire_request_not_duplicated(service):
    s = service
    tid = await active(s)
    rid = await request(s, tid)
    raw = s.live_requests[rid]
    await s.on_event({"id": 123, "method": "item/commandExecution/requestApproval", "params": raw})
    assert len(s.db.all("SELECT * FROM requests")) == 1
