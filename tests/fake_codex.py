#!/usr/bin/env python3
"""Deterministic stdio protocol peer for process-lifecycle tests. No network/real model."""
import asyncio
import json
import sys
import uuid
from pathlib import Path

state_file = Path(__file__).with_suffix(".state.json")
threads = json.loads(state_file.read_text()) if state_file.exists() else {}
running = {}
pending = {}


def emit(msg):
    print(json.dumps(msg), flush=True)


def notification(method, params):
    emit({"method": method, "params": params})


async def finish(thread, turn, delay=0.1):
    await asyncio.sleep(delay)
    notification("item/completed", {"threadId": thread, "turnId": turn, "item": {
        "id": "item-result", "type": "agentMessage", "text": "FAKE_DONE"}})
    notification("turn/completed", {"threadId": thread, "turn": {"id": turn, "status": "completed", "error": None}})


async def handle(msg):
    if "method" not in msg:
        entry = pending.pop(str(msg["id"]), None)
        if entry:
            running[entry[0]] = asyncio.create_task(finish(*entry))
        return
    method, params = msg["method"], msg.get("params", {})
    result = {}
    if method == "initialize":
        result = {"userAgent": "fake-codex/0.160.0"}
    elif method == "thread/start":
        thread = uuid.uuid4().hex
        threads[thread] = {"id": thread, "cwd": params["cwd"], "status": {"type": "idle"}, "turns": []}
        state_file.write_text(json.dumps(threads))
        result = {"thread": threads[thread]}
    elif method in ("thread/read", "thread/resume"):
        result = {"thread": threads[params["threadId"]]}
    elif method == "turn/start":
        thread, turn = params["threadId"], uuid.uuid4().hex
        result = {"turn": {"id": turn, "status": "inProgress"}}
        emit({"id": msg["id"], "result": result})
        notification("turn/started", {"threadId": thread, "turn": result["turn"]})
        text = params["input"][0]["text"]
        if "approval" in text:
            rid = "approval-" + turn
            pending[rid] = (thread, turn)
            emit({"id": rid, "method": "item/commandExecution/requestApproval", "params": {
                "threadId": thread, "turnId": turn, "itemId": "command-1", "startedAtMs": 1,
                "command": "touch example", "cwd": threads[thread]["cwd"], "reason": "test",
                "availableDecisions": ["accept", "decline", "cancel"]}})
        else:
            running[thread] = asyncio.create_task(finish(thread, turn, 1.5 if "slow" in text else 0.1))
        return
    elif method == "turn/interrupt":
        thread, turn = params["threadId"], params["turnId"]
        if thread in running:
            running.pop(thread).cancel()
        emit({"id": msg["id"], "result": {}})
        notification("turn/completed", {"threadId": thread, "turn": {"id": turn, "status": "interrupted", "error": None}})
        return
    if "id" in msg:
        emit({"id": msg["id"], "result": result})


async def main():
    reader = asyncio.StreamReader()
    protocol = asyncio.StreamReaderProtocol(reader)
    await asyncio.get_running_loop().connect_read_pipe(lambda: protocol, sys.stdin)
    while line := await reader.readline():
        await handle(json.loads(line))


asyncio.run(main())
