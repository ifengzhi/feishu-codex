#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
project_dir="$PWD"
python3 -m venv .venv
.venv/bin/pip install -e '.[test]'
mkdir -p "$HOME/.config/feishu-codex" "$HOME/.config/systemd/user" "$HOME/.local/bin"
chmod 700 "$HOME/.config/feishu-codex"
if [[ ! -f "$HOME/.config/feishu-codex/config.toml" ]]; then
  install -m 600 config.example.toml "$HOME/.config/feishu-codex/config.toml"
  .venv/bin/python -c 'import pathlib,shutil; p=pathlib.Path.home()/".config/feishu-codex/config.toml"; p.write_text(p.read_text().replace("/home/zhi/.npm-global/bin/codex",shutil.which("codex") or "codex"))'
fi
ln -sfn "$project_dir/.venv/bin/feishu-codex" "$HOME/.local/bin/feishu-codex"
.venv/bin/python scripts/render-service.py
systemctl --user daemon-reload
printf '%s\n' '安装完成；原有配置未覆盖。启动：systemctl --user enable --now feishu-codex'
printf '%s\n' '当前终端启用命令：export PATH="$HOME/.local/bin:$PATH"'
printf '%s\n' '若新开的 zsh 仍找不到命令，请将上述 PATH 配置加入 ~/.zshrc；也可直接运行 ~/.local/bin/feishu-codex。'
