import asyncio
import json
import os
import tempfile
import uuid
from contextlib import asynccontextmanager

import pytest
from conftest import active, completed, create, request
from websockets.asyncio.client import unix_connect
from websockets.exceptions import ConnectionClosed

from feishu_codex.config import UserError
from feishu_codex.native import NativeEndpoint, open_native
from feishu_codex.protocol import text_input


async def receive(ws, predicate):
    async with asyncio.timeout(3):
        while True:
            msg = json.loads(await ws.recv())
            if predicate(msg):
                return msg


async def call(ws, method, params=None, rid=None):
    rid = rid or uuid.uuid4().hex
    await ws.send(json.dumps({"id": rid, "method": method, "params": params or {}}))
    return await receive(ws, lambda m: m.get("id") == rid and "method" not in m)


@asynccontextmanager
async def terminal(s, tid):
    # Keep Unix paths below Linux's 108-byte limit, including long pytest names.
    directory = tempfile.TemporaryDirectory(prefix="fc-tui-")
    original_path = s.cfg.socket_path
    s.cfg.socket_path = directory.name + "/control.sock"
    info = await open_native(s, tid, uuid.uuid4().hex)
    endpoint = s.native[tid]
    try:
        async with unix_connect(info["endpoint"].removeprefix("unix://")) as ws:
            assert (await call(ws, "initialize"))["result"]["platformOs"] == "linux"
            assert "result" in await call(ws, "thread/resume", {"threadId": s.db.task(tid)["thread"]})
            yield ws, endpoint
    finally:
        endpoint.disconnect()
        if endpoint.cleanup:
            await endpoint.cleanup
        s.cfg.socket_path = original_path
        directory.cleanup()


async def test_real_unix_gateway_joins_without_starting_and_streams_only_bound_task(service):
    s = service
    tid = await active(s)
    count = len([m for m, _ in s.rpc.calls if m == "turn/start"])
    async with terminal(s, tid) as (ws, endpoint):
        assert os.stat(endpoint.path).st_mode & 0o777 == 0o600
        assert len([m for m, _ in s.rpc.calls if m == "turn/start"]) == count
        thread = s.db.task(tid)["thread"]
        await s.on_event({"method": "item/agentMessage/delta", "params": {"threadId": "other", "delta": "private"}})
        await s.on_event({"method": "item/agentMessage/delta", "params": {"threadId": thread, "delta": "visible"}})
        msg = await receive(ws, lambda m: m.get("method") == "item/agentMessage/delta")
        assert msg["params"]["delta"] == "visible"
        assert "error" in await call(ws, "thread/read", {"threadId": "other"})
        with pytest.raises(UserError, match="Terminal"):
            await s.execute({"op": "append", "task": tid, "text": "no"}, "ou_test")
        assert (await s.execute({"op": "status", "task": tid}, "ou_test"))["control"] == "terminal"
    assert s.db.task(tid)["status"] == "running"
    assert tid not in s.leases and not endpoint.path.exists()


@pytest.mark.parametrize("decision", ["accept", "decline"])
async def test_pending_approval_shared_once_across_takeover(service, decision):
    s = service
    tid = await active(s)
    rid = await request(s, tid)
    async with terminal(s, tid) as (ws, endpoint):
        prompt = await receive(ws, lambda m: m.get("id") == "native:" + rid)
        assert prompt["method"] == "item/commandExecution/requestApproval"
        assert prompt["params"]["availableDecisions"] == ["accept", "decline", "cancel"]
        with pytest.raises(UserError, match="Terminal"):
            await s.execute({"op": "decide", "task": tid, "request": rid, "decision": decision}, "ou_test")
        answer = {"id": prompt["id"], "result": {"decision": decision}}
        await ws.send(json.dumps(answer))
        await receive(ws, lambda m: m.get("method") == "serverRequest/resolved")
        assert s.db.one("SELECT status FROM requests WHERE id=?", (rid,))["status"] == "resolved"
        assert len(s.rpc.replies) == 1 and s.rpc.replies[0][1] == {"decision": decision}
        await ws.send(json.dumps(answer))
        with pytest.raises(ConnectionClosed):
            await ws.recv()
        assert len(s.rpc.replies) == 1


async def test_native_questions_and_invalid_session_grant(service):
    s = service
    tid = await active(s)
    rid = await request(s, tid, "item/tool/requestUserInput", questions=[{"id": "q", "header": "Q", "question": "Choose?"}])
    async with terminal(s, tid) as (ws, endpoint):
        await receive(ws, lambda m: m.get("id") == "native:" + rid)
        await ws.send(json.dumps({"id": "native:" + rid, "result": {"answers": {"q": {"answers": ["A"]}}}}))
        await receive(ws, lambda m: m.get("method") == "serverRequest/resolved")
        assert s.rpc.replies[-1][1] == {"answers": {"q": {"answers": ["A"]}}}
        rid = await request(s, tid)
        await receive(ws, lambda m: m.get("id") == "native:" + rid)
        await ws.send(json.dumps({"id": "native:" + rid, "result": {"decision": "acceptForSession"}}))
        with pytest.raises(ConnectionClosed):
            await ws.recv()
        assert s.db.one("SELECT status FROM requests WHERE id=?", (rid,))["status"] == "pending"
        assert tid not in s.leases
    assert s.db.one("SELECT status FROM outbox WHERE id=?", ("request:" + rid,))["status"] == "pending"


async def test_native_start_steer_interrupt_and_duplicate_ids(service):
    s = service
    tid = await active(s)
    await completed(s, tid)
    thread = s.db.task(tid)["thread"]
    async with terminal(s, tid) as (ws, endpoint):
        params = {"threadId": thread, "input": text_input("followup")}
        r = await call(ws, "turn/start", params, rid="one")
        assert r["result"]["turn"]["id"] == s.db.task(tid)["turn"]
        assert s.db.task(tid)["status"] == "running"
        assert "error" in await call(ws, "turn/start", params, rid="one")
        assert len([m for m, _ in s.rpc.calls if m == "turn/start"]) == 2
        r = await call(ws, "turn/steer", {**params, "expectedTurnId": s.db.task(tid)["turn"]})
        assert "result" in r and s.rpc.calls[-1][0] == "turn/steer"
        r = await call(ws, "turn/interrupt", {"threadId": thread, "turnId": s.db.task(tid)["turn"]})
        assert "result" in r and s.rpc.calls[-1][0] == "turn/interrupt"


async def test_native_enforces_serial_gate_and_sandbox(service):
    s = service
    tid = await active(s)
    await completed(s, tid)
    other = await active(s, cwd="other")
    async with terminal(s, tid) as (ws, endpoint):
        params = {"threadId": s.db.task(tid)["thread"], "input": text_input("followup")}
        assert "串行" in (await call(ws, "turn/start", params))["error"]["message"]
        await completed(s, other)
        for overrides in ({"approvalPolicy": "never"}, {"sandboxPolicy": {"type": "dangerFullAccess"}},
                          {"approvalsReviewer": "auto_review"}, {"permissions": "full-access"}, {"cwd": s.db.task(other)["cwd"]}):
            assert "error" in await call(ws, "turn/start", {**params, **overrides})
        for method in ("thread/start", "thread/fork", "command/exec", "process/spawn", "config/value/write"):
            assert "error" in await call(ws, method, params)
        assert s.db.task(tid)["status"] == "completed"


async def test_native_expired_request_and_restart_drop_control(service):
    s = service
    tid = await active(s)
    rid = await request(s, tid)
    async with terminal(s, tid) as (ws, endpoint):
        await receive(ws, lambda m: m.get("id") == "native:" + rid)
        s.db.run("UPDATE requests SET expires=0 WHERE id=?", (rid,))
        await ws.send(json.dumps({"id": "native:" + rid, "result": {"decision": "accept"}}))
        with pytest.raises(ConnectionClosed):
            while True:
                await ws.recv()
        assert not s.rpc.replies
        assert s.db.one("SELECT status FROM requests WHERE id=?", (rid,))["status"] == "expired"
    # Separate live task demonstrates app-server epoch loss closes the native UI.
    await completed(s, tid, "interrupted")
    tid = await active(s, cwd="next")
    async with terminal(s, tid) as (ws, endpoint):
        s.recover("restart")
        with pytest.raises(ConnectionClosed):
            await ws.recv()
        assert tid not in s.leases


async def test_native_disconnect_preserves_approval_and_reissues_notice(service):
    s = service
    tid = await active(s)
    rid = await request(s, tid)
    async with terminal(s, tid) as (ws, endpoint):
        await receive(ws, lambda m: m.get("id") == "native:" + rid)
        assert tid in s.leases
    assert s.db.task(tid)["status"] == "waiting"
    assert s.db.one("SELECT status FROM requests WHERE id=?", (rid,))["status"] == "pending"
    assert not s.rpc.replies
    assert len(s.db.all("SELECT * FROM outbox WHERE id=?", ("request:" + rid,))) == 1
    async with terminal(s, tid) as (ws, endpoint):
        await receive(ws, lambda m: m.get("id") == "native:" + rid)


async def test_native_not_ready_does_not_take_lease(service):
    tid = await create(service)
    with pytest.raises(UserError, match="尚未创建"):
        await open_native(service, tid, "test")
    assert not service.leases


async def test_bound_gateway_policy_validates_without_connecting(service):
    tid = await active(service)
    e = NativeEndpoint(service, tid, "test")
    e.sandbox_policy = {"type": "workspaceWrite", "writableRoots": []}
    p = {"input": text_input("ok"), "sandboxPolicy": e.sandbox_policy}
    e.validate_turn(p, service.db.task(tid))


async def test_native_scoped_directory_trust_and_task_picker(service):
    s = service
    tid = await active(s)
    async with terminal(s, tid) as (ws, endpoint):
        key = "projects." + json.dumps(s.db.task(tid)["cwd"]) + ".trust_level"
        params = {"edits": [{"keyPath": key, "value": "trusted", "mergeStrategy": "replace"}],
                  "reloadUserConfig": True, "filePath": None, "expectedVersion": None}
        assert "result" in await call(ws, "config/batchWrite", params)
        assert s.rpc.calls[-1][1]["reloadUserConfig"] is False
        assert "error" in await call(ws, "config/batchWrite", {**params, "filePath": "/tmp/other"})
        for key, value in (("approval_policy", "never"), ('projects."/other".trust_level', "trusted"), ("model", "other")):
            assert "error" in await call(ws, "config/value/write", {"keyPath": key, "value": value, "mergeStrategy": "replace"})
        assert (await call(ws, "thread/loaded/list"))["result"]["data"] == [s.db.task(tid)["thread"]]
        result = (await call(ws, "thread/list"))["result"]
        assert [t["id"] for t in result["data"]] == [s.db.task(tid)["thread"]]
        assert (await call(ws, "thread/list", {"cwd": "/other"}))["result"]["data"] == []


async def test_native_peer_close_and_lease_expiry_release(service):
    s = service
    tid = await active(s)
    async with terminal(s, tid) as (ws, endpoint):
        # Mirrors IPC owner disconnect while the TUI transport remains open.
        s.release(tid, endpoint.token)
        with pytest.raises(ConnectionClosed):
            await ws.recv()
        assert s.db.task(tid)["status"] == "running"
    async with terminal(s, tid) as (ws, endpoint):
        s.leases[tid] = (endpoint.token, 0)
        s.check("local", tid)
        with pytest.raises(ConnectionClosed):
            await ws.recv()
        assert s.db.task(tid)["status"] == "running"


async def test_native_start_failure_never_replays_and_stops_owner(service):
    s = service
    tid = await active(s)
    await completed(s, tid)
    async with terminal(s, tid) as (ws, endpoint):
        s.rpc.fail = "turn/start"
        await ws.send(json.dumps({"id": "start", "method": "turn/start", "params": {
            "threadId": s.db.task(tid)["thread"], "input": text_input("do once")}}))
        with pytest.raises(ConnectionClosed):
            await ws.recv()
        assert s.rpc.stopped and s.db.task(tid)["status"] == "interrupted"
        assert s.db.task(tid)["pending"] is None
        assert len([m for m, _ in s.rpc.calls if m == "turn/start"]) == 2


async def test_native_permission_subset_is_not_expanded(service):
    s = service
    tid = await active(s)
    permissions = {"network": {"enabled": True}, "fileSystem": {"read": ["/tmp/a", "/tmp/b"]}}
    rid = await request(s, tid, "item/permissions/requestApproval", permissions=permissions)
    async with terminal(s, tid) as (ws, endpoint):
        await receive(ws, lambda m: m.get("id") == "native:" + rid)
        subset = {"fileSystem": {"read": ["/tmp/a"]}, "network": None}
        await ws.send(json.dumps({"id": "native:" + rid, "result": {"permissions": subset, "scope": "turn"}}))
        await receive(ws, lambda m: m.get("method") == "serverRequest/resolved")
        assert s.rpc.replies[-1][1] == {"permissions": subset, "scope": "turn"}
        rid = await request(s, tid, "item/permissions/requestApproval", permissions=permissions)
        await receive(ws, lambda m: m.get("id") == "native:" + rid)
        await ws.send(json.dumps({"id": "native:" + rid, "result": {"permissions": {"fileSystem": {"write": ["/etc"]}}}}))
        with pytest.raises(ConnectionClosed):
            await ws.recv()
        assert len(s.rpc.replies) == 1
        assert s.db.one("SELECT status FROM requests WHERE id=?", (rid,))["status"] == "pending"


async def test_native_resume_buffers_events_until_history_response(service):
    s = service
    tid = await active(s)
    original = s.rpc.call
    async def completing_resume(method, params):
        if method == "thread/resume":
            await completed(s, tid)
        return await original(method, params)
    s.rpc.call = completing_resume
    async with terminal(s, tid) as (ws, endpoint):
        event = await receive(ws, lambda m: m.get("method") == "turn/completed")
        assert event["params"]["turn"]["id"] == s.db.task(tid)["turn"]


def test_default_root_is_ws():
    from pathlib import Path

    from feishu_codex.config import Config
    assert Config().work_roots[0] == str(Path.home() / "ws")
