#!/usr/bin/env python3
"""Validate the installed systemd user's model configuration through the public IPC."""
import asyncio
import tempfile
from pathlib import Path

from feishu_codex.config import load_config
from feishu_codex.ipc import Client


async def main():
    cfg = load_config()
    client = await Client(cfg).connect()
    with tempfile.TemporaryDirectory(prefix="smoke-", dir=cfg.work_roots[0]) as tmp:
        try:
            result = await client.call({"op": "create", "cwd": str(Path(tmp) / "new"), "mkdir": True,
                                       "text": "在当前目录创建 hello.py，内容 print(42)，创建简短 README.md，然后运行 python3 hello.py 确认结果。无需其他文件，不要联网，不要操作 Git，不要读取任何配置。完成后回复 SYSTEMD_SMOKE_OK。"})
            tid = result["task"]
            print("systemd task:", tid, flush=True)
            async with asyncio.timeout(240):
                while True:
                    t = await client.call({"op": "status", "task": tid})
                    if t["status"] == "waiting":
                        raise RuntimeError("需要人工审批；脚本不会自动批准")
                    if t["status"] in ("completed", "failed", "interrupted"):
                        break
                    await asyncio.sleep(0.5)
            assert t["status"] == "completed", t["summary"]
            assert (Path(tmp) / "new/hello.py").read_text().strip() == "print(42)"
            print("systemd model + new cwd + file execution: OK", flush=True)
        finally:
            await client.close()


asyncio.run(main())
