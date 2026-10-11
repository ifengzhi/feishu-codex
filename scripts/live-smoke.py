#!/usr/bin/env python3
"""Opt-in live test: existing Codex config, temporary workspace, no automatic approvals."""
import asyncio
import json
import tempfile
from pathlib import Path

from feishu_codex.config import Config, Redactor
from feishu_codex.service import Service
from feishu_codex.store import Store


async def main():
    with tempfile.TemporaryDirectory(prefix="feishu-codex-live-") as tmp:
        cfg = Config(work_roots=[tmp], data_dir=tmp, turn_timeout=240)
        db = Store(Path(tmp) / "tasks.sqlite3", Redactor(cfg))
        s = Service(cfg, db)
        jobs = []
        try:
            await s.rpc.start()
            s.connected = True
            print("initialize: OK", flush=True)
            jobs = [asyncio.create_task(s.consume()), asyncio.create_task(s.worker())]
            result = await s.execute({"op": "create", "cwd": str(Path(tmp) / "new-project"), "mkdir": True,
                                      "key": "live-smoke", "text": "这是最小集成测试。在当前目录创建 hello.py，内容是 print(42)，创建简短 README.md，运行 python3 hello.py 确认输出 42。不要访问网络，不要安装依赖，不要创建其他报告，不要进行 git 操作。完成后回复 LIVE_SMOKE_OK。"})
            tid = result["task"]
            print("create nonexistent cwd: OK", flush=True)
            async def finish():
                while True:
                    t = db.task(tid)
                    if t["status"] == "waiting":
                        raise RuntimeError("真实任务需要人工审批；测试不会自动批准，请通过控制台验收")
                    if t["status"] not in ("queued", "starting", "running"):
                        return t
                    await asyncio.sleep(0.5)
            t = await asyncio.wait_for(finish(), 240)
            print(json.dumps({k: t[k] for k in ("status", "summary", "thread")}, ensure_ascii=False), flush=True)
            assert t["status"] == "completed", t["summary"]
            assert (Path(t["cwd"]) / "hello.py").read_text().strip() == "print(42)"
            assert (Path(t["cwd"]) / "README.md").exists()
            assert not (Path(t["cwd"]) / "reports").exists()
            first_thread = t["thread"]
            await s.execute({"op": "continue", "task": tid, "text": "不要调用工具，只回复 CONTINUE_OK。"})
            t = await asyncio.wait_for(finish(), 120)
            assert t["status"] == "completed" and t["thread"] == first_thread
            print("continue same thread: OK", flush=True)
            for job in jobs:
                job.cancel()
            await asyncio.gather(*jobs, return_exceptions=True)
            await s.rpc.stop()
            s.recover()
            await s.rpc.start()
            s.connected = True
            jobs = [asyncio.create_task(s.consume()), asyncio.create_task(s.worker())]
            await s.execute({"op": "continue", "task": tid, "text": "恢复验证：不要调用工具，只回复 RESUME_OK。"})
            t = await asyncio.wait_for(finish(), 120)
            assert t["status"] == "completed" and t["thread"] == first_thread, t["summary"]
            print("thread/read + thread/resume after restart: OK", flush=True)
        finally:
            for job in jobs:
                job.cancel()
            await asyncio.gather(*jobs, return_exceptions=True)
            await s.rpc.stop()
            db.db.close()


asyncio.run(main())
