import json
from datetime import timedelta

import pytest

from app.indices import IndexService
from app.normalization import iso, utcnow
from app.price_audit import PriceAudit, VERSION, assess
from app.service import Store


@pytest.mark.parametrize("title,category,code", [
    ("R7 7800X3D+5070 12G 黑色海景房主机", "cpu", "complete_computer_offer"),
    ("出自用高配主机，AMD R7 7800X3D", "cpu", "complete_computer_offer"),
    ("R7 7800X3D 七彩虹5070 12G战斧显卡，不议价不包邮，准新机器质保两年", "cpu", "complete_computer_offer"),
    ("CPU：R7 7800X3D 主板：B650M 内存：DDR5 32G", "cpu", "multi_component_offer"),
    ("R7 7800X3D 4070显卡 DDR5 32G内存 1T固态", "cpu", "multi_component_offer"),
    ("i9 14900KF Z790主板 RTX4090显卡 DDR5 32G内存打包出售，支持自提", "cpu", "multi_component_offer"),
    ("仅出售CPU+主板套装，i9-14900KF+B760M，整套价格", "cpu", "multi_component_offer"),
    ("i9-14900KF+B760M 板U组合，整套价格", "cpu", "multi_component_offer"),
    ("R7 7800X3D 7个。", "cpu", "bulk_quote_unit_unknown"),
    ("DDR5 32G 拆机内存 62根打包出", "ram", "bulk_quote_unit_unknown"),
    ("RTX4090 24GB显卡，两张打包出售", "gpu", "bulk_quote_unit_unknown"),
    ("DDR5 16G×2套条，两套打包出售", "ram", "bulk_quote_unit_unknown"),
    ("DDR5 16G×2套条，2套打包出售", "ram", "bulk_quote_unit_unknown"),
    ("DDR5 32G套条16G×2套装，很新单个价格，两根打包包邮", "ram", "kit_unit_quote_conflict"),
    ("DDR5 16G×2套条，标价单条价", "ram", "kit_unit_quote_conflict"),
])
def test_clear_noncomparable_offers_are_excluded_without_price_heuristics(title, category, code):
    assert code in {reason[0] for reason in assess(title, category)}


@pytest.mark.parametrize("title,category", [
    ("从主机拆下 DDR5 16G×2", "ram"),
    ("主机上正常拆机下来的 DDR5 32G内存", "ram"),
    ("RTX4070 SUPER 12G 显卡，来源客户整机拆机件，裸卡出售", "gpu"),
    ("RTX4090 显卡支持光追，ROG主机拆机显卡，保修到29年", "gpu"),
    ("本来想组整机但没组所以出显卡 RTX4090 24GB", "gpu"),
    ("CPU散片 i9-14900KF", "cpu"),
    ("仅出售CPU，主板显卡不卖，仅兼容说明", "cpu"),
    ("i9-14900KF散片，2025年板U套装购入，搭配Z790主板。单CPU出售，不含主板", "cpu"),
    ("i9-14900KF散片，适合台式机升级或组装主机用，兼容主流主板。支持自提", "cpu"),
    ("i9-14900 CPU，自带核显770，支持D4D5内存，主板支持才拍", "cpu"),
    ("华硕B650M-F主板，支持锐龙7000/8000/9000处理器，双DDR5内存槽，带M.2固态位", "motherboard"),
    ("B650M主板支持AM D 7000/9000 CPU，DDR5内存，M.2接口", "motherboard"),
    ("DDR5 32G套条，单根16G一共32G，标价套装价格", "ram"),
    ("DDR5 16G×2套条，标价不是单条价，是整套价格", "ram"),
    ("DDR5 32GB(16GB+16GB)，低价处理50套，支持零售，标价1套", "ram"),
    ("翔升RTX4070S，一共有5张，4800元一个", "gpu"),
    ("DDR5 16G×2套条，现货50套，每套2300元", "ram"),
    ("DDR5 16G×2套条，现货50套，2300元/套", "ram"),
    ("RTX4090 24G两张，单张18000元", "gpu"),
    ("高配游戏主机 CPU7800X3D RTX5070 DDR5 32G", "computer"),
])
def test_provenance_single_units_compatibility_and_explicit_unit_quotes_not_newly_excluded(title, category):
    assert assess(title, category) == []


@pytest.mark.parametrize("amount", ["2900", "3000", "3060", "4090", "5070"])
def test_currency_in_title_cannot_be_mistaken_for_gpu_model(amount):
    assert not assess(f"i9-14900KF处理器，主板支持才拍，支持DDR5内存，标价{amount}元", "cpu")


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path/"audit.sqlite3", production=True)


def add(store, item_id, title, price, days=1):
    return store.ingest([dict(item_id=item_id, title=title, category="cpu", model="i9-14900KF", price=price,
                              observed_at=iso(utcnow()-timedelta(days=days)))])


def raw_snapshot(store):
    with store.db() as db:
        return {table: [tuple(row) for row in db.execute(f"SELECT * FROM {table} ORDER BY rowid")]
                for table in ("items", "observations", "tasks", "runs")}


def test_preview_apply_rollback_only_touch_derived_tables_and_price_views_agree(store):
    add(store, "single", "i9-14900KF散片", 3000)
    add(store, "host", "出自用高配主机 i9-14900KF", 11000)
    before = raw_snapshot(store)
    audit = store.price_audit
    preview = audit.preview()
    assert preview["affected_items"] == 1
    assert preview["changes"][0]["price"] == 11000
    assert raw_snapshot(store) == before
    assert store.overview("live")["summary"]["median_price"] == 7000
    applied = audit.apply()
    assert applied["version"] == VERSION
    assert raw_snapshot(store) == before
    assert store.overview("live")["summary"]["median_price"] == 3000
    assert store.overview("live")["models"][0]["median_price"] == 3000
    assert len(store.filtered_items(source="live")) == 1
    all_items = store.filtered_items(source="live", include_excluded=True)
    host = next(item for item in all_items if item["item_id"] == "host")
    assert host["excluded"] and not host["raw_excluded"]
    assert host["price_audit_flags"] and host["quality_flags"] and host["price"] == 11000
    points = [p for p in store.price_trends("live")["aggregate"] if p["samples"]]
    assert len(points) == 1 and points[0]["median_price"] == 3000 and points[0]["samples"] == 1
    assert audit.apply()["revision"] == applied["revision"]  # idempotent
    rolled = audit.rollback()
    assert rolled["version"] is None and rolled["revision"] == applied["revision"]+1
    assert raw_snapshot(store) == before
    assert store.overview("live")["summary"]["median_price"] == 7000
    with store.db() as db:
        assert db.execute("SELECT count(*) FROM price_eligibility_audit").fetchone()[0] == 2


def test_new_ingestion_audited_and_manual_status_does_not_copy_derived_flags_to_raw(store):
    store.price_audit.apply()
    add(store, "host", "出自用高配主机 i9-14900KF", 11000)
    item = store.filtered_items(source="live", include_excluded=True)[0]
    assert item["excluded"] and not item["raw_excluded"]
    store.set_status(item["id"], "sold", "人工看见明确已售出")
    with store.db() as db:
        raw = db.execute("SELECT excluded,quality_flags FROM observations WHERE item_pk=? ORDER BY observed_at DESC LIMIT 1", (item["id"],)).fetchone()
        assert raw["excluded"] == 0 and json.loads(raw["quality_flags"]) == []
        assert db.execute("SELECT count(*) FROM price_eligibility_audit").fetchone()[0] == 2


def test_existing_raw_exclusions_are_never_cleared_by_positive_audit(store):
    add(store, "fault", "i9-14900KF故障维修件", 300)
    store.price_audit.apply()
    item = store.filtered_items(source="live", include_excluded=True)[0]
    assert item["raw_excluded"] and item["excluded"]
    assert item["price_audit_flags"] == []


def test_indices_hash_and_build_replay_freeze_eligibility_revision(store):
    add(store, "single", "i9-14900KF散片", 3000)
    add(store, "host", "出自用高配主机 i9-14900KF", 11000)
    index = IndexService(store)
    old = index.rebuild(days=7)
    old_replay = index.replay(old["id"])
    store.price_audit.apply()
    pending = index.get(days=7)
    assert pending["coverage"]["version_status"] == "pending_rebuild"
    assert pending["input_hash"] != old["input_hash"]
    assert pending["methodology"]["price_eligibility_audit"]["version"] == VERSION
    with store.db() as db:
        assert db.execute("SELECT count(*) FROM index_builds").fetchone()[0] == 1
    after = index.rebuild(days=7)
    assert after["id"] != old["id"] and after["input_hash"] != old["input_hash"]
    assert after["version"] != old["version"]
    assert index.replay(old["id"])["input_hash"] == old_replay["input_hash"]
    assert index.replay(old["id"])["price_indices"] == old_replay["price_indices"]
    assert index.replay(after["id"])["input_hash"] == after["input_hash"]
