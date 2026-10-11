import asyncio
import json
from pathlib import Path

import pytest
from conftest import active

from feishu_codex.config import UserError
from feishu_codex.ipc import IPCServer


async def text(ipc, message, key):
    return await ipc.remote({"actor": "ou_test", "event_id": key, "chat": "oc_test", "chat_type": "p2p", "text": message})


async def click(ipc, action, key, **values):
    return await ipc.remote({"actor": "ou_test", "event_id": key, "chat": "oc_test",
                             "card": {"ui": action, **values}})


def session(s):
    return s.db.one("SELECT * FROM ui_sessions WHERE owner='ou_test'")


def last_card(s):
    return json.loads(s.db.one("SELECT body FROM outbox WHERE kind='ui' ORDER BY rowid DESC LIMIT 1")["body"])


async def test_wizard_new_directory_and_explicit_context(service):
    s, ipc = service, IPCServer(service)
    await text(ipc, "菜单", "e1")
    assert "创建任务" in json.dumps(last_card(s), ensure_ascii=False)
    await click(ipc, "create", "e2")
    assert session(s)["stage"] == "directory"
    await text(ipc, "weather-demo", "e3")
    assert session(s)["stage"] == "description"
    result = await text(ipc, "创建天气演示项目，补充 README 和测试", "e4")
    tid = result["task"]
    assert s.db.task(tid)["cwd"] == str(Path(s.cfg.work_roots[0]) / "weather-demo")
    assert Path(s.db.task(tid)["cwd"]).is_dir()
    assert session(s)["current_task"] == tid
    assert (await text(ipc, "创建天气演示项目，补充 README 和测试", "e4"))["task"] == tid
    assert len(s.db.all("SELECT * FROM tasks")) == 1
    await click(ipc, "command", "e5")
    await text(ipc, "再增加命令行参数", "e6")
    assert "再增加" in s.db.task(tid)["pending"]
    await click(ipc, "end", "e7")
    flow = session(s)["flow"]
    await click(ipc, "confirm_end", "e8", task=tid, flow=flow)
    assert s.db.task(tid)["status"] == "cancelled"
    assert session(s)["current_task"] is None
    repeat = await click(ipc, "confirm_end", "e9", task=tid, flow=flow)
    assert "失效" in repeat["error"]


async def test_directory_listing_manual_input_and_stale_cards(service, tmp_path):
    s, ipc = service, IPCServer(service)
    root = Path(s.cfg.work_roots[0])
    (root / "existing").mkdir(parents=True)
    (root / "escape").symlink_to(tmp_path)
    await click(ipc, "create", "e1")
    flow = session(s)["flow"]
    card = json.dumps(last_card(s), ensure_ascii=False)
    assert "existing" in card and '"name": "escape"' not in card
    bad = await text(ipc, "../outside", "e2")
    assert "error" in bad and session(s)["stage"] == "directory"
    await click(ipc, "directory", "e3", name="existing", flow=flow)
    assert json.loads(session(s)["data"])["cwd"] == str(root / "existing")
    bad = await click(ipc, "directory", "e4", name="other", flow=flow)
    assert "失效" in bad["error"]
    # Wizard state survives reconstructing the handler/service UI adapter.
    new_ipc = IPCServer(s)
    await text(new_ipc, "实现一个程序", "e5")
    assert session(s)["stage"] == "idle"


async def test_menu_event_and_takeover_blocks_commands(service):
    s, ipc = service, IPCServer(service)
    r = await click(ipc, "command", "e0")
    assert "尚未选择" in r["error"]
    tid = await active(s)
    await click(ipc, "select", "e1", task=tid)
    r = await ipc.remote({"actor": "ou_test", "event_id": "menu1", "menu": "input_command"})
    assert r["stage"] == "command"
    s.acquire(tid, "term")
    r = await text(ipc, "修改代码", "e2")
    assert "Terminal" in r["error"]
    r = await click(ipc, "end", "e3")
    assert "Terminal" in r["error"]
    r = await click(ipc, "status", "e4")
    assert "error" not in r
    s.release(tid, "term")
    await text(ipc, "修改代码", "e5")
    assert s.rpc.calls[-1][0] == "turn/steer"


async def test_menu_can_bootstrap_without_chat_id(service):
    ipc = IPCServer(service)
    await ipc.remote({"actor": "ou_test", "event_id": "m1", "menu": "create_task"})
    assert session(service)["chat"] == "open_id:ou_test"
    await text(ipc, "new-project", "e1")
    assert session(service)["chat"] == "oc_test"
    assert session(service)["stage"] == "description"
    with pytest.raises(UserError):
        await ipc.remote({"actor": "ou_intruder", "event_id": "m2", "menu": "create_task"})


async def test_selection_and_cancel_confirmation_bind_target(service):
    s, ipc = service, IPCServer(service)
    a = await active(s)
    from conftest import create
    b = await create(s, cwd="other")
    await click(ipc, "select", "e1", task=a)
    await click(ipc, "end", "e2")
    flow = session(s)["flow"]
    await click(ipc, "list", "e3")
    assert a in json.dumps(last_card(s)) and b in json.dumps(last_card(s))
    await click(ipc, "select", "e4", task=b)
    stale_command = await click(ipc, "command", "old-home", task=a)
    assert "不是当前任务" in stale_command["error"]
    result = await click(ipc, "confirm_end", "e5", task=a, flow=flow)
    assert "error" in result
    assert s.db.task(a)["status"] == "running" and s.db.task(b)["status"] == "queued"


async def test_ended_task_hidden_but_history_retained(service):
    from conftest import completed, create

    from feishu_codex.commands import parse
    s, ipc = service, IPCServer(service)
    finished = await active(s, cwd="finished")
    await completed(s, finished)
    ended = await create(s)
    await click(ipc, "select", "select", task=ended)
    await s.execute({"op": "cancel", "task": ended})
    assert session(s)["current_task"] is None
    await click(ipc, "list", "list")
    assert ended not in json.dumps(last_card(s))
    assert finished in json.dumps(last_card(s))
    assert [t["id"] for t in await s.execute(parse("tasks"))] == [finished]
    assert len(await s.execute(parse("tasks --all"))) == 2
    assert "已结束" in (await click(ipc, "select", "stale", task=ended))["error"]
    await click(ipc, "history", "history")
    assert ended in json.dumps(last_card(s))
    await click(ipc, "history_status", "result", task=ended)
    assert session(s)["current_task"] is None
    assert s.db.task(ended)["prompt"]


async def test_double_click_preserves_flow_and_coalesces_unsent_guides(service):
    s, ipc = service, IPCServer(service)
    await click(ipc, "create", "first")
    flow = session(s)["flow"]
    for i in range(20):
        r = await click(ipc, "create", f"repeat{i}")
        assert "无需重复" in r["message"]
    assert session(s)["flow"] == flow
    assert len(s.db.all("SELECT * FROM outbox WHERE kind='ui'")) == 1
    await click(ipc, "list", "switch")
    assert len(s.db.all("SELECT * FROM outbox WHERE kind='ui' AND status='pending'")) == 1
    assert s.db.one("SELECT count(*) n FROM dedup WHERE status='processing'")["n"] == 0


async def test_slow_command_does_not_queue_more_clicks_or_block_queries(service):
    s, ipc = service, IPCServer(service)
    tid = await active(s)
    await click(ipc, "select", "select", task=tid)
    await click(ipc, "command", "command")
    entered, release = asyncio.Event(), asyncio.Event()
    original = s.rpc.call
    async def slow(method, params):
        entered.set()
        await release.wait()
        return await original(method, params)
    s.rpc.call = slow
    pending = asyncio.create_task(text(ipc, "修改程序", "slow"))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        r = await asyncio.wait_for(click(ipc, "list", "busy"), 0.5)
        assert "正在处理" in r["error"]
        r = await asyncio.wait_for(text(ipc, f"status {tid}", "query"), 0.5)
        assert r["id"] == tid
        r = await asyncio.wait_for(ipc.remote({"actor": "ou_other", "event_id": "other", "menu": "list_tasks"}), 0.5)
        assert "error" not in r
    finally:
        release.set()
        await pending


async def test_cancel_during_running_hidden_even_after_recovery(service):
    s = service
    tid = await active(s)
    await s.execute({"op": "cancel", "task": tid})
    assert s.db.task(tid)["status"] == "cancelling"
    assert await s.execute({"op": "tasks"}) == []
    s.recover("test restart")
    assert await s.execute({"op": "tasks"}) == []
    with pytest.raises(UserError, match="已取消"):
        await s.execute({"op": "append", "task": tid, "text": "不能复活"})


async def test_existing_database_migrates_cancelled_history(service, tmp_path):
    from conftest import create

    from feishu_codex.store import Store
    s = service
    tid = await create(s)
    await click(IPCServer(s), "select", "select", task=tid)
    s.db.update(tid, status="cancelled")
    s.db.run("ALTER TABLE tasks DROP COLUMN closed")
    migrated = Store(tmp_path / "state.sqlite3", s.db.redact)
    try:
        assert migrated.task(tid)["closed"] == 1
        assert migrated.one("SELECT current_task FROM ui_sessions")["current_task"] is None
    finally:
        migrated.db.close()
