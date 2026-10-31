import asyncio
import importlib
import time
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.collector import CollectorError
from app.normalization import SHANGHAI, ValidationError, infer_hardware, iso, normalize_record, utcnow
from app.service import Store


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "market.sqlite3")


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(tmp_path / "api.sqlite3")) as c:
        yield c


def row(item_id="one", days=1, **overrides):
    return {"item_id": item_id, "title": "华硕RTX4090显卡24G自用", "price": 12000,
            "observed_at": iso(utcnow() - timedelta(days=days)), **overrides}


@pytest.mark.parametrize("title,category,model", [
    ("华硕RTX4090显卡", "gpu", "RTX 4090"),
    ("RTX409024G自用", "gpu", "RTX 4090 24GB"),
    ("微星RTX4070TISUPER16G显卡", "gpu", "RTX 4070 TI SUPER 16GB"),
    ("RTX 4070 Ti Super 16G", "gpu", "RTX 4070 TI SUPER 16GB"),
    ("蓝宝石RX7900XTX24G自用", "gpu", "RX 7900 XTX 24GB"),
    ("蓝戟ARCA77016G显卡", "gpu", "Arc A770 16GB"),
    ("金士顿DDR5内存16G×2 6000", "ram", "DDR5 32GB (16GB×2) 6000"),
    ("DDR5内存32G单条6000", "ram", "DDR5 32GB 6000"),
    ("DDR4 16G 3200内存", "ram", "DDR4 16GB 3200"),
    ("海力士DDR532G台式内存", "ram", "DDR5 32GB"),
    ("DDR532G", "ram", "DDR5 32GB"),
    ("DDR516Gx2套条", "ram", "DDR5 32GB (16GB×2)"),
    ("处理器i5-12400F自用", "cpu", "i5-12400F"),
    ("AMD锐龙R77800X3D处理器", "cpu", "Ryzen 7800X3D"),
    ("微星B650M迫击炮主板", "motherboard", "B650M"),
    ("三星990PRO 2TB固态SSD", "storage", "SSD 2TB"),
    ("STM32F103C8T6开发板", "component", "STM32F103C8T6"),
    ("原装ESP32-S3开发板", "component", "ESP32-S3"),
    ("树莓派RP2040开发板", "component", "RP2040"),
])
def test_hardware_normalization(title, category, model):
    assert infer_hardware(title) == (category, model)


def test_target_model_never_relabels_unrelated_results():
    item = normalize_record(row(title="RTX 4070 SUPER 12G显卡"), {"category": "gpu", "model": "RTX 4090"})
    assert item["model"] == "RTX 4070 SUPER 12GB"
    assert item["excluded"]
    matching = normalize_record(row(title="RTX409024G自用"), {"category": "gpu", "model": "RTX4090"})
    assert not matching["excluded"]


@pytest.mark.parametrize("title", [
    "映众iCHILL RTX4070 SUPER超级冰龙显卡，1 2G显存，三风扇散热",
    "RTX4090显卡 2 4GB显存 自用闲置",
])
def test_split_vram_digits_are_uncertain_not_trailing_digit_capacity(title):
    category, model = infer_hardware(title)
    assert category == "gpu"
    assert "显存规格待确认" in model
    assert not model.endswith(("2GB", "4GB"))
    item = normalize_record(row(title=title))
    assert item["excluded"]
    assert "型号/规格待确认，暂不合并行情" in item["quality_flags"]


def test_memory_capacity_never_truncates_a_timing_token_into_kit_size():
    title = "金百达银爵6000c3616gx2 32G套装条内存条ddr 5，刚拆机出来，实物拍摄 包好正常使用！拍下包邮！"
    category, model = infer_hardware(title)
    assert category == "ram"
    assert model == "DDR5 32GB"
    assert "1232GB" not in model and "616GB" not in model
    item = normalize_record(row(title=title, price=600))
    assert item["model"] == "DDR5 32GB"
    assert not item["excluded"]


def test_memory_capacity_embedded_in_timing_without_independent_capacity_stays_unknown():
    item = normalize_record(row(title="DDR5内存6000C3616GX2套装", price=600))
    assert "容量待确认" in item["model"]
    assert item["excluded"]


@pytest.mark.parametrize("title,model", [
    ("苹果iPhone15 256G无锁", "iPhone 15 256GB"),
    ("苹果15promax512G国行", "iPhone 15 PRO MAX 512GB"),
    ("iphone15Pro1TB", "iPhone 15 PRO 1TB"),
    ("iPhone15 Plus 128GB", "iPhone 15 PLUS 128GB"),
    ("华为Mate60Pro 12G+256G", "Huawei MATE60 PRO 256GB"),
    ("华为Pura70Pro 512GB", "Huawei PURA70 PRO 512GB"),
    ("红米K70 12+256", "Redmi K70 256GB"),
    ("小米14 Ultra 16+512G", "Xiaomi 14 ULTRA 512GB"),
    ("三星Galaxy S24Ultra256G", "Samsung S24 ULTRA 256GB"),
    ("vivoX100Pro 512G", "Vivo X100 PRO 512GB"),
    ("iQOO12 Pro 256G", "iQOO 12 PRO 256GB"),
    ("OPPO Find X7 Ultra 16+512G", "Oppo FINDX7 ULTRA 512GB"),
    ("一加12 256G", "OnePlus 12 256GB"),
    ("荣耀Magic6 Pro 512GB", "Honor MAGIC6 PRO 512GB"),
])
def test_phone_model_variant_and_storage_normalization(title, model):
    assert infer_hardware(title) == ("phone", model)


@pytest.mark.parametrize("suffix", ["手机壳", "贴膜", "屏幕总成", "官解", "有锁", "卡贴机", "监管机", "换屏", "烧屏", "128/256G"])
def test_phone_accessories_problem_units_and_mixed_capacity_are_excluded(suffix):
    item = normalize_record(row(title=f"iPhone15 256GB {suffix}", price=3000), {"category": "phone"})
    assert item["excluded"]


def test_clean_unlocked_phone_is_eligible_and_unknown_storage_is_reviewable():
    good = normalize_record(row(title="iPhone15 256GB 无锁无拆修无进水 送手机壳", price=3000))
    assert good["category"] == "phone" and not good["excluded"]
    unknown = normalize_record(row(title="iPhone15 手机国行", price=3000), {"category": "phone", "model": "iPhone15"})
    assert unknown["excluded"] and "容量待确认" in unknown["model"]


def test_phone_preset_migration_is_idempotent_and_preserves_existing_tasks(store):
    original = store.tasks()[0]
    store.save_task({"name": "保留用户设置", "enabled": True, "interval_minutes": 180}, original["id"])
    with store.db() as db:
        db.execute("DELETE FROM meta WHERE key='phone_preset_v1'")
        db.execute("DELETE FROM tasks WHERE category='phone'")
    store.initialize()
    phone = [task for task in store.tasks() if task["category"] == "phone"]
    assert len(phone) == 1
    assert phone[0]["keyword"] == "iPhone 15 256GB" and not phone[0]["enabled"]
    user = store.get_task(original["id"])
    assert user["name"] == "保留用户设置" and user["enabled"] and user["interval_minutes"] == 180
    store.initialize()
    assert len([task for task in store.tasks() if task["category"] == "phone"]) == 1
    store.delete_task(phone[0]["id"])
    store.initialize()
    assert not [task for task in store.tasks() if task["category"] == "phone"]


def test_price_trends_empty_half_year_never_fabricates_history(store):
    empty = store.price_trends("live", days=180)
    assert empty["basis"] == "listing"
    assert empty["coverage"]["observed_days"] == 0
    assert empty["coverage"]["requested_days"] == 180
    assert empty["coverage"]["first_observed_at"] is None
    assert len(empty["aggregate"]) == 180 and empty["series"] == []
    assert all(p["samples"] == 0 and p["median_price"] is None and p["mean_price"] is None for p in empty["aggregate"])
    current = utcnow() - timedelta(minutes=1)
    store.ingest([row("new", observed_at=iso(current), published_at=iso(current - timedelta(days=170)))])
    actual = store.price_trends("live", days=180)
    assert actual["coverage"]["observed_days"] == 1
    assert actual["coverage"]["first_observed_at"] == iso(current)
    assert sum(point["samples"] > 0 for point in actual["aggregate"]) == 1
    assert actual["series"][0]["change_pct"] is None


def test_price_trends_daily_last_snapshot_statistics_and_real_change(store):
    local = utcnow().astimezone(SHANGHAI)
    previous = (local - timedelta(days=2)).replace(hour=10, minute=0, second=0, microsecond=0)
    last_day = (local - timedelta(days=1)).replace(hour=10, minute=0, second=0, microsecond=0)
    store.ingest([row("a", observed_at=iso(previous), price=100),
                  row("a", observed_at=iso(previous + timedelta(minutes=1)), price=200),
                  row("b", observed_at=iso(previous), price=400),
                  row("c", observed_at=iso(previous), price=600),
                  row("d", observed_at=iso(previous), price=900),
                  row("a", observed_at=iso(last_day), price=600)])
    result = store.price_trends("live", days=7)
    first = next(point for point in result["aggregate"] if point["date"] == previous.date().isoformat())
    assert first == dict(date=previous.date().isoformat(), samples=4, min_price=200, p25_price=350,
                         median_price=500, mean_price=525, p75_price=675, max_price=900)
    series = result["series"][0]
    assert series["total_items"] == 4 and series["observed_days"] == 2
    assert series["latest_price"] == 600 and series["change_pct"] == 20


def test_price_trends_preserves_historical_metadata_after_reclassification_or_sale(store):
    old = (utcnow().astimezone(SHANGHAI) - timedelta(days=2)).replace(hour=10, minute=0, second=0, microsecond=0)
    newer = old + timedelta(days=1)
    store.ingest([row("changed", observed_at=iso(old), price=200), row("sold", observed_at=iso(old), price=400)])
    store.ingest([row("changed", title="iPhone15 256GB", category="phone", observed_at=iso(newer), price=3000),
                  row("sold", observed_at=iso(newer), status="sold", evidence="详情已售出")])
    gpu = store.price_trends("live", days=7, category="gpu")
    assert len(gpu["series"]) == 1
    assert gpu["series"][0]["total_items"] == 2
    assert gpu["coverage"]["observed_days"] == 1
    assert gpu["series"][0]["latest_price"] == 300
    phone = store.price_trends("live", days=7, category="phone")
    assert phone["series"][0]["model"] == "iPhone 15 256GB"
    assert phone["series"][0]["latest_price"] == 3000


def test_price_trends_uses_final_day_quality_and_returns_every_model(store):
    old = (utcnow().astimezone(SHANGHAI) - timedelta(days=2)).replace(hour=10, minute=0, second=0, microsecond=0)
    store.ingest([row(f"model-{i}", observed_at=iso(old), model=f"Explicit model {i}", price=100+i) for i in range(25)])
    store.ingest([row("bad", observed_at=iso(old), price=300),
                  row("bad", title="RTX4090故障卡", observed_at=iso(old + timedelta(minutes=5)), price=100)])
    result = store.price_trends("live", days=7)
    assert len(result["series"]) == 25
    assert sum(point["samples"] for point in result["aggregate"]) == 25
    assert len(store.price_trends("live", days=7, model="Explicit model 24")["series"]) == 1


def test_price_trends_shanghai_day_boundary_and_phone_api_validation(client):
    base = (utcnow().astimezone(SHANGHAI) - timedelta(days=2)).replace(hour=23, minute=59, second=0, microsecond=0)
    records = [row("phone", title="iPhone15 256GB", observed_at=iso(base), price=3000),
               row("phone", title="iPhone15 256GB", observed_at=iso(base + timedelta(minutes=2)), price=2900)]
    assert client.post("/api/import", json={"records": records}).status_code == 200
    response = client.get("/api/price-trends?source=live&days=180&category=phone")
    assert response.status_code == 200
    result = response.json()
    assert result["coverage"]["observed_days"] == 2
    assert len(result["aggregate"]) == 180
    assert result["series"][0]["observed_days"] == 2
    assert client.get("/api/price-trends?days=366").status_code == 422
    assert client.get("/api/price-trends?source=invalid").status_code == 422
    assert client.get("/api/price-trends?category=invalid").status_code == 422


@pytest.mark.parametrize("title,target,category", [
    ("原装74HC595移位寄存器批量20只", "74HC595", "component"),
    ("TI 74 hc595 芯片批量20只", "74HC595", "component"),
    ("联想 ThinkPad T14 Gen3 笔记本电脑", "ThinkPad T14 Gen3", "computer"),
    ("ThinkpadT14GEN3笔记本", "ThinkPad T14 Gen3", "computer"),
])
def test_custom_target_can_resolve_unknown_model_by_exact_title_token(title, target, category):
    item = normalize_record(row(title=title, price=100), {"category": category, "model": target})
    assert item["model"] == target
    assert not item["excluded"]


@pytest.mark.parametrize("title,target,category", [
    ("74HC595A移位寄存器", "74HC595", "component"),
    ("X74HC595移位寄存器", "74HC595", "component"),
    ("ThinkPad T14 Gen4 笔记本", "ThinkPad T14 Gen3", "computer"),
    ("RTX4070显卡", "RTX4090", "gpu"),
    ("华硕 RTX4070 显卡附赠74HC595芯片", "74HC595", "gpu"),
])
def test_custom_target_never_accepts_missing_partial_or_known_different_model(title, target, category):
    item = normalize_record(row(title=title, price=1000), {"category": category, "model": target})
    assert item["excluded"]
    assert item["model"] != target


@pytest.mark.parametrize("model", ["STM32F103C8T6", "ESP32-S3", "RP2040"])
def test_recognized_development_boards_are_eligible_with_explicit_task_model(model):
    item = normalize_record(row(title=f"原装{model}开发板", price=100), {"category": "component", "model": model})
    assert item["model"] == model
    assert not item["excluded"]


@pytest.mark.parametrize("price", [0.01, 1, 5, 10])
def test_positive_low_price_components_are_not_placeholder_prices(price):
    item = normalize_record(row(title="原装ESP32-S3开发板", price=price), {"category": "component"})
    assert not item["excluded"]
    assert item["price"] == price


def test_zero_component_and_low_price_gpu_are_still_excluded():
    component = normalize_record(row(title="RP2040开发板", price=0), {"category": "component"})
    assert component["excluded"]
    assert "零元占位/异常价格" in component["quality_flags"]
    gpu = normalize_record(row(title="RTX4090显卡", price=10))
    assert gpu["excluded"]
    assert "低价占位/异常价格（≤10元）" in gpu["quality_flags"]


@pytest.mark.parametrize("title,price", [("求购RTX4090显卡", 12000), ("RTX4090故障花屏", 2000),
                                        ("RTX4090显卡支架", 50), ("RTX4090定金", 500), ("RTX4090", 1)])
def test_irrelevant_or_bad_price_samples_retained_but_excluded(title, price):
    item = normalize_record(row(title=title, price=price))
    assert item["excluded"]
    assert item["quality_flags"]


def test_on_sale_without_sold_price_and_evidence_rules():
    assert normalize_record(row())["sold_price"] is None
    for state in ("sold", "delisted"):
        with pytest.raises(ValidationError):
            normalize_record(row(status=state))
    with pytest.raises(ValidationError):
        normalize_record(row(sold_price=11500))
    item = normalize_record(row(status="sold", evidence="详情页显示已售出"))
    assert item["sold_price"] is None


def test_import_is_transactional_even_on_late_invalid_record(store):
    with pytest.raises(ValidationError, match="第2条"):
        store.ingest([row(), row("invalid", status="sold")])
    assert store.overview("live")["summary"]["total_items"] == 0


def test_dedup_keeps_history_and_older_import_does_not_replace_latest(store):
    latest = row(days=1, price=12000)
    assert store.ingest([latest])["imported"] == 1
    assert store.ingest([latest])["skipped"] == 1
    store.ingest([row(days=5, price=13000)])
    item = store.filtered_items(source="live")[0]
    assert item["price"] == 12000
    assert item["first_seen"] < item["last_seen"]
    assert len(store.history(item["id"])["observations"]) == 2
    # Conflicts at the same exact time are rejected, never overwrite history.
    with pytest.raises(ValidationError, match="冲突"):
        store.ingest([{**latest, "price": 1}])
    assert store.get_item(item["id"])["price"] == 12000


def test_same_cohort_denominator_excludes_old_sold_and_delisted(store):
    store.ingest([row("old", days=20), row("a", days=3), row("b", days=2), row("c", days=2)])
    store.ingest([row("old", days=.2, status="sold", evidence="页面已售出"),
                  row("a", days=.2, status="sold", evidence="页面已售出"),
                  row("b", days=.2, status="delisted", evidence="页面已下架")])
    view = store.overview("live", days=7)
    summary = view["summary"]
    assert summary["new_items"] == 3
    assert summary["confirmed_sold"] == 1
    assert summary["sold_ratio"] == 33.33
    assert sum(point["confirmed_sold"] for point in view["trend"]) == 2
    assert summary["median_price"] == 12000


def test_repeated_day_observations_do_not_overweight_price(store):
    stamp = utcnow() - timedelta(hours=1)
    store.ingest([row("a", observed_at=iso(stamp), price=100),
                  row("a", observed_at=iso(stamp + timedelta(minutes=1)), price=200),
                  row("b", observed_at=iso(stamp), price=400)])
    view = store.overview("live", days=2)
    point = next(p for p in view["trend"] if p["samples"])
    assert point["samples"] == 2
    assert point["median_price"] == 300


def test_window_price_sample_matches_table_and_excludes_stale_items(store):
    store.ingest([row("old", days=60, price=1000), row("recent", days=1, price=3000)])
    overview = store.overview("live", days=7)
    assert overview["summary"]["median_price"] == 3000
    assert overview["summary"]["active_items"] == 1
    assert overview["summary"]["total_items"] == len(store.filtered_items(source="live", days=7)) == 1
    assert overview["models"][0]["median_price"] == 3000
    assert sum(bucket["count"] for bucket in overview["distribution"]) == 1


def test_manual_status_keeps_task_quality_exclusions(store):
    store.ingest([row("overpriced", price=3000)], task={"max_price": 2000})
    item = store.filtered_items(source="live", include_excluded=True)[0]
    assert item["excluded"]
    changed = store.set_status(item["id"], "sold", "人工看到已售出")
    assert changed["excluded"]
    assert "高于任务价格上限" in changed["quality_flags"]
    assert store.overview("live")["summary"]["confirmed_sold"] == 0
    assert store.overview("live")["summary"]["new_items"] == 0


def test_publication_must_not_be_after_observation(store):
    with pytest.raises(ValidationError, match="发布时间"):
        store.ingest([row(published_at="2100-01-01T00:00:00+08:00")])
    assert store.overview("live")["summary"]["published_items"] == 0


def test_source_isolation_reset_and_no_new_items_have_null_ratio(store):
    assert store.overview("demo")["summary"]["total_items"] > 0
    assert store.overview("live")["summary"]["sold_ratio"] is None
    store.ingest([row()])
    live_pk = store.filtered_items(source="live")[0]["id"]
    store.seed_demo()
    assert store.get_item(live_pk)["source"] == "live"
    assert store.overview("live")["summary"]["total_items"] == 1


def test_demo_export_cannot_be_accidentally_imported_as_live(store):
    demo = store.filtered_items(source="demo")[0]
    with pytest.raises(ValidationError, match="演示"):
        store.ingest([demo], source="live")
    with pytest.raises(ValidationError, match="演示"):
        store.ingest([row("demo:5")], source="live")
    with pytest.raises(ValidationError, match="演示"):
        store.ingest([{"商品信息": row("demo:5")}], source="live")
    with pytest.raises(ValidationError, match="演示"):
        store.ingest([{"product": {**row("real-shaped-id"), "source": "demo"}}], source="live")
    assert store.overview("live")["summary"]["total_items"] == 0


def test_upstream_chinese_and_excontent_imports():
    legacy = normalize_record({"爬取时间": "2026-01-02 12:00:00", "商品信息": {"商品标题": "RTX4090 24G", "当前售价": "¥1.2万", "商品链接": "https://www.goofish.com/item?id=123", "发布时间": "1小时前"}})
    assert legacy["item_id"] == "123"
    assert legacy["price"] == 12000
    assert legacy["published_at"] is None
    direct = normalize_record({"data": {"item": {"main": {"exContent": {"itemId": "456", "title": "RTX4060 8G", "price": [{"text": "¥"}, {"text": "1500"}]}}}}})
    assert direct["item_id"] == "456"
    assert direct["price"] == 1500


def test_formula_safe_csv_and_literal_search(store):
    store.ingest([row(title="=HYPERLINK(1) RTX4090")])
    csv = store.export_csv(source="live")
    assert csv.startswith("\ufeff")
    assert "'=HYPERLINK" in csv
    assert not store.filtered_items(source="live", q="%")


def test_api_crud_import_history_and_manual_evidence(client):
    assert client.get("/api/health").json() == {"ok": True}
    result = client.post("/api/import", json={"records": [row()]})
    assert result.status_code == 200
    assert result.json()["imported"] == 1
    item = client.get("/api/items?source=live").json()["items"][0]
    assert client.patch(f"/api/items/{item['id']}/status", json={"status": "sold"}).status_code == 422
    sold = client.patch(f"/api/items/{item['id']}/status", json={"status": "sold", "evidence": "用户人工看到已售出"})
    assert sold.status_code == 200
    assert sold.json()["sold_price"] is None
    assert len(client.get(f"/api/items/{item['id']}/history").json()["observations"]) == 2
    response = client.post("/api/tasks", json={"name": "显卡观察", "keyword": "RTX4090", "category": "gpu"})
    assert response.status_code == 201
    task = response.json()
    assert task["enabled"] is False
    assert client.patch(f"/api/tasks/{task['id']}", json={"interval_minutes": 1}).status_code == 422
    assert client.patch(f"/api/tasks/{task['id']}", json={"enabled": True}).json()["next_run_at"]
    assert client.delete(f"/api/tasks/{task['id']}").json()["ok"] is True
    export = client.get("/api/export?source=live")
    assert "live" in export.text
    assert "csv" in export.headers["content-type"]


def test_api_rejects_malformed_and_cross_origin_import(client):
    assert client.post("/api/import", json={"source": "demo", "records": [row()]}).status_code == 422
    assert client.post("/api/import", json={"records": [row(price="NaN")]}).status_code == 422
    assert client.post("/api/import", json={"records": [row()]}, headers={"Origin": "https://malicious.example"}).status_code == 403
    assert client.get("/api/overview?source=invalid").status_code == 422
    assert client.get("/api/overview?days=999999").status_code == 422
    assert client.get("/api/items/999999/history").status_code == 404
    assert client.post("/api/import", json={"task_id": {}, "records": [row()]}).status_code == 422
    assert client.post("/api/import", json={"records": [{"data": {"item": []}}]}).status_code == 422
    assert client.post("/api/tasks", json={"name": "a", "keyword": "b", "category": {}}).status_code == 422
    assert client.patch("/api/items/1/status", json={"status": {}}).status_code == 422


def await_terminal_run(client, task_id):
    """Let the app's real event loop progress via bounded API round-trips."""
    deadline = time.monotonic() + 3
    for _ in range(100):
        records = client.get("/api/runs").json()["runs"]
        for record in records:
            if record["task_id"] == task_id and record["status"] not in {"running", "queued"}:
                return record
        if time.monotonic() >= deadline:
            break
    pytest.fail("后台采集在有界轮询内未结束")


def fake_collector(monkeypatch, collect, available=True):
    main = importlib.import_module("app.main")
    module = SimpleNamespace(
        collector_status=lambda: {
            "available": available, "session_exists": available,
            "browser_installed": True, "login_running": False,
            "message": "已就绪" if available else "请先在本机完成闲鱼登录",
        },
        collect=collect,
    )
    monkeypatch.setattr(main, "collector", lambda: module)
    return module


def test_task_run_pipeline_persists_observations_run_and_task_tracking(client, monkeypatch):
    captured = {}

    async def collect(task):
        captured["task"] = task
        await asyncio.sleep(0)
        return [row("tracked", days=0, status="sold", evidence="商品自身详情状态明确显示已售出"),
                row("new", days=0, price=11500)]

    fake_collector(monkeypatch, collect)
    task = client.post("/api/tasks", json={
        "name": "流水线验证", "keyword": "RTX4090", "category": "gpu",
    }).json()
    imported = client.post("/api/import", json={"task_id": task["id"], "records": [row("tracked", days=2)]})
    assert imported.status_code == 200
    response = client.post(f"/api/tasks/{task['id']}/run")
    assert response.status_code == 200
    assert response.json()["ok"] is True
    run = await_terminal_run(client, task["id"])
    assert run["status"] == "success"
    assert run["task_name"] == task["name"]
    assert run["items_seen"] == run["items_saved"] == 2
    assert run["finished_at"] and run["error"] is None

    assert captured["task"]["id"] == task["id"]
    assert captured["task"]["keyword"] == "RTX4090"
    assert [item["item_id"] for item in captured["task"]["tracked_items"]] == ["tracked"]
    items = client.get("/api/items?source=live").json()["items"]
    assert len(items) == 2
    tracked = next(item for item in items if item["item_id"] == "tracked")
    assert tracked["status"] == "sold"
    history = client.get(f"/api/items/{tracked['id']}/history").json()["observations"]
    assert len(history) == 2 and history[0]["status"] == "sold"
    assert history[0]["sold_price"] is None
    store = client.app.state.store
    with store.db() as db:
        bindings = db.execute("SELECT o.task_id FROM observations o JOIN items i ON i.id=o.item_pk WHERE i.source='live'").fetchall()
        run_bindings = db.execute("SELECT o.run_id FROM observations o JOIN items i ON i.id=o.item_pk WHERE i.source='live'").fetchall()
    assert len(bindings) == 3
    assert all(binding["task_id"] == task["id"] for binding in bindings)
    assert sum(binding["run_id"] == run["id"] for binding in run_bindings) == 2
    assert sum(binding["run_id"] is None for binding in run_bindings) == 1
    saved_task = next(t for t in client.get("/api/tasks").json()["tasks"] if t["id"] == task["id"])
    assert saved_task["last_run_at"] and saved_task["last_error"] is None


def test_task_run_pipeline_records_collector_failure_without_fake_success(client, monkeypatch):
    async def collect(task):
        await asyncio.sleep(0)
        raise CollectorError("平台要求重新登录或完成验证，本次采集已停止")

    fake_collector(monkeypatch, collect)
    task = client.post("/api/tasks", json={"name": "验证失败路径", "keyword": "RTX4090", "category": "gpu"}).json()
    assert client.post(f"/api/tasks/{task['id']}/run").status_code == 200
    run = await_terminal_run(client, task["id"])
    assert run["status"] == "failed"
    assert "验证" in run["error"]
    assert run["finished_at"]
    assert run["items_saved"] == 0
    assert not any(r["status"] == "success" for r in client.get("/api/runs").json()["runs"])
    assert client.get("/api/items?source=live").json()["total"] == 0
    saved_task = next(t for t in client.get("/api/tasks").json()["tasks"] if t["id"] == task["id"])
    assert "验证" in saved_task["last_error"]


def test_unavailable_collector_rejects_run_without_creating_success_log(client, monkeypatch):
    called = []

    async def collect(task):
        called.append(task)
        return []

    fake_collector(monkeypatch, collect, available=False)
    task = client.post("/api/tasks", json={"name": "等待登录", "keyword": "RTX4090", "category": "gpu"}).json()
    response = client.post(f"/api/tasks/{task['id']}/run")
    assert response.status_code == 409
    assert "登录" in response.json()["detail"]
    assert client.get("/api/runs").json()["runs"] == []
    assert not called
    assert client.get("/api/items?source=live").json()["total"] == 0
    saved_task = next(t for t in client.get("/api/tasks").json()["tasks"] if t["id"] == task["id"])
    assert saved_task["last_run_at"] is None
