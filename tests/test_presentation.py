import json

import pytest
from conftest import active, request
from test_service import RecordingSender
from test_ui import click, last_card

from feishu_codex.ipc import IPCServer
from feishu_codex.notifications import Outbox, card_for
from feishu_codex.presentation import card, code, markdown

RESULT = "当前分支为 `main`，工作区干净。\n\n共有 **4 个提交**。\n\n| Commit | 内容 |\n|---|---|\n| `9eeb0f6` | 官方终端 |\n| `79908f7` | PATH |\n| `89fb204` | 归档 |\n| `2e5332d` | 初始化 |"


def rendered(c):
    return "\n".join(e["content"] for e in c["body"]["elements"] if e["tag"] == "markdown")


@pytest.mark.parametrize("kind", ["completed", "control", "progress", "failed", "reply"])
async def test_result_notifications_are_rich_cards(service, kind):
    tid = await active(service)
    service.notify(tid, kind, RESULT)
    sender = RecordingSender()
    await Outbox(service, sender).once()
    c = sender.sent[-1][1]
    assert c["schema"] == "2.0"
    assert "**4 个提交**" in rendered(c) and "| `9eeb0f6` |" in rendered(c)
    assert tid in json.dumps(c) and "工作区干净" in rendered(c)


async def test_status_and_history_share_markdown_and_button_callbacks(service):
    tid = await active(service)
    service.db.update(tid, summary=RESULT)
    ipc = IPCServer(service)
    await click(ipc, "select", "select", task=tid)
    await click(ipc, "status", "status", task=tid)
    c = last_card(service)
    assert rendered(c).endswith("| `2e5332d` | 初始化 |")
    buttons = [column["elements"][0] for e in c["body"]["elements"] if e["tag"] == "column_set" for column in e["columns"]]
    command = next(b for b in buttons if b["text"]["content"] == "输入命令")
    value = command["behaviors"][0]["value"]
    assert value == {"ui": "command", "task": tid}
    await click(ipc, value.pop("ui"), "command", **value)
    assert ipc.wizard.session("ou_test", "oc_test")["stage"] == "command"
    await click(ipc, "history_status", "history", task=tid)
    assert "**4 个提交**" in rendered(last_card(service))


def test_long_chinese_code_and_tables_have_bounded_payload_and_closed_fences():
    text = "# 结果\n\n```python\n" + 'print("中文")\n' * 5000
    c = card("任务完成", markdown(text))
    assert len(json.dumps(c, ensure_ascii=False).encode()) < 20000
    assert rendered(c).endswith("```")
    assert "仅显示前段" in str(c)
    c = card("多表格", markdown(("\n\n|a|b|\n|---|---|\n|1|2|\n" * 6)))
    assert len(c["body"]["elements"]) == 6


def test_model_html_cannot_ping_people_and_code_is_preserved():
    source = "<at id=all></at>\n\n" + code('print("<at id=all></at>")\n```')
    c = card("结果", markdown(source))
    assert rendered(c).startswith("&lt;at id=all>&lt;/at>")
    assert 'print("<at id=all></at>")' in rendered(c)
    assert "````plain_text" in rendered(c)


async def test_approval_layout_keeps_scope_and_supported_decisions(service):
    tid = await active(service)
    rid = await request(service, tid, availableDecisions=["decline", "cancel"])
    c = card_for(service.db, rid)
    assert "touch harmless.txt" in rendered(c)
    assert "仅本次操作" in str(c) and "请求原因" in str(c)
    buttons = c["body"]["elements"][-1]["columns"]
    assert [b["elements"][0]["behaviors"][0]["value"]["decision"] for b in buttons] == ["decline", "cancel"]
    service.invalidate(rid, "invalidated")
    assert '"tag": "button"' not in json.dumps(card_for(service.db, rid))
    rid = await request(service, tid, command="x" * 20000)
    c = card_for(service.db, rid)
    assert '"decision": "accept"' not in json.dumps(c)
    assert "不提供批准" in str(c)


async def test_user_message_does_not_consume_progress_throttle(service):
    tid = await active(service)
    t = service.db.task(tid)
    for kind, text in (("userMessage", "输入"), ("agentMessage", "**正在检查**")):
        await service.on_event({"method": "item/completed", "params": {"threadId": t["thread"],
            "turnId": t["turn"], "item": {"id": kind, "type": kind, "text": text}}})
    rows = service.db.all("SELECT body FROM outbox WHERE kind='progress'")
    assert len(rows) == 1 and "agentMessage" in rows[0]["body"]
