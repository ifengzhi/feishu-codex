#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p .cache/protocol
codex --version
codex app-server generate-json-schema --experimental --out .cache/protocol
.venv/bin/pytest -q tests/test_schema.py
