"""Sparse, screen-verified official App observations, separate from listings."""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from pathlib import Path

from .normalization import CATEGORIES, SHANGHAI, ValidationError, iso, number, utcnow

EVIDENCE_DIR = Path(__file__).resolve().parent.parent / "data" / "market_evidence"
METRIC = "rolling_7d_sold_mean"
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def exact_date(value, field):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValidationError(f"{field} 须为精确 YYYY-MM-DD 日期")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValidationError(f"{field} 日期无效") from exc


class OfficialMarket:
    def __init__(self, store, evidence_dir=None):
        self.store = store
        self.evidence_dir = Path(evidence_dir or EVIDENCE_DIR)
        with store.db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS official_snapshots (
                    id INTEGER PRIMARY KEY, query TEXT NOT NULL, model TEXT NOT NULL,
                    category TEXT NOT NULL, metric TEXT NOT NULL,
                    captured_at TEXT NOT NULL, period_start TEXT NOT NULL, period_end TEXT NOT NULL,
                    evidence_filename TEXT NOT NULL,
                    UNIQUE(category,model,query,captured_at));
                CREATE TABLE IF NOT EXISTS official_points (
                    snapshot_id INTEGER NOT NULL REFERENCES official_snapshots(id) ON DELETE CASCADE,
                    date TEXT NOT NULL, price REAL NOT NULL,
                    PRIMARY KEY(snapshot_id,date));
            """)

    def evidence_file(self, filename):
        if (not isinstance(filename, str) or ".." in filename or
                not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\.(?:png|jpe?g)", filename, re.I)):
            raise ValidationError("证据文件须为安全的 PNG/JPG/JPEG 文件名，不接受路径")
        try:
            base = self.evidence_dir.resolve()
            candidate = (base / filename).resolve(strict=True)
            candidate.relative_to(base)
            if not candidate.is_file() or candidate.stat().st_size > 20 * 1024 * 1024:
                raise ValidationError("图片证据文件不存在或超过20MB")
            with candidate.open("rb") as image:
                header = image.read(8)
                suffix = Path(filename).suffix.lower()
                if (candidate.suffix.lower() == ".png") != (suffix == ".png"):
                    raise ValidationError("证据图片实际格式与文件扩展名不匹配")
                matches = header == PNG_SIGNATURE if suffix == ".png" else header.startswith(b"\xff\xd8\xff")
                if not matches:
                    raise ValidationError("证据图片实际格式与文件扩展名不匹配")
            return candidate
        except (OSError, ValueError) as exc:
            if isinstance(exc, ValidationError):
                raise
            raise ValidationError("图片证据文件不存在或不在允许目录中") from None

    def import_snapshot(self, payload):
        expected = {"query", "model", "category", "metric", "captured_at", "period_start", "period_end", "evidence_filename", "points"}
        if not isinstance(payload, dict) or set(payload) != expected:
            raise ValidationError("官方行情导入须提供 query/model/category/metric/captured_at/period_start/period_end/evidence_filename/points，且不能添加其他字段")
        data = dict(payload)
        for field in ("query", "model"):
            value = data[field]
            if not isinstance(value, str) or not value.strip() or len(value) > 160:
                raise ValidationError(f"{field} 须为1至160字文本")
            data[field] = value.strip()
        if not isinstance(data["category"], str) or data["category"] not in CATEGORIES:
            raise ValidationError("官方行情分类无效")
        if data["metric"] != METRIC:
            raise ValidationError("官方 App 指标只能是 rolling_7d_sold_mean（历史近7日成交均价）")
        if not isinstance(data["captured_at"], str) or not data["captured_at"].strip():
            raise ValidationError("须提供实际 captured_at 核对时间")
        data["captured_at"] = iso(data["captured_at"])
        captured = datetime.fromisoformat(data["captured_at"])
        if captured > utcnow() + timedelta(minutes=5):
            raise ValidationError("核对时间不能在未来")
        captured_date = captured.astimezone(SHANGHAI).date()
        today = utcnow().astimezone(SHANGHAI).date()
        start = exact_date(data["period_start"], "period_start")
        end = exact_date(data["period_end"], "period_end")
        if start > end or end > captured_date or end > today:
            raise ValidationError("声明周期须有序，且不能超出核对日期或今天")
        records = data["points"]
        if not isinstance(records, list) or not 1 <= len(records) <= 1000:
            raise ValidationError("每个官方快照须含1至1000个实际核对点")
        points = {}
        for index, point in enumerate(records, 1):
            if not isinstance(point, dict) or set(point) != {"date", "price"}:
                raise ValidationError(f"第{index}个点须且仅含 date 和 price")
            day = exact_date(point["date"], f"第{index}个点")
            if day < start or day > end or day > today or day > captured_date:
                raise ValidationError(f"第{index}个点超出声明周期或在未来")
            # Official screen readouts must be precise numeric values, not currency strings.
            if isinstance(point["price"], bool) or not isinstance(point["price"], (int, float)):
                raise ValidationError(f"第{index}个点须提供数字价格")
            price = number(point["price"], "price")
            key = day.isoformat()
            if key in points and points[key] != price:
                raise ValidationError("同一日期有冲突读数，请回到证据图片核对")
            points[key] = price
        self.evidence_file(data["evidence_filename"])
        fields = ["query", "model", "category", "metric", "captured_at", "period_start", "period_end", "evidence_filename"]
        with self.store.db() as db:
            db.execute(f"INSERT INTO official_snapshots({','.join(fields)}) VALUES ({','.join('?' for _ in fields)}) ON CONFLICT(category,model,query,captured_at) DO UPDATE SET metric=excluded.metric,period_start=excluded.period_start,period_end=excluded.period_end,evidence_filename=excluded.evidence_filename", [data[field] for field in fields])
            row = db.execute("SELECT id FROM official_snapshots WHERE category=? AND model=? AND query=? AND captured_at=?", (data["category"], data["model"], data["query"], data["captured_at"])).fetchone()
            snapshot_id = row["id"]
            db.execute("DELETE FROM official_points WHERE snapshot_id=?", (snapshot_id,))
            db.executemany("INSERT INTO official_points(snapshot_id,date,price) VALUES (?,?,?)", [(snapshot_id, day, price) for day, price in sorted(points.items())])
        return {"ok": True, "id": snapshot_id, "sampled_points": len(points), "read_method": "screen_verified"}

    def list_snapshots(self, days=180, category="all", model="all"):
        if category != "all" and category not in CATEGORIES:
            raise ValidationError("官方行情分类无效")
        start = (utcnow().astimezone(SHANGHAI).date() - timedelta(days=days - 1)).isoformat()
        sql, params = "SELECT * FROM official_snapshots WHERE period_end>=?", [start]
        if category != "all":
            sql += " AND category=?"
            params.append(category)
        if model != "all":
            sql += " AND model=?"
            params.append(model)
        sql += " ORDER BY captured_at DESC,id DESC"
        series = []
        with self.store.db() as db:
            for row in db.execute(sql, params).fetchall():
                points = [dict(point) for point in db.execute("SELECT date,price FROM official_points WHERE snapshot_id=? AND date>=? ORDER BY date", (row["id"], start)).fetchall()]
                if not points:
                    continue
                snapshot = {key: row[key] for key in ("id", "query", "model", "category", "metric", "captured_at", "period_start", "period_end")}
                first, last = points[0]["price"], points[-1]["price"]
                snapshot.update(read_method="screen_verified", evidence_url=f"/api/official-market/{row['id']}/evidence", points=points,
                                sampled_points=len(points), first_price=first, last_price=last,
                                change_pct=round((last / first - 1) * 100, 2) if len(points) > 1 and first > 0 else None,
                                coverage_message=f"近{days}天内人工核对{len(points)}个精确日期；指标为各日的近7日成交均价，非完整逐日数据，非自动API接入。未采样日期不插值。")
                series.append(snapshot)
        return {"series": series, "message": "闲鱼 App 镜像人工核对的成交行情，与本地挂牌观测独立。历史各日的近7日成交均价不等同于当日成交价；稀疏核对点及原图不代表完整日数据。"}

    def get_evidence(self, snapshot_id):
        with self.store.db() as db:
            row = db.execute("SELECT evidence_filename FROM official_snapshots WHERE id=?", (snapshot_id,)).fetchone()
        if not row:
            return None
        return self.evidence_file(row["evidence_filename"])
