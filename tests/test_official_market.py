import base64
import importlib
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.normalization import SHANGHAI, ValidationError, iso, utcnow
from app.official_market import OfficialMarket
from app.service import Store

PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aD1sAAAAASUVORK5CYII=")


@pytest.fixture
def official(tmp_path):
    evidence = tmp_path / "market_evidence"
    evidence.mkdir()
    (evidence / "cpu-history.png").write_bytes(PNG)
    return OfficialMarket(Store(tmp_path / "official.sqlite3"), evidence)


def payload(**changes):
    now = utcnow()
    today = now.astimezone(SHANGHAI).date()
    return {"query": "14900KF", "model": "i9-14900KF", "category": "cpu",
            "metric": "rolling_7d_sold_mean", "captured_at": iso(now),
            "period_start": (today - timedelta(days=179)).isoformat(), "period_end": today.isoformat(),
            "evidence_filename": "cpu-history.png",
            "points": [{"date": (today - timedelta(days=90)).isoformat(), "price": 2350},
                       {"date": today.isoformat(), "price": 2115}], **changes}


def test_sparse_official_snapshot_stays_separate_from_listing_and_no_daily_fill(official):
    assert official.list_snapshots()["series"] == []
    data = payload()
    response = official.import_snapshot(data)
    series = official.list_snapshots()["series"][0]
    assert response["ok"] and response["sampled_points"] == 2
    assert series["read_method"] == "screen_verified"
    assert series["metric"] == "rolling_7d_sold_mean"
    assert series["points"] == data["points"]
    assert series["sampled_points"] == 2
    assert series["first_price"] == 2350 and series["last_price"] == 2115
    assert series["change_pct"] == -10
    assert "非完整逐日数据" in series["coverage_message"]
    assert official.store.overview("live")["summary"]["total_items"] == 0
    assert official.store.price_trends("live")["series"] == []


def test_duplicate_points_deduplicate_and_same_snapshot_replaces_atomically(official):
    data = payload()
    data["points"].append(dict(data["points"][0]))
    first = official.import_snapshot(data)
    assert first["sampled_points"] == 2
    replacement = {**data, "points": [data["points"][1]]}
    second = official.import_snapshot(replacement)
    assert second["id"] == first["id"]
    assert official.list_snapshots()["series"][0]["points"] == replacement["points"]
    conflicting = {**data, "points": [data["points"][0], {**data["points"][0], "price": 999}]}
    with pytest.raises(ValidationError, match="冲突"):
        official.import_snapshot(conflicting)
    assert official.list_snapshots()["series"][0]["points"] == replacement["points"]


@pytest.mark.parametrize("change", [
    {"metric": "listing_mean"}, {"category": "invalid"}, {"query": ""},
    {"read_method": "private_api"}, {"points": []},
    {"points": [{"date": "2026-02-30", "price": 20}]},
    {"points": [{"date": "2026-01-01", "price": 20}]},
    {"captured_at": "2100-01-01T12:00:00+08:00"},
    {"period_end": "2100-01-01"},
])
def test_invalid_metadata_points_or_future_dates_never_partial_write(official, change):
    with pytest.raises(ValidationError):
        official.import_snapshot(payload(**change))
    assert official.list_snapshots()["series"] == []


@pytest.mark.parametrize("price", [float("nan"), float("inf"), -1, True, "2350"])
def test_prices_must_be_finite_nonnegative_numeric_reads(official, price):
    data = payload()
    data["points"][0]["price"] = price
    with pytest.raises(ValidationError):
        official.import_snapshot(data)
    assert official.list_snapshots()["series"] == []


@pytest.mark.parametrize("filename", ["../session_meta.json", "/tmp/cpu-history.png", "sub/cpu-history.png", "sub\\cpu-history.png", "session_meta.json", "missing.png"])
def test_evidence_import_requires_existing_safe_png_basename(official, filename):
    with pytest.raises(ValidationError):
        official.import_snapshot(payload(evidence_filename=filename))


def test_fake_png_and_symlink_outside_evidence_directory_rejected(official, tmp_path):
    (official.evidence_dir / "text.png").write_text("not a png")
    outside = tmp_path / "outside.png"
    outside.write_bytes(PNG)
    (official.evidence_dir / "link.png").symlink_to(outside)
    for filename in ("text.png", "link.png"):
        with pytest.raises(ValidationError):
            official.import_snapshot(payload(evidence_filename=filename))


@pytest.mark.parametrize("extension", ["jpg", "jpeg", "JPG"])
def test_jpeg_evidence_accepts_matching_extension_and_magic(official, extension):
    filename = f"phone-history.{extension}"
    (official.evidence_dir / filename).write_bytes(b"\xff\xd8\xff\xe0" + b"screen snapshot")
    response = official.import_snapshot(payload(evidence_filename=filename))
    assert response["ok"]
    assert official.get_evidence(response["id"]).name == filename


@pytest.mark.parametrize("filename,content", [("fake.png", b"\xff\xd8\xff\xe0jpeg"), ("fake.jpg", PNG), ("fake.jpeg", b"plain text")])
def test_evidence_extension_must_match_actual_image_magic(official, filename, content):
    (official.evidence_dir / filename).write_bytes(content)
    with pytest.raises(ValidationError, match="格式"):
        official.import_snapshot(payload(evidence_filename=filename))


def test_filtering_windows_and_categories_preserves_declared_original_chart_span(official):
    data = payload()
    official.import_snapshot(data)
    recent = official.list_snapshots(days=7)["series"][0]
    assert len(recent["points"]) == recent["sampled_points"] == 1
    assert recent["change_pct"] is None
    assert recent["period_start"] == data["period_start"]
    assert official.list_snapshots(category="phone")["series"] == []
    assert official.list_snapshots(model="unmatched")["series"] == []
    assert len(official.list_snapshots(category="cpu", model="i9-14900KF")["series"]) == 1


def test_official_http_routes_validate_and_only_serve_snapshot_evidence(tmp_path, monkeypatch):
    module = importlib.import_module("app.official_market")
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    (evidence / "cpu-history.png").write_bytes(PNG)
    monkeypatch.setattr(module, "EVIDENCE_DIR", evidence)
    with TestClient(create_app(tmp_path / "api.sqlite3")) as client:
        imported = client.post("/api/official-market/import", json=payload())
        assert imported.status_code == 200
        snapshot_id = imported.json()["id"]
        series = client.get("/api/official-market?category=cpu&days=180").json()["series"][0]
        assert "evidence_filename" not in series
        assert str(tmp_path) not in str(series)
        image = client.get(series["evidence_url"])
        assert image.status_code == 200 and image.content == PNG
        assert image.headers["content-type"] == "image/png"
        assert client.get("/api/official-market/999999/evidence").status_code == 404
        assert client.get("/api/official-market?days=366").status_code == 422
        assert client.get("/api/official-market?category=invalid").status_code == 422
        assert client.post("/api/official-market/import", json=payload(evidence_filename="../secret.png")).status_code == 422
        assert client.post("/api/official-market/import", json=payload(), headers={"Origin": "https://other.example"}).status_code == 403
        jpeg_bytes = b"\xff\xd8\xff\xe0" + b"screen snapshot"
        (evidence / "phone-history.jpg").write_bytes(jpeg_bytes)
        jpeg_import = client.post("/api/official-market/import", json=payload(evidence_filename="phone-history.jpg"))
        assert jpeg_import.status_code == 200
        jpeg_image = client.get(f"/api/official-market/{jpeg_import.json()['id']}/evidence")
        assert jpeg_image.headers["content-type"] == "image/jpeg"
        assert jpeg_image.content == jpeg_bytes
        # Restore the PNG snapshot for the missing-file path below.
        assert client.post("/api/official-market/import", json=payload(evidence_filename="cpu-history.png")).status_code == 200
        (evidence / "cpu-history.png").unlink()
        assert client.get(f"/api/official-market/{snapshot_id}/evidence").status_code == 404
