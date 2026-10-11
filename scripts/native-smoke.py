#!/usr/bin/env python3
"""Opt-in real Codex TUI smoke test in an isolated temporary daemon/project.

Uses existing Codex auth/provider; never reads/prints credentials, never Feishu.
Creates one harmless background turn so Codex persists a real session.
--turn also submits a follow-up through the native terminal.
"""
import argparse
import asyncio
import contextlib
import fcntl
import json
import os
import pty
import re
import struct
import sys
import tempfile
import termios
from pathlib import Path

from feishu_codex.config import Config, Redactor, load_config
from feishu_codex.ipc import IPCServer
from feishu_codex.native import NativeEndpoint
from feishu_codex.protocol import text_input
from feishu_codex.service import INSTRUCTIONS, Service
from feishu_codex.store import Store


async def run(turn=False, detach_running=False, steer_running=False, verbose=False):
    existing = load_config()
    with tempfile.TemporaryDirectory(prefix="fc-native-") as tmp:
        root = Path(tmp)
        root.chmod(0o700)
        command = existing.codex_command
        config = root / "config.toml"
        config.write_text(f'work_roots = [{json.dumps(tmp)}]\ndata_dir = {json.dumps(tmp)}\n'
                          f'codex_command = {json.dumps(command)}\n')
        cfg = Config(work_roots=[tmp], data_dir=tmp, codex_command=command)
        db = Store(root / "tasks.sqlite3", Redactor(cfg))
        service = Service(cfg, db)
        ipc = IPCServer(service)
        consumer = asyncio.create_task(service.consume())
        calls = []
        wire_calls = []
        upstream = service.rpc.call
        async def scratch_config(method, params):
            wire_calls.append(method)
            result = await upstream(method, params)
            if method == "config/read":
                # Test-only in-memory trust for the empty generated project.
                # Do not persist trust or alter the real user's Codex config.
                project = str(root / "project")
                result["config"].setdefault("projects", {})[project] = {"trust_level": "trusted"}
                for layer in result.get("layers") or []:
                    layer["config"].setdefault("projects", {})[project] = {"trust_level": "trusted"}
            return result
        service.rpc.call = scratch_config
        dispatch = NativeEndpoint.dispatch
        async def trace(endpoint, method, params):
            # Method/field names only: raw params and TUI buffers are never logged.
            calls.append((method, sorted(params)))
            if verbose:
                print("TUI RPC:", method, sorted(params), flush=True)
            try:
                return await dispatch(endpoint, method, params)
            except Exception as e:
                if verbose:
                    print("RPC error:", db.redact(str(e))[:500], flush=True)
                raise
        NativeEndpoint.dispatch = trace
        master = slave = None
        proc = None
        try:
            await service.rpc.start()
            service.connected = True
            await ipc.start()
            tid = service.create({"cwd": "project", "mkdir": True, "text": "Native terminal smoke"}, "local")["task"]
            t = db.task(tid)
            result = await service.rpc.call("thread/start", {"cwd": t["cwd"], "approvalPolicy": cfg.approval_policy,
                "approvalsReviewer": "user", "sandbox": cfg.sandbox, "developerInstructions": INSTRUCTIONS})
            thread = result["thread"]["id"]
            db.update(tid, thread=thread, status="completed", pending=None)
            service.loaded.add(thread)
            await service.rpc.call("turn/start", {"threadId": thread,
                "input": text_input("Reply exactly BACKGROUND_READY. Do not run tools or modify files.")})
            async with asyncio.timeout(120):
                while db.task(tid)["summary"] != "BACKGROUND_READY" or db.task(tid)["status"] != "completed":
                    if db.task(tid)["status"] in ("failed", "interrupted", "waiting"):
                        raise RuntimeError("Background smoke turn failed or requires manual approval")
                    await asyncio.sleep(0.05)
            print("Background turn completed; attaching to existing persisted thread", flush=True)
            active_turn = None
            if detach_running or steer_running:
                reply = await service.rpc.call("turn/start", {"threadId": thread, "input": text_input(
                    "Run python3 -c 'import time; time.sleep(15)' once, then reply exactly NATIVE_BACKGROUND_DONE. Do not modify files.")})
                active_turn = reply["turn"]["id"]
            master, slave = pty.openpty()
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 32, 110, 0, 0))
            def terminal():
                os.setsid()
                fcntl.ioctl(0, termios.TIOCSCTTY, 0)
            proc = await asyncio.create_subprocess_exec(sys.executable, "-m", "feishu_codex.cli",
                "--config", str(config), "attach", tid, stdin=slave, stdout=slave, stderr=slave,
                preexec_fn=terminal, env={**os.environ, "TERM": "xterm-256color"})
            os.close(slave)
            slave = None
            os.set_blocking(master, False)
            sent_turn = exited = False
            joined = None
            buffer = bytearray()
            started = asyncio.get_running_loop().time()
            diagnosed = False
            async with asyncio.timeout(60):
                while proc.returncode is None:
                    try:
                        data = os.read(master, 65536)
                        buffer.extend(data)
                        if b"\x1b[6n" in data:
                            os.write(master, b"\x1b[1;1R")
                    except (BlockingIOError, OSError):
                        pass
                    if verbose and not diagnosed and asyncio.get_running_loop().time() - started > 10:
                        plain = re.sub(rb"\x1b\[[0-?]*[ -/]*[@-~]", b"", bytes(buffer)).decode(errors="replace")
                        print("Startup diagnostic:", db.redact(plain[-2200:]), flush=True)
                        diagnosed = True
                    if any(m == "thread/resume" for m, _ in calls) and joined is None:
                        joined = asyncio.get_running_loop().time()
                    if joined and asyncio.get_running_loop().time() - joined > 3:
                        if (turn or steer_running) and not sent_turn:
                            prompt = b"After the running command returns, reply exactly NATIVE_TUI_OK." if steer_running else b"Reply exactly NATIVE_TUI_OK. Do not run tools or modify files."
                            os.write(master, prompt)
                            await asyncio.sleep(0.3)  # Let Codex's paste-burst detector settle.
                            os.write(master, b"\r")
                            sent_turn = True
                        ready = not (turn or steer_running) or db.task(tid)["summary"] == "NATIVE_TUI_OK"
                        if ready and not exited:
                            os.write(master, b"/quit")
                            await asyncio.sleep(0.3)
                            os.write(master, b"\r")
                            exited = True
                    await asyncio.sleep(0.05)
            while True:
                try:
                    chunk = os.read(master, 65536)
                    if not chunk:
                        break
                    buffer.extend(chunk)
                except OSError:
                    break
            print("TUI process exit:", proc.returncode)
            # Safe diagnostic: only errors from our gateway, not raw terminal output.
            plain = re.sub(rb"\x1b\[[0-?]*[ -/]*[@-~]", b"", bytes(buffer)).decode(errors="replace")
            print("Native UI rendered:", "Codex" in plain, "Task state:", db.task(tid)["status"])
            print("Lease released:", tid not in service.leases)
            if turn or steer_running:
                print("Model response verified:", db.task(tid)["summary"] == "NATIVE_TUI_OK")
                assert db.task(tid)["summary"] == "NATIVE_TUI_OK"
            if steer_running:
                assert "turn/steer" in wire_calls
                assert active_turn == db.task(tid)["turn"]
                assert wire_calls.count("turn/start") == 2
                print("Running native input used turn/steer without a new turn:", True)
            if detach_running:
                assert active_turn == db.task(tid)["turn"]
                assert "turn/interrupt" not in wire_calls
                assert db.task(tid)["status"] in ("running", "waiting")
                async with asyncio.timeout(90):
                    while db.task(tid)["status"] == "running":
                        await asyncio.sleep(0.1)
                assert db.task(tid)["status"] == "completed", "Real task needs manual approval; no auto-approval performed"
                assert db.task(tid)["summary"] == "NATIVE_BACKGROUND_DONE"
                print("Running task survived native /quit and completed in same turn:", True)
            if proc.returncode or not joined or not exited or tid in service.leases:
                print("TUI failure diagnostic:", db.redact(plain[-3000:]), flush=True)
                raise RuntimeError("Native TUI smoke failed")
        finally:
            if proc and proc.returncode is None:
                proc.kill()
                await proc.wait()
            for fd in (master, slave):
                if fd is not None:
                    os.close(fd)
            print("Observed RPC methods:", sorted({m for m, _ in calls}))
            await ipc.close()
            consumer.cancel()
            await asyncio.gather(consumer, return_exceptions=True)
            await service.rpc.stop()
            db.db.close()
            NativeEndpoint.dispatch = dispatch


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--turn", action="store_true")
    mode.add_argument("--detach-running", action="store_true")
    mode.add_argument("--steer-running", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(run(args.turn, args.detach_running, args.steer_running, args.verbose))
