#!/usr/bin/env python3
"""Check tracked project files and application data against locally known credentials.

Only names/counts are printed. Never print matched content or secret values.
"""
import sqlite3
import subprocess
from pathlib import Path

from feishu_codex.config import Redactor, load_config

cfg = load_config()
redact = Redactor(cfg)
files = subprocess.run(["git", "ls-files", "-z"], check=True, capture_output=True).stdout.split(b"\0")
failures = []
checked = 0
for raw in files:
    if not raw:
        continue
    path = Path(raw.decode())
    content = path.read_bytes()
    checked += 1
    if any(secret.encode() in content for secret in redact.secrets):
        failures.append(str(path))
for path in Path(cfg.data_dir).glob("*.log*"):
    data = path.read_bytes()
    if any(secret.encode() in data for secret in redact.secrets):
        failures.append(path.name)
database = Path(cfg.data_dir) / "tasks.sqlite3"
if database.exists():
    with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as conn:
        for line in conn.iterdump():
            if any(secret in line for secret in redact.secrets):
                failures.append("runtime database")
                break
if failures:
    print("Credential matches in files (values withheld):", ", ".join(failures))
    raise SystemExit(1)
print(f"Credential audit: {checked} tracked files plus runtime logs/database, no known credential matches")
