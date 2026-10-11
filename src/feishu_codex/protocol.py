"""Line-delimited JSON-RPC, verified against codex-cli 0.160.0 schema."""
import asyncio
import json
import os
import signal
import uuid

from .config import Redactor


class RpcError(Exception):
    pass


class AppServer:
    def __init__(self, config, events):
        self.config = config
        self.redact = Redactor(config)
        self.events = events
        self.pending = {}
        self.proc = None
        self.reader_task = None
        self.stderr_task = None
        self.epoch = ""
        self.ready = False
        self.counter = 0
        self.initialize_result = None

    async def start(self):
        self.epoch = uuid.uuid4().hex
        # Do not pass Feishu credentials to Codex child tools.
        env = {k: v for k, v in os.environ.items() if not k.startswith("FEISHU_")}
        self.proc = await asyncio.create_subprocess_exec(
            *self.config.codex_command, stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            limit=8 * 1024 * 1024, start_new_session=True, env=env)
        self.reader_task = asyncio.create_task(self._read())
        self.stderr_task = asyncio.create_task(self._stderr())
        result = await self.call("initialize", {
            "clientInfo": {"name": "feishu_codex", "title": "Feishu Codex", "version": "0.1.0"},
            "capabilities": {"experimentalApi": True}})
        await self.send({"method": "initialized"})
        self.ready = True
        self.initialize_result = result
        return result

    async def send(self, msg):
        if not self.proc or self.proc.returncode is not None:
            raise RpcError("app-server 未连接")
        self.proc.stdin.write((json.dumps(msg, ensure_ascii=False) + "\n").encode())
        await self.proc.stdin.drain()

    async def call(self, method, params):
        self.counter += 1
        rid = self.counter
        future = asyncio.get_running_loop().create_future()
        self.pending[rid] = future
        try:
            await self.send({"id": rid, "method": method, "params": params})
            return await asyncio.wait_for(future, self.config.rpc_timeout)
        except TimeoutError:
            raise RpcError(f"{method} 响应超时，执行结果不确定；不会自动重发") from None
        finally:
            self.pending.pop(rid, None)

    async def reply(self, rid, result=None, error=None):
        await self.send({"id": rid, **({"error": error} if error else {"result": result})})

    async def _read(self):
        try:
            while line := await self.proc.stdout.readline():
                msg = json.loads(line)
                if "method" in msg:
                    await self.events.put((self.epoch, msg))
                elif (f := self.pending.get(msg.get("id"))) is not None and not f.done():
                    if "error" in msg:
                        # Do not include provider response bodies/credentials in errors.
                        f.set_exception(RpcError(f"app-server RPC 拒绝 code={msg['error'].get('code')}: "
                                                 + self.redact(str(msg['error'].get('message', '')))))
                    else:
                        f.set_result(msg.get("result", {}))
        except (OSError, ValueError, asyncio.LimitOverrunError):
            pass
        finally:
            self.ready = False
            for f in list(self.pending.values()):
                if not f.done():
                    f.set_exception(RpcError("app-server 断开；请求是否执行不确定"))
            await self.events.put((self.epoch, {"method": "_disconnected", "params": {}}))

    async def _stderr(self):
        # Drain but never persist raw stderr: upstream logs may contain provider URLs/tokens.
        while await self.proc.stderr.read(8192):
            pass

    async def stop(self):
        self.ready = False
        if self.proc:
            try:
                os.killpg(self.proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(self.proc.wait(), 5)
            except TimeoutError:
                os.killpg(self.proc.pid, signal.SIGKILL)
                await self.proc.wait()
            for task in (self.reader_task, self.stderr_task):
                if task:
                    task.cancel()
            await asyncio.gather(self.reader_task, self.stderr_task, return_exceptions=True)


def text_input(text):
    return [{"type": "text", "text": text, "text_elements": []}]
