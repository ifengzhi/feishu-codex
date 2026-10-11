import asyncio
import fcntl
import logging
import os
import signal
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .config import Redactor, UserError
from .ipc import IPCServer
from .notifications import FeishuSender, Outbox
from .service import Service
from .store import Store


async def run(config, config_file=None):
    os.umask(0o077)
    data = Path(config.data_dir)
    data.mkdir(parents=True, exist_ok=True, mode=0o700)
    data.chmod(0o700)
    parent = Path(config.socket_path).parent
    parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if parent.stat().st_uid != os.getuid() or parent.stat().st_mode & 0o077:
        raise UserError("socket 所在目录必须由当前用户拥有且权限为 700")
    lock_file = open(data / "daemon.lock", "a")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise UserError("此数据目录已有后台进程运行") from None
    # Also protect custom socket paths shared by configurations with different data dirs.
    socket_lock = open(str(config.socket_path) + ".lock", "a")
    try:
        fcntl.flock(socket_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise UserError("此 socket 已被另一个后台占用") from None
    handler = RotatingFileHandler(data / "daemon.log", maxBytes=config.log_max_bytes,
                                  backupCount=config.log_backups, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)
    for name in ("Lark", "lark_oapi", "httpx", "httpcore", "websockets"):
        logger = logging.getLogger(name)
        logger.handlers = [logging.NullHandler()]
        logger.propagate = False
        logger.disabled = True
    store = Store(data / "tasks.sqlite3", Redactor(config))
    service = Service(config, store)
    service.recover()
    ipc = IPCServer(service)
    await ipc.start()
    sender = FeishuSender(config) if config.feishu_configured else None
    jobs = [asyncio.create_task(coro) for coro in (
        service.consume(), service.connect_loop(), service.worker(), service.maintain(), Outbox(service, sender).run())]
    bridge_proc = None

    async def bridge_supervisor():
        nonlocal bridge_proc
        while True:
            args = [sys.executable, "-m", "feishu_codex.cli"]
            if config_file:
                args += ["--config", str(config_file)]
            args += ["_feishu"]
            bridge_proc = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.DEVNULL,
                                                              stderr=asyncio.subprocess.DEVNULL)
            await bridge_proc.wait()
            service.feishu_state = "连接进程退出；30 秒后重试"
            logging.warning("Feishu connection process exited")
            await asyncio.sleep(30)

    if config.feishu_configured:
        jobs.append(asyncio.create_task(bridge_supervisor()))
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    # A failed core loop must not leave a half-alive daemon.
    for job in jobs:
        job.add_done_callback(lambda task: stop.set() if not task.cancelled() else None)
    print(f"后台已启动：{config.socket_path}；飞书{'已配置，正在连接' if config.feishu_configured else '未配置'}", flush=True)
    await stop.wait()
    service.closing = True
    if bridge_proc and bridge_proc.returncode is None:
        bridge_proc.terminate()
        try:
            await asyncio.wait_for(bridge_proc.wait(), 5)
        except TimeoutError:
            bridge_proc.kill()
            await bridge_proc.wait()
    for job in jobs:
        job.cancel()
    await asyncio.gather(*jobs, return_exceptions=True)
    await service.rpc.stop()
    service.recover("后台停止；原活动 turn 中断，旧审批失效；明确继续才会启动新 turn")
    await ipc.close()
    store.db.close()
    lock_file.close()
    socket_lock.close()
