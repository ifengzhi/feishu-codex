import json
import time

import pytest
from conftest import active, completed, create, request
from test_ui import click, last_card, session, text

from feishu_codex.ipc import IPCServer


async def question(s, tid, **extra):
    return await request(s, tid, method="item/tool/requestUserInput", questions=extra.get("questions", [
        {"id": "plan", "question": "请选择方案", "isSecret": extra.get("secret", False),
         "options": [{"label": "快速实现", "description": "快"}, {"label": "完整实现", "description": "完整"}]}]))


def question_buttons(s):
    return [b["elements"][0]["behaviors"][0]["value"] for e in last_card(s)["body"]["elements"]
            if e["tag"] == "column_set" for b in e["columns"]]


async def test_direct_chat_steers_selected_task_and_continues_same_thread(service):
    s, ipc = service, IPCServer(service)
    tid = await active(s)
    await create(s, cwd="newer")
    await click(ipc, "select", "s", task=tid)
    # A selected task is persistent; the 30-minute wizard timeout does not expire it.
    s.db.run("UPDATE ui_sessions SET expires=0")
    await text(ipc, "增加日志", "m1")
    assert s.rpc.calls[-1][0] == "turn/steer"
    assert s.rpc.calls[-1][1]["threadId"] == s.db.task(tid)["thread"]
    before = len(s.rpc.calls)
    await text(ipc, "增加日志", "m1")
    assert len(s.rpc.calls) == before
    await text(ipc, "再补充测试", "m2")
    assert s.rpc.calls[-1][1]["input"][0]["text"] == "再补充测试"
    await completed(s, tid)
    thread = s.db.task(tid)["thread"]
    await text(ipc, "选择 B，继续", "m3")
    assert s.db.task(tid)["pending"] == "选择 B，继续"
    assert s.db.task(tid)["status"] == "queued" and s.db.task(tid)["thread"] == thread
    assert tid in str(last_card(s)) and s.db.task(tid)["cwd"] in str(last_card(s))


async def test_unselected_ended_terminal_and_other_chat_do_not_accept_text(service):
    s, ipc = service, IPCServer(service)
    tid = await active(s)
    assert "尚未选择" in (await text(ipc, "你好", "u"))["error"]
    await click(ipc, "select", "s", task=tid)
    s.acquire(tid, "term")
    assert "Terminal" in (await text(ipc, "继续", "blocked"))["error"]
    s.release(tid, "term")
    r = await ipc.remote({"actor": "ou_test", "event_id": "other-chat", "chat": "wrong", "chat_type": "p2p", "text": "继续"})
    assert "上下文" in r["error"]
    r = await ipc.remote({"actor": "ou_other", "event_id": "other-user", "chat": "other", "chat_type": "p2p", "text": "继续"})
    assert "尚未选择" in r["error"]
    await s.execute({"op": "cancel", "task": tid})
    assert "尚未选择" in (await text(ipc, "继续", "ended"))["error"]
    assert not any(method == "turn/steer" for method, _ in s.rpc.calls)


@pytest.mark.parametrize("answer,expected", [("2", "完整实现"), ("B", "完整实现"), ("快速实现", "快速实现"), ("其他想法", "其他想法")])
async def test_single_question_direct_answer(service, answer, expected):
    s, ipc = service, IPCServer(service)
    tid = await active(s)
    await click(ipc, "select", "s", task=tid)
    rid = await question(s, tid)
    before = len(s.rpc.calls)
    await text(ipc, answer, "answer")
    assert s.rpc.replies[-1][1] == {"answers": {"plan": {"answers": [expected]}}}
    assert len(s.rpc.calls) == before
    assert s.db.one("SELECT status FROM requests WHERE id=?", (rid,))["status"] == "resolved"
    await text(ipc, answer, "answer")
    assert len(s.rpc.replies) == 1 and len(s.rpc.calls) == before


async def test_multi_question_selection_partial_answers_and_restart(service):
    s, ipc = service, IPCServer(service)
    tid = await active(s)
    await click(ipc, "select", "s", task=tid)
    rid = await question(s, tid, questions=[{"id": "one", "question": "语言？"}, {"id": "two", "question": "框架？"}])
    await text(ipc, "不能猜是哪一题", "ambiguous")
    assert not s.rpc.replies and "尚未提交" in str(last_card(s))
    old = question_buttons(s)[0]
    await click(ipc, old["ui"], "choose", **{k: v for k, v in old.items() if k != "ui"})
    await text(ipc, "Python", "first-answer")
    assert not s.rpc.replies and "暂存" in str(last_card(s))
    await click(ipc, "command", "optional-guide")
    assert json.loads(session(s)["data"])["answers"] == {"one": ["Python"]}
    # Reconstructing the frontend preserves partial answers, not just process memory.
    ipc = IPCServer(s)
    assert "失效" in (await click(ipc, old["ui"], "stale", **{k: v for k, v in old.items() if k != "ui"}))["error"]
    choice = question_buttons(s)[0]
    assert choice["question"] == "two"
    await click(ipc, choice.pop("ui"), "choose-second", **choice)
    await text(ipc, "FastAPI", "second-answer")
    assert s.rpc.replies[-1][1] == {"answers": {"one": {"answers": ["Python"]}, "two": {"answers": ["FastAPI"]}}}
    assert session(s)["stage"] == "idle"
    assert s.db.one("SELECT status FROM requests WHERE id=?", (rid,))["status"] == "resolved"


async def test_multiple_requests_and_other_task_questions_do_not_guess(service):
    s, ipc = service, IPCServer(service)
    tid = await active(s)
    await click(ipc, "select", "s", task=tid)
    first = await question(s, tid)
    second = await question(s, tid)
    await text(ipc, "1", "ambiguous")
    choices = question_buttons(s)
    assert {c["request"] for c in choices} == {first, second}
    chosen = next(c for c in choices if c["request"] == second)
    await click(ipc, chosen.pop("ui"), "choose", **chosen)
    await text(ipc, "B", "reply")
    assert s.db.one("SELECT status FROM requests WHERE id=?", (first,))["status"] == "pending"
    other = await create(s, cwd="other")
    await click(ipc, "select", "other", task=other)
    await text(ipc, "直接追加给另一个任务", "other-message")
    assert "直接追加" in s.db.task(other)["pending"] and len(s.rpc.replies) == 1


@pytest.mark.parametrize("state", ["expired", "epoch", "resolved", "terminal", "secret"])
async def test_stale_or_protected_question_never_becomes_append(service, state):
    s, ipc = service, IPCServer(service)
    tid = await active(s)
    await click(ipc, "select", "s", task=tid)
    rid = await question(s, tid, secret=state == "secret")
    # Bind a question just as selecting a card would; stale state must not fall through.
    row = session(s)
    ipc.wizard.stage(row, "answer", {"request": rid, "question": "plan", "answers": {}})
    if state == "expired":
        s.db.run("UPDATE requests SET expires=? WHERE id=?", (time.time() - 1, rid))
    elif state == "epoch":
        s.rpc.epoch = "restarted"
    elif state == "resolved":
        s.invalidate(rid, "resolved_by_server")
    elif state == "terminal":
        s.acquire(tid, "terminal")
    before = len(s.rpc.calls)
    assert "error" in await text(ipc, "B", "reply")
    assert len(s.rpc.calls) == before and not s.rpc.replies


async def test_approval_is_never_granted_by_chat_or_command_button(service):
    s, ipc = service, IPCServer(service)
    tid = await active(s)
    await click(ipc, "select", "s", task=tid)
    await request(s, tid)
    assert "操作授权" in (await text(ipc, "批准", "approve"))["error"]
    await click(ipc, "command", "command")
    assert "操作授权" in (await text(ipc, "继续", "continue"))["error"]
    assert not s.rpc.replies and not any(m == "turn/steer" for m, _ in s.rpc.calls)


async def test_wizard_and_explicit_commands_keep_priority(service):
    s, ipc = service, IPCServer(service)
    tid = await active(s)
    await click(ipc, "select", "s", task=tid)
    await question(s, tid)
    await click(ipc, "create", "create")
    await text(ipc, "new-project", "directory")
    assert session(s)["stage"] == "description" and not s.rpc.replies
    assert (await text(ipc, f"status {tid}", "status"))["id"] == tid
    assert session(s)["stage"] == "description"
    await text(ipc, "做一个新程序", "description")
    assert session(s)["current_task"] != tid
    assert len(s.db.all("SELECT id FROM tasks")) == 2 and not s.rpc.replies
    await text(ipc, "补充说明", "followup")
    assert "补充说明" in s.db.task(session(s)["current_task"])["pending"]
    assert "answers" not in json.loads(session(s)["data"])


async def test_command_guide_answers_question_and_terminal_release_restores_chat(service):
    s, ipc = service, IPCServer(service)
    tid = await active(s)
    await click(ipc, "select", "s", task=tid)
    await question(s, tid)
    s.acquire(tid, "terminal")
    assert "Terminal" in (await text(ipc, "B", "blocked"))["error"]
    s.release(tid, "terminal")
    await click(ipc, "command", "guide")
    await text(ipc, "B", "answer")
    assert s.rpc.replies[-1][1] == {"answers": {"plan": {"answers": ["完整实现"]}}}
    await text(ipc, "增加测试", "followup")
    assert s.rpc.calls[-1][0] == "turn/steer"


async def test_resolved_question_picker_cannot_turn_answer_into_new_instruction(service):
    s, ipc = service, IPCServer(service)
    tid = await active(s)
    await click(ipc, "select", "s", task=tid)
    rids = [await question(s, tid), await question(s, tid)]
    await text(ipc, "B", "picker")
    for rid in rids:
        s.invalidate(rid, "resolved_by_server")
    before = len(s.rpc.calls)
    assert "失效" in (await text(ipc, "B", "late-answer"))["error"]
    assert len(s.rpc.calls) == before and not s.rpc.replies
