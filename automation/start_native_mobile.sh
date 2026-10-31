#!/usr/bin/env bash
# Explicit local startup only. This script is not invoked during build/tests.
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p data
node automation/native_mobile_runner.mjs "$@" >>data/native_mobile_worker.log 2>&1 &
worker_pid=$!
printf '%s\n' "$worker_pid" > data/native_mobile_worker.pid
printf '独立手机worker进程已启动（PID %s）；设备连接和授权结果请查看实际状态。\n' "$worker_pid"
