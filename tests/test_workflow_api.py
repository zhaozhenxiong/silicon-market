import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.mobile_runtime import MobileRuntimeError


@pytest.fixture
def api(tmp_path):
    app = create_app(tmp_path / "workflow.sqlite3")
    client = TestClient(app, client=("127.0.0.1", 31001))
    return app, client


def live_task(app, enabled=True):
    return app.state.store.save_task({"name": "流程验证任务", "keyword": "RTX 4090", "category": "gpu",
                                      "source": "live", "enabled": enabled, "interval_minutes": 120})


def test_listing_queue_is_persistent_idempotent_and_does_not_enable_tasks(api):
    app, client = api
    task = live_task(app)
    paused = live_task(app, enabled=False)
    first = client.post("/api/workflow/listing/queue", json={}).json()
    second = client.post("/api/workflow/listing/queue", json={"task_ids": [task["id"]]}).json()
    assert first["count"] == 1
    assert first["queued"][0]["listing_job_id"] == second["queued"][0]["listing_job_id"]
    assert first["queued"][0]["status"] == "queued"
    assert len(client.get("/api/workflow/listing/queue").json()["jobs"]) == 1
    assert client.post("/api/workflow/listing/queue", json={"task_ids": [paused["id"]]}).status_code == 422
    assert app.state.store.get_task(paused["id"])["enabled"] is False


@pytest.mark.parametrize("payload", [{"task_ids": [True]}, {"task_ids": [0]}, {"command": "anything"}])
def test_listing_queue_rejects_unbounded_or_arbitrary_controls(api, payload):
    _, client = api
    assert client.post("/api/workflow/listing/queue", json=payload).status_code == 422


def test_phone_pause_persists_and_prevents_automatic_reconnect(api):
    app, client = api
    result = client.post("/api/workflow/mobile/pause", json={"reason": "用户已取走手机"})
    assert result.status_code == 200 and result.json()["mobile"]["operator_paused"] is True
    assert client.get("/api/workflow/status").json()["mobile"]["pause_reason"] == "用户已取走手机"
    assert client.post("/api/mobile/heartbeat", json={"worker_id": "native-mobile-v1", "device_status": "ready"}).status_code == 409
    assert client.post("/api/mobile/claim", json={"worker_id": "native-mobile-v1"}).status_code == 409
    assert app.state.workflow.is_mobile_paused()


def test_explicit_phone_start_resumes_but_failed_start_keeps_pause(api):
    app, client = api
    class FakeRuntime:
        def start(self, *_):
            raise MobileRuntimeError("测试启动失败")
    app.state.mobile_runtime = FakeRuntime()
    app.state.workflow.pause_mobile("手机未归还")
    assert client.post("/api/mobile/worker/start", json={"mode": "all"}).status_code == 409
    assert app.state.workflow.mobile_pause()["pause_reason"] == "手机未归还"
    app.state.mobile_runtime.start = lambda *_: {"running": True}
    assert client.post("/api/mobile/worker/start", json={"mode": "all"}).status_code == 200
    assert not app.state.workflow.is_mobile_paused()


def test_real_review_material_roundtrip_is_only_a_suggestion(api):
    app, client = api
    app.state.store.ingest([{"item_id": "workflow-api-1", "title": "显卡型号待确认", "price": 500,
                            "category": "gpu", "evidence": "测试来源标题"}], "live")
    batch = client.post("/api/workflow/ai-review-batches", json={"limit": 40}).json()
    assert batch["item_count"] == 1 and batch["external_ai_called"] is False
    assert client.get(batch["download_url"]).json()["content_hash"] == batch["content_hash"]
    payload = batch["decision_template"]
    payload["reviewer"] = {"kind": "human", "name": "测试复核"}
    payload["decisions"][0].update(decision="needs_review", quote="型号待确认", confidence=.5,
                                      reason_codes=["model_unknown"], explanation="原文没有精确型号")
    result = client.post("/api/workflow/review-decisions", json=payload).json()
    assert result["imported"] == 1 and result["applied"] == 0 and result["raw_unchanged"] is True
    assert client.post("/api/workflow/review-decisions", json=payload).json()["existing"] == 1
    assert client.get("/api/workflow/status").json()["counts"]["review_awaiting_quality"] == 1
    saved = client.get(f"/api/workflow/review-decisions?batch_id={batch['id']}&limit=40").json()
    assert saved["total"] == 1 and saved["automatic_apply"] is False
    assert saved["decisions"][0]["quote"] == "型号待确认"
    assert saved["decisions"][0]["reason_codes"] == ["model_unknown"]
    assert saved["decisions"][0]["review_status"] == "suggested"


def test_workflow_mutations_are_local_json_only(api):
    app, client = api
    assert client.post("/api/workflow/listing/queue", json={}, headers={"Origin": "https://example.com"}).status_code == 403
    assert client.post("/api/workflow/listing/queue", content="{}", headers={"Content-Type": "text/plain"}).status_code in {415, 422}
    remote = TestClient(app, client=("198.51.100.1", 31002))
    assert remote.post("/api/workflow/ai-review-batches", json={}).status_code == 403


def test_exact_listing_job_ids_find_old_jobs_beyond_latest_hundred(api):
    app, client = api
    task = live_task(app)
    wanted = app.state.store.enqueue_listing(task)
    with app.state.store.db() as db:
        db.execute("UPDATE listing_jobs SET status='success' WHERE id=?", (wanted,))
    for _ in range(101):
        job = app.state.store.enqueue_listing(task)
        with app.state.store.db() as db:
            db.execute("UPDATE listing_jobs SET status='success' WHERE id=?", (job,))
    assert wanted not in {row["id"] for row in client.get("/api/workflow/listing/queue").json()["jobs"]}
    result = client.get("/api/workflow/listing/queue", params={"job_ids":f"{wanted},{job}"}).json()
    assert {row["id"] for row in result["jobs"]} == {wanted,job}


@pytest.mark.parametrize("job_ids", ["", "0", "-1", "1 OR 1=1", "1,,2", "True", "9223372036854775808", ",".join(str(n) for n in range(1,102))])
def test_exact_listing_job_filter_is_bounded_and_cannot_inject_sql(api,job_ids):
    _, client = api
    assert client.get("/api/workflow/listing/queue",params={"job_ids":job_ids}).status_code == 422


def test_http_audit_uses_this_service_store_and_never_changes_raw_data(api):
    app, client = api
    store = app.state.store
    store.ingest([dict(item_id="same-service-audit",title="R7 7800X3D+5070 12G 黑色海景房主机",price=10900)],"live")
    with store.db() as db:
        before = {table:[tuple(row) for row in db.execute(f"SELECT * FROM {table} ORDER BY rowid")]
                  for table in ("items","observations","tasks")}
    preview = client.post("/api/workflow/price-audit/preview",json={})
    assert preview.status_code == 200
    assert preview.json()["changes"][0]["item_id"] == "same-service-audit"
    result = client.post("/api/workflow/price-audit/apply",json={})
    assert result.status_code == 200 and result.json()["raw_unchanged"] is True
    assert client.post("/api/workflow/price-audit/state",json={}).json()["version"] == result.json()["version"]
    assert client.post("/api/workflow/price-audit/rollback",json={}).json()["version"] is None
    with store.db() as db:
        assert {table:[tuple(row) for row in db.execute(f"SELECT * FROM {table} ORDER BY rowid")]
                for table in before} == before


@pytest.mark.parametrize("route,payload", [("apply",{"path":"elsewhere.sqlite3"}), ("preview",{"cmd":"pwd"}),
    ("state",{"operation":"apply"}), ("apply?db=elsewhere.sqlite3",{}), ("arbitrary",{})])
def test_http_audit_refuses_paths_commands_extra_fields_and_operations(api,route,payload):
    _, client = api
    assert client.post("/api/workflow/price-audit/"+route,json=payload).status_code == 422


def test_http_audit_requires_loopback_same_origin_json(api):
    app, client = api
    assert client.post("/api/workflow/price-audit/apply",json={},headers={"Origin":"https://example.org"}).status_code == 403
    assert client.post("/api/workflow/price-audit/apply",content="{}",headers={"Content-Type":"text/plain"}).status_code in {415,422}
    remote = TestClient(app,client=("198.51.100.1",31002))
    assert remote.post("/api/workflow/price-audit/preview",json={}).status_code == 403
