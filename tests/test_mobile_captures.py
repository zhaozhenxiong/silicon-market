"""Temporary screenshots/DB only; capture archival never operates a device."""
import hashlib
import json
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from app.catalog import Catalog
from app.jobs import LeaseError,MobileJobs
from app.main import create_app
from app.mobile_captures import MobileCaptures
from app.mobile_targets import MobileTargets
from app.normalization import ValidationError,iso,utcnow
from app.official_market import OfficialMarket
from app.service import Store


@pytest.fixture
def system(tmp_path):
    store=Store(tmp_path/"captures.sqlite3",production=True)
    catalog=Catalog(store)
    targets=MobileTargets(store,catalog)
    evidence=tmp_path/"evidence";evidence.mkdir()
    for name,content in {"a.png":b"\x89PNG\r\n\x1a\nfirst temporary screenshot","b.jpg":b"\xff\xd8\xff\xe0second temporary screenshot"}.items():
        (evidence/name).write_bytes(content)
    official=OfficialMarket(store,evidence)
    MobileJobs(store,catalog,official)
    paused=[False]
    captures=MobileCaptures(store,targets,official,lambda:paused[0])
    task=store.save_task(dict(name="14900截图任务",keyword="14900",category="cpu",source="live",enabled=True))
    target=targets.list()["targets"][0]
    return store,catalog,targets,captures,task,target,paused


def frame(captures,name="a.png",**changes):
    result=dict(evidence_filename=name,evidence_hash=hashlib.sha256((captures.official.evidence_dir/name).read_bytes()).hexdigest(),
                captured_at=iso(utcnow()-timedelta(seconds=1)),chart_x=.3,screen_date=None)
    result.update(changes)
    return result


def raw_snapshot(store):
    with store.db() as db:
        return {table:[tuple(row) for row in db.execute(f"SELECT * FROM {table} ORDER BY rowid")]
                for table in ("items","observations","tasks","mobile_jobs","mobile_points","mobile_readings")}


def batch(system,reviewed=False):
    _,catalog,_,captures,_,target,_=system
    if reviewed:
        row=catalog.get(target["catalog_id"])
        catalog.patch(row["id"],{"scope":{**row["scope"],"verified":True},"status":"ready"})
    return captures.create_batch({"catalog_id":target["catalog_id"]})


def test_unverified_scope_is_archived_queued_without_claiming_any_price_or_date(system):
    store,_,_,captures,_,target,_=system
    before=raw_snapshot(store)
    created=batch(system)
    assert created["status"]=="needs_scope" and not created["scope_verified"]
    assert created["task_ids"]==target["task_ids"] and created["created"]
    image=frame(captures)
    result=captures.register({"batch_id":created["id"],"frames":[image]})
    saved=result["frames"][0]
    assert saved["status"]=="queued_scope" and saved["price"] is None and saved["screen_date"] is None
    assert saved["screen_date_verified"] is False and saved["scope_is_target_only"] is True
    assert saved["catalog_version"]==created["catalog_version"] and saved["task_ids"]==target["task_ids"]
    assert result["price_points_created"]==result["verified_days_added"]==0
    assert raw_snapshot(store)==before
    assert captures.pending()["total"]==1 and captures.status()["pending_frames"]==1
    assert captures.evidence(saved["id"]).read_bytes()==(captures.official.evidence_dir/"a.png").read_bytes()


def test_reviewed_target_still_queues_images_not_verified_price_points(system):
    store,_,_,captures,_,_,_=system
    created=batch(system,reviewed=True)
    assert created["status"]=="queued" and created["scope_verified"]
    image=frame(captures,screen_date=(utcnow()-timedelta(days=5)).date().isoformat())
    result=captures.register({"batch_id":created["id"],"frames":[image]})
    assert result["frames"][0]["status"]=="queued" and not result["frames"][0]["screen_date_verified"]
    with store.db() as db:
        assert db.execute("SELECT count(*) FROM mobile_points").fetchone()[0]==0


def test_batches_and_same_frame_retries_are_idempotent_and_do_not_overwrite_metadata(system):
    _,_,_,captures,_,_,_=system
    first=batch(system);again=batch(system)
    assert first["id"]==again["id"] and again["created"] is False
    image=frame(captures)
    result=captures.register({"batch_id":first["id"],"frames":[image]})
    repeated=captures.register({"batch_id":first["id"],"frames":[image]})
    assert repeated["registered"]==0 and repeated["existing"]==1
    assert repeated["frames"][0]["id"]==result["frames"][0]["id"]
    with pytest.raises(ValidationError,match="元数据"):
        captures.register({"batch_id":first["id"],"frames":[{**image,"chart_x":.7}]})
    assert captures.pending()["total"]==1


def test_different_real_frames_with_identical_bytes_are_marked_duplicates_not_distinct_dates(system):
    _,_,_,captures,_,_,_=system
    root=captures.official.evidence_dir
    (root/"copy.png").write_bytes((root/"a.png").read_bytes())
    created=batch(system)
    result=captures.register({"batch_id":created["id"],"frames":[frame(captures),frame(captures,"copy.png",chart_x=.6)]})
    assert result["registered"]==2
    assert result["frames"][1]["duplicate_hash_of"]==result["frames"][0]["id"]
    assert result["verified_days_added"]==0


@pytest.mark.parametrize("extra",[{"price":3000},{"sold_price":2500},{"model":"other"},{"catalog_id":999},{"status":"verified"},{"ocr_text":"imagined"}])
def test_capture_stage_refuses_prices_identity_overrides_and_recognition_results(system,extra):
    _,_,_,captures,_,_,_=system
    created=batch(system)
    with pytest.raises(ValidationError):
        captures.register({"batch_id":created["id"],"frames":[{**frame(captures),**extra}]})
    assert captures.status()["frame_count"]==0


@pytest.mark.parametrize("changes",[{"chart_x":True},{"chart_x":float("nan")},{"chart_x":1.1},
    {"captured_at":"2100-01-01T00:00:00Z"},{"captured_at":"2026-10-30T12:00:00"},
    {"screen_date":"2100-01-01"},{"screen_date":"2026-02-30"},{"screen_date":"06.09"},
    {"evidence_hash":"made-up"}])
def test_frame_metadata_does_not_invent_dates_or_accept_nonfinite_coordinates(system,changes):
    _,_,_,captures,_,_,_=system
    created=batch(system)
    with pytest.raises(ValidationError):captures.register({"batch_id":created["id"],"frames":[frame(captures,**changes)]})
    assert captures.status()["frame_count"]==0


def test_bulk_registration_is_transactional_when_later_image_hash_is_wrong(system):
    _,_,_,captures,_,_,_=system
    created=batch(system)
    with pytest.raises(ValidationError,match="SHA256"):
        captures.register({"batch_id":created["id"],"frames":[frame(captures),frame(captures,"b.jpg",evidence_hash="0"*64)]})
    assert captures.status()["frame_count"]==0


@pytest.mark.parametrize("change",["disabled","deleted","model","scope","version","task_links"])
def test_registration_rechecks_exact_current_enabled_task_scope_and_version(system,change):
    store,catalog,_,captures,task,target,_=system
    created=batch(system)
    if change=="disabled":store.save_task({"enabled":False},task["id"])
    elif change=="deleted":store.delete_task(task["id"])
    elif change=="model":store.save_task({"model":"i9-14900KF"},task["id"])
    elif change in {"scope","version"}:
        row=catalog.get(target["catalog_id"])
        catalog.patch(row["id"],{"scope":{**row["scope"],"condition":"全新"}} if change=="scope" else {"query":"14900"})
    else:store.save_task(dict(name="同型号监控",keyword="14900",category="cpu",enabled=True))
    with pytest.raises(LeaseError):captures.register({"batch_id":created["id"],"frames":[frame(captures)]})
    assert captures.status()["frame_count"]==0


def test_pause_blocks_new_batches_and_frames_but_keeps_pending_manifest(system):
    _,_,_,captures,_,target,paused=system
    created=batch(system)
    captures.register({"batch_id":created["id"],"frames":[frame(captures)]})
    paused[0]=True
    with pytest.raises(LeaseError,match="已人工暂停"):captures.create_batch({"catalog_id":target["catalog_id"]})
    with pytest.raises(LeaseError,match="已人工暂停"):captures.register({"batch_id":created["id"],"frames":[frame(captures,"b.jpg")]})
    assert captures.pending()["total"]==1


def test_capture_archive_survives_restart_and_keeps_timestamp_and_hash(system):
    store,_,targets,captures,_,_,_=system
    created=batch(system);image=frame(captures)
    captures.register({"batch_id":created["id"],"frames":[image]})
    restarted=MobileCaptures(store,targets,captures.official)
    saved=restarted.pending()["frames"][0]
    assert saved["captured_at"]==image["captured_at"] and saved["evidence_hash"]==image["evidence_hash"]


def test_evidence_cannot_be_replaced_or_escape_the_evidence_directory(system,tmp_path):
    _,_,_,captures,_,_,_=system
    created=batch(system)
    saved=captures.register({"batch_id":created["id"],"frames":[frame(captures)]})["frames"][0]
    (captures.official.evidence_dir/"a.png").write_bytes(b"\x89PNG\r\n\x1a\nchanged")
    with pytest.raises(ValidationError,match="SHA256"):captures.evidence(saved["id"])
    outside=tmp_path/"outside.png";outside.write_bytes(b"\x89PNG\r\n\x1a\nprivate fixture")
    (captures.official.evidence_dir/"link.png").symlink_to(outside)
    for filename in ("../outside.png","link.png","not_found.png"):
        bad=frame(captures,"b.jpg",evidence_filename=filename)
        with pytest.raises(ValidationError):captures.register({"batch_id":created["id"],"frames":[bad]})


def test_queue_limit_bounds_and_pagination_never_count_images_as_date_coverage(system):
    _,_,_,captures,_,_,_=system
    created=batch(system)
    captures.register({"batch_id":created["id"],"frames":[frame(captures),frame(captures,"b.jpg")]})
    result=captures.pending(created["id"],limit=1,offset=1)
    assert result["total"]==2 and len(result["frames"])==1 and result["verified_days_added"]==0
    with pytest.raises(ValidationError):captures.pending(limit=201)
    with pytest.raises(ValidationError):captures.register({"batch_id":created["id"],"frames":[]})
    with pytest.raises(ValidationError):captures.register({"batch_id":created["id"],"frames":[frame(captures)]*201})


@pytest.fixture
def api(tmp_path):
    app=create_app(tmp_path/"api.sqlite3")
    evidence=tmp_path/"evidence";evidence.mkdir()
    (evidence/"a.png").write_bytes(b"\x89PNG\r\n\x1a\nAPI fixture")
    app.state.official_market.evidence_dir=evidence
    task=app.state.store.save_task(dict(name="14900截图任务",keyword="14900",category="cpu",enabled=True))
    target=app.state.mobile_targets.list()["targets"][0]
    return app,TestClient(app,client=("127.0.0.1",31001)),task,target


def test_local_api_capture_manifest_evidence_and_scope_pending_roundtrip(api):
    app,client,_,target=api
    created=client.post("/api/mobile/capture-batches",json={"catalog_id":target["catalog_id"]}).json()
    assert created["status"]=="needs_scope"
    response=client.post("/api/mobile/captures",json={"batch_id":created["id"],"frames":[frame(app.state.mobile_captures)]})
    assert response.status_code==200
    saved=response.json()["frames"][0]
    assert client.get(saved["evidence_url"]).content==b"\x89PNG\r\n\x1a\nAPI fixture"
    assert client.get("/api/mobile/captures/pending?limit=20").json()["frames"][0]["screen_date"] is None
    assert client.get("/api/mobile/captures/status").json()["frame_status_counts"]=={"queued_scope":1}


def test_capture_api_and_manual_resume_require_same_origin_loopback_json(api,monkeypatch):
    app,client,_,target=api
    app.state.workflow.pause_mobile("先停独立worker，再明确授权MCP截图")
    monkeypatch.setattr(app.state.mobile_runtime,"start",lambda *_:pytest.fail("resume must not start native process"))
    monkeypatch.setattr(app.state.mobile_runtime,"status",lambda *_:pytest.fail("resume must not check device"))
    assert client.post("/api/mobile/capture-batches",json={"catalog_id":target["catalog_id"]}).status_code==409
    resumed=client.post("/api/workflow/mobile/resume",json={"reason":"本人明确采集截图，稍后识别"})
    assert resumed.status_code==200 and not resumed.json()["mobile"]["operator_paused"]
    assert resumed.json()["native_worker_started"] is False and resumed.json()["device_connection_checked"] is False
    assert client.post("/api/mobile/capture-batches",json={"catalog_id":target["catalog_id"]}).status_code==200
    for route,body in [("/api/workflow/mobile/resume",{}),("/api/mobile/capture-batches",{"catalog_id":target["catalog_id"]})]:
        assert client.post(route,json=body,headers={"Origin":"https://example.org"}).status_code==403
        assert client.post(route,content="{}",headers={"Content-Type":"text/plain"}).status_code in {415,422}
    assert client.post("/api/workflow/mobile/resume",json={"command":"start"}).status_code==422
    assert client.post("/api/workflow/mobile/resume?cmd=start",json={}).status_code==422
    remote=TestClient(app,client=("198.51.100.1",31002))
    assert remote.post("/api/workflow/mobile/resume",json={}).status_code==403
    assert remote.get("/api/mobile/captures/pending").status_code==403
    assert remote.post("/api/mobile/capture-batches",json={"catalog_id":target["catalog_id"]}).status_code==403


def saved_frame(system,name="a.png"):
    captures=system[3]
    created=batch(system)
    return captures.register({"batch_id":created["id"],"frames":[frame(captures,name)]})["frames"][0]


def recognition(saved,**changes):
    result=dict(frame_id=saved["id"],evidence_hash=saved["evidence_hash"],observed_model="14900",
        metric="rolling_7d_sold_mean",displayed_mmdd="05.10",price=2500,currency="CNY",readable=True,
        scope_text="品牌 +选择；功能状态 +选择；成色等 +选择",raw_label="05.10 近7日成交均价 ¥2500",notes="原图读数，年份和范围仍待核对")
    result.update(changes)
    return result


def model_result(*results,request_id="session-1",**changes):
    payload=dict(model="gpt-6.1-sol",adapter="codex_session",request_id=request_id,results=list(results))
    payload.update(changes)
    return payload


def test_hot_loop_control_does_not_load_progress_images_or_device_state(system,monkeypatch):
    store,_,targets,captures,_,_,_=system
    created=batch(system)
    before=raw_snapshot(store)
    monkeypatch.setattr(targets,"list",lambda:pytest.fail("must not compute directory coverage"))
    monkeypatch.setattr(captures,"status",lambda:pytest.fail("must not scan archive progress"))
    monkeypatch.setattr(captures.official,"evidence_file",lambda *_:pytest.fail("must not read screenshot"))
    control=captures.control(created["id"])
    assert control["allowed"] is True and control["operator_paused"] is False
    assert control["device_connection_checked"] is False
    assert control["task_ids"]==created["task_ids"] and raw_snapshot(store)==before


@pytest.mark.parametrize("change",["paused","disabled","scope","version","task_links"])
def test_control_immediately_refuses_revoked_capture_batch(system,change):
    store,catalog,_,captures,task,target,paused=system
    created=batch(system)
    if change=="paused":paused[0]=True
    elif change=="disabled":store.save_task({"enabled":False},task["id"])
    elif change in {"scope","version"}:
        row=catalog.get(target["catalog_id"])
        catalog.patch(row["id"],{"scope":{**row["scope"],"condition":"99新"}} if change=="scope" else {"query":"14900"})
    else:store.save_task(dict(name="同型号监控",keyword="14900",category="cpu",enabled=True))
    assert captures.control(created["id"])["allowed"] is False
    assert captures.control(created["id"])["operator_paused"] is (change=="paused")


def test_actual_result_shapes_remain_proposals_without_year_scope_or_canonical_prices(system):
    store,catalog,_,captures,_,target,_=system
    root=captures.official.evidence_dir
    for name in ("c.png","d.png"):(root/name).write_bytes(b"\x89PNG\r\n\x1a\n"+name.encode())
    created=batch(system)
    saved=captures.register({"batch_id":created["id"],"frames":[frame(captures,name) for name in ("a.png","b.jpg","c.png","d.png")]})["frames"]
    before=raw_snapshot(store);before_catalog=catalog.get(target["catalog_id"])
    assert captures.status()["recognition"]["last_request_succeeded"] is False
    results=[recognition(saved[0],displayed_mmdd=None,price=2400),recognition(saved[1],displayed_mmdd=None,price=2400),
             recognition(saved[2]),recognition(saved[3],displayed_mmdd="05.11")]
    response=captures.register_recognitions(model_result(*results))
    assert response["registered"]==4 and response["price_points_created"]==response["verified_days_added"]==0
    assert [row["status"] for row in response["results"]]==["needs_review","needs_review","parsed","parsed"]
    assert all(row["date_year"] is None and row["displayed_date"] is None and not row["canonical_verified"] for row in response["results"])
    assert raw_snapshot(store)==before and catalog.get(target["catalog_id"])==before_catalog
    assert captures.pending()["total"]==0
    archived=captures.frames()["frames"]
    assert len(archived)==4 and all(row["price"] is None for row in archived)
    assert archived[2]["recognition"]["proposed_price"]==2500 and archived[2]["screen_date"] is None
    assert captures.status()["batches"][0]["status"]=="needs_scope"
    state=captures.status()["recognition"]
    assert state["model"]=="gpt-6.1-sol" and state["adapter"]=="codex_session" and state["last_request_succeeded"]
    assert state["registered_call_count"]==1 and state["result_count"]==4
    assert state["state_basis"]=="accepted_local_result_registration" and state["model_calls_executed_by_this_service"]==0


def test_model_results_can_be_processed_after_monitor_and_phone_are_paused(system):
    store,_,_,captures,task,_,paused=system
    saved=saved_frame(system)
    store.save_task({"enabled":False},task["id"]);paused[0]=True
    assert captures.register_recognitions(model_result(recognition(saved)))["registered"]==1
    assert captures.frames()["frames"][0]["recognition"]["proposal_only"] is True


def test_recognition_retry_is_idempotent_new_calls_append_and_old_results_never_overwritten(system):
    captures=system[3];saved=saved_frame(system)
    payload=model_result(recognition(saved))
    first=captures.register_recognitions(payload)
    repeated=captures.register_recognitions(payload)
    assert repeated["registered"]==0 and repeated["existing"]==1
    assert repeated["results"][0]["id"]==first["results"][0]["id"]
    with pytest.raises(ValidationError,match="已有不同结果"):
        captures.register_recognitions(model_result(recognition(saved,price=2600)))
    captures.register_recognitions(model_result(recognition(saved,price=2600),request_id="session-2"))
    history=captures.recognitions(saved["id"])
    assert history["total"]==2 and [row["proposed_price"] for row in history["results"]]==[2600,2500]
    assert captures.frames()["frames"][0]["recognition"]["proposed_price"]==2600
    assert captures.frames()["frames"][0]["price"] is None


@pytest.mark.parametrize("changes",[{"displayed_mmdd":None},{"price":None},{"price":0},{"readable":False}])
def test_overview_missing_value_and_unreadable_results_need_review(system,changes):
    captures=system[3];saved=saved_frame(system)
    response=captures.register_recognitions(model_result(recognition(saved,**changes)))
    assert response["results"][0]["status"]=="needs_review"


@pytest.mark.parametrize("mmdd",["02.29","05-10","05/10"])
def test_month_day_normalization_never_selects_a_year(system,mmdd):
    captures=system[3];saved=saved_frame(system)
    result=captures.register_recognitions(model_result(recognition(saved,displayed_mmdd=mmdd)))["results"][0]
    assert result["displayed_mmdd"]==mmdd.replace("-",".").replace("/",".") and result["date_year"] is None
    assert result["raw_result"]["displayed_mmdd"]==mmdd


@pytest.mark.parametrize("changes",[{"displayed_mmdd":"02.30"},{"displayed_mmdd":"2026-05-10"},{"displayed_mmdd":"0510"},
    {"price":float("nan")},{"price":True},{"price":-1},{"price":10**1000},{"currency":"USD"},
    {"metric":"listing_price"},{"readable":"yes"},{"year":2026},{"scope_verified":True},{"path":"anywhere"}])
def test_invalid_model_fields_are_rejected_without_success_metadata_or_partial_rows(system,changes):
    captures=system[3];saved=saved_frame(system)
    with pytest.raises(ValidationError):captures.register_recognitions(model_result(recognition(saved,**changes)))
    assert captures.status()["recognition"]["last_request_succeeded"] is False
    assert captures.recognitions()["total"]==0 and captures.pending()["total"]==1


@pytest.mark.parametrize("changes",[{"model":"another-model"},{"adapter":"external-api"},{"adapter":[]},
    {"request_id":"../command"},{"command":"run"},{"results":[]}])
def test_result_envelope_is_fixed_and_cannot_execute_paths_or_commands(system,changes):
    captures=system[3];saved=saved_frame(system)
    with pytest.raises(ValidationError):captures.register_recognitions(model_result(recognition(saved),**changes))
    assert captures.status()["recognition"]["registered_call_count"]==0


def test_recognition_bulk_rolls_back_if_second_evidence_hash_is_wrong(system):
    captures=system[3];created=batch(system)
    saved=captures.register({"batch_id":created["id"],"frames":[frame(captures),frame(captures,"b.jpg")]})["frames"]
    with pytest.raises(ValidationError):captures.register_recognitions(model_result(recognition(saved[0]),recognition(saved[1],evidence_hash="0"*64)))
    assert captures.status()["recognition"]["registered_call_count"]==0 and captures.pending()["total"]==2
    (captures.official.evidence_dir/"a.png").write_bytes(b"\x89PNG\r\n\x1a\nreplaced after archival")
    with pytest.raises(ValidationError,match="SHA256"):captures.register_recognitions(model_result(recognition(saved[0])))
    assert captures.recognitions()["total"]==0


def test_recognition_batch_bounds_duplicate_frames_and_cli_adapter_provenance(system):
    captures=system[3];saved=saved_frame(system);row=recognition(saved)
    for rows in ([row]*101,[row,row]):
        with pytest.raises(ValidationError):captures.register_recognitions(model_result(*rows))
    result=captures.register_recognitions(model_result(row,adapter="codex_cli_existing_connection"))
    assert result["results"][0]["adapter"]=="codex_cli_existing_connection"


def test_light_control_and_off_phone_result_api_origin_guards_and_archive(api,monkeypatch):
    app,client,task,target=api
    created=client.post("/api/mobile/capture-batches",json={"catalog_id":target["catalog_id"]}).json()
    monkeypatch.setattr(app.state.workflow,"status",lambda *_:pytest.fail("control must not invoke slow workflow status"))
    control_url=f"/api/mobile/capture-batches/{created['id']}/control"
    assert client.get(control_url).json()["allowed"] is True
    registered=client.post("/api/mobile/captures",json={"batch_id":created["id"],"frames":[frame(app.state.mobile_captures)]}).json()
    saved=registered["frames"][0];payload=model_result(recognition(saved))
    app.state.workflow.pause_mobile("手机拿走，继续识别已有图片")
    app.state.store.save_task({"enabled":False},task["id"])
    assert client.get(control_url).json()["allowed"] is False
    assert client.post("/api/mobile/captures/recognitions",json=payload,headers={"Origin":"https://example.org"}).status_code==403
    assert client.post("/api/mobile/captures/recognitions",content="{}").status_code in {415,422}
    assert client.post("/api/mobile/captures/recognitions",json=payload).status_code==200
    assert client.get("/api/mobile/captures/pending").json()["total"]==0
    archive=client.get("/api/mobile/captures/frames?status=parsed").json()["frames"]
    assert archive[0]["recognition"]["proposed_price"]==2500 and archive[0]["price"] is None
    assert client.get(f"/api/mobile/captures/recognitions?frame_id={saved['id']}").json()["total"]==1
    remote=TestClient(app,client=("198.51.100.1",31002))
    assert remote.get(control_url).status_code==403
    assert remote.post("/api/mobile/captures/recognitions",json=payload).status_code==403
