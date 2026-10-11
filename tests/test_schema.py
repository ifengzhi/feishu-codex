"""Validate our wire payloads against the installed binary's generated schemas."""
import json
from pathlib import Path

import jsonschema
import pytest
from conftest import active, request

from feishu_codex.protocol import text_input


def validate(name, value):
    schemas = Path(__file__).parents[1] / ".cache/protocol"
    found = list(schemas.rglob(name + ".json"))
    if not found:
        pytest.skip("run scripts/check-protocol.sh to generate installed Codex schemas")
    jsonschema.validate(value, json.loads(found[0].read_text()))


async def test_generated_request_and_response_schemas(service):
    s = service
    tid = await active(s)
    names = {"thread/start": "ThreadStartParams", "turn/start": "TurnStartParams", "turn/steer": "TurnSteerParams"}
    await s.execute({"op": "append", "task": tid, "text": "steer"}, "ou_test")
    for method, params in s.rpc.calls:
        validate(names[method], params)
    for method, response in [("item/commandExecution/requestApproval", "CommandExecutionRequestApprovalResponse"),
                             ("item/fileChange/requestApproval", "FileChangeRequestApprovalResponse"),
                             ("item/permissions/requestApproval", "PermissionsRequestApprovalResponse")]:
        rid = await request(s, tid, method, permissions={"network": {"enabled": True}})
        await s.execute({"op": "decide", "task": tid, "request": rid, "decision": "decline"}, "ou_test")
        validate(response, s.rpc.replies[-1][1])
    rid = await request(s, tid, "item/tool/requestUserInput", questions=[{"id": "q", "header": "Q", "question": "Q?"}])
    await s.execute({"op": "answer", "task": tid, "request": rid, "answers": {"q": ["A"]}}, "ou_test")
    validate("ToolRequestUserInputResponse", s.rpc.replies[-1][1])


def test_input_wire_format():
    validate("TurnStartParams", {"threadId": "t", "input": text_input("你好")})
