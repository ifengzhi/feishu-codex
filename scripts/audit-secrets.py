#!/usr/bin/env python3
"""Check project files and optional reachable Git history for credential material.

Only names/counts are printed. Never print matched content or secret values.
"""
import argparse
import re
import sqlite3
import subprocess
from pathlib import Path

from feishu_codex.config import Redactor, load_config

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--history", action="store_true", help="also inspect all reachable Git blobs and commit messages")
args = parser.parse_args()
cfg = load_config()
redact = Redactor(cfg)
files = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"], check=True, capture_output=True).stdout.split(b"\0")
failures = []
checked = 0


def inspect(content, location):
    kinds = []
    if any(secret.encode() in content for secret in redact.secrets):
        kinds.append("known credential")
    patterns = (
        rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----",
        rb"\bsk-[A-Za-z0-9_-]{20,}\b",
        rb"\bgh[pousr]_[A-Za-z0-9]{30,}\b",
        rb"\bAKIA[0-9A-Z]{16}\b",
        rb'''(?i)["']?(?:api[_-]?key|app[_-]?secret|access[_-]?token|password)["']?\s*[:=]\s*["']([A-Za-z0-9_./+=-]{16,})["']''',
    )
    for pattern in patterns:
        for match in re.finditer(pattern, content):
            candidate = match.group(1) if match.lastindex else match.group()
            if candidate.upper().startswith((b"FAKE_", b"TEST_", b"EXAMPLE_", b"YOUR_")):
                continue
            kinds.append("credential-like pattern (review required)")
            break
    if kinds:
        failures.append(f"{location}: {', '.join(sorted(set(kinds)))}")


for raw in files:
    if not raw:
        continue
    path = Path(raw.decode())
    if not path.is_file():
        continue
    content = path.read_bytes()
    checked += 1
    inspect(content, str(path))

history_blobs = 0
history_commits = 0
if args.history:
    objects = subprocess.run(["git", "rev-list", "--objects", "--all"], check=True, capture_output=True).stdout
    # Batch reads stay in memory; never print blob contents, commit messages, or values.
    ids = [line.split(b" ", 1)[0] for line in objects.splitlines()]
    batch = subprocess.run(["git", "cat-file", "--batch"], input=b"\n".join(ids) + b"\n",
                           check=True, capture_output=True).stdout
    offset = 0
    for oid in ids:
        end = batch.index(b"\n", offset)
        _, kind, size = batch[offset:end].split()
        start = end + 1
        content = batch[start:start + int(size)]
        offset = start + int(size) + 1
        if kind in (b"blob", b"commit"):
            inspect(content, f"Git {kind.decode()} {oid.decode()[:12]}")
            history_blobs += kind == b"blob"
            history_commits += kind == b"commit"
for path in Path(cfg.data_dir).glob("*.log*"):
    data = path.read_bytes()
    inspect(data, path.name)
database = Path(cfg.data_dir) / "tasks.sqlite3"
if database.exists():
    with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as conn:
        for line in conn.iterdump():
            before = len(failures)
            inspect(line.encode(), "runtime database")
            if len(failures) > before:
                break
if failures:
    print("Credential findings (values withheld):", ", ".join(failures))
    raise SystemExit(1)
print(f"Credential audit: {checked} tracked/untracked nonignored files, {history_blobs} historical blobs, "
      f"{history_commits} commits, plus runtime logs/database; no known credential or configured pattern matches")
