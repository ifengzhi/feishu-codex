import asyncio
import base64
import json
import threading

import lark_oapi as lark
import pytest
from lark_oapi.ws.client import HEADER_MESSAGE_ID, HEADER_SEQ, HEADER_SUM, HEADER_TRACE_ID, HEADER_TYPE
from lark_oapi.ws.pb.pbbp2_pb2 import Frame

from feishu_codex.feishu import Bridge, CardCompatibleClient


def payload(kind="card.action.trigger"):
    return {"schema": "2.0", "header": {"event_id": "ev-123", "event_type": kind,
            "create_time": "1", "token": "", "app_id": "cli_fake", "tenant_key": "tenant"},
            "event": {"operator": {"open_id": "ou_test"}, "token": "test_token",
                      "action": {"tag": "button", "value": {"task": "task-1", "request": "r-1", "decision": "decline"}},
                      "context": {"open_chat_id": "oc_test", "open_message_id": "om_test"}}}


def frame(data, kind="card", seq=0, total=1):
    f = Frame()
    f.method = 1
    f.SeqID, f.LogID, f.service = 1, 1, 1
    for key, value in ((HEADER_TYPE, kind), (HEADER_MESSAGE_ID, "message-1"),
                       (HEADER_SUM, str(total)), (HEADER_SEQ, str(seq)), (HEADER_TRACE_ID, "trace-1")):
        h = f.headers.add()
        h.key, h.value = key, value
    f.payload = data
    return f


@pytest.mark.parametrize("kind,fragmented", [("card", False), ("event", False), ("card", True)])
async def test_real_sdk_frame_dispatch_and_ack(service, kind, fragmented):
    calls, sent = [], []
    def call(req):
        calls.append(req)
        return {"status": "resolved"}
    bridge = Bridge(service.cfg, call)
    client = CardCompatibleClient("cli_fake", "fake_secret", event_handler=bridge.handler(), log_level=lark.LogLevel.ERROR)
    async def write(data):
        f = Frame()
        f.ParseFromString(data)
        sent.append(f)
    client._write_message = write
    data = json.dumps(payload()).encode()
    if fragmented:
        half = len(data) // 2
        await client._handle_data_frame(frame(data[:half], kind, 0, 2))
        assert not sent and not calls
        await client._handle_data_frame(frame(data[half:], kind, 1, 2))
    else:
        await client._handle_data_frame(frame(data, kind))
    assert len(calls) == len(sent) == 1
    assert calls[0]["actor"] == "ou_test"
    assert calls[0]["card"]["decision"] == "decline"
    response = json.loads(sent[0].payload)
    assert response["code"] == 200
    assert json.loads(base64.b64decode(response["data"]))["toast"]["type"] == "success"
    assert next(h.value for h in sent[0].headers if h.key == HEADER_TYPE) == kind


def test_sdk_message_parsing_and_empty_whitelist(service):
    calls = []
    bridge = Bridge(service.cfg, lambda x: calls.append(x))
    p = payload("im.message.receive_v1")
    p["event"] = {"sender": {"sender_id": {"open_id": "ou_test"}, "sender_type": "user"},
                  "message": {"message_id": "om_msg", "chat_id": "oc_test", "chat_type": "p2p",
                              "message_type": "text", "content": json.dumps({"text": "tasks"})}}
    bridge.handler()._do_without_validation(json.dumps(p).encode())
    assert calls[0]["text"] == "tasks" and calls[0]["event_id"] == "message:om_msg"
    service.cfg.feishu["allowed_users"] = []
    bridge.handler()._do_without_validation(json.dumps(p).encode())
    assert len(calls) == 1


def test_sdk_bot_menu_dispatch(service):
    calls = []
    bridge = Bridge(service.cfg, lambda x: calls.append(x))
    p = payload("application.bot.menu_v6")
    p["event"] = {"operator": {"operator_id": {"open_id": "ou_test"}, "operator_name": "test"},
                  "event_key": "create_task", "timestamp": 1}
    bridge.handler()._do_without_validation(json.dumps(p).encode())
    assert calls[0]["menu"] == "create_task" and calls[0]["actor"] == "ou_test"


def test_identity_discovery_does_not_grant_access(service):
    calls = []
    service.cfg.feishu["allowed_users"] = []
    bridge = Bridge(service.cfg, lambda x: calls.append(x))
    p = payload("im.message.receive_v1")
    p["event"] = {"sender": {"sender_id": {"open_id": "ou_test"}, "sender_type": "user"},
                  "message": {"message_id": "om_msg", "chat_id": "oc_test", "chat_type": "p2p",
                              "message_type": "text", "content": json.dumps({"text": "/whoami"})}}
    bridge.handler()._do_without_validation(json.dumps(p).encode())
    assert calls == [{"op": "identity_seen", "open_id": "ou_test"}]
    assert service.cfg.feishu["allowed_users"] == []


@pytest.mark.parametrize("kind", ["card", "event"])
async def test_slow_callback_leaves_websocket_loop_responsive(service, kind):
    release = threading.Event()
    entered = threading.Event()
    def call(req):
        entered.set()
        release.wait(2)
        return {}
    client = CardCompatibleClient("cli_fake", "fake_secret", event_handler=Bridge(service.cfg, call).handler())
    async def write(data):
        pass
    client._write_message = write
    pending = asyncio.create_task(client._handle_data_frame(frame(json.dumps(payload()).encode(), kind)))
    try:
        async with asyncio.timeout(1):
            while not entered.is_set():
                await asyncio.sleep(0.001)
        # The old synchronous dispatcher only yielded after its two-second wait.
        assert not pending.done()
    finally:
        release.set()
        await pending
