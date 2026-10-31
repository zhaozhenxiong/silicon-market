"""Local process controller for the allowlisted native mobile runner.

It never performs UI operations. Starting the fixed runner is an explicit local
API action; status only inspects that process and bounded local status/log files.
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit


class MobileRuntimeError(ValueError):
    pass


class MobileRuntime:
    WORKER_ID = "native-mobile-v1"
    PHASES = {"starting", "operator_paused", "waiting_permission", "waiting_device", "waiting_tasks", "recognizing", "navigating", "verifying_scope", "needs_ai_guidance", "discovering", "reading", "idle", "stopped", "failed"}
    TARGET_POLICY = "enabled_live_monitor_tasks_only"
    READY_CATALOG = {"ready", "partial", "complete"}
    MAX_STATE_BYTES = 65536
    MAX_LOG_BYTES = 12000

    def __init__(self, root: Path, catalog_provider, enabled=True, target_provider=None):
        self.root = Path(root).resolve()
        self.enabled = enabled
        self.data = self.root / "data"
        self.runner = self.root / "automation" / "native_mobile_runner.mjs"
        self.state_file = self.data / "native_mobile_worker_state.json"
        self.log_file = self.data / "native_mobile_worker.log"
        self.record_file = self.data / "native_mobile_runtime.json"
        self.catalog_provider = catalog_provider
        self.target_provider = target_provider
        self._process = None
        self._record = None
        self._lock = threading.RLock()

    def target_snapshot(self):
        # Never substitute historical inventory/catalogue for live monitor tasks.
        snapshot = self.target_provider() if self.target_provider else {}
        if not isinstance(snapshot, dict) or snapshot.get("policy") != self.TARGET_POLICY:
            return dict(targets=[], enabled_task_count=0, skipped_tasks=[], policy=self.TARGET_POLICY)
        return snapshot

    def eligible_catalogs(self, snapshot=None):
        rows = (snapshot if snapshot is not None else self.target_snapshot()).get("targets", [])
        return [dict(id=row["catalog_id"], model=row["model"], category=row["category"], query=row["query"], task_ids=row.get("task_ids", []))
                for row in rows if isinstance(row, dict) and type(row.get("catalog_id")) is int
                and row["catalog_id"] > 0 and row.get("enabled") is True
                and row.get("readable") is True and row.get("scope_verified") is True
                and row.get("status") in self.READY_CATALOG]

    @staticmethod
    def _navigation(state):
        nav = state.get("navigation")
        if not isinstance(nav, dict):
            return None
        result = {key: str(nav[key])[:80] for key in ("status", "screen_kind") if key in nav}
        request = nav.get("request")
        if isinstance(request, dict):
            safe = {key: str(request[key])[:1500] for key in ("request_id", "screen_hash", "reason", "created_at", "expires_at") if key in request}
            target = request.get("target", {})
            if isinstance(target, dict):
                safe["target"] = {key: target[key] for key in ("catalog_id", "model", "query", "task_ids") if key in target}
            shot = request.get("screenshot", {})
            if isinstance(shot, dict):
                safe["screenshot"] = {key: shot[key] for key in ("width", "height") if type(shot.get(key)) is int}
                safe["screenshot"]["filename"] = Path(str(shot.get("filename", ""))).name[:200]
            if isinstance(request.get("ocr"), list):
                safe["ocr"] = [{"text": str(line.get("text", ""))[:300]} for line in request["ocr"][:40] if isinstance(line, dict)]
            result["request"] = safe
        return result

    @staticmethod
    def validate_base_url(value):
        try:
            parsed = urlsplit(value)
            port = parsed.port
        except (TypeError, ValueError):
            raise MobileRuntimeError("无效的本机服务地址") from None
        if (parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or port is None
                or not 1 <= port <= 65535 or parsed.username or parsed.password
                or parsed.path not in {"", "/"} or parsed.query or parsed.fragment):
            raise MobileRuntimeError("采集端仅允许连接当前127.0.0.1服务端口")
        return f"http://127.0.0.1:{port}"

    def _node(self):
        found = shutil.which("node")
        return str(Path(found).resolve()) if found else None

    def _read_json(self, path):
        try:
            if path.is_symlink() or path.stat().st_size > self.MAX_STATE_BYTES:
                return {}
            obj = json.loads(path.read_text(encoding="utf-8"))
            return obj if isinstance(obj, dict) else {}
        except (OSError, ValueError, UnicodeError):
            return {}

    def _safe_paths(self):
        if self.data.is_symlink():
            raise MobileRuntimeError("运行资料目录不可使用符号链接")
        self.data.mkdir(parents=True, exist_ok=True, mode=0o700)
        if any(path.is_symlink() for path in (self.state_file, self.log_file, self.record_file)):
            raise MobileRuntimeError("运行状态或日志路径不可使用符号链接")
        if not self.runner.is_file() or self.runner.resolve().parent != (self.root / "automation").resolve():
            raise MobileRuntimeError("独立采集端脚本尚未就绪")

    def _valid_command(self, command):
        if not isinstance(command, list) or len(command) not in {7, 8} or not all(isinstance(x, str) for x in command):
            return False
        if command[:2] != [self._node(), str(self.runner)] or command[2] != "--base-url" or command[4:6] != ["--worker-id", self.WORKER_ID]:
            return False
        if len(command) == 7:
            if command[6] not in {"--monitored", "--discover"}:
                return False
        elif command[6] != "--catalog-id" or not command[7].isdigit() or int(command[7]) <= 0:
            return False
        try:
            return self.validate_base_url(command[3]) == command[3]
        except MobileRuntimeError:
            return False

    def _matching_process(self, record):
        pid = record.get("pid")
        if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 1 or not self._valid_command(record.get("command")):
            return False
        if self._process is not None and self._process.pid == pid:
            return self._process.poll() is None
        try:
            # Process identity is checked before adopting a PID after API restart.
            # A stale or edited status file cannot authorize killing an arbitrary PID.
            check = subprocess.run(["/bin/ps", "-p", str(pid), "-o", "command="], capture_output=True,
                                   text=True, timeout=2, check=False)
            return check.returncode == 0 and check.stdout.strip() == " ".join(record["command"])
        except (OSError, subprocess.SubprocessError):
            return False

    def _log_tail(self):
        try:
            if self.log_file.is_symlink():
                return []
            with self.log_file.open("rb") as handle:
                handle.seek(0, 2)
                handle.seek(max(0, handle.tell() - self.MAX_LOG_BYTES))
                return handle.read(self.MAX_LOG_BYTES).decode("utf-8", errors="replace").splitlines()[-15:]
        except OSError:
            return []

    def status(self):
        with self._lock:
            record = self._record or self._read_json(self.record_file)
            running = self._matching_process(record)
            state = self._read_json(self.state_file)
            fresh = False
            if running and state.get("pid") == record.get("pid"):
                try:
                    updated = datetime.fromisoformat(str(state.get("last_update", "")).replace("Z", "+00:00"))
                    age = (datetime.now(timezone.utc) - updated).total_seconds()
                    fresh = -5 <= age <= 90
                except (TypeError, ValueError):
                    pass
            phase = state.get("phase") if fresh and state.get("phase") in self.PHASES else "starting" if running else "stopped"
            exit_code = self._process.poll() if self._process is not None else None
            if not running and exit_code not in {None, 0}:
                phase = "failed"
            elif not running and state.get("pid") == record.get("pid") and state.get("phase") == "failed":
                phase = "failed"
            snapshot = self.target_snapshot()
            targets = snapshot.get("targets", [])
            catalogs = self.eligible_catalogs(snapshot)
            command = record.get("command")
            valid_command = self._valid_command(command)
            mode = "monitored" if not valid_command or command[-1] in {"--monitored", "--discover"} else "single"
            catalog_id = int(command[-1]) if valid_command and mode == "single" else None
            selected = next((item for item in catalogs if item["id"] == catalog_id), None)
            scope_label = "已启用监控任务" if mode == "monitored" else f"单型号：{selected['model']} (#{catalog_id})" if selected else f"单型号 #{catalog_id}"
            node, installed = self._node(), self.runner.is_file()
            helper_installed = (self.root / "output" / "bin" / "native-mobile").is_file()
            available = self.enabled and sys.platform == "darwin" and bool(node) and installed and helper_installed
            message = (str(state.get("message") or "独立采集端正在等待实际设备状态")[:1500] if fresh
                       else "进程仍在运行，尚未收到新鲜状态；不能据此认定设备已连接" if running
                       else "独立采集端已停止" if exit_code in {None, 0} else "采集端进程已退出，请查看日志")
            if not available:
                message = "测试实例不启动本机采集进程" if not self.enabled else "独立手机采集端需要macOS、本机Node、固定runner与native-mobile组件就绪"
            if not targets and not running and phase != "failed":
                phase, message = "waiting_tasks", "等待启用监控任务；当前没有可确定型号的已启用任务，不操作手机"
            return dict(available=available, helper_installed=helper_installed, runner_installed=installed, node_available=bool(node), running=running, pid=record.get("pid") if running else None,
                        worker_id=self.WORKER_ID, phase=phase, connected=bool(running and fresh
                        and phase in {"recognizing", "navigating", "verifying_scope", "discovering", "reading", "idle"} and state.get("connected") is True),
                        device_status=state.get("device_status") if fresh else "disconnected",
                        fresh=fresh, last_update=state.get("last_update") if fresh else None,
                        started_at=record.get("started_at"), mode=mode, scope_label=scope_label, catalog_id=catalog_id,
                        active_catalog=state.get("active_catalog") if fresh else None,
                        message=message, exit_code=exit_code, eligible_catalogs=catalogs,
                        monitored_targets=targets, target_count=len(targets), policy=self.TARGET_POLICY,
                        enabled_task_count=snapshot.get("enabled_task_count", 0), skipped_tasks=snapshot.get("skipped_tasks", []),
                        navigation=self._navigation(state) if fresh else None,
                        capabilities=dict(ocr="local_vision", visual_navigation=True, offline_guidance=True, multimodal_model_connected=False),
                        log_tail=self._log_tail())

    def start(self, base_url, payload=None):
        with self._lock:
            if not self.enabled:
                raise MobileRuntimeError("测试实例不启动本机采集进程")
            payload = {} if payload is None else payload
            if not isinstance(payload, dict) or set(payload) - {"catalog_id", "mode"}:
                raise MobileRuntimeError("只允许选择已启用监控任务，不接受命令、路径或服务地址")
            mode = payload.get("mode", "single" if "catalog_id" in payload else "monitored")
            if not isinstance(mode, str) or mode not in {"monitored", "single"}:
                raise MobileRuntimeError("采集模式只允许monitored或single；全目录采集已停用")
            if mode == "monitored" and "catalog_id" in payload:
                raise MobileRuntimeError("监控任务模式不可同时指定单型号")
            base_url = self.validate_base_url(base_url)
            snapshot = self.target_snapshot()
            if not snapshot.get("targets"):
                raise MobileRuntimeError("没有已启用且可确定型号的监控任务，请先启用明确型号任务")
            catalog_id = None
            if mode == "single":
                allowed = {item["id"] for item in self.eligible_catalogs(snapshot)}
                catalog_id = payload.get("catalog_id")
                if isinstance(catalog_id, bool) or not isinstance(catalog_id, int) or catalog_id not in allowed:
                    raise MobileRuntimeError("请先选择属于已启用监控任务、范围已核实且状态可采集的型号")
            current = self.status()
            if current["running"]:
                if current["mode"] != mode or current["catalog_id"] != catalog_id:
                    raise MobileRuntimeError("已有独立采集端运行；切换模式或型号前请先停止当前进程")
                return {**current, "ok": True, "message": "监控任务采集端已在运行" if mode == "monitored" else "该型号采集端已在运行"}
            if sys.platform != "darwin" or not self._node():
                raise MobileRuntimeError("独立手机采集端需要macOS与本机Node")
            self._safe_paths()
            if not (self.root / "output" / "bin" / "native-mobile").is_file():
                raise MobileRuntimeError("原生单窗口组件尚未构建，请先完成本机组件安装")
            command = [self._node(), str(self.runner), "--base-url", base_url,
                       "--worker-id", self.WORKER_ID]
            command += ["--monitored"] if mode == "monitored" else ["--catalog-id", str(catalog_id)]
            # Do not pass provider credentials or arbitrary caller-supplied environment.
            env = {key: os.environ[key] for key in ("PATH", "HOME", "TMPDIR", "LANG", "LC_ALL", "USER", "LOGNAME") if key in os.environ}
            try:
                with self.log_file.open("ab") as logfile:
                    process = subprocess.Popen(command, cwd=str(self.root), env=env, stdin=subprocess.DEVNULL,
                                               stdout=logfile, stderr=subprocess.STDOUT, start_new_session=True)
            except OSError:
                raise MobileRuntimeError("无法启动固定采集端，请检查本机Node与运行目录权限") from None
            self._process = process
            self._record = dict(pid=process.pid, command=command, mode=mode, catalog_id=catalog_id,
                                started_at=datetime.now(timezone.utc).isoformat())
            temp = self.record_file.with_suffix(".tmp")
            temp.write_text(json.dumps(self._record), encoding="utf-8")
            temp.replace(self.record_file)
            return {**self.status(), "ok": True, "message": "独立采集进程已启动，设备与权限就绪状态以实际心跳为准"}

    def stop(self, payload=None):
        with self._lock:
            if not self.enabled:
                raise MobileRuntimeError("测试实例不控制本机采集进程")
            if payload not in (None, {}):
                raise MobileRuntimeError("停止操作不接受PID、命令或路径")
            record = self._record or self._read_json(self.record_file)
            if self._matching_process(record):
                pid = record["pid"]
                try:
                    os.kill(pid, signal.SIGTERM)
                    if self._process is not None and self._process.pid == pid:
                        try:
                            self._process.wait(timeout=4)
                        except subprocess.TimeoutExpired:
                            if self._matching_process(record) and os.getpgid(pid) == pid:
                                os.killpg(pid, signal.SIGKILL)
                            self._process.wait(timeout=2)
                    else:
                        deadline = time.monotonic() + 4
                        while self._matching_process(record) and time.monotonic() < deadline:
                            time.sleep(.1)
                        if self._matching_process(record) and os.getpgid(pid) == pid:
                            os.killpg(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                except (OSError, subprocess.SubprocessError):
                    raise MobileRuntimeError("无法确认采集端已停止，请查看真实进程状态") from None
            result = self.status()
            result["ok"] = True
            result["message"] = "停止信号已发送，等待进程退出" if result["running"] else "独立采集端已停止，已核验读数与队列保留"
            return result
