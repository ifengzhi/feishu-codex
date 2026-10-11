import argparse
import asyncio
import json
import os
import shlex
import signal
import sys
import termios

from .commands import HELP, parse, render
from .config import UserError, config_path, load_config
from .ipc import Client


async def attach(client, tid):
    initial = await client.call({"op": "attach", "task": tid})
    print(render(initial))
    print("历史进展：")
    for event in initial["events"]:
        print(f"[{event['seq']}] {event['kind']}: {json.dumps(event['body'], ensure_ascii=False)}")
    print("控制台：/append 要求 | /decide 请求ID accept|decline|cancel | /answer 请求ID JSON")
    print("/interrupt 中断 turn | /cancel 取消任务 | /status | /detach 退出但后台继续。Ctrl+C 同 /detach。")
    reader = asyncio.StreamReader()
    protocol = asyncio.StreamReaderProtocol(reader)
    transport, _ = await asyncio.get_running_loop().connect_read_pipe(lambda: protocol, sys.stdin)
    cursor = initial["events"][-1]["seq"] if initial["events"] else 0

    async def watch():
        nonlocal cursor
        while True:
            await asyncio.sleep(min(2, client.config.lease_timeout / 3))
            await client.call({"op": "heartbeat", "task": tid})
            t = await client.call({"op": "status", "task": tid, "after": cursor})
            for event in t["events"]:
                print(f"\n[{event['seq']}] {event['kind']}: {json.dumps(event['body'], ensure_ascii=False)}", flush=True)
                cursor = event["seq"]

    watcher = asyncio.create_task(watch())
    try:
        while True:
            print("fc> ", end="", flush=True)
            read = asyncio.create_task(reader.readline())
            done, _ = await asyncio.wait((read, watcher), return_when=asyncio.FIRST_COMPLETED)
            if watcher in done:
                read.cancel()
                await watcher
            line = (await read).decode().strip()
            if line in ("/detach", "/quit", "/exit") or not line and reader.at_eof():
                break
            if not line:
                continue
            try:
                op, _, rest = line.removeprefix("/").partition(" ")
                if op == "help":
                    print(HELP)
                    continue
                if op in ("status", "cancel", "interrupt"):
                    req = {"op": op, "task": tid}
                elif op in ("append", "continue", "decide", "answer"):
                    req = parse(f"{op} {tid} {rest}")
                elif line.startswith("/"):
                    raise UserError("未知控制台命令")
                else:
                    req = {"op": "append", "task": tid, "text": line}
                print(render(await client.call(req)))
            except UserError as e:
                print(f"错误：{e}")
    finally:
        watcher.cancel()
        await asyncio.gather(watcher, return_exceptions=True)
        transport.close()
        try:
            await client.call({"op": "detach", "task": tid})
        except Exception:
            pass
        print("\n已退出界面；后台任务继续运行，控制权已释放。")


async def native_attach(client, tid):
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise UserError("原生 Codex 界面需要交互终端；管道/简易控制台请使用 attach --plain <任务编号>")
    info = await client.call({"op": "native_attach", "task": tid})
    print(f"正在接管任务 {tid} · {info['cwd']}\n进入官方 Codex 界面；/quit 退出并释放控制权。Ctrl+C 遵循 Codex 原生行为，可能中断当前执行。", flush=True)
    proc = watcher = waiter = None
    terminal_state = termios.tcgetattr(sys.stdin.fileno())
    loop = asyncio.get_running_loop()
    terminated = asyncio.Event()
    # Both parent and TUI receive terminal SIGINT; only the TUI interprets it.
    loop.add_signal_handler(signal.SIGINT, lambda: None)
    loop.add_signal_handler(signal.SIGTERM, terminated.set)
    try:
        env = {k: v for k, v in os.environ.items() if not k.startswith("FEISHU_")}
        proc = await asyncio.create_subprocess_exec(client.config.codex_command[0],
            "resume", "--remote", info["endpoint"], "--cd", info["cwd"],
            info["thread"], env=env)
        async def heartbeat():
            while True:
                await asyncio.sleep(min(2, info["lease_timeout"] / 3))
                await client.call({"op": "heartbeat", "task": tid})
        watcher = asyncio.create_task(heartbeat())
        waiter = asyncio.create_task(proc.wait())
        stopping = asyncio.create_task(terminated.wait())
        try:
            done, _ = await asyncio.wait((watcher, waiter, stopping), return_when=asyncio.FIRST_COMPLETED)
            if watcher in done:
                try:
                    await watcher
                except (UserError, ConnectionError):
                    # Native /quit closes its WebSocket before the TUI process
                    # finishes restoring the terminal; the daemon releases early.
                    try:
                        await asyncio.wait_for(asyncio.shield(waiter), 3)
                    except TimeoutError:
                        raise UserError("终端控制权已失效，已断开；请查询任务状态后重新 attach") from None
                    if proc.returncode:
                        raise
            if waiter in done and proc.returncode:
                raise UserError(f"Codex 界面退出 code={proc.returncode}；可重试或使用 attach --plain {tid}")
        finally:
            stopping.cancel()
            await asyncio.gather(stopping, return_exceptions=True)
    finally:
        if proc and proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), 3)
            except TimeoutError:
                proc.kill()
                await proc.wait()
        for job in (watcher, waiter):
            if job:
                job.cancel()
        await asyncio.gather(*(j for j in (watcher, waiter) if j), return_exceptions=True)
        try:
            await client.call({"op": "detach", "task": tid})
        except Exception:
            pass
        loop.remove_signal_handler(signal.SIGINT)
        loop.remove_signal_handler(signal.SIGTERM)
        termios.tcsetattr(sys.stdin.fileno(), termios.TCSANOW, terminal_state)
        print("\n已退出原生界面；后台任务保留，控制权和飞书通知已恢复。")


async def local(config, args):
    client = Client(config)
    try:
        await client.connect()
        if args[0] == "attach":
            plain = "--plain" in args[1:]
            remaining = [a for a in args[1:] if a != "--plain"]
            if len(remaining) != 1:
                raise UserError("用法：attach [--plain] <任务编号>")
            await (attach if plain else native_attach)(client, remaining[0])
        else:
            if args[0] == "identities":
                print(render(await client.call({"op": "identities"})))
                return
            if args[0] == "create":
                try:
                    split = args.index("--")
                except ValueError:
                    raise UserError("create 必须用 -- 分隔选项和正文") from None
                line = shlex.join(args[:split]) + " -- " + " ".join(args[split + 1:])
            else:
                line = " ".join(args)
            print(render(await client.call(parse(line))))
    except (FileNotFoundError, ConnectionRefusedError):
        raise UserError("后台未运行；先执行 feishu-codex serve 或启动 systemd 用户服务") from None
    finally:
        await client.close()


def main():
    parser = argparse.ArgumentParser(description="飞书 + Codex 独立任务后台", epilog=HELP,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", default=str(config_path()))
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    try:
        config = load_config(args.config)
        command = args.command or ["help"]
        if command[0] == "help":
            print(HELP + "\n本机专用：serve | attach [--plain] <任务编号>（默认官方 Codex 界面） | doctor | identities（查看发送 /whoami 的用户 ID，不自动授权）")
        elif command[0] == "serve":
            from .daemon import run
            asyncio.run(run(config, args.config))
        elif command[0] == "_feishu":
            from .feishu import run
            run(config)
        elif command[0] == "doctor":
            import importlib.metadata
            import shutil
            import subprocess
            print(f"Python {sys.version.split()[0]} | lark-oapi {importlib.metadata.version('lark-oapi')}")
            binary = shutil.which(config.codex_command[0])
            print("Codex：" + (subprocess.run([binary, "--version"], capture_output=True, text=True).stdout.strip() if binary else "未找到"))
            print(f"配置：{args.config}；飞书{'已配置' if config.feishu_configured else '未配置'}；白名单 {len(config.feishu.get('allowed_users', []))} 人")
            print(f"工作根目录：{config.work_roots}；IPC：{config.socket_path}")
            asyncio.run(local(config, ["diagnose"]))
        else:
            asyncio.run(local(config, command))
    except KeyboardInterrupt:
        pass
    except (UserError, OSError, ValueError) as e:
        print(f"错误：{e}", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
