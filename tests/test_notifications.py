import asyncio
from types import SimpleNamespace

from conftest import active, create
from test_service import RecordingSender

from feishu_codex.notifications import FeishuSender, Outbox


def test_sdk_send_and_patch_builders():
    calls = []
    def send(req):
        calls.append(req)
        return SimpleNamespace(success=lambda: True, data=SimpleNamespace(message_id="om_result"))
    sender = FeishuSender.__new__(FeishuSender)
    sender.client = SimpleNamespace(im=SimpleNamespace(v1=SimpleNamespace(message=SimpleNamespace(create=send, patch=send))))
    row = {"id": "id", "chat": "open_id:ou_test", "kind": "ui", "message_id": None}
    assert sender.send(row, {"elements": []}) == "om_result"
    assert calls[-1].receive_id_type == "open_id"
    assert calls[-1].request_body.receive_id == "ou_test"
    assert calls[-1].request_body.msg_type == "interactive"
    row.update(kind="completed")
    sender.send(row, {"schema": "2.0", "body": {"elements": []}})
    assert calls[-1].request_body.msg_type == "interactive"
    row.update(kind="request", message_id="om_result")
    sender.send(row, {"elements": []})
    assert calls[-1].message_id == "om_result"


async def test_paused_backlog_cannot_starve_other_tasks(service):
    s = service
    a = await active(s)
    b = await create(s, cwd="other")
    s.acquire(a, "terminal")
    for i in range(60):
        s.notify(a, "test", str(i), key=str(i))
    s.notify(b, "test", "deliver")
    sender = RecordingSender()
    await Outbox(s, sender).once()
    assert len(sender.sent) == 1 and sender.sent[0][0]["task"] == b


async def test_superseded_ui_in_loaded_batch_is_not_sent(service, monkeypatch):
    s = service
    s.db.notify("reply", None, "oc_test", "reply", "reply")
    s.db.notify("old-ui", None, "oc_test", "ui", {"old": True})
    sent = []
    async def send(fn, row, body):
        sent.append(row["id"])
        if row["id"] == "reply":
            s.db.notify("new-ui", None, "oc_test", "ui", {"new": True})
        return "om_test"
    monkeypatch.setattr(asyncio, "to_thread", send)
    outbox = Outbox(s, RecordingSender())
    await outbox.once()
    assert sent == ["reply"]
    await outbox.once()
    assert sent == ["reply", "new-ui"]
