from __future__ import annotations

import json
import signal
import subprocess
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.mobile_runtime import MobileRuntime, MobileRuntimeError
import app.mobile_runtime as runtime_module


class FakeProcess:
    pid = 43210

    def __init__(self):
        self.returncode = None

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        self.returncode = 0
        return 0


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    (tmp_path / "automation").mkdir()
    (tmp_path / "automation" / "native_mobile_runner.mjs").write_text("// mocked; never executed")
    (tmp_path / "output" / "bin").mkdir(parents=True)
    (tmp_path / "output" / "bin" / "native-mobile").write_text("mocked; never executed")
    rows = [dict(id=4, model="i9-14900KF", category="cpu", query="14900kf", enabled=True,
                 status="partial", scope={"verified": True}),
            dict(id=5, model="unreviewed", category="gpu", query="test", enabled=True,
                 status="ready", scope={"verified": False}),
            dict(id=6, model="unsupported", category="phone", query="test", enabled=True,
                 status="unsupported", scope={"verified": True})]
    def targets():
        return dict(policy=MobileRuntime.TARGET_POLICY, enabled_task_count=len(rows), skipped_tasks=[],
                    targets=[{**row, "catalog_id": row["id"], "task_ids": [row["id"] + 100],
                              "scope_verified": row["scope"]["verified"],
                              "readable": row["scope"]["verified"] is True and row["status"] in MobileRuntime.READY_CATALOG}
                             for row in rows if row["enabled"] is True])
    result = MobileRuntime(tmp_path, lambda: {"items": rows}, target_provider=targets)
    monkeypatch.setattr(result, "_node", lambda: "/fixed/node")
    monkeypatch.setattr(runtime_module, "sys", SimpleNamespace(platform="darwin"))
    calls = []
    process = FakeProcess()

    def fake_popen(command, **kwargs):
        calls.append((command, kwargs))
        return process

    monkeypatch.setattr(runtime_module.subprocess, "Popen", fake_popen)
    # Defense in depth: a missed mock cannot send signals to any real process.
    signals = []
    monkeypatch.setattr(runtime_module.os, "kill", lambda pid, sig: signals.append((pid, sig)))
    monkeypatch.setattr(runtime_module.os, "killpg", lambda pid, sig: signals.append((pid, sig)))
    result.test_rows, result.test_calls, result.test_process, result.test_signals = rows, calls, process, signals
    return result


def test_fixed_command_dynamic_catalog_and_secret_free_environment(runtime, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "not-forwarded-test-value")
    result = runtime.start("http://127.0.0.1:8765", {"catalog_id": 4})
    assert result["running"] and not result["connected"]
    assert len(runtime.test_calls) == 1
    command, kwargs = runtime.test_calls[0]
    assert command == ["/fixed/node", str(runtime.runner), "--base-url", "http://127.0.0.1:8765",
                       "--worker-id", "native-mobile-v1", "--catalog-id", "4"]
    assert kwargs["start_new_session"] is True
    assert kwargs["cwd"] == str(runtime.root)
    assert "shell" not in kwargs and "OPENAI_API_KEY" not in kwargs["env"]
    assert runtime.start("http://127.0.0.1:8765", {"catalog_id": 4})["running"]
    assert len(runtime.test_calls) == 1
    runtime.test_rows[1]["scope"]["verified"] = True
    assert [row["id"] for row in runtime.eligible_catalogs()] == [4, 5]
    with pytest.raises(MobileRuntimeError, match="切换模式或型号"):
        runtime.start("http://127.0.0.1:8765", {"catalog_id": 5})


@pytest.mark.parametrize("payload", [{"cmd": "echo unsafe"}, {"path": "/tmp/script"}, {"base_url": "https://example.com"},
                                     {"catalog_id": 5}, {"catalog_id": 6}, {"catalog_id": True}, {"catalog_id": "4"},
                                     {"catalog_id": -1}, {"catalog_id": 4, "env": {}}, []])
def test_invalid_start_payload_cannot_launch_any_process(runtime, payload):
    with pytest.raises(MobileRuntimeError):
        runtime.start("http://127.0.0.1:8765", payload)
    assert runtime.test_calls == []


@pytest.mark.parametrize("url", ["http://example.com:8765", "http://localhost:8765", "https://127.0.0.1:8765",
                                 "http://127.0.0.1", "http://u:p@127.0.0.1:8765", "http://127.0.0.1:8765/other",
                                 "http://127.0.0.1:8765?cmd=x", "http://127.0.0.1:99999"])
def test_external_or_ambiguous_server_url_rejected(runtime, url):
    with pytest.raises(MobileRuntimeError):
        runtime.start(url, {"catalog_id": 4})
    assert runtime.test_calls == []


def write_state(runtime, **changes):
    state = dict(pid=runtime.test_process.pid, worker_id="native-mobile-v1", phase="reading",
                 connected=True, device_status="reading", active_catalog=4,
                 last_update=datetime.now(timezone.utc).isoformat(), message="actual fresh state")
    state.update(changes)
    runtime.state_file.write_text(json.dumps(state))


def test_fresh_state_requires_matching_live_pid_and_permissions_are_real_wait(runtime):
    runtime.start("http://127.0.0.1:8765", {"catalog_id": 4})
    write_state(runtime)
    assert runtime.status()["connected"] is True
    write_state(runtime, phase="waiting_permission", device_status="waiting_user", connected=True,
                message="需要辅助功能与屏幕录制权限")
    status = runtime.status()
    assert status["running"] and not status["connected"]
    assert status["phase"] == "waiting_permission" and "权限" in status["message"]
    write_state(runtime, last_update=(datetime.now(timezone.utc)-timedelta(seconds=100)).isoformat())
    assert runtime.status()["fresh"] is False and runtime.status()["connected"] is False
    write_state(runtime, pid=1)
    assert runtime.status()["fresh"] is False
    runtime.test_process.returncode = 2
    write_state(runtime)
    assert runtime.status()["running"] is False and runtime.status()["connected"] is False
    assert runtime.status()["phase"] == "failed"


def test_stop_never_accepts_arbitrary_pid_and_preserves_queue(runtime):
    runtime.start("http://127.0.0.1:8765", {"catalog_id": 4})
    with pytest.raises(MobileRuntimeError):
        runtime.stop({"pid": 1})
    assert not runtime.test_signals
    status = runtime.stop({})
    assert runtime.test_signals == [(runtime.test_process.pid, signal.SIGTERM)]
    assert not status["running"] and "队列保留" in status["message"]
    assert runtime.stop({})["running"] is False


def test_restart_adoption_checks_exact_fixed_command_before_signaling(runtime, monkeypatch):
    runtime.start("http://127.0.0.1:8765", {"catalog_id": 4})
    command = runtime._record["command"]
    runtime._process = None
    monkeypatch.setattr(runtime_module.subprocess, "run", lambda *a, **kw: SimpleNamespace(returncode=0, stdout="/unrelated/process"))
    assert not runtime.status()["running"]
    assert not runtime.stop({})["running"]
    assert not runtime.test_signals
    monkeypatch.setattr(runtime_module.subprocess, "run", lambda *a, **kw: SimpleNamespace(returncode=0, stdout=" ".join(command)))
    assert runtime.status()["running"] is True
    runtime._record["command"][1] = "/tmp/other.mjs"
    assert runtime.status()["running"] is False


def test_symbolic_runtime_paths_and_test_mode_cannot_start(runtime):
    runtime.data.mkdir(exist_ok=True)
    other = runtime.root / "outside.log"
    other.write_text("outside")
    runtime.log_file.symlink_to(other)
    with pytest.raises(MobileRuntimeError, match="符号链接"):
        runtime.start("http://127.0.0.1:8765")
    assert not runtime.test_calls
    runtime.enabled = False
    with pytest.raises(MobileRuntimeError, match="测试实例"):
        runtime.start("http://127.0.0.1:8765")
    with pytest.raises(MobileRuntimeError, match="测试实例"):
        runtime.stop({})


def test_bounded_log_tail_and_corrupt_state(runtime):
    runtime.data.mkdir()
    runtime.log_file.write_text("x"*20000+"\nlast line")
    runtime.state_file.write_text("invalid json")
    status = runtime.status()
    assert not status["running"] and not status["connected"]
    assert sum(len(line) for line in status["log_tail"]) <= runtime.MAX_LOG_BYTES
    assert status["log_tail"][-1] == "last line"


def test_http_runtime_is_loopback_same_origin_json_only(tmp_path, runtime):
    app = create_app(tmp_path / "api.sqlite3")
    app.state.mobile_runtime = runtime
    with TestClient(app, base_url="http://127.0.0.1:8765", client=("127.0.0.1", 50000)) as client:
        assert client.get("/api/mobile/worker/status").status_code == 200
        assert client.post("/api/mobile/worker/start", json={}, headers={"Origin": "https://foreign.test"}).status_code == 403
        assert client.post("/api/mobile/worker/start", json={}, headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
        assert client.post("/api/mobile/worker/start", data="").status_code == 415
        assert client.post("/api/mobile/worker/start", json={"cmd": "unsafe"}).status_code == 409
        assert not runtime.test_calls
        response = client.post("/api/mobile/worker/start", json={"catalog_id": 4}, headers={"Origin": "http://127.0.0.1:8765"})
        assert response.status_code == 200 and response.json()["running"]
        assert client.post("/api/mobile/worker/stop", json={}).status_code == 200
    with TestClient(app, base_url="http://127.0.0.1:8765", client=("192.0.2.1", 50000)) as client:
        assert client.get("/api/mobile/worker/status").status_code == 403
        assert client.post("/api/mobile/worker/start", json={}).status_code == 403


def test_http_start_cannot_compete_with_another_fresh_worker(tmp_path, runtime):
    app = create_app(tmp_path / "other-worker.sqlite3")
    app.state.mobile_runtime = runtime
    with TestClient(app, base_url="http://127.0.0.1:8765", client=("127.0.0.1", 50000)) as client:
        response = client.post("/api/mobile/heartbeat", json={"worker_id": "existing-cua-worker", "device_status": "ready", "message": "test heartbeat"})
        assert response.status_code == 200
        response = client.post("/api/mobile/worker/start", json={"catalog_id": 4})
        assert response.status_code == 409 and "其他采集端" in response.json()["detail"]
        assert runtime.test_calls == []


def test_missing_helper_and_non_boolean_scope_do_not_launch(runtime):
    runtime.test_rows[0]["scope"]["verified"] = "true"
    assert runtime.eligible_catalogs() == []
    with pytest.raises(MobileRuntimeError):
        runtime.start("http://127.0.0.1:8765", {"catalog_id": 4})
    runtime.test_rows[0]["scope"]["verified"] = True
    (runtime.root / "output" / "bin" / "native-mobile").unlink()
    assert runtime.status()["available"] is False
    with pytest.raises(MobileRuntimeError, match="尚未构建"):
        runtime.start("http://127.0.0.1:8765", {"catalog_id": 4})
    assert runtime.test_calls == []


def test_monitored_mode_uses_only_task_template_and_can_verify_unknown_scope(runtime):
    runtime.test_rows[:] = [runtime.test_rows[1]]
    result = runtime.start("http://127.0.0.1:8765", {"mode": "monitored"})
    command = runtime.test_calls[0][0]
    assert command == ["/fixed/node", str(runtime.runner), "--base-url", "http://127.0.0.1:8765",
                       "--worker-id", "native-mobile-v1", "--monitored"]
    assert runtime._valid_command(command)
    assert result["running"] and result["mode"] == "monitored" and result["catalog_id"] is None
    assert result["scope_label"] == "已启用监控任务"
    assert result["eligible_catalogs"] == []
    assert runtime.start("http://127.0.0.1:8765", {"mode": "monitored"})["mode"] == "monitored"
    assert len(runtime.test_calls) == 1
    # Serialized metadata cannot relabel the actual command's mode.
    runtime._record["mode"] = "single"
    assert runtime.status()["mode"] == "monitored"


@pytest.mark.parametrize("payload", [{"mode": "all", "catalog_id": 4}, {"mode": "all", "catalog_id": None},
                                     {"mode": "all", "args": ["--once"]}, {"mode": "all", "cmd": "node other"},
                                     {"mode": "discover"}, {"mode": True}, {"mode": []}, {"mode": {}}])
def test_all_mode_rejects_ambiguous_or_arbitrary_payloads(runtime, payload):
    with pytest.raises(MobileRuntimeError):
        runtime.start("http://127.0.0.1:8765", payload)
    assert not runtime.test_calls


def test_command_allowlist_has_exact_monitored_single_and_legacy_stop_templates(runtime):
    prefix = ["/fixed/node", str(runtime.runner), "--base-url", "http://127.0.0.1:8765", "--worker-id", "native-mobile-v1"]
    assert runtime._valid_command(prefix + ["--discover"])
    assert runtime._valid_command(prefix + ["--monitored"])
    assert runtime._valid_command(prefix + ["--catalog-id", "4"])
    for tail in (["--discover", "true"], ["--discover", "--once"], ["--catalog-id", "4", "--discover"],
                 ["--discover=1"], [], ["--catalog-id", "0"], ["--catalog-id", "-4"]):
        assert not runtime._valid_command(prefix + tail)
    runtime.start("http://127.0.0.1:8765", {"catalog_id": 4})
    with pytest.raises(MobileRuntimeError, match="切换模式"):
        runtime.start("http://127.0.0.1:8765", {"mode": "monitored"})
    assert len(runtime.test_calls) == 1


def test_http_monitored_mode_keeps_origin_guards_and_does_not_require_preverified_scope(tmp_path, runtime):
    app = create_app(tmp_path / "all-mode.sqlite3")
    app.state.mobile_runtime = runtime
    runtime.test_rows[:] = [runtime.test_rows[1]]
    with TestClient(app, base_url="http://127.0.0.1:8765", client=("127.0.0.1", 50000)) as client:
        response = client.post("/api/mobile/worker/start", json={"mode": "monitored"}, headers={"Origin": "https://foreign.test"})
        assert response.status_code == 403 and not runtime.test_calls
        response = client.post("/api/mobile/worker/start", json={"mode": "monitored"}, headers={"Origin": "http://127.0.0.1:8765"})
        assert response.status_code == 200 and response.json()["mode"] == "monitored"
        assert runtime.test_calls[0][0][-1] == "--monitored"
        assert client.post("/api/mobile/worker/stop", json={}).status_code == 200


def test_zero_targets_never_launches_and_catalog_is_not_a_fallback(runtime):
    runtime.test_rows.clear()
    with pytest.raises(MobileRuntimeError, match="没有已启用"):
        runtime.start("http://127.0.0.1:8765")
    assert not runtime.test_calls
    assert runtime.status()["phase"] == "waiting_tasks"
    runtime.target_provider = None
    runtime.catalog_provider = lambda: {"items": [dict(id=4, enabled=True)]}
    assert runtime.eligible_catalogs() == []


def test_targets_revoke_single_choice_and_unknown_policy_fails_closed(runtime):
    runtime.test_rows[0]["enabled"] = False
    with pytest.raises(MobileRuntimeError):
        runtime.start("http://127.0.0.1:8765", {"catalog_id": 4})
    runtime.target_provider = lambda: dict(policy="all_inventory", targets=[{"catalog_id": 4}])
    with pytest.raises(MobileRuntimeError, match="没有已启用"):
        runtime.start("http://127.0.0.1:8765")
    assert not runtime.test_calls


def test_navigation_status_is_bounded_and_does_not_imply_model_connection(runtime):
    runtime.start("http://127.0.0.1:8765", {"catalog_id": 4})
    write_state(runtime, phase="needs_ai_guidance", navigation={"status": "needs_ai_guidance", "screen_kind": "unknown",
                "request": {"request_id": "test", "reason": "x" * 2000, "screenshot": {"filename": "/private/path/test.png"},
                            "ocr": [{"text": "x" * 500}] * 50, "raw_image": "never-expose"}})
    status = runtime.status()
    assert status["phase"] == "needs_ai_guidance" and not status["connected"]
    assert status["capabilities"]["multimodal_model_connected"] is False
    request = status["navigation"]["request"]
    assert request["screenshot"]["filename"] == "test.png" and "raw_image" not in request
    assert len(request["reason"]) == 1500 and len(request["ocr"]) == 40


def test_operator_pause_and_failed_exit_are_not_hidden_by_no_targets(runtime):
    runtime.start("http://127.0.0.1:8765")
    write_state(runtime, phase="operator_paused", device_status="waiting_user", connected=False)
    assert runtime.status()["phase"] == "operator_paused"
    runtime.test_rows.clear()
    runtime.test_process.returncode = 2
    assert runtime.status()["phase"] == "failed"


def test_single_mode_requires_explicit_target(runtime):
    with pytest.raises(MobileRuntimeError):
        runtime.start("http://127.0.0.1:8765", {"mode": "single"})
    assert not runtime.test_calls


def test_http_stopped_worker_reports_explicit_phone_pause(tmp_path, runtime):
    app = create_app(tmp_path / "phone-pause.sqlite3")
    app.state.mobile_runtime = runtime
    app.state.workflow.pause_mobile("用户已拿走手机；仅处理已存截图")
    with TestClient(app, base_url="http://127.0.0.1:8765", client=("127.0.0.1", 50000)) as client:
        status = client.get("/api/mobile/worker/status").json()
        assert status["operator_paused"] and status["phase"] == "operator_paused"
        assert not status["running"] and not status["connected"] and "已存截图" in status["message"]
        assert not runtime.test_calls
