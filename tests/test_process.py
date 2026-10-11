import asyncio
import json
import os
import shutil
import signal
import sys
from pathlib import Path

import pytest

from feishu_codex.config import UserError, load_config
from feishu_codex.ipc import Client


async def until(client, tid, states):
    async with asyncio.timeout(10):
        while True:
            t = await client.call({"op": "status", "task": tid})
            if t["status"] in states:
                return t
            await asyncio.sleep(0.04)


@pytest.fixture
async def daemon(tmp_path):
    fake = tmp_path / "fake-codex"
    shutil.copyfile(Path(__file__).with_name("fake_codex.py"), fake)
    fake.chmod(0o700)
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text(f'work_roots = [{json.dumps(str(tmp_path / "work"))}]\ndata_dir = {json.dumps(str(tmp_path / "data"))}\ncodex_command = [{json.dumps(str(fake))}, "app-server", "--stdio"]\n')
    cfg = load_config(cfg_file)
    procs = []

    async def start():
        p = await asyncio.create_subprocess_exec(sys.executable, "-m", "feishu_codex.cli", "--config", str(cfg_file), "serve",
                                                 stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
        procs.append(p)
        async with asyncio.timeout(10):
            while True:
                if p.returncode is not None:
                    raise AssertionError((await p.stderr.read()).decode())
                try:
                    c = await Client(cfg).connect()
                    d = await c.call({"op": "diagnose"})
                    if d["app_server"] == "ready":
                        return p, c
                    await c.close()
                except (OSError, UserError):
                    pass
                await asyncio.sleep(0.05)
    yield cfg, start
    for p in procs:
        if p.returncode is None:
            p.terminate()
            await asyncio.wait_for(p.wait(), 10)


async def test_process_client_disconnect_and_takeover(daemon):
    cfg, start = daemon
    proc, c = await start()
    t = await c.call({"op": "create", "cwd": "new", "mkdir": True, "key": "key-1", "text": "slow"})
    tid = t["task"]
    await until(c, tid, {"running"})
    await c.call({"op": "attach", "task": tid})
    assert os.stat(cfg.socket_path).st_mode & 0o777 == 0o600
    await c.close()
    fresh = await Client(cfg).connect()
    try:
        t = await until(fresh, tid, {"completed"})
        assert t["control"] == "available" and t["summary"] == "FAKE_DONE"
        assert proc.returncode is None
    finally:
        await fresh.close()


async def test_process_restart_invalidates_approval_without_replay(daemon):
    cfg, start = daemon
    proc, c = await start()
    tid = (await c.call({"op": "create", "cwd": "new", "mkdir": True, "text": "approval"}))["task"]
    t = await until(c, tid, {"waiting"})
    rid, thread = t["requests"][0]["id"], t["thread"]
    proc.kill()  # simulate ungraceful daemon death; stdio peer exits on EOF
    await proc.wait()
    await c.close()
    proc2, c2 = await start()
    try:
        t = await c2.call({"op": "status", "task": tid})
        assert t["status"] == "interrupted"
        assert t["requests"][0]["status"] == "invalidated"
        with pytest.raises(UserError, match="失效"):
            await c2.call({"op": "decide", "task": tid, "request": rid, "decision": "accept"})
        await c2.call({"op": "continue", "task": tid, "text": "inspect then complete"})
        t = await until(c2, tid, {"completed"})
        assert t["thread"] == thread
        assert any(e["kind"] == "history_checked" for e in t["events"])
    finally:
        await c2.close()


async def test_process_shared_approval_and_cancellation(daemon):
    cfg, start = daemon
    _, c = await start()
    try:
        tid = (await c.call({"op": "create", "cwd": "new", "mkdir": True, "text": "approval"}))["task"]
        t = await until(c, tid, {"waiting"})
        rid = t["requests"][0]["id"]
        await c.call({"op": "attach", "task": tid})
        await c.call({"op": "decide", "task": tid, "request": rid, "decision": "accept"})
        await until(c, tid, {"completed"})
        with pytest.raises(UserError):
            await c.call({"op": "decide", "task": tid, "request": rid, "decision": "accept"})
        await c.call({"op": "continue", "task": tid, "text": "slow"})
        await until(c, tid, {"running"})
        await c.call({"op": "append", "task": tid, "text": "additional requirement"})
        await c.call({"op": "cancel", "task": tid})
        await until(c, tid, {"cancelled"})
    finally:
        await c.close()


@pytest.mark.parametrize("exit_mode", ["detach", "sigint"])
async def test_terminal_interface_exit_preserves_task(daemon, exit_mode):
    cfg, start = daemon
    _, c = await start()
    tid = (await c.call({"op": "create", "cwd": "new", "mkdir": True, "text": "approval"}))["task"]
    await until(c, tid, {"waiting"})
    config_file = Path(cfg.data_dir).parent / "config.toml"
    terminal = await asyncio.create_subprocess_exec(sys.executable, "-m", "feishu_codex.cli", "--config", str(config_file),
        "attach", tid, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        async with asyncio.timeout(5):
            while (await c.call({"op": "status", "task": tid}))["control"] != "terminal":
                await asyncio.sleep(0.04)
        if exit_mode == "detach":
            terminal.stdin.write(b"/detach\n")
            await terminal.stdin.drain()
        else:
            # Wait until the interactive loop has installed its input reader.
            await asyncio.sleep(0.1)
            terminal.send_signal(signal.SIGINT)
        output, error = await asyncio.wait_for(terminal.communicate(), 5)
        assert terminal.returncode == 0, error.decode()
        assert "后台任务继续运行" in output.decode()
        async with asyncio.timeout(5):
            while (t := await c.call({"op": "status", "task": tid}))["control"] != "available":
                await asyncio.sleep(0.04)
        assert t["status"] == "waiting" and t["requests"][0]["status"] == "pending"
    finally:
        if terminal.returncode is None:
            terminal.kill()
            await terminal.wait()
        await c.close()
