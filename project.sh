#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
if [[ -x .venv/bin/python ]]; then
  exec .venv/bin/python -m app.project_cli "$@"
elif command -v python3 >/dev/null 2>&1; then
  exec python3 -m app.project_cli "$@"
else
  printf '%s\n' '{"ok":false,"error":"python_unavailable","message":"需要Python 3.11或更新版本；此入口不会安装依赖或触发手机。"}'
  exit 2
fi
