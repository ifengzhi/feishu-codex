#!/usr/bin/env python3
import os
import shutil
from pathlib import Path

project = Path(__file__).resolve().parents[1]
config = Path(os.environ.get("FEISHU_CODEX_CONFIG", "~/.config/feishu-codex/config.toml")).expanduser()
# systemd paths/Environment require quoting and literal percent escaping.
def quoted(value):
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%") + '"'

template = (project / "deploy/feishu-codex.service.in").read_text()
template = template.replace("WorkingDirectory=@PROJECT@", "WorkingDirectory=" + str(project).replace("%", "%%"))
template = template.replace("@PROJECT@/.venv/bin/feishu-codex", quoted(project / ".venv/bin/feishu-codex"))
template = template.replace("@CONFIG@", quoted(config))
codex_dir = str(Path(shutil.which("codex") or "/usr/local/bin/codex").parent)
stable_path = ":".join(dict.fromkeys([codex_dir, str(Path.home() / ".local/bin"),
                                     str(Path.home() / ".npm-global/bin"), str(Path.home() / ".local/go/bin"),
                                     "/usr/local/sbin", "/usr/local/bin", "/usr/sbin", "/usr/bin", "/sbin", "/bin", "/snap/bin"]))
template = template.replace("PATH=@PATH@", quoted("PATH=" + stable_path))
dest = Path.home() / ".config/systemd/user/feishu-codex.service"
dest.write_text(template)
dest.chmod(0o600)
print("systemd 用户服务已生成（不含凭据）")
