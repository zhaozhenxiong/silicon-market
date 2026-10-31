"""Task authorization only; all SQLite/evidence fixtures are temporary."""
import json

import pytest

from app.catalog import Catalog
from app.jobs import LeaseError
from app.mobile_targets import MobileTargets, MonitoredMobileJobs, task_model
from app.official_market import OfficialMarket
from app.service import Store


@pytest.fixture
def system(tmp_path):
    store = Store(tmp_path/"targets.sqlite3",production=True)
    catalog = Catalog(store)
    targets = MobileTargets(store,catalog)
    evidence = tmp_path/"evidence";evidence.mkdir()
    (evidence/"read.jpg").write_bytes(b"\xff\xd8\xff\xe0temporary evidence")
    official = OfficialMarket(store,evidence)
    jobs = MonitoredMobileJobs(store,catalog,official,targets)
    return store,catalog,targets,jobs


def monitor(store,keyword="iPhone 15 256GB",category="phone",**changes):
    payload=dict(name=keyword,keyword=keyword,category=category,enabled=True,source="live")
    payload.update(changes)
    return store.save_task(payload)


def ready(system,task=None):
    store,catalog,targets,jobs=system
    task=task or monitor(store)
    target=targets.list()["targets"][0]
    row=catalog.get(target["catalog_id"])
    catalog.patch(row["id"],{"scope":{**row["scope"],"verified":True},"status":"ready"})
    jobs.create_batch({"catalog_ids":[row["id"]],"days":1})
    jobs.heartbeat({"worker_id":"worker","device_status":"ready"})
    lease=jobs.claim({"worker_id":"worker","catalog_id":row["id"]})
    return task,row,lease


def reading(lease):
    job=lease["jobs"][0]
    return dict(worker_id="worker",lease_token=lease["lease_token"],readings=[dict(
        job_id=job["id"],displayed_date=job["target_date"],price=3000,evidence_filename="read.jpg",
        observed_model=job["model"],observed_query=job["query"],scope=job["scope"],
        read_method="mobile_screen_ocr",verification_count=2,ocr_text="双次实际截图",parser_version="fixture")])


@pytest.mark.parametrize("keyword,category,expected",[
    ("RTX 4070 SUPER","gpu","RTX 4070 SUPER"),("DDR5 32G","ram","DDR5 32GB"),
    ("R7 7800X3D","cpu","Ryzen 7800X3D"),("B650 主板","motherboard","B650"),
    ("14900","cpu","i9-14900"),("14900KF","cpu","i9-14900KF"),
    ("RTX4090","gpu","RTX 4090"),("5070ti","gpu","RTX 5070 TI"),
    (" 5070 Ti ","gpu","RTX 5070 TI"),("4070 super","gpu","RTX 4070 SUPER"),
    ("4070 Ti Super","gpu","RTX 4070 TI SUPER"),("iPhone 15 256GB","phone","iPhone 15 256GB")])
def test_exact_monitor_mapping_never_expands_suffix_vram_or_frequency(keyword,category,expected):
    assert task_model(dict(keyword=keyword,category=category,model="auto"))==(expected,"keyword")


@pytest.mark.parametrize("keyword,category",[("台式电脑 整机","computer"),("整机 i9 14900 RTX4090","computer"),
    ("CPU","cpu"),("DDR5 内存","ram"),("RTX4090 或 RTX4070","gpu"),
    ("RTX4090 RTX4070","gpu"),("14900 14900KF","cpu")])
def test_uncertain_or_generic_targets_require_model(keyword,category):
    assert task_model(dict(keyword=keyword,category=category,model="auto")) is None


@pytest.mark.parametrize("keyword",["9999","5070ti 4070","5070ti/4070","x5070ti","5070ti显卡","5070ti 16G","5070ti系列"])
def test_bare_rtx_alias_requires_entire_known_keyword_and_no_variant_guess(keyword):
    assert task_model(dict(keyword=keyword,category="gpu",model="auto")) is None


def test_monitor_name_does_not_supply_missing_model(system):
    store,_,targets,_=system
    monitor(store,"显卡",category="gpu",name="RTX5070ti")
    result=targets.list()
    assert result["targets"]==[] and result["skipped_tasks"][0]["reason"]=="needs_model"


def test_current_five_monitor_shapes_map_one_target_each_and_expose_catalog_versions(system):
    store,catalog,targets,_=system
    for keyword,category in [("DDR5 32G","ram"),("14900","cpu"),("RTX4090","gpu"),("iPhone 15 256GB","phone"),("5070ti","gpu")]:
        monitor(store,keyword,category=category)
    result=targets.list()
    assert result["count"]==result["enabled_task_count"]==5 and result["skipped_tasks"]==[]
    assert {row["model"] for row in result["targets"]}=={"DDR5 32GB","i9-14900","RTX 4090","iPhone 15 256GB","RTX 5070 TI"}
    for row in result["targets"]:
        assert row["catalog_version"]==catalog.get(row["catalog_id"])["version"]


def test_explicit_model_is_used_instead_of_keyword_guess(system):
    store,_,targets,_=system
    monitor(store,"RTX 4070",category="gpu",model="RTX 4090 24GB")
    result=targets.list()
    assert result["count"]==1 and result["targets"][0]["model"]=="RTX 4090 24GB"
    assert result["targets"][0]["mapping_sources"]==["explicit_model"]


def test_disabled_tasks_demo_and_unrelated_live_items_cannot_create_targets(system):
    store,catalog,targets,jobs=system
    store.ingest([dict(item_id="unrelated",title="RTX 4090 48GB",price=9900)],"live")
    monitor(store,"RTX 4090",category="gpu",enabled=False)
    demo=monitor(store,"R7 7800X3D",category="cpu")
    with store.db() as db:
        db.execute("UPDATE tasks SET source='demo' WHERE id=?",(demo["id"],))
        before=[tuple(row) for row in db.execute("SELECT * FROM tasks ORDER BY id")]
    result=targets.list()
    assert result["enabled_task_count"]==result["count"]==0 and result["targets"]==[]
    assert catalog.list()["total"]==0
    with store.db() as db:
        assert before==[tuple(row) for row in db.execute("SELECT * FROM tasks ORDER BY id")]
    with pytest.raises(LeaseError,match="没有已启用"):
        jobs.claim({"worker_id":"worker"})


def test_duplicate_monitor_targets_keep_all_task_links_and_idempotent_catalog(system):
    store,catalog,targets,_=system
    a=monitor(store,"RTX4090",category="gpu")
    b=monitor(store,"RTX 4090",category="gpu",name="另一条同型号监控")
    monitor(store,"台式电脑 整机",category="computer")
    first=targets.list();second=targets.list()
    assert first==second and first["enabled_task_count"]==3 and first["count"]==1
    row=first["targets"][0]
    assert row["task_ids"]==[a["id"],b["id"]] and row["task_names"]==[a["name"],b["name"]]
    assert row["scope_verified"] is False and row["readable"] is False
    assert first["skipped_tasks"][0]["reason"]=="needs_model" and catalog.list()["total"]==1


def test_unique_verified_scope_is_reused_instead_of_pending_default_phone_duplicate(system):
    store,catalog,targets,_=system
    monitor(store)
    first=targets.list()["targets"][0]
    row=catalog.get(first["catalog_id"])
    verified=catalog.patch(row["id"],{"query":"iphone15","scope":{**row["scope"],"condition":"少量磕碰划痕","verified":True},"status":"ready"})
    # Legacy sync may have a second, unverified broad/default record. It must
    # not replace the one actually checked on screen or inherit its history.
    catalog.sync()
    assert len([row for row in catalog.list()["items"] if row["model"]=="iPhone 15 256GB"])==2
    resolved=targets.list()["targets"][0]
    assert resolved["catalog_id"]==verified["id"] and resolved["market_scope_id"]==verified["market_scope_id"]
    assert resolved["scope"]["condition"]=="少量磕碰划痕" and resolved["query"]=="iphone15"
    assert resolved["scope_verified"] and resolved["readable"]


def test_multiple_distinct_verified_scopes_are_not_arbitrarily_chosen(system):
    store,catalog,targets,_=system
    monitor(store)
    first=targets.list()["targets"][0]
    row=catalog.get(first["catalog_id"])
    catalog.patch(row["id"],{"scope":{**row["scope"],"condition":"全新","verified":True}})
    catalog.sync()
    other=next(item for item in catalog.list()["items"] if item["model"]==row["model"] and item["id"]!=row["id"])
    catalog.patch(other["id"],{"scope":{**other["scope"],"condition":"99新","verified":True}})
    result=targets.list()
    assert result["targets"]==[] and result["skipped_tasks"][0]["reason"]=="needs_scope"


def test_disabled_catalog_is_not_recreated_to_bypass_operator_setting(system):
    store,catalog,targets,_=system
    monitor(store)
    target=targets.list()["targets"][0]
    catalog.patch(target["catalog_id"],{"enabled":False})
    count=catalog.list()["total"]
    result=targets.list()
    assert result["count"]==0 and result["skipped_tasks"][0]["reason"]=="catalog_disabled"
    assert catalog.list()["total"]==count


@pytest.mark.parametrize("status",["ready","partial","complete","pending","waiting_device","needs_review","no_market","unsupported"])
def test_readable_metadata_matches_runtime_ready_states_even_when_scope_is_verified(system,status):
    store,catalog,targets,_=system
    monitor(store)
    target=targets.list()["targets"][0]
    row=catalog.get(target["catalog_id"])
    catalog.patch(row["id"],{"scope":{**row["scope"],"verified":True},"status":status})
    result=targets.list()["targets"][0]
    assert result["scope_verified"] is True
    assert result["readable"] is (status in {"ready","partial","complete"})


@pytest.mark.parametrize("change",["disable","delete","demo","model"])
def test_disable_delete_source_or_target_change_revokes_existing_reading_lease(system,change):
    store,catalog,targets,jobs=system
    task,row,lease=ready(system)
    if change=="disable":store.save_task({"enabled":False},task["id"])
    elif change=="delete":store.delete_task(task["id"])
    elif change=="demo":
        with store.db() as db:db.execute("UPDATE tasks SET source='demo' WHERE id=?",(task["id"],))
    else:store.save_task({"model":"iPhone 16 256GB"},task["id"])
    with pytest.raises(LeaseError,match="当前已启用"):
        jobs.submit(reading(lease))
    with store.db() as db:
        assert db.execute("SELECT count(*) FROM mobile_readings").fetchone()[0]==0
    assert catalog.get(row["id"]) is not None  # preserve catalog/history/settings


def test_final_write_transaction_rechecks_monitor_after_evidence_was_prepared(system,monkeypatch):
    store,_,_,jobs=system
    task,_,lease=ready(system)
    original=jobs.official.evidence_file
    def revoke_after_first_validation(filename):
        path=original(filename)
        store.save_task({"enabled":False},task["id"])
        return path
    monkeypatch.setattr(jobs.official,"evidence_file",revoke_after_first_validation)
    with pytest.raises(LeaseError,match="当前已启用"):
        jobs.submit(reading(lease))
    with store.db() as db:
        assert db.execute("SELECT count(*) FROM mobile_readings").fetchone()[0]==0
        assert db.execute("SELECT count(*) FROM mobile_points").fetchone()[0]==0


def test_no_catalog_id_claim_skips_archived_unmonitored_catalogs(system):
    store,catalog,targets,jobs=system
    old=monitor(store,"RTX4090",category="gpu")
    old_target=targets.list()["targets"][0]
    row=catalog.get(old_target["catalog_id"])
    catalog.patch(row["id"],{"scope":{**row["scope"],"verified":True}})
    batch=jobs.create_batch({"catalog_ids":[row["id"]],"days":1})
    store.save_task({"enabled":False},old["id"])
    monitor(store)
    current=targets.list()["targets"][0]
    row=catalog.get(current["catalog_id"])
    catalog.patch(row["id"],{"scope":{**row["scope"],"verified":True}})
    jobs.create_batch({"catalog_ids":[row["id"]],"days":1})
    jobs.heartbeat({"worker_id":"worker","device_status":"ready"})
    with pytest.raises(LeaseError,match="当前已启用"):
        jobs.claim({"worker_id":"worker","catalog_id":old_target["catalog_id"]})
    lease=jobs.claim({"worker_id":"worker"})
    assert lease["catalog"]["id"]==current["catalog_id"]
    assert jobs.jobs(batch_id=batch["id"])["jobs"][0]["attempts"]==0
    assert jobs.submit(reading(lease))["accepted"]==1


def test_duplicate_monitor_survives_one_disabled_but_not_all(system):
    store,_,targets,jobs=system
    first=monitor(store)
    second=monitor(store,name="同型号另一个监控")
    _,_,lease=ready(system,first)
    store.save_task({"enabled":False},first["id"])
    assert targets.list()["targets"][0]["task_ids"]==[second["id"]]
    assert jobs.submit(reading(lease))["accepted"]==1


def test_automatic_claim_skips_unverified_monitor_and_takes_verified_monitor(system):
    store,catalog,targets,jobs=system
    monitor(store,"RTX4090",category="gpu")
    gpu=targets.list()["targets"][0]
    jobs.create_batch({"catalog_ids":[gpu["catalog_id"]],"days":1})
    monitor(store)
    phone=next(row for row in targets.list()["targets"] if row["category"]=="phone")
    row=catalog.get(phone["catalog_id"])
    catalog.patch(row["id"],{"scope":{**row["scope"],"verified":True}})
    jobs.create_batch({"catalog_ids":[row["id"]],"days":1})
    jobs.heartbeat({"worker_id":"worker","device_status":"ready"})
    assert jobs.claim({"worker_id":"worker"})["catalog"]["id"]==phone["catalog_id"]


@pytest.fixture
def api(tmp_path):
    from fastapi.testclient import TestClient
    from app.main import create_app
    app=create_app(tmp_path/"api.sqlite3",production=True)
    evidence=tmp_path/"evidence";evidence.mkdir()
    (evidence/"read.jpg").write_bytes(b"\xff\xd8\xff\xe0temporary evidence")
    app.state.official_market.evidence_dir=evidence
    return app,TestClient(app,client=("127.0.0.1",31001))


def test_targets_api_disabled_presets_stay_disabled_and_do_not_touch_runtime(api,monkeypatch):
    app,client=api
    monkeypatch.setattr(app.state.mobile_runtime,"status",lambda *_:pytest.fail("targets must not inspect native device"))
    monkeypatch.setattr(app.state.mobile_runtime,"start",lambda *_:pytest.fail("targets must not start native device"))
    before=app.state.store.tasks()
    result=client.get("/api/mobile/targets")
    assert result.status_code==200
    data=result.json()
    assert data["targets"]==data["skipped_tasks"]==[] and data["enabled_task_count"]==data["count"]==0
    assert app.state.store.tasks()==before
    result=client.post("/api/mobile/claim",json={"worker_id":"worker"})
    assert result.status_code==409 and "没有已启用" in result.json()["detail"]


def test_targets_api_live_monitor_mapping_and_uncertain_skipped_schema(api):
    app,client=api
    task=monitor(app.state.store,"14900",category="cpu")
    unclear=monitor(app.state.store,"台式电脑 整机",category="computer")
    data=client.get("/api/mobile/targets").json()
    assert data["policy"]=="enabled_live_monitor_tasks_only" and data["enabled_task_count"]==2
    assert data["targets"][0]["model"]=="i9-14900" and data["targets"][0]["task_ids"]==[task["id"]]
    assert data["skipped_tasks"][0]["task_id"]==unclear["id"] and data["skipped_tasks"][0]["reason"]=="needs_model"
    assert client.post("/api/mobile/claim",json={"worker_id":"worker","catalog_id":999}).status_code==409


def test_api_disabled_monitor_cannot_submit_previously_claimed_reading(api):
    app,client=api
    system=(app.state.store,app.state.catalog,app.state.mobile_targets,app.state.mobile_jobs)
    task,_,lease=ready(system)
    assert client.patch(f"/api/tasks/{task['id']}",json={"enabled":False}).status_code==200
    result=client.post("/api/mobile/readings",json=reading(lease))
    assert result.status_code==409 and "当前已启用" in result.json()["detail"]
    with app.state.store.db() as db:
        assert db.execute("SELECT count(*) FROM mobile_readings").fetchone()[0]==0


def test_api_operator_pause_also_revokes_reading_submission(api):
    app,client=api
    system=(app.state.store,app.state.catalog,app.state.mobile_targets,app.state.mobile_jobs)
    _,_,lease=ready(system)
    app.state.workflow.pause_mobile("本人暂停")
    result=client.post("/api/mobile/readings",json=reading(lease))
    assert result.status_code==409 and "已人工暂停" in result.json()["detail"]
