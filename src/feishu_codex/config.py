import json
import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


class UserError(Exception):
    """Safe, actionable error for the caller."""


def expand(value):
    return Path(value).expanduser().absolute()


@dataclass
class Config:
    work_roots: list = field(default_factory=lambda: [str(Path.home() / "ws"), str(Path.home() / "remote-ws")])
    data_dir: str = str(Path.home() / ".local/share/feishu-codex")
    socket_path: str = ""
    codex_command: list = field(default_factory=lambda: ["codex", "app-server", "--stdio"])
    approval_policy: str = "on-request"
    sandbox: str = "workspace-write"
    rpc_timeout: float = 45
    turn_timeout: float = 7200
    approval_timeout: float = 1800
    interrupt_timeout: float = 15
    lease_timeout: float = 30
    progress_interval: float = 30
    notification_retries: int = 4
    log_max_bytes: int = 2097152
    log_backups: int = 3
    feishu: dict = field(default_factory=dict)

    def __post_init__(self):
        self.data_dir = str(expand(self.data_dir))
        self.socket_path = str(expand(self.socket_path or str(Path(self.data_dir) / "control.sock")))
        self.work_roots = [str(expand(p).resolve()) for p in self.work_roots]
        if not self.work_roots:
            raise UserError("work_roots 不能为空")
        if self.approval_policy not in ("on-request", "untrusted"):
            raise UserError("approval_policy 只允许 on-request / untrusted")
        if self.sandbox not in ("read-only", "workspace-write"):
            raise UserError("sandbox 只允许 read-only / workspace-write")
        if self.codex_command[1:] != ["app-server", "--stdio"]:
            raise UserError("codex_command 必须为 [codex可执行文件, app-server, --stdio]")
        for name in ("rpc_timeout", "turn_timeout", "approval_timeout", "interrupt_timeout", "lease_timeout",
                     "progress_interval", "notification_retries", "log_max_bytes", "log_backups"):
            if getattr(self, name) <= 0:
                raise UserError(f"{name} 必须大于零")

    @property
    def feishu_configured(self):
        return bool(self.feishu.get("app_id") and self.feishu.get("app_secret"))

    def workdir(self, value, create=False):
        if not value or not isinstance(value, str):
            raise UserError("必须指定 workdir")
        raw = Path(value).expanduser()
        if not raw.is_absolute():
            raw = Path(self.work_roots[0]) / raw
        path = raw.resolve()
        if not any(path.is_relative_to(Path(root)) for root in self.work_roots):
            raise UserError("workdir 超出配置的工作根目录（含符号链接目标）")
        if create:
            path.mkdir(parents=True, exist_ok=True)
        if not path.is_dir():
            raise UserError("workdir 不存在或不是目录；新目录需显式 --mkdir")
        # Recheck after creation, and again immediately before starting a turn.
        if path.resolve() != path:
            raise UserError("目录在检查期间发生变化")
        return str(path)


def config_path():
    return expand(os.environ.get("FEISHU_CODEX_CONFIG", "~/.config/feishu-codex/config.toml"))


def load_config(path=None):
    p = expand(path) if path else config_path()
    if not p.exists():
        return Config()
    obj = tomllib.loads(p.read_text())
    if obj.get("feishu", {}).get("app_secret") and p.stat().st_mode & 0o077:
        raise UserError("含凭据的配置文件必须 chmod 600")
    return Config(**obj)


class Redactor:
    def __init__(self, config):
        self.secrets = set()
        self.add(config.feishu.get("app_secret", ""))
        for k, v in os.environ.items():
            if re.search(r"KEY|SECRET|TOKEN|PASSWORD", k, re.I):
                self.add(v)
        # Read only for redaction. Never print or modify Codex authentication/config.
        base = expand(os.environ.get("CODEX_HOME", "~/.codex"))
        for name in ("auth.json", "config.toml"):
            try:
                data = (base / name).read_text()
                obj = json.loads(data) if name.endswith("json") else tomllib.loads(data)
                self._collect(obj)
            except (OSError, ValueError):
                pass

    def add(self, value):
        if isinstance(value, str) and len(value) >= 6:
            self.secrets.add(value)

    def _collect(self, obj):
        if isinstance(obj, dict):
            for k, v in obj.items():
                if re.search(r"key|secret|token|password|authorization", k, re.I):
                    self.add(v)
                self._collect(v)
        elif isinstance(obj, list):
            for v in obj:
                self._collect(v)

    def __call__(self, value):
        if isinstance(value, dict):
            return {k: self(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self(v) for v in value]
        if not isinstance(value, str):
            return value
        for secret in sorted(self.secrets, key=len, reverse=True):
            value = value.replace(secret, "[REDACTED]")
        value = re.sub(r"(?i)(bearer\s+)[\w.\-/+=]+", r"\1[REDACTED]", value)
        value = re.sub(r"\bsk-[A-Za-z0-9_-]{12,}", "[REDACTED]", value)
        return re.sub(r'(?i)((?:api[_-]?key|app[_-]?secret|password|access_token)\s*[=:]\s*)[^\s,;]+',
                      r'\1[REDACTED]', value)
