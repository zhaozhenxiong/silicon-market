"""All fixtures are temporary SQLite; no phone, browser, model, or network."""
import copy
import json
from datetime import timedelta

import pytest

from app.catalog import Catalog
from app.indices import IndexService
from app.jobs import MobileJobs
from app.normalization import ValidationError, iso, utcnow
from app.official_market import OfficialMarket
from app.service import Store
from app.workflow import Workflow


@pytest.fixture
def system(tmp_path):
    store = Store(tmp_path / "workflow.sqlite3", production=True)
    catalog = Catalog(store)
    jobs = MobileJobs(store, catalog, OfficialMarket(store))
    indices = IndexService(store)
    return store, catalog, jobs, indices, Workflow(store, catalog, jobs, indices)


def add(store, item_id="review-1", title="i9-14900KF 故障维修件", price=233.45, **extra):
    record = dict(item_id=item_id, title=title, price=price, observed_at=iso(utcnow()-timedelta(days=1)))
    record.update(extra)
    return store.ingest([record], "live")


def raw_snapshot(store):
    with store.db() as db:
        return {table: [tuple(row) for row in db.execute(f"SELECT * FROM {table} ORDER BY rowid")]
                for table in ("items", "observations", "tasks", "runs", "mobile_jobs", "mobile_points")}


def suggestion(batch, index=0, **extra):
    row = batch["items"][index]
    decision = dict(observation_id=row["observation_id"], source_hash=row["source_hash"], decision="exclude",
                    quote=row["title"], confidence=.94, reason_codes=["fault_or_accessory"])
    decision.update(extra)
    return dict(batch_id=batch["id"], content_hash=batch["content_hash"], reviewer={"kind": "codex", "name": "本机复核"}, decisions=[decision])


def test_status_empty_and_gets_never_operate_or_claim_mobile(system, monkeypatch):
    store, catalog, jobs, indices, flow = system
    monkeypatch.setattr(jobs, "claim", lambda *_: pytest.fail("must not claim"))
    monkeypatch.setattr(jobs, "heartbeat", lambda *_: pytest.fail("must not report health"))
    before = raw_snapshot(store)
    state = flow.status()
    assert state["counts"]["live_observations"] == 0
    assert [row["id"] for row in state["stages"]] == ["sources", "listing", "rules", "review", "quality", "indices"]
    assert state["mobile"]["operator_paused"] is False  # new DB does not change existing API authorization
    assert state["mobile"]["connected"] is False and not state["mobile"]["connection_checked"]
    assert state["ai"]["external_calls"] is False
    assert state["quality_gate"]["can_rebuild"] is False
    assert raw_snapshot(store) == before
    assert flow.create_review_batch()["status"] == "empty"
    assert flow.review_batches()["total"] == 0


def test_operator_pause_is_persistent_and_preserves_exact_mobile_breakpoint(system):
    store, catalog, jobs, indices, flow = system
    catalog.sync()
    batch = jobs.create_batch({"catalog_ids": [catalog.list()["items"][0]["id"]], "days": 3})
    before = raw_snapshot(store)
    paused = flow.pause_mobile("用户将手机取走，等待本人归还后明确恢复")
    assert paused["operator_paused"] is True
    with store.db() as db:
        db.execute("UPDATE mobile_device SET device_status='ready',last_heartbeat=?,message='旧的健康报告' WHERE id=1", (iso(),))
    restarted = Workflow(store, catalog, jobs, indices)
    branch = restarted.status()["mobile"]
    assert branch["status"] == "operator_paused" and branch["operator_paused"] is True
    assert branch["pause_reason"] == paused["pause_reason"]
    assert branch["last_device_report"]["device_status"] == "ready"  # cannot resume based on this
    assert branch["connected"] is False and branch["automatic_resume"] is False
    assert branch["breakpoint"]["counts"]["total"] == 3
    assert branch["breakpoint"]["counts"]["remaining"] == 3
    assert branch["breakpoint"]["next_pending"]["batch_id"] == batch["id"]
    assert raw_snapshot(store) == before
    resumed = restarted.resume_mobile("本人明确允许重新尝试连接")
    assert resumed["operator_paused"] is False
    assert restarted.status()["mobile"]["status"] == "awaiting_device"
    assert restarted.status()["mobile"]["connected"] is False


def test_review_export_keeps_exact_recorded_values_and_never_includes_demo(system):
    store, _, _, _, flow = system
    add(store)
    store.ingest([dict(item_id="demo:fixture", title="RTX 4090 故障", price=1)], "demo")
    before = raw_snapshot(store)
    batch = flow.create_review_batch()
    assert batch["created"] and not batch["cached"] and batch["item_count"] == 1
    row = batch["items"][0]
    assert row["price_original"] == row["price"] == 233.45
    assert row["title"] == "i9-14900KF 故障维修件"
    assert row["evidence"]["title"] == row["title"]
    assert row["source_hash"] and row["observation_id"] and row["item_id"] == "review-1"
    assert row["raw_quality_flags"] and row["review_reasons"]
    assert batch["external_ai_called"] is False and batch["automatic_apply"] is False
    assert batch["decision_template"]["decisions"][0]["confidence"] is None
    assert batch["json_schema"]["properties"]["decisions"]["maxItems"] == 40
    assert flow.review_batch(batch["id"])["items"] == batch["items"]
    cached = flow.create_review_batch()
    assert cached["id"] == batch["id"] and cached["cached"] and not cached["created"]
    assert flow.status()["counts"]["demo_observations_ignored"] == 1
    assert raw_snapshot(store) == before


def test_review_queue_chunks_at_40_and_deduplicates_each_input_version(system):
    store, _, _, _, flow = system
    store.ingest([dict(item_id=f"unknown-{n}", title=f"未知元件 待核实标识 {n}", price=100+n) for n in range(75)], "live")
    first, second = flow.create_review_batch(), flow.create_review_batch()
    assert first["item_count"] == 40 and second["item_count"] == 35
    assert not ({row["source_hash"] for row in first["items"]} & {row["source_hash"] for row in second["items"]})
    assert flow.create_review_batch()["cached"]
    assert flow.review_batches()["total"] == 2
    # A changed eligibility version is a new review input, not a reused decision.
    store.price_audit.apply()
    updated = flow.create_review_batch()
    assert updated["created"] and updated["id"] not in {first["id"], second["id"]}
    assert updated["input_version"] != first["input_version"]
    assert updated["items"][0]["source_hash"] != first["items"][0]["source_hash"]


def test_identical_item_snapshots_share_one_case_and_only_representative_receives_suggestion(system):
    store, _, _, _, flow = system
    add(store, observed_at=iso(utcnow()-timedelta(days=2)))
    add(store, observed_at=iso(utcnow()-timedelta(days=1)))
    before = raw_snapshot(store)
    batch = flow.create_review_batch()
    assert batch["item_count"] == 1
    case = batch["items"][0]
    assert case["linked_observation_count"] == 2
    assert len(case["linked_observation_ids"]) == len(case["linked_observation_refs"]) == 2
    assert case["observation_id"] == min(case["linked_observation_ids"])
    result = flow.import_review_decisions(suggestion(batch))
    assert result["imported"] == 1
    with store.db() as db:
        stored = db.execute("SELECT observation_id,review_status FROM review_decisions").fetchall()
    assert [(row["observation_id"], row["review_status"]) for row in stored] == [(case["observation_id"], "suggested")]
    counts = flow.status()["counts"]
    assert counts["review_candidate_observations"] == 2
    assert counts["review_candidates"] == 1 and counts["review_cases_deduplicated"] == 1
    assert raw_snapshot(store) == before  # links never rewrite/collapse evidence


def test_new_identical_snapshot_preserves_source_hash_and_does_not_reexport(system):
    store, _, _, _, flow = system
    add(store, observed_at=iso(utcnow()-timedelta(days=2)))
    first = flow.create_review_batch()
    original = first["items"][0]
    # Even a later import of an earlier timestamp must not replace the first
    # recorded representative or spend another review slot.
    add(store, observed_at=iso(utcnow()-timedelta(days=4)))
    current = flow._materials()[0]
    assert current["observation_id"] == original["observation_id"]
    assert current["source_hash"] == original["source_hash"]
    assert current["linked_observation_count"] == 2
    cached = flow.create_review_batch()
    assert cached["id"] == first["id"] and cached["content_hash"] == first["content_hash"]
    assert cached["cached"] and not cached["created"]
    assert flow.review_batches()["total"] == 1 and flow.status()["counts"]["review_unexported"] == 0
    # The frozen export's original refs stay immutable; current status reflects
    # the additional evidence without invalidating an in-progress suggestion.
    assert flow.review_batch(first["id"])["items"] == first["items"]
    before = raw_snapshot(store)
    assert flow.import_review_decisions(suggestion(first))["imported"] == 1
    assert raw_snapshot(store) == before


def test_changed_price_creates_new_case_and_a_different_item_never_reuses_it(system):
    store, _, _, _, flow = system
    add(store, observed_at=iso(utcnow()-timedelta(days=2)))
    first = flow.create_review_batch()
    old_hash = first["items"][0]["source_hash"]
    add(store, price=777.89, observed_at=iso(utcnow()-timedelta(days=1)))
    second = flow.create_review_batch()
    assert second["created"] and second["item_count"] == 1
    assert second["items"][0]["price_original"] == 777.89
    assert second["items"][0]["source_hash"] != old_hash
    assert flow._materials()[0]["source_hash"] == old_hash
    add(store, "another-seller-item", price=777.89)
    third = flow.create_review_batch()
    assert third["created"] and third["item_count"] == 1
    assert third["items"][0]["item_pk"] != second["items"][0]["item_pk"]
    assert third["items"][0]["source_hash"] != second["items"][0]["source_hash"]


@pytest.mark.parametrize("change", [
    {"title": "i9-14900K 故障维修件"}, {"price": 234.45}, {"task_query": "不同查询"},
    {"category": "component"}, {"model": "i9-14900K"}, {"status": "unknown"},
    {"quality_flags": '["原规则排除原因变化"]'}, {"price_audit_flags": ["单件报价单位仍不明"]},
])
def test_semantic_input_changes_are_not_deduplicated(system, change):
    store, _, _, _, flow = system
    add(store)
    original = flow._observations()[0]
    changed = {**copy.deepcopy(original), "id": original["id"]+100, "observed_at": iso(), **change}
    cases = flow._materials([original, changed])
    assert len(cases) == 2
    assert cases[0]["source_hash"] != cases[1]["source_hash"]
    assert all(case["linked_observation_count"] == 1 for case in cases)


def test_old_review_hash_version_requires_explicit_new_case_export(system):
    store, _, _, _, flow = system
    add(store)
    batch = flow.create_review_batch()
    with store.db() as db:
        payload = json.loads(db.execute("SELECT payload_json FROM review_batches WHERE id=?", (batch["id"],)).fetchone()[0])
        payload["schema_version"] = "offline-title-review-v1"
        db.execute("UPDATE review_batches SET payload_json=? WHERE id=?", (json.dumps(payload), batch["id"]))
    with pytest.raises(ValidationError, match="复核规则版本已升级"):
        flow.import_review_decisions(suggestion(batch))


def test_export_uses_frozen_run_query_instead_of_changed_task_query(system):
    store, _, _, _, flow = system
    task = store.tasks()[0]
    run_id = store.begin_run(task)
    add(store)
    with store.db() as db:
        db.execute("UPDATE observations SET task_id=?,run_id=?", (task["id"], run_id))
        db.execute("UPDATE tasks SET keyword='后来修改的查询' WHERE id=?", (task["id"],))
    material = flow.create_review_batch()["items"][0]
    assert material["query"] == task["keyword"]
    assert material["query_source"] == "frozen_run_config"


def test_import_only_records_suggestions_and_is_idempotent_without_raw_or_index_changes(system):
    store, _, _, indices, flow = system
    add(store)
    store.price_audit.apply()
    indices.rebuild(days=7)
    batch = flow.create_review_batch()
    before = raw_snapshot(store)
    audit_before = store.price_audit.state()
    result = flow.import_review_decisions(suggestion(batch))
    assert result["imported"] == 1 and result["applied"] == 0 and result["review_status"] == "suggested"
    assert result["raw_unchanged"] and not result["external_ai_called"]
    duplicate = flow.import_review_decisions(suggestion(batch))
    assert duplicate["imported"] == 0 and duplicate["existing"] == 1
    assert duplicate["decision_ids"] == result["decision_ids"]
    assert raw_snapshot(store) == before
    assert store.price_audit.state() == audit_before
    with store.db() as db:
        assert db.execute("SELECT count(*) FROM index_builds").fetchone()[0] == 1
        assert db.execute("SELECT review_status FROM review_decisions").fetchone()[0] == "suggested"
    state = flow.status()
    assert state["counts"]["review_suggestions"] == 1
    assert state["counts"]["review_awaiting_quality"] == 1 and state["counts"]["review_applied"] == 0
    assert state["quality_gate"]["can_apply_review"] is False


@pytest.mark.parametrize("change", [
    {"quote": "不存在于标题的规格 RTX4090 48GB"}, {"quote": "i"},
    {"confidence": True}, {"confidence": float("nan")}, {"confidence": 1.01},
    {"decision": "apply"}, {"decision": []},
    {"reason_codes": ["made_up"]}, {"reason_codes": ["model_unknown", "model_unknown"]},
    {"proposed_category": "money"}, {"proposed_model": ""},
    {"price": 100}, {"observed_at": "2020-01-01"},
    {"source_hash": "0"*64}, {"observation_id": True},
])
def test_import_rejects_unsupported_fields_or_missing_raw_quote_atomically(system, change):
    store, _, _, _, flow = system
    add(store)
    batch = flow.create_review_batch()
    with pytest.raises(ValidationError):
        flow.import_review_decisions(suggestion(batch, **change))
    with store.db() as db:
        assert db.execute("SELECT count(*) FROM review_decisions").fetchone()[0] == 0


def test_mixed_import_is_all_or_nothing_and_source_version_must_still_match(system):
    store, _, _, _, flow = system
    add(store, "first")
    add(store, "second")
    batch = flow.create_review_batch()
    payload = suggestion(batch)
    bad = suggestion(batch, index=1)["decisions"][0]
    bad["quote"] = "编造文本"
    payload["decisions"].append(bad)
    with pytest.raises(ValidationError):
        flow.import_review_decisions(payload)
    with store.db() as db:
        assert db.execute("SELECT count(*) FROM review_decisions").fetchone()[0] == 0
    store.price_audit.apply()
    with pytest.raises(ValidationError, match="规则版本已变化"):
        flow.import_review_decisions(suggestion(batch))


def test_import_rejects_batch_hash_duplicate_and_invalid_reviewer(system):
    store, _, _, _, flow = system
    add(store)
    batch = flow.create_review_batch()
    for transform in (
        lambda p: p.update(content_hash="0"*64),
        lambda p: p["decisions"].append(copy.deepcopy(p["decisions"][0])),
        lambda p: p.update(reviewer={"kind": "remote_ai", "name": "unknown"}),
        lambda p: p.update(reviewer={"kind": [], "name": "unknown"}),
    ):
        payload = suggestion(batch)
        transform(payload)
        with pytest.raises(ValidationError):
            flow.import_review_decisions(payload)


def test_quality_allows_rebuild_of_already_eligible_rows_but_never_publishes_pending_suggestions(system):
    store, _, _, _, flow = system
    add(store, "single", "i9-14900KF 散片 CPU", 3000)
    add(store)
    store.price_audit.apply()
    state = flow.status()
    assert state["quality_gate"]["can_rebuild"] is True
    assert state["quality_gate"]["can_publish_composite"] is False
    assert state["quality_gate"]["status"] == "needs_review"
    assert state["counts"]["eligible_observations"] == 1
    assert state["counts"]["excluded_observations"] == 1
    assert state["stages"][-1]["status"] == "pending_rebuild"


def test_unrecorded_live_audit_decisions_block_rules_and_rebuild_until_explicit_backfill(system):
    store, _, _, _, flow = system
    add(store, "audited", "i9-14900KF 散片 CPU", 3000)
    store.price_audit.apply()
    add(store, "legacy-arrival", "i9-14900KF 散片 CPU", 3100)
    # Emulate an old service which inserted a real observation without running
    # the new ingestion audit. mask_observations may still calculate a fallback.
    with store.db() as db:
        missing_id = db.execute("SELECT o.id FROM observations o JOIN items i ON i.id=o.item_pk WHERE i.item_id='legacy-arrival'").fetchone()[0]
        db.execute("DELETE FROM price_eligibility_audit WHERE observation_id=?", (missing_id,))
    before = raw_snapshot(store)
    state = flow.status()
    rules = next(stage for stage in state["stages"] if stage["id"] == "rules")
    audit_check = next(check for check in state["quality_gate"]["checks"] if check["id"] == "price_audit")
    assert (rules["total"], rules["completed"], rules["pending"]) == (2, 1, 1)
    assert rules["status"] == audit_check["status"] == "needs_review"
    assert audit_check["covered"] == 1 and audit_check["pending"] == 1 and audit_check["complete"] is False
    assert state["quality_gate"]["can_rebuild"] is False
    assert state["counts"]["audit_pending_observations"] == 1
    assert raw_snapshot(store) == before  # status never silently backfills audits
    store.price_audit.apply()
    ready = flow.status()
    rules = next(stage for stage in ready["stages"] if stage["id"] == "rules")
    assert (rules["completed"], rules["pending"], rules["status"]) == (2, 0, "ready")
    assert ready["quality_gate"]["can_rebuild"] is True


def test_audit_coverage_counts_current_live_ids_not_global_decision_count(system):
    store, _, _, _, flow = system
    add(store, "live", "i9-14900KF 散片 CPU", 3000)
    store.price_audit.apply()
    with store.db() as db:
        live = db.execute("SELECT * FROM price_eligibility_audit LIMIT 1").fetchone()
        # Historical stale decisions may remain after their source row is gone;
        # neither old versions nor foreign IDs count toward active live coverage.
        for observation_id in (50000, 50001):
            db.execute("INSERT INTO price_eligibility_audit(item_pk,item_id,observation_id,version,eligible,reasons,created_at) VALUES (?,?,?,?,1,'[]',?)",
                       (live["item_pk"], live["item_id"], observation_id, live["version"], iso()))
        db.execute("DELETE FROM price_eligibility_audit WHERE observation_id=? AND version=?", (live["observation_id"], live["version"]))
        db.execute("INSERT INTO price_eligibility_audit(item_pk,item_id,observation_id,version,eligible,reasons,created_at) VALUES (?,?,?,'old-test-version',1,'[]',?)",
                   (live["item_pk"], live["item_id"], live["observation_id"], iso()))
    assert store.price_audit.state()["decisions"] == 2
    state = flow.status()
    rules = next(stage for stage in state["stages"] if stage["id"] == "rules")
    assert (rules["total"], rules["completed"], rules["pending"]) == (1, 0, 1)
    assert rules["status"] == "needs_review" and state["quality_gate"]["can_rebuild"] is False


@pytest.mark.parametrize("value", [0, 41, True, "40"])
def test_batch_limit_is_strict(system, value):
    with pytest.raises(ValidationError):
        system[-1].create_review_batch(value)
