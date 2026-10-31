import json
import sqlite3
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from app.catalog import Catalog
from app.jobs import LeaseError, MobileJobs
from app.main import create_app
from app.normalization import SHANGHAI, ValidationError, iso, utcnow
from app.official_market import OfficialMarket
from app.service import Store


@pytest.fixture
def system(tmp_path):
    store = Store(tmp_path / "market.sqlite3", production=True)
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    (evidence / "read.jpg").write_bytes(b"\xff\xd8\xff\xe0screen evidence")
    official = OfficialMarket(store, evidence)
    catalog = Catalog(store)
    jobs = MobileJobs(store, catalog, official)
    catalog.sync()
    phone = next(item for item in catalog.list()["items"] if item["category"] == "phone")
    return store, catalog, jobs, phone


def begin(system, days=3):
    store, catalog, jobs, phone = system
    current = catalog.get(phone["id"])
    if current["scope"].get("verified") is not True:
        catalog.patch(phone["id"], {"scope": {**current["scope"], "verified": True}})
    batch = jobs.create_batch({"catalog_ids": [phone["id"]], "days": days})
    jobs.heartbeat({"worker_id": "real-worker", "lease_token": None, "device_status": "ready"})
    lease = jobs.claim({"worker_id": "real-worker", "catalog_id": phone["id"]})
    return batch, lease


def reading(job, **overrides):
    return dict(job_id=job["id"], displayed_date=job["target_date"], price=3000,
                evidence_filename="read.jpg", observed_model=job["model"], observed_query=job["query"],
                scope={**job["scope"], "verified": True}, read_method="mobile_screen_ocr",
                verification_count=2, ocr_text=f"{job['target_date']} 近7日成交均价 ¥3000", parser_version="test-1", **overrides)


def submit(jobs, lease, records):
    return jobs.submit({"worker_id": "real-worker", "lease_token": lease["lease_token"], "readings": records})


def test_catalog_is_real_only_idempotent_preserves_settings_and_scope_version(system):
    store, catalog, jobs, phone = system
    first = catalog.sync()
    assert first["added"] == 0
    updated = catalog.patch(phone["id"], {"enabled": False, "query": "苹果15 256GB", "status": "needs_review"})
    assert updated["version"] == phone["version"]+1
    catalog.sync()
    assert not catalog.get(phone["id"])["enabled"]
    with pytest.raises(ValidationError, match="宽容量"):
        catalog.patch(phone["id"], {"scope": {**phone["scope"], "storage": None}})
    with store.db() as db:
        assert db.execute("SELECT count(*) FROM items WHERE source='demo'").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM catalog_revisions WHERE catalog_id=?", (phone["id"],)).fetchone()[0] == 2


def test_changed_scope_cannot_inherit_old_batch_complete_coverage(system):
    store, catalog, jobs, phone = system
    _, lease = begin(system, days=1)
    submit(jobs, lease, [reading(lease["jobs"][0])])
    current = catalog.get(phone["id"])
    assert next(i for i in catalog.list()["items"] if i["id"] == phone["id"])["status"] == "complete"
    catalog.patch(phone["id"], {"scope": {**current["scope"], "condition": "99新", "verified": False}, "status": "needs_review"})
    updated = next(i for i in catalog.list()["items"] if i["id"] == phone["id"])
    assert updated["market_scope_id"] != current["market_scope_id"]
    assert updated["status"] == "needs_review"
    assert updated["target_days"] == updated["verified_days"] == 0


@pytest.mark.parametrize("status", ["needs_review", "no_market", "unsupported"])
def test_operator_review_status_is_not_overwritten_by_old_success(system, status):
    store, catalog, jobs, phone = system
    _, lease = begin(system, days=1)
    submit(jobs, lease, [reading(lease["jobs"][0])])
    catalog.patch(phone["id"], {"status": status, "last_error": "需核对型号实际范围"})
    current = next(i for i in catalog.list()["items"] if i["id"] == phone["id"])
    assert current["status"] == status
    assert current["verified_days"] == 1  # historical proof retained, eligibility revoked


def test_scope_verification_cannot_be_a_truthy_string(system):
    store, catalog, jobs, phone = system
    with pytest.raises(ValidationError, match="布尔"):
        catalog.patch(phone["id"], {"scope": {**phone["scope"], "verified": "true"}})


def test_180_date_jobs_fixed_yesterday_and_idempotent_batch(system):
    store, catalog, jobs, phone = system
    batch = jobs.create_batch({"catalog_ids": [phone["id"]], "days": 180})
    again = jobs.create_batch({"catalog_ids": [phone["id"]], "days": 180})
    assert batch["id"] == again["id"]
    records = jobs.jobs(batch_id=batch["id"], limit=1000)["jobs"]
    yesterday = utcnow().astimezone(SHANGHAI).date()-timedelta(days=1)
    assert len(records) == 180 and len({j["target_date"] for j in records}) == 180
    assert max(j["target_date"] for j in records) == yesterday.isoformat()
    assert batch["coverage_pct"] == 0 and batch["status"] != "complete"
    with pytest.raises(ValidationError):
        jobs.create_batch({"catalog_ids": [phone["id"]], "end_date": utcnow().astimezone(SHANGHAI).date().isoformat()})


def test_mobile_chart_retains_first_of_180_complete_days(system):
    _, _, jobs, _ = system
    _, lease = begin(system, days=180)
    first = lease["jobs"][0]
    submit(jobs, lease, [reading(first)])
    expected = (utcnow().astimezone(SHANGHAI).date()-timedelta(days=180)).isoformat()
    assert first["target_date"] == expected
    assert jobs.official_series(days=180)[0]["points"] == [{"date": expected, "price": 3000.0}]


def test_catalog_review_defers_unread_dates_and_approval_only_resumes_scope_waits(system):
    store, catalog, jobs, phone = system
    _, lease = begin(system, days=3)
    first, second, third = lease["jobs"]
    submit(jobs, lease, [reading(first)])
    catalog.patch(phone["id"], {"status": "needs_review", "last_error": "App页面只显示宽容量，未核实规格"})
    jobs.sync_catalog_state(phone["id"])
    rows = jobs.jobs(catalog_id=phone["id"])["jobs"]
    assert [row["status"] for row in rows] == ["verified", "needs_review", "needs_review"]
    assert "宽容量" in rows[1]["error"]
    with store.db() as db:
        db.execute("UPDATE mobile_jobs SET error='同日读数冲突，保留证据' WHERE id=?", (third["id"],))
    catalog.patch(phone["id"], {"status": "ready"})
    jobs.sync_catalog_state(phone["id"])
    rows = jobs.jobs(catalog_id=phone["id"])["jobs"]
    assert [row["status"] for row in rows] == ["verified", "pending", "needs_review"]
    assert rows[1]["attempts"] == second["attempts"]
    assert rows[0]["price"] == 3000


def test_new_batch_for_review_scope_has_explicit_date_reasons(system):
    _, catalog, jobs, phone = system
    catalog.patch(phone["id"], {"status": "needs_review", "last_error": "没有可核实容量筛选"})
    batch = jobs.create_batch({"catalog_ids": [phone["id"]], "days": 3})
    rows = jobs.jobs(batch_id=batch["id"])["jobs"]
    assert batch["review_days"] == 3 and batch["pending_days"] == 0
    assert all(row["status"] == "needs_review" and "容量筛选" in row["error"] for row in rows)
    assert all(row["price"] is None for row in rows)
    assert jobs.retry(batch["id"])["retried"] == 0  # retry cannot approve an unknown scope
    assert jobs.jobs(batch_id=batch["id"])["jobs"][0]["error"] == rows[0]["error"]


def test_single_device_requires_real_heartbeat_and_blocks_second_worker(system):
    store, catalog, jobs, phone = system
    jobs.create_batch({"catalog_ids": [phone["id"]], "days": 2})
    assert not jobs.status()["connected"]
    with pytest.raises(LeaseError):
        jobs.claim({"worker_id": "real-worker"})
    catalog.patch(phone["id"], {"scope": {**phone["scope"], "verified": True}})
    jobs.heartbeat({"worker_id": "real-worker", "device_status": "ready"})
    lease = jobs.claim({"worker_id": "real-worker"})
    with pytest.raises(LeaseError):
        jobs.heartbeat({"worker_id": "other", "device_status": "ready"})
    with pytest.raises(LeaseError):
        jobs.claim({"worker_id": "other"})
    assert jobs.status()["active_catalog"] == lease["catalog"]["id"]


def test_lock_preserves_jobs_and_unlock_reclaims_without_fake_coverage(system):
    store, catalog, jobs, phone = system
    batch, lease = begin(system)
    jobs.heartbeat({"worker_id": "real-worker", "lease_token": lease["lease_token"], "device_status": "locked", "message": "等待本人Touch ID"})
    assert jobs.status()["device_status"] == "locked"
    assert all(job["status"] == "waiting_device" for job in jobs.jobs()["jobs"])
    with pytest.raises(LeaseError):
        submit(jobs, lease, [reading(lease["jobs"][0])])
    jobs.heartbeat({"worker_id": "real-worker", "device_status": "ready"})
    resumed = jobs.claim({"worker_id": "real-worker"})
    assert {j["id"] for j in resumed["jobs"]} == {j["id"] for j in lease["jobs"]}
    assert all(j["attempts"] == 2 for j in resumed["jobs"])
    assert jobs.batches()["batches"][0]["verified_days"] == 0


def test_expired_lease_survives_new_service_and_can_be_reclaimed(system):
    store, catalog, jobs, phone = system
    batch, lease = begin(system)
    expired = iso(utcnow()-timedelta(seconds=10))
    with store.db() as db:
        db.execute("UPDATE mobile_device SET lease_expires_at=?", (expired,))
        db.execute("UPDATE mobile_jobs SET lease_expires_at=? WHERE status='reading'", (expired,))
    restarted = MobileJobs(store, Catalog(store), jobs.official)
    assert not restarted.status()["connected"]
    restarted.heartbeat({"worker_id": "real-worker", "device_status": "ready"})
    resumed = restarted.claim({"worker_id": "real-worker"})
    assert len(resumed["jobs"]) == 3
    assert resumed["lease_token"] != lease["lease_token"]


@pytest.mark.parametrize("bad", ["date", "model", "scope", "verification", "price", "evidence"])
def test_readings_validate_complete_batch_before_any_write(system, bad):
    store, catalog, jobs, phone = system
    batch, lease = begin(system)
    first, second = reading(lease["jobs"][0]), reading(lease["jobs"][1])
    if bad == "date":
        second["displayed_date"] = lease["jobs"][0]["target_date"]
    elif bad == "model":
        second["observed_model"] = "iPhone 15 Pro 256GB"
    elif bad == "scope":
        second["scope"]["storage"] = None
    elif bad == "verification":
        second["verification_count"] = 1
    elif bad == "price":
        second["price"] = float("nan")
    else:
        second["evidence_filename"] = "../session_meta.json"
    with pytest.raises(ValidationError):
        submit(jobs, lease, [first, second])
    with store.db() as db:
        assert db.execute("SELECT count(*) FROM mobile_readings").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM mobile_points").fetchone()[0] == 0


def test_verified_points_reuse_across_batches_evidence_hash_and_completion(system):
    store, catalog, jobs, phone = system
    batch, lease = begin(system)
    result = submit(jobs, lease, [reading(job) for job in lease["jobs"]])
    assert result == {"ok": True, "accepted": 3, "conflicts": 0}
    summary = jobs.batches()["batches"][0]
    assert summary["status"] == "complete" and summary["coverage_pct"] == 100
    longer = jobs.create_batch({"catalog_ids": [phone["id"]], "days": 4})
    assert longer["existing_verified"] == 3 and longer["coverage_pct"] == 75
    record = jobs.jobs(batch_id=batch["id"])["jobs"][0]
    assert jobs.evidence(record["reading_id"]).is_file()
    (jobs.official.evidence_dir / "read.jpg").write_bytes(b"\xff\xd8\xff\xe0altered")
    with pytest.raises(ValidationError, match="哈希"):
        jobs.evidence(record["reading_id"])
    assert jobs.official_series()[0]["read_method"] == "mobile_screen_ocr"


def test_conflicting_read_retains_both_evidence_without_overwriting_verified_point(system):
    store, catalog, jobs, phone = system
    batch, lease = begin(system, days=1)
    job = lease["jobs"][0]
    submit(jobs, lease, [reading(job)])
    with store.db() as db:
        db.execute("UPDATE mobile_jobs SET status='pending' WHERE id=?", (job["id"],))
    another = jobs.claim({"worker_id": "real-worker"})
    conflicting = reading(another["jobs"][0])
    conflicting["price"] = 3500
    result = submit(jobs, another, [conflicting])
    assert result["conflicts"] == 1
    with store.db() as db:
        assert db.execute("SELECT price FROM mobile_points").fetchone()[0] == 3000
        assert db.execute("SELECT count(*) FROM mobile_readings").fetchone()[0] == 2
    assert jobs.jobs()["jobs"][0]["status"] == "needs_review"
    assert jobs.batches()["batches"][0]["status"] == "partial"
    assert jobs.retry(batch["id"])["retried"] == 1


def test_observed_query_or_explicit_alias_matches_only_after_scope_verification(system):
    store, catalog, jobs, phone = system
    catalog.patch(phone["id"], {"query": "苹果15 256G", "scope": {**phone["scope"], "verified": True, "aliases": ["iPhone15-256GB"]}})
    batch, lease = begin(system, days=2)
    first = reading(lease["jobs"][0])
    first["observed_model"] = "苹果15 256g"
    second = reading(lease["jobs"][1])
    second["observed_model"] = "IPHONE15-256GB"
    assert submit(jobs, lease, [first, second])["accepted"] == 2


def test_model_alias_matching_never_uses_prefix_or_inherits_variant(system):
    store, catalog, jobs, phone = system
    catalog.patch(phone["id"], {"scope": {**phone["scope"], "verified": True}})
    batch, lease = begin(system, days=1)
    invalid = reading(lease["jobs"][0])
    invalid["observed_model"] = "iPhone 15"
    with pytest.raises(ValidationError, match="型号"):
        submit(jobs, lease, [invalid])


def test_worker_cannot_self_assert_unverified_scope_by_copying_requested_catalog(system):
    store, catalog, jobs, phone = system
    jobs.create_batch({"catalog_ids": [phone["id"]], "days": 1})
    jobs.heartbeat({"worker_id": "real-worker", "device_status": "ready"})
    lease = jobs.claim({"worker_id": "real-worker"})
    assert lease["jobs"] == [] and lease["lease_token"] is None
    assert jobs.batches()["batches"][0]["verified_days"] == 0
    assert jobs.jobs()["jobs"][0]["attempts"] == 0
    # Operator proof of the very same economic scope can unlock existing jobs.
    catalog.patch(phone["id"], {"scope": {**phone["scope"], "verified": True}})
    lease = jobs.claim({"worker_id": "real-worker"})
    assert submit(jobs, lease, [reading(lease["jobs"][0])])["accepted"] == 1


def test_claim_reviews_obsolete_pending_scope_and_takes_new_verified_batch(system):
    store, catalog, jobs, phone = system
    old = jobs.create_batch({"catalog_ids": [phone["id"]], "days": 3})
    old_jobs = jobs.jobs(batch_id=old["id"])["jobs"]
    catalog.patch(phone["id"], {"scope": {**phone["scope"], "condition": "少量磕碰划痕", "verified": True}, "status": "ready"})
    new = jobs.create_batch({"catalog_ids": [phone["id"]], "days": 3})
    jobs.heartbeat({"worker_id": "real-worker", "device_status": "ready"})
    lease = jobs.claim({"worker_id": "real-worker", "catalog_id": phone["id"]})
    assert {job["batch_id"] for job in lease["jobs"]} == {new["id"]}
    assert lease["catalog"]["scope"]["condition"] == "少量磕碰划痕"
    archived = jobs.jobs(batch_id=old["id"])["jobs"]
    assert all(job["status"] == "needs_review" and job["attempts"] == 0 for job in archived)
    assert all(job["scope"] == old_jobs[0]["scope"] for job in archived)
    assert all("目录范围已修订" in job["error"] for job in archived)
    assert jobs.retry(old["id"])["retried"] == 0
    with store.db() as db:
        assert db.execute("SELECT count(*) FROM mobile_job_events WHERE status='needs_review'").fetchone()[0] == 3


def test_scope_change_during_lease_rejects_old_read_and_preserves_verified_history(system):
    store, catalog, jobs, phone = system
    old, lease = begin(system, days=2)
    submit(jobs, lease, [reading(lease["jobs"][0])])
    current = catalog.get(phone["id"])
    catalog.patch(phone["id"], {"scope": {**current["scope"], "condition": "少量磕碰划痕", "verified": True}, "status": "ready"})
    with pytest.raises(ValidationError, match="范围已修订"):
        submit(jobs, lease, [reading(lease["jobs"][1])])
    new = jobs.create_batch({"catalog_ids": [phone["id"]], "days": 2})
    next_lease = jobs.claim({"worker_id": "real-worker", "catalog_id": phone["id"]})
    assert {j["batch_id"] for j in next_lease["jobs"]} == {new["id"]}
    old_states = [j["status"] for j in jobs.jobs(batch_id=old["id"])["jobs"]]
    assert old_states == ["verified", "needs_review"]
    assert new["existing_verified"] == 0
    with store.db() as db:
        assert db.execute("SELECT count(*) FROM mobile_points").fetchone()[0] == 1


@pytest.mark.parametrize("patch", [{"enabled": False}, {"status": "needs_review"}, {"status": "no_market"}, {"status": "unsupported"}])
def test_claim_skips_disabled_or_unreviewed_catalog_without_consuming_attempts(system, patch):
    store, catalog, jobs, phone = system
    catalog.patch(phone["id"], {"scope": {**phone["scope"], "verified": True}, **patch})
    jobs.create_batch({"catalog_ids": [phone["id"]], "days": 1})
    jobs.heartbeat({"worker_id": "real-worker", "device_status": "ready"})
    assert jobs.claim({"worker_id": "real-worker"})["jobs"] == []
    assert jobs.jobs()["jobs"][0]["attempts"] == 0


def test_revoking_scope_proof_after_claim_cannot_use_frozen_old_verified_flag(system):
    store, catalog, jobs, phone = system
    batch, lease = begin(system, days=1)
    current = catalog.get(phone["id"])
    catalog.patch(phone["id"], {"scope": {**current["scope"], "verified": False}})
    with pytest.raises(ValidationError, match="尚未核对"):
        submit(jobs, lease, [reading(lease["jobs"][0])])
    with store.db() as db:
        assert db.execute("SELECT count(*) FROM mobile_points").fetchone()[0] == 0


def test_production_migration_backs_up_only_demo_and_preserves_live_and_operator_tasks(tmp_path):
    path = tmp_path / "market.sqlite3"
    fixture = Store(path)
    fixture.ingest([dict(item_id="real", title="RTX4090 24G", price=12000)])
    task = fixture.tasks()[0]
    fixture.save_task({"enabled": True, "interval_minutes": 60}, task["id"])
    production = Store(path, production=True)
    assert production.overview("live")["summary"]["total_items"] == 1
    assert production.overview("demo")["summary"]["total_items"] == 0
    assert production.get_task(task["id"])["enabled"]
    assert production.get_task(task["id"])["interval_minutes"] == 60
    with production.db() as db:
        audit = json.loads(db.execute("SELECT value FROM meta WHERE key='production_demo_migration_v1'").fetchone()[0])
    assert audit["removed_demo"] == 180 and audit["live_preserved"] == 1
    backup = tmp_path / "backups" / audit["backup"]
    assert backup.is_file()
    with sqlite3.connect(backup) as db:
        assert db.execute("SELECT count(*) FROM items WHERE source='demo'").fetchone()[0] == 180
    with TestClient(create_app(path, production=True)) as client:
        assert client.get("/api/overview").json()["source"] == "live"
        assert client.get("/api/items?source=demo").status_code == 422
        assert client.post("/api/demo/reset").status_code == 404


def test_listing_queue_restart_and_sampling_versions_are_durable(system):
    store, catalog, jobs, phone = system
    task = store.tasks()[0]
    job_id = store.enqueue_listing(task)
    assert store.enqueue_listing(task) == job_id
    assert store.start_listing(job_id)["id"] == task["id"]
    run = store.begin_run(task, job_id)
    restarted = Store(store.path, production=True)
    pending = restarted.pending_listing()
    assert pending["id"] == job_id and pending["status"] == "queued"
    assert restarted.runs()[0]["status"] == "interrupted"
    assert restarted.runs()[0]["sampling_config_hash"]
    assert restarted.runs()[0]["initial_scan"] == 1


def test_sampling_frequency_changes_hash_and_restarts_initial_scan_per_version(system):
    store, catalog, jobs, phone = system
    task = store.tasks()[0]
    original_hash, original_json = store.sampling_config(task)
    original_run = store.begin_run(task)
    store.finish_run(original_run, task["id"])
    # A verified empty successful first scan also establishes the sampling version.
    repeat_run = store.begin_run(task)
    assert store.runs()[0]["initial_scan"] == 0
    store.finish_run(repeat_run, task["id"])
    updated = store.save_task({"interval_minutes": task["interval_minutes"] + 30}, task["id"])
    changed_hash, changed_json = store.sampling_config(updated)
    assert changed_hash != original_hash
    assert json.loads(changed_json)["interval_minutes"] == updated["interval_minutes"]
    assert json.loads(original_json)["interval_minutes"] == task["interval_minutes"]
    changed_run = store.begin_run(updated)
    assert store.runs()[0]["initial_scan"] == 1
    store.finish_run(changed_run, task["id"])
    store.begin_run(updated)
    assert store.runs()[0]["initial_scan"] == 0
    with store.db() as db:
        historical = db.execute("SELECT * FROM runs WHERE id=?", (original_run,)).fetchone()
        assert historical["sampling_config_hash"] == original_hash
        assert historical["config_json"] == original_json


def test_operator_pause_or_delete_cancels_queued_listing_rounds(system):
    store, catalog, jobs, phone = system
    task = store.tasks()[0]
    store.enqueue_listing(task)
    store.save_task({"enabled": False}, task["id"])
    assert store.pending_listing() is None
    store.enqueue_listing(task)
    store.delete_task(task["id"])
    assert store.pending_listing() is None
