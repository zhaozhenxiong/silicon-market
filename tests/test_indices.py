"""Real input contracts for fixed indices; all fixtures use isolated SQLite."""
import json
from datetime import date, datetime, timedelta, timezone

import pytest

from app import indices
from app.indices import IndexService, calibration, equal_weights, standardize
from app.official_market import OfficialMarket
from app.service import Store

NOW = datetime(2026, 10, 30, 4, tzinfo=timezone.utc)


@pytest.fixture
def service(tmp_path, monkeypatch):
    monkeypatch.setattr(indices, "utcnow", lambda: NOW)
    store = Store(tmp_path / "indices.sqlite", production=True)
    OfficialMarket(store, tmp_path / "evidence")
    return IndexService(store)


def add_official(service, model, category, points, query=None):
    with service.store.db() as db:
        cur = db.execute("INSERT INTO official_snapshots(query,model,category,metric,captured_at,period_start,period_end,evidence_filename) VALUES (?,?,?,'rolling_7d_sold_mean',?,?,?,'fixture.png')",
            (query or model, model, category, NOW.isoformat(), min(points), max(points)))
        db.executemany("INSERT INTO official_points VALUES (?,?,?)", [(cur.lastrowid, day, price) for day, price in points.items()])


def price_days(start="2026-10-20", count=10, price=100):
    return {(date.fromisoformat(start) + timedelta(days=i)).isoformat(): price + i for i in range(count)}


def listing(service, item_id, observed, price=100, status="on_sale", source="live", task=None):
    service.store.ingest([dict(item_id=str(item_id), title="RTX 4060 Ti 16GB", category="gpu", model="RTX 4060 Ti 16GB",
        price=price, status=status, evidence="实际页面明确售出" if status == "sold" else "实际挂牌观测", observed_at=observed)], source, task)


def test_common_seven_day_baseline_and_geometric_price_series(service):
    add_official(service, "RTX 4060", "gpu", price_days())
    add_official(service, "DDR5 32GB", "ram", price_days(price=200))
    result = service.rebuild(days=10)
    official = result["price_indices"][0]
    assert official["base_start"] == "2026-10-20"
    assert official["base_end"] == "2026-10-26"
    assert official["base_prices"][official["basket"][0]["id"]] in {103, 203}
    assert official["points"][-1]["value"] == pytest.approx(100 * ((109 / 103) * (209 / 203)) ** .5, abs=1e-6)
    assert sum(m["weight"] for m in official["basket"]) == pytest.approx(1)
    assert result["price_indices"][1]["status"] == "insufficient_data"


def test_sparse_points_cannot_publish_fake_baseline_or_price_100(service):
    add_official(service, "i9-14900KF", "cpu", {"2026-05-05": 2000, "2026-10-27": 2200})
    result = service.get()
    assert result["version"] == "unpublished"
    assert result["price_indices"][0]["status"] == "insufficient_data"
    assert all(p["value"] is None for p in result["price_indices"][0]["points"])
    assert result["features"][0]["score"] is None
    with service.store.db() as db:
        assert db.execute("SELECT COUNT(*) FROM index_builds").fetchone()[0] == 0


def test_missing_component_is_null_and_does_not_renormalize(service):
    add_official(service, "RTX 4060", "gpu", price_days())
    second = price_days(price=200)
    second.pop("2026-10-29")
    add_official(service, "DDR5 32GB", "ram", second)
    result = service.rebuild(days=10)
    last = result["price_indices"][0]["points"][-1]
    assert last["coverage_pct"] == 50
    assert last["value"] is None
    assert last["missing_models"] == ["DDR5 32GB"]


def test_category_then_model_weights_are_not_currency_weights():
    rows = equal_weights([dict(category="gpu", model="a"), dict(category="gpu", model="b"), dict(category="ram", model="c")])
    assert [r["weight"] for r in rows] == [.25, .25, .5]


def test_legacy_same_query_curve_is_not_repeated_for_different_sku_labels(service):
    add_official(service, "iPhone 15 128GB", "phone", price_days(), query="iPhone15")
    # Same query identifies one unknown-width legacy curve, not verified variants.
    add_official(service, "iPhone 15 256GB", "phone", price_days(), query="iphone 15")
    result = service.rebuild()
    assert len(result["price_indices"][0]["basket"]) == 1
    assert result["coverage"]["official_scopes"] == 1


def test_demo_does_not_enter_listing_prices_or_factor_denominators(service):
    for day in range(20, 30):
        for item in range(20):
            listing(service, f"real-{item}", f"2026-10-{day}T10:00:00+08:00", price=100 + item)
            listing(service, f"demo-{item}", f"2026-10-{day}T10:00:00+08:00", price=10000, source="demo")
    result = service.rebuild(days=10)
    series = result["price_indices"][1]
    assert len(series["basket"]) == 1
    assert list(series["base_prices"].values()) == [109.5]
    assert series["points"][-1]["value"] == 100
    details = result["features"][0]["factors"][3]["raw_details"]
    assert details["samples"] == 20


def test_latest_sold_snapshot_is_not_a_same_day_active_listing(service):
    for item in range(20):
        listing(service, item, "2026-10-29T09:00:00+08:00")
        listing(service, item, "2026-10-29T10:00:00+08:00", status="sold")
    result = service.get()
    assert result["coverage"]["listing_models"] == 0


def test_fixed_basket_get_does_not_add_new_model_until_explicit_rebuild(service):
    add_official(service, "RTX 4060", "gpu", price_days())
    first = service.rebuild()
    add_official(service, "DDR5 32GB", "ram", price_days(price=200))
    read = service.get()
    assert read["version"] == first["version"]
    assert len(read["price_indices"][0]["basket"]) == 1
    rebuilt = service.rebuild()
    assert rebuilt["version"] != first["version"]
    assert len(rebuilt["price_indices"][0]["basket"]) == 2
    replay = service.replay(first["id"])
    assert replay["input_hash"] == first["input_hash"]
    assert replay["price_indices"] == first["price_indices"]
    assert replay["features"] == first["features"]


def test_calibration_requires_30_actual_days_strictly_before_scoring():
    values = {(date(2026, 9, 1) + timedelta(days=i)).isoformat(): dict(value=i, eligible=True) for i in range(40)}
    assert calibration(values, "2026-09-30") is None
    frozen = calibration(values, "2026-10-01")
    assert frozen["samples"] == 30
    assert frozen["end"] == "2026-09-30"
    assert standardize(30, frozen, 1, "2026-09-30") is None
    assert standardize(30, frozen, 1, "2026-10-01") == 100
    assert standardize(30, frozen, -1, "2026-10-01") == 0
    values["2026-10-02"]["value"] = 100000
    assert calibration(values, "2026-10-01") == frozen


def test_collapsed_quantiles_are_not_neutral_if_history_has_real_outlier():
    values = {(date(2026, 9, 1) + timedelta(days=i)).isoformat(): dict(value=0 if i < 29 else 100, eligible=True) for i in range(30)}
    frozen = calibration(values, "2026-10-01")
    assert frozen["q05"] == frozen["q95"] == 0
    assert not frozen["constant"]
    assert standardize(100, frozen, 1, "2026-10-01") is None


def test_reference_factors_compute_real_raw_values_but_no_composite_without_listing(service):
    points = {(date(2026, 7, 1) + timedelta(days=i)).isoformat(): 100 * 1.001 ** i for i in range(121)}
    add_official(service, "RTX 4060", "gpu", points)
    result = service.rebuild()
    row = result["features"][0]
    momentum, liquidity, supply, concentration, stability = row["factors"]
    assert momentum["raw_value"] == pytest.approx((1.001 ** 28 - 1) * 100, abs=1e-7)
    assert momentum["score"] is not None
    assert stability["raw_value"] == pytest.approx(0, abs=1e-8)
    assert stability["score"] == 50  # actual constant smooth-price-return history
    assert liquidity["raw_value"] is None and supply["score"] is None and concentration["score"] is None
    assert row["score"] is None and result["composite_score"] is None
    assert sum(f["weight"] for f in row["factors"]) == 1


def test_mature_cohort_uses_initial_on_sale_and_keeps_unconfirmed_denominator(service):
    for item in range(20):
        listing(service, item, "2026-10-05T09:00:00+08:00")
        if item < 10:
            listing(service, item, "2026-10-10T10:00:00+08:00", status="sold")
        else:
            listing(service, item, "2026-10-20T10:00:00+08:00")
    listing(service, "already-sold", "2026-10-05T10:00:00+08:00", status="sold")
    result = service.get()
    liquidity = result["features"][0]["factors"][1]
    assert liquidity["raw_value"] == 50
    assert liquidity["raw_details"]["samples"] == 20
    assert liquidity["raw_details"]["tracking_coverage_pct"] == 100
    assert liquidity["score"] is None  # not 30 calibrated factor days


def test_supply_requires_stable_config_warmup_and_comparable_windows(service):
    task = service.store.tasks()[0]
    with service.store.db() as db:
        for n in range(15):
            day = (date(2026, 10, 15) + timedelta(days=n)).isoformat()
            db.execute("INSERT INTO runs(task_id,task_name,status,started_at,finished_at,sampling_config_hash,config_json,initial_scan) VALUES (?,?,'success',?,?,'config-a','{}',?)",
                       (task["id"], "fixture", day + "T09:00:00+08:00", day + "T10:00:00+08:00", n == 0))
    for n in range(14):
        day = (date(2026, 10, 16) + timedelta(days=n)).isoformat()
        with service.store.db() as db:
            task["_run_id"] = db.execute("SELECT id FROM runs WHERE started_at LIKE ?", (day + "%",)).fetchone()[0]
        for j in range(1 if n < 7 else 2):
            listing(service, f"s-{n}-{j}", day + "T09:00:00+08:00", task=task)
    result = service.get()
    supply = result["features"][0]["factors"][2]
    assert supply["raw_value"] == 100
    assert supply["raw_details"]["previous_new"] == 7
    assert supply["raw_details"]["current_new"] == 14
    with service.store.db() as db:
        db.execute("UPDATE observations SET run_id=NULL")
    assert service.get()["features"][0]["factors"][2]["raw_value"] is None
    with service.store.db() as db:
        db.execute("UPDATE observations SET run_id=(SELECT id FROM runs WHERE substr(started_at,1,10)=substr(observations.observed_at,1,10))")
    with service.store.db() as db:
        db.execute("UPDATE runs SET sampling_config_hash='config-b' WHERE started_at LIKE '2026-10-24%'")
    assert service.get()["features"][0]["factors"][2]["raw_value"] is None


def test_canonical_mobile_scope_counts_once_and_never_patches_from_legacy_scope(service):
    add_official(service, "iPhone 15 128GB", "phone", price_days(), query="iPhone15")
    with service.store.db() as db:
        db.executescript("""
            CREATE TABLE catalog(id INTEGER PRIMARY KEY,category TEXT,model TEXT,query TEXT,scope TEXT,market_scope_id TEXT,enabled INTEGER);
            CREATE TABLE mobile_points(market_scope_id TEXT,metric TEXT,date TEXT,price REAL,reading_id INTEGER,verified_at TEXT);
        """)
        for n, model in enumerate(["iPhone 15 128GB", "iPhone 15 wide display"], 1):
            db.execute("INSERT INTO catalog VALUES (?,'phone',?,'iPhone15','{}','same-actual-scope',1)", (n, model))
        for day, price in price_days(count=3).items():
            db.execute("INSERT INTO mobile_points VALUES ('same-actual-scope','rolling_7d_sold_mean',?,?,1,?)", (day, price, NOW.isoformat()))
    result = service.rebuild()
    assert result["coverage"]["official_scopes"] == 1
    assert result["price_indices"][0]["status"] == "insufficient_data"
    # Seven old manual points cannot be copied into the three-day canonical scope.
    assert result["price_indices"][0]["basket"] == []


def test_pending_discovered_variant_stays_out_of_formal_basket_but_raw_is_preserved(service):
    from app.catalog import Catalog
    for day in range(20, 30):
        for item in range(20):
            listing(service, f"variant-{item}", f"2026-10-{day}T10:00:00+08:00")
    catalog = Catalog(service.store)
    catalog.sync()
    result = service.rebuild()
    assert result["price_indices"][1]["basket"] == []
    feature = next(f for f in result["features"] if f["model"] == "RTX 4060 Ti 16GB")
    assert feature["status"] == "needs_review" and not feature["basket_eligible"]
    assert feature["factors"][3]["raw_value"] == 0
    assert all(f["score"] is None for f in feature["factors"])


def test_revoked_scope_breaks_existing_component_without_redistributing_weights(service):
    from app.catalog import Catalog
    for day in range(20, 30):
        for item in range(20):
            listing(service, f"verified-{item}", f"2026-10-{day}T10:00:00+08:00")
    catalog = Catalog(service.store)
    catalog.sync()
    gpu = next(c for c in catalog.list()["items"] if c["model"] == "RTX 4060 Ti 16GB")
    catalog.patch(gpu["id"], {"scope": {**gpu["scope"], "verified": True}, "status": "ready"})
    result = service.rebuild(days=10)
    assert result["price_indices"][1]["points"][-1]["value"] == 100
    catalog.patch(gpu["id"], {"status": "unsupported"})
    current = service.get(days=10)
    assert len(current["price_indices"][1]["basket"]) == 1
    assert current["price_indices"][1]["points"][-1]["coverage_pct"] == 0
    assert current["price_indices"][1]["points"][-1]["value"] is None
    assert service.replay(result["id"])["price_indices"][1]["points"][-1]["value"] == 100


def test_all_five_factors_can_publish_once_real_data_matures(service):
    task = service.store.tasks()[0]
    start, end = date(2026, 7, 1), date(2026, 10, 29)
    history = price_days(start.isoformat(), (end - start).days + 1, 1000)
    add_official(service, "RTX 4060 Ti 16GB", "gpu", history)
    records = []
    for n in range((end - start).days + 1):
        day = start + timedelta(days=n)
        stamp = day.isoformat() + "T09:00:00+08:00"
        for item in range(20):
            records.append(dict(item_id=f"base-{item}", title="RTX 4060 Ti 16GB", category="gpu", model="RTX 4060 Ti 16GB",
                                price=100 + item + n, observed_at=stamp))
        for item in range(2):
            row = dict(item_id=f"new-{n}-{item}", title="RTX 4060 Ti 16GB", category="gpu", model="RTX 4060 Ti 16GB",
                       price=120 + n, observed_at=stamp)
            records.append(row)
            followup = day + timedelta(days=7 if item == 0 else 14)
            if followup <= end:
                records.append({**row, "observed_at": followup.isoformat() + "T10:00:00+08:00",
                                "status": "sold" if item == 0 else "on_sale", "evidence": "实际页面明确售出" if item == 0 else "实际复查在售"})
    service.store.ingest(records, "live", task)
    with service.store.db() as db:
        for n in range((end - start).days + 1):
            day = (start + timedelta(days=n)).isoformat()
            db.execute("INSERT INTO runs(task_id,task_name,status,started_at,finished_at,sampling_config_hash,config_json,initial_scan) VALUES (?,?,'success',?,?,'stable-v1','{}',?)",
                       (task["id"], "fixture", day + "T09:00:00+08:00", day + "T11:00:00+08:00", n == 0))
        db.execute("UPDATE observations SET run_id=(SELECT id FROM runs WHERE task_id=observations.task_id AND substr(started_at,1,10)=substr(observations.observed_at,1,10))")
    result = service.rebuild()
    feature, = result["features"]
    assert all(f["raw_value"] is not None and f["score"] is not None for f in feature["factors"])
    assert feature["status"] == "ready"
    assert feature["score"] == pytest.approx(sum(f["score"] * f["weight"] for f in feature["factors"]), abs=1e-6)
    assert result["composite_score"] == feature["score"]
    assert 0 <= feature["score"] <= 100


@pytest.mark.parametrize("days,category", [(0, "all"), (366, "all"), (True, "all"), (30, "unknown")])
def test_invalid_requests(service, days, category):
    with pytest.raises(ValueError):
        service.get(days, category)
