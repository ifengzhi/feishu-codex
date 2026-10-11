"""Official SDK long connection in a separate process; no public listener."""
import asyncio
import base64
import json
import logging
import socket
import threading
import time

import lark_oapi as lark
from lark_oapi.event.callback.model.p2_card_action_trigger import P2CardActionTriggerResponse
from lark_oapi.ws.client import (
    HEADER_BIZ_RT,
    HEADER_MESSAGE_ID,
    HEADER_SEQ,
    HEADER_SUM,
    HEADER_TYPE,
    MessageType,
    Response,
)


class CardCompatibleClient(lark.ws.Client):
    """lark-oapi 1.7.3 drops CARD frames. Route them through its event dispatcher.

    The SDK still owns framing, fragmentation, dispatch, callback response encoding,
    ping/pong and reconnect. Pinned and covered with a protobuf frame regression test.
    """
    async def _handle_data_frame(self, frame):
        header = next((h for h in frame.headers if h.key == HEADER_TYPE), None)
        if not header or header.value not in (MessageType.CARD.value, MessageType.EVENT.value):
            return await super()._handle_data_frame(frame)
        headers = {h.key: h.value for h in frame.headers}
        payload = frame.payload
        if int(headers[HEADER_SUM]) > 1:
            payload = self._combine(headers[HEADER_MESSAGE_ID], int(headers[HEADER_SUM]),
                                    int(headers[HEADER_SEQ]), payload)
            if payload is None:
                return
        response = Response(code=200)
        try:
            started = time.monotonic()
            # Local IPC can wait for Codex; never block SDK heartbeats/other frames.
            result = await asyncio.to_thread(self._event_handler._do_without_validation, payload)
            timing = frame.headers.add()
            timing.key, timing.value = HEADER_BIZ_RT, str(int((time.monotonic() - started) * 1000))
            if result is not None:
                response.data = base64.b64encode(lark.JSON.marshal(result).encode())
        except Exception:
            response = Response(code=500)
        frame.payload = lark.JSON.marshal(response).encode()
        await self._write_message(frame.SerializeToString())


def local_call(config, req, timeout=2):
    with socket.socket(socket.AF_UNIX) as sock:
        sock.settimeout(timeout)
        sock.connect(config.socket_path)
        sock.sendall((json.dumps(req, ensure_ascii=False) + "\n").encode())
        data = bytearray()
        while not data.endswith(b"\n"):
            part = sock.recv(65536)
            if not part:
                raise ConnectionError("backend disconnected")
            data.extend(part)
            if len(data) > 2 * 1024 * 1024:
                raise ValueError("response too large")
        result = json.loads(data)
        if not result["ok"]:
            return {"error": result["error"]}
        return result["result"]


class Bridge:
    def __init__(self, config, call=None):
        self.config = config
        self.call = call or (lambda req: local_call(config, req))

    def message(self, data):
        event = data.event
        if event.sender.sender_type != "user" or event.message.message_type != "text":
            return
        text = json.loads(event.message.content).get("text", "")
        if text.strip() in ("whoami", "/whoami") and event.message.chat_type == "p2p":
            self.call({"op": "identity_seen", "open_id": event.sender.sender_id.open_id})
            return
        if event.sender.sender_id.open_id not in self.config.feishu.get("allowed_users", []):
            # No task execution and no message to an unknown sender.
            return
        try:
            self.call({"op": "feishu_event", "event_id": "message:" + event.message.message_id,
                       "actor": event.sender.sender_id.open_id, "chat": event.message.chat_id,
                       "chat_type": event.message.chat_type, "text": text})
        except Exception:
            # Raising triggers SDK failure acknowledgement; Feishu can redeliver safely.
            raise RuntimeError("后台暂不可用，请稍后查询任务") from None

    def card(self, data):
        event = data.event
        try:
            result = self.call({"op": "feishu_event", "event_id": "card:" + data.header.event_id,
                                "actor": event.operator.open_id, "chat": event.context.open_chat_id,
                                "card": event.action.value})
            error = result.get("error") if isinstance(result, dict) else None
            message = result.get("message", "已发送下一步指引") if isinstance(result, dict) else "已发送下一步指引"
            return P2CardActionTriggerResponse({"toast": {
                "type": "error" if error else "success", "content": error or (message if event.action.value.get("ui") else "决定已处理；卡片将更新")}})
        except Exception:
            return P2CardActionTriggerResponse({"toast": {"type": "error", "content": "后台响应超时；请查询请求状态，勿重复批准"}})

    def menu(self, data):
        event = data.event
        self.call({"op": "feishu_event", "event_id": "menu:" + data.header.event_id,
                   "actor": event.operator.operator_id.open_id, "menu": event.event_key})

    def handler(self):
        return lark.EventDispatcherHandler.builder("", "", lark.LogLevel.ERROR).register_p2_im_message_receive_v1(
            self.message).register_p2_card_action_trigger(self.card).register_p2_application_bot_menu_v6(self.menu).build()


def run(config):
    if not config.feishu_configured:
        print("飞书未配置；本地后台与控制台不受影响")
        return
    # SDK debug/error payloads can contain tokens or user text; never persist them.
    logging.disable(logging.CRITICAL)
    bridge = Bridge(config)
    client = CardCompatibleClient(config.feishu["app_id"], config.feishu["app_secret"],
                                  event_handler=bridge.handler(), log_level=lark.LogLevel.ERROR)

    def health():
        while True:
            try:
                local_call(config, {"op": "feishu_health", "state": "长连接已连接" if client._conn is not None else "连接中/重连中"})
            except Exception:
                pass
            time.sleep(10)

    threading.Thread(target=health, daemon=True).start()
    try:
        client.start()
    except Exception:
        # Do not print SDK exception bodies or token-bearing endpoint URLs.
        print("飞书长连接启动失败；检查凭据、应用配置、网络；后台将有限速重启连接进程")
        raise SystemExit(1) from None
