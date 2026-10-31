"""Estimated image shape indices use temporary evidence and never phone input."""
from copy import deepcopy
from datetime import datetime,timedelta,timezone
import hashlib
import json

import pytest
from fastapi.testclient import TestClient

import app.image_trends as module
from app.catalog import Catalog
from app.image_trends import ImageTrends,estimated_axis,shape_score
from app.jobs import LeaseError,MobileJobs
from app.mobile_captures import MobileCaptures
from app.mobile_targets import MobileTargets
from app.normalization import ValidationError,iso
from app.official_market import OfficialMarket
from app.service import Store

NOW=datetime(2026,10,30,12,tzinfo=timezone.utc)


@pytest.fixture
def system(tmp_path,monkeypatch):
    monkeypatch.setattr(module,"utcnow",lambda:NOW)
    store=Store(tmp_path/"trends.sqlite3",production=True)
    catalog=Catalog(store);targets=MobileTargets(store,catalog)
    evidence=tmp_path/"evidence";evidence.mkdir()
    official=OfficialMarket(store,evidence);MobileJobs(store,catalog,official)
    captures=MobileCaptures(store,targets,official)
    trends=ImageTrends(store,targets,captures)
    return store,catalog,targets,captures,trends


def add_target(system,keyword="14900",category="cpu",condition=None):
    store,catalog,targets,captures,trends=system
    task=store.save_task(dict(name=keyword,keyword=keyword,category=category,enabled=True))
    target=next(row for row in targets.list()["targets"] if task["id"] in row["task_ids"])
    if condition:
        row=catalog.get(target["catalog_id"])
        catalog.patch(row["id"],{"scope":{**row["scope"],"condition":condition}})
        target=next(row for row in targets.list()["targets"] if task["id"] in row["task_ids"])
    batch=captures.create_batch({"catalog_id":target["catalog_id"]})
    filename=f"mobile_{task['id']}.png"
    data=b"\x89PNG\r\n\x1a\n"+filename.encode()
    (captures.official.evidence_dir/filename).write_bytes(data)
    frame=captures.register({"batch_id":batch["id"],"frames":[dict(evidence_filename=filename,evidence_hash=hashlib.sha256(data).hexdigest(),
        captured_at=iso(NOW-timedelta(hours=12)),chart_x=None,screen_date=None)]})["frames"][0]
    return task,target,frame


def payload(frame,direction="up",kind="price_curve",end="11.01"):
    values=[25+index*2 for index in range(21)] if direction=="up" else [65-index*2 for index in range(21)] if direction=="down" else [50]*21
    return dict(frame_id=frame["id"],source_hash=frame["evidence_hash"],extractor_version="pixel-v1",signal_kind=kind,
        quality=dict(sampled_columns=100,valid_columns=100,column_coverage=1,trace_gap_max_px=0),
        points=[dict(x=index/20,level=value) for index,value in enumerate(values)],
        axis_labels=[dict(x=.02,label="04.28"),dict(x=.97,label=end)],header_price=2400,
        raw_metric_label="近7日成交均价" if kind=="price_curve" else "搜索热度",notes="原图像素追踪；非人民币涨跌幅")


def snapshot(store):
    with store.db() as db:
        return {name:[tuple(row) for row in db.execute(f"SELECT * FROM {name} ORDER BY rowid")]
                for name in ("items","observations","catalog","official_points","mobile_points","mobile_readings")}


def test_import_and_build_preserve_raw_prices_and_unverified_scope(system):
    store,catalog,_,_,trends=system
    _,target,frame=add_target(system)
    before=snapshot(store)
    created=trends.import_extract(payload(frame))
    assert created["created"] and trends.import_extract(payload(frame))["created"] is False
    item=trends.list()["items"][0]
    assert item["estimated_scope"] is True and item["current_score"]>50
    assert item["score_x_basis"]=="observed_trace_extent" and item["price_change_pct"] is None
    assert item["eligibility"]["date_axis"]["period_end_estimated"]=="2026-11-01"
    assert item["eligibility"]["date_axis"]["canonical_dates"] is False
    built=trends.rebuild()
    assert built["market_index"]["score"]>50 and built["composite_index"]["score"]==built["market_index"]["score"]
    assert built["composite_index"]["coverage"]==.7
    assert [x["effective_weight"] for x in built["composite_index"]["components"]]==[1,0]
    assert snapshot(store)==before and catalog.get(target["catalog_id"])["scope"]["verified"] is False


def test_observed_trace_extent_normalization_and_recent_momentum_are_explicit():
    points=[dict(x=i/20,level=80 if i<17 else 80-(i-16)*15) for i in range(21)]
    score,components=shape_score(points)
    assert score<50 and components[1]["score"]<components[0]["score"]
    shifted=[dict(x=.04+.91*p["x"],level=p["level"]) for p in points]
    shifted_score,_=shape_score(shifted)
    assert shifted_score==pytest.approx(score)
    assert [p["nominal_weight"] for p in components]==[.4,.6]
    assert shape_score([dict(x=i/20,level=50) for i in range(21)])[0]==50
    assert shape_score([])[0] is None


def test_stale_4090_axis_and_heat_remain_visible_but_only_three_curves_enter_fixed_basket(system):
    _,_,_,_,trends=system
    for keyword,category,kind,end in [("14900","cpu","price_curve","11.01"),("RTX4090","gpu","price_curve","06.01"),
        ("5070ti","gpu","price_curve","11.01"),("iPhone15 256GB","phone","price_curve","11.01"),("DDR5 32G","ram","popularity_only","10.29")]:
        _,_,frame=add_target(system,keyword,category)
        data=payload(frame,kind=kind,end=end)
        if end=="06.01":data["axis_labels"][0]["label"]="05.02"
        trends.import_extract(data)
    result=trends.rebuild()
    market=result["market_index"]
    assert market["member_count"]==market["expected_target_count"]==5
    assert market["available_price_curve_count"]==3 and market["captured_price_curve_count"]==4
    assert market["task_coverage"]==.6 and market["coverage"]==pytest.approx(.625)
    members={r["model"]:r for r in market["members"]}
    assert members["i9-14900"]["effective_weight"]==pytest.approx(.4)
    assert members["RTX 5070 TI"]["effective_weight"]==pytest.approx(.2)
    assert members["iPhone 15 256GB"]["effective_weight"]==pytest.approx(.4)
    assert members["RTX 4090"]["score"] is None and members["DDR5 32GB"]["score"] is None
    assert {r["code"] for r in members["RTX 4090"]["reasons"]}=={"stale_axis"}
    assert members["DDR5 32GB"]["reasons"][0]["code"]=="popularity_only"
    assert next(r for r in trends.list()["items"] if r["model"]=="RTX 4090")["points"]


@pytest.mark.parametrize("change,code",[("quality","low_geometric_quality"),("short","short_history"),("axis_short","short_axis"),
    ("future","future_axis"),("axis_missing","axis_unresolved"),("label","metric_unproven")])
def test_quality_history_and_axis_gates_leave_missing_values_not_neutral_fifty(system,change,code):
    trends=system[4];_,_,frame=add_target(system);data=payload(frame)
    if change=="quality":data["quality"].update(valid_columns=20,column_coverage=.2,trace_gap_max_px=60)
    elif change=="short":data["points"]=data["points"][::5]
    elif change=="axis_short":data["axis_labels"]=[dict(x=0,label="10.20"),dict(x=1,label="11.01")]
    elif change=="future":data["axis_labels"][-1]["label"]="12.31"
    elif change=="axis_missing":data["axis_labels"]=[]
    else:data["raw_metric_label"]="搜索热度"
    trends.import_extract(data);item=trends.list()["items"][0]
    assert code in {r["code"] for r in item["eligibility"]["reasons"]}
    assert item["current_score"] is None
    assert trends.rebuild()["composite_index"]["score"] is None


def test_source_age_and_hash_loss_do_not_reuse_an_old_frozen_score_as_current(system,monkeypatch):
    _,_,_,captures,trends=system
    _,_,frame=add_target(system);trends.import_extract(payload(frame));built=trends.rebuild()
    assert trends.get()["pending_rebuild"] is False
    monkeypatch.setattr(module,"utcnow",lambda:NOW+timedelta(days=8))
    assert "stale_source" in {r["code"] for r in trends.list()["items"][0]["eligibility"]["reasons"]}
    current=trends.get()
    assert current["pending_rebuild"] and current["market_index"]["score"] is None
    assert trends.replay(built["build_id"])["market_index"]["score"]==built["market_index"]["score"]
    (captures.official.evidence_dir/frame["evidence_filename"]).write_bytes(b"\x89PNG\r\n\x1a\nchanged")
    assert "source_unavailable" in {r["code"] for r in trends.list()["items"][0]["eligibility"]["reasons"]}


@pytest.mark.parametrize("change",["disable","scope","version"])
def test_current_task_scope_and_version_gate_import_and_current_index(system,change):
    store,catalog,_,_,trends=system
    task,target,frame=add_target(system);trends.import_extract(payload(frame));built=trends.rebuild()
    if change=="disable":store.save_task({"enabled":False},task["id"])
    else:
        row=catalog.get(target["catalog_id"])
        catalog.patch(row["id"],{"scope":{**row["scope"],"condition":"99新"}} if change=="scope" else {"query":"14900"})
    with pytest.raises(LeaseError):trends.import_extract(payload(frame))
    assert trends.get()["market_index"]["score"] is None
    assert trends.replay(built["build_id"])["input_hash"]==built["input_hash"]


def test_listing_factor_uses_audited_latest_live_exact_model_and_never_blends_phone_conditions(system):
    store,_,_,_,trends=system
    _,_,cpu=add_target(system);trends.import_extract(payload(cpu))
    _,_,phone=add_target(system,"iPhone15 256GB","phone",condition="少量磕碰划痕");trends.import_extract(payload(phone))
    for index in range(6):
        store.ingest([dict(item_id=f"cpu-{index}",title="i9-14900 CPU散片 单CPU出售",price=2400+index*20,observed_at=iso(NOW-timedelta(hours=1)))],"live")
        store.ingest([dict(item_id=f"phone-{index}",title="iPhone 15 256GB 无锁无拆修",price=2200+index*20,observed_at=iso(NOW-timedelta(hours=1)))],"live")
    store.ingest([dict(item_id="host",title="CPU:i9-14900 主板:B650M 内存:DDR5 32G 整机",model="i9-14900",category="cpu",price=9900,observed_at=iso(NOW))],"live")
    store.price_audit.apply()
    result=trends.rebuild();listing=result["composite_index"]["listing_members"]
    cpu_row=next(x for x in listing if x["model"]=="i9-14900")
    phone_row=next(x for x in listing if x["model"]=="iPhone 15 256GB")
    assert cpu_row["sample_count"]==6 and cpu_row["score"] is not None
    assert phone_row["score"] is None and "scope_unmatched" in phone_row["reason"]
    assert [x["effective_weight"] for x in result["composite_index"]["components"]]==[.7,.3]
    assert result["composite_index"]["listing_coverage"]==.5
    assert result["status"]==result["composite_index"]["status"]=="partial_estimate"
    assert result["market_index"]["coverage"]==1
    assert result["composite_index"]["factor_coverage_pct"]==100
    assert result["composite_index"]["source_coverage_pct"]==85


def test_factor_coverage_and_weighted_source_coverage_are_distinct_without_score_reweighting(system):
    store,_,_,_,trends=system
    for keyword,category,kind,end in [("14900","cpu","price_curve","11.01"),("RTX4090","gpu","price_curve","06.01"),
        ("5070ti","gpu","price_curve","11.01"),("iPhone15 256GB","phone","price_curve","11.01"),("DDR5 32G","ram","popularity_only","10.29")]:
        _,_,frame=add_target(system,keyword,category)
        data=payload(frame,kind=kind,end=end)
        if end=="06.01":data["axis_labels"][0]["label"]="05.02"
        trends.import_extract(data)
    for i in range(5):store.ingest([dict(item_id=f"cpu-listing{i}",title="i9-14900 CPU散片",price=2400+i*50,observed_at=iso(NOW))],"live")
    result=trends.rebuild();composite=result["composite_index"]
    assert result["market_index"]["coverage"]==.625 and composite["listing_coverage"]==.25
    assert composite["coverage_pct"]==composite["factor_coverage_pct"]==100
    assert composite["source_coverage_pct"]==51.25
    assert [row["effective_weight"] for row in composite["components"]]==[.7,.3]
    assert composite["score"]==pytest.approx(.7*composite["components"][0]["score"]+.3*composite["components"][1]["score"],abs=.0001)


def test_listing_alone_never_creates_composite_and_no_data_never_fills_fifty(system):
    store,_,_,_,trends=system
    add_target(system)
    for i in range(6):store.ingest([dict(item_id=f"quote{i}",title="i9-14900 CPU散片",price=2500+i,observed_at=iso(NOW))],"live")
    result=trends.rebuild()
    assert result["market_index"]["score"] is None and result["composite_index"]["score"] is None
    assert result["composite_index"]["components"][1]["score"] is not None


def test_actual_image_condition_blocks_proxy_even_when_expected_catalog_scope_is_broad(system):
    store,_,_,captures,trends=system
    _,_,frame=add_target(system,"RTX4090","gpu")
    data=payload(frame)
    trends.import_extract(data)
    captures.register_recognitions(dict(model="gpt-6.1-sol",adapter="codex_session",request_id="scope-proof",
        results=[dict(frame_id=frame["id"],evidence_hash=frame["evidence_hash"],observed_model="RTX4090",
        metric="rolling_7d_sold_mean",displayed_mmdd=None,price=2400,currency="CNY",readable=True,
        scope_text="成色 少量磕碰划痕",raw_label="近7日成交均价",notes="仅范围原文")]))
    for i in range(6):store.ingest([dict(item_id=f"card{i}",title="RTX 4090 显卡",price=12000+i*100,observed_at=iso(NOW))],"live")
    built=trends.rebuild();row=built["composite_index"]["listing_members"][0]
    assert row["sample_count"]==6 and row["score"] is None and "scope_unmatched" in row["reason"]
    assert row["observed_scope_text"]=="成色 少量磕碰划痕"
    assert trends.list()["items"][0]["observed_scope_text"]=="成色 少量磕碰划痕"


def test_unreviewed_phone_scope_cannot_blend_all_conditions_even_without_recognition(system):
    store,_,_,_,trends=system
    _,_,frame=add_target(system,"iPhone15 256GB","phone")
    data=payload(frame);data["notes"]="存储容量256GB；成色 少量磕碰划痕"
    trends.import_extract(data)
    for i in range(6):store.ingest([dict(item_id=f"phone{i}",title="iPhone 15 256GB 无锁无拆修",price=2200+i*20,observed_at=iso(NOW))],"live")
    built=trends.rebuild()
    assert built["composite_index"]["listing_members"][0]["score"] is None
    assert "scope_unmatched" in built["composite_index"]["listing_members"][0]["reason"]


def test_frozen_result_view_verifies_input_and_config_hash_and_does_not_claim_recalculation(system):
    store,_,_,_,trends=system
    _,_,frame=add_target(system);trends.import_extract(payload(frame));built=trends.rebuild()
    viewed=trends.replay(built["build_id"])
    assert viewed["replay_kind"]=="frozen_result" and viewed["frozen_integrity_verified"]
    with store.db() as db:db.execute("UPDATE image_trend_index_builds SET config='{}' WHERE id=?",(built["build_id"],))
    with pytest.raises(ValidationError,match="哈希"):trends.replay(built["build_id"])


def test_versioned_extraction_never_overwrites_old_curve_or_accepts_fabricated_units(system):
    trends=system[4];_,_,frame=add_target(system);data=payload(frame)
    trends.import_extract(data)
    changed=deepcopy(data);changed["points"][0]["level"]+=1
    with pytest.raises(ValidationError,match="新extractor_version"):trends.import_extract(changed)
    changed["extractor_version"]="pixel-v2"
    assert trends.import_extract(changed)["created"]
    for bad in ({**data,"confidence":.99},{**data,"source_hash":"0"*64},{**data,"points":[dict(x=0,level=1000),dict(x=1,level=2000)]}):
        with pytest.raises(ValidationError):trends.import_extract(bad)


def test_monthday_axis_is_only_near_year_metadata_and_allows_normal_future_tick():
    axis=estimated_axis([dict(x=0,label="04.28"),dict(x=1,label="11.01")],iso(NOW))
    assert axis["axis_end_lag_days"]==-2 and axis["date_precision"]=="estimated_monthday" and not axis["canonical_dates"]
    assert estimated_axis([dict(x=0,label="05.02"),dict(x=1,label="06.01")],iso(NOW))["axis_end_lag_days"]==151


def test_image_api_and_frozen_build_replay_are_local_and_do_not_clip_original_window(tmp_path,monkeypatch):
    from app.main import create_app
    monkeypatch.setattr(module,"utcnow",lambda:NOW)
    app=create_app(tmp_path/"api.sqlite3")
    root=tmp_path/"evidence";root.mkdir();app.state.official_market.evidence_dir=root
    system=(app.state.store,app.state.catalog,app.state.mobile_targets,app.state.mobile_captures,app.state.image_trends)
    _,_,frame=add_target(system);data=payload(frame)
    client=TestClient(app,client=("127.0.0.1",31001))
    assert client.post("/api/image-trends",json=data).status_code==200
    assert client.get("/api/image-trends?category=cpu").json()["items"][0]["points"]==data["points"]
    assert client.get("/api/image-trends?days=7").status_code==422
    assert client.get("/api/trend-indices").json()["market_index"]["score"] is None
    built=client.post("/api/trend-indices/rebuild",json={}).json()
    assert built["build_id"] and client.get("/api/trend-indices").json()["pending_rebuild"] is False
    assert client.get(f"/api/trend-indices/builds/{built['build_id']}").json()["input_hash"]==built["input_hash"]
    assert client.post("/api/trend-indices/rebuild",json={"command":"anything"}).status_code==422
    assert client.post("/api/image-trends",json=data,headers={"Origin":"https://example.org"}).status_code==403
    remote=TestClient(app,client=("198.51.100.1",31002))
    assert remote.get("/api/image-trends").status_code==403
