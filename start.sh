#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
PORT="${1:-8765}"
if ! [[ "$PORT" =~ ^[0-9]+$ ]] || (( PORT < 1024 || PORT > 65535 )); then
  echo '端口需为 1024 到 65535 之间的整数。' >&2
  exit 1
fi
if command -v uv >/dev/null 2>&1; then
  uv sync --locked
  echo "硅价已启动，请打开 http://127.0.0.1:$PORT （按 Ctrl+C 退出）"
  exec uv run uvicorn app.main:app --host 127.0.0.1 --port "$PORT" --workers 1
fi
if [ ! -x .venv/bin/python ]; then
  python3 -m venv .venv
fi
if ! .venv/bin/python -m pip --version >/dev/null 2>&1; then
  .venv/bin/python -m ensurepip --upgrade
fi
.venv/bin/python -m pip install -r requirements.txt
echo "硅价已启动，请打开 http://127.0.0.1:$PORT （按 Ctrl+C 退出）"
exec .venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port "$PORT" --workers 1
