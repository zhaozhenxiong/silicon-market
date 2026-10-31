"""SQLite persistence, point observations and auditable sample statistics."""
from __future__ import annotations

import csv
import io
import hashlib
import json
import math
import random
import sqlite3
import threading
from collections import Counter, defaultdict
from contextlib import contextmanager
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from statistics import median

from .normalization import CATEGORIES, SHANGHAI, STATUSES, ValidationError, iso, normalize_record, utcnow


def quantile(values, q):
    if not values:
        return None
    values = sorted(values)
    pos = (len(values) - 1) * q
    lo, hi = math.floor(pos), math.ceil(pos)
    return round(values[lo] + (values[hi] - values[lo]) * (pos - lo), 2)


def ratio(a, b):
    return round(a / b * 100, 2) if b else None


def local_day(stamp):
    return datetime.fromisoformat(stamp).astimezone(SHANGHAI).date().isoformat()


class Store:
    def __init__(self, path, production=False, initialize=True):
        self.path = str(path)
        self.production = production
        if not initialize and not Path(self.path).is_file():
            raise ValidationError("只读接入的现有数据库不存在")
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        if initialize:
            self.initialize()
        from .price_audit import PriceAudit
        self.price_audit = PriceAudit(self)

    @contextmanager
    def db(self):
        with self.lock:
            conn = sqlite3.connect(self.path, timeout=20)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys=ON")
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

    def initialize(self):
        with self.db() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS items (
                    id INTEGER PRIMARY KEY, source TEXT NOT NULL CHECK(source IN ('demo','live')),
                    item_id TEXT NOT NULL, title TEXT NOT NULL, url TEXT NOT NULL,
                    category TEXT NOT NULL, model TEXT NOT NULL, price REAL NOT NULL,
                    status TEXT NOT NULL, evidence TEXT NOT NULL, sold_price REAL,
                    quality_flags TEXT NOT NULL, excluded INTEGER NOT NULL,
                    first_seen TEXT NOT NULL, last_seen TEXT NOT NULL, published_at TEXT,
                    UNIQUE(source,item_id));
                CREATE TABLE IF NOT EXISTS observations (
                    id INTEGER PRIMARY KEY, item_pk INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    observed_at TEXT NOT NULL, title TEXT NOT NULL, category TEXT NOT NULL,
                    model TEXT NOT NULL, price REAL NOT NULL, status TEXT NOT NULL,
                    evidence TEXT NOT NULL, sold_price REAL, quality_flags TEXT NOT NULL,
                    excluded INTEGER NOT NULL, task_id INTEGER,run_id INTEGER,
                    UNIQUE(item_pk,observed_at));
                CREATE INDEX IF NOT EXISTS observation_time ON observations(observed_at);
                CREATE INDEX IF NOT EXISTS item_filter ON items(source,category,model,last_seen);
                CREATE TABLE IF NOT EXISTS tasks (
                    id INTEGER PRIMARY KEY, name TEXT NOT NULL, keyword TEXT NOT NULL,
                    category TEXT NOT NULL, model TEXT NOT NULL DEFAULT 'auto',
                    min_price REAL, max_price REAL, exclude_keywords TEXT NOT NULL DEFAULT '[]',
                    interval_minutes INTEGER NOT NULL DEFAULT 120, enabled INTEGER NOT NULL DEFAULT 0,
                    source TEXT NOT NULL DEFAULT 'live', last_run_at TEXT, next_run_at TEXT,
                    last_error TEXT, created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS runs (
                    id INTEGER PRIMARY KEY, task_id INTEGER, task_name TEXT NOT NULL,
                    status TEXT NOT NULL, started_at TEXT NOT NULL, finished_at TEXT,
                    items_seen INTEGER NOT NULL DEFAULT 0, items_saved INTEGER NOT NULL DEFAULT 0,
                    error TEXT);
                CREATE TABLE IF NOT EXISTS listing_jobs (
                    id INTEGER PRIMARY KEY,task_id INTEGER NOT NULL,task_snapshot TEXT NOT NULL,
                    config_hash TEXT NOT NULL,config_json TEXT NOT NULL,status TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,next_attempt_at TEXT NOT NULL,
                    lease_expires_at TEXT,run_id INTEGER,error TEXT,created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL);
                CREATE UNIQUE INDEX IF NOT EXISTS listing_one_pending ON listing_jobs(task_id)
                    WHERE status IN ('queued','running');
            """)
            run_columns = {row[1] for row in db.execute("PRAGMA table_info(runs)")}
            for column, sql_type in (("sampling_config_hash", "TEXT"), ("config_json", "TEXT"), ("initial_scan", "INTEGER"), ("listing_job_id", "INTEGER")):
                if column not in run_columns:
                    db.execute(f"ALTER TABLE runs ADD COLUMN {column} {sql_type}")
            if "run_id" not in {row[1] for row in db.execute("PRAGMA table_info(observations)")}:
                db.execute("ALTER TABLE observations ADD COLUMN run_id INTEGER")
            # A process restart must not leave jobs permanently running.
            db.execute("UPDATE runs SET status='interrupted', finished_at=?, error='服务重启，采集未完成；已有记录保留' WHERE status IN ('running','queued')", (iso(),))
            db.execute("UPDATE listing_jobs SET status=CASE WHEN attempts>=3 THEN 'failed' ELSE 'queued' END,lease_expires_at=NULL,error='服务重启，持久队列等待续跑',next_attempt_at=?,updated_at=? WHERE status='running'", (iso(), iso()))
            first = not db.execute("SELECT 1 FROM meta WHERE key='initialized'").fetchone()
            if first:
                db.execute("INSERT INTO meta VALUES ('initialized','1')")
                presets = [("RTX 4070 SUPER 显卡", "RTX 4070 SUPER", "gpu"), ("DDR5 台式机内存", "DDR5 32G", "ram"), ("Ryzen 处理器", "R7 7800X3D", "cpu"), ("B650 主板", "B650 主板", "motherboard"), ("电脑整机", "台式电脑 整机", "computer")]
                for name, keyword, category in presets:
                    db.execute("INSERT INTO tasks(name,keyword,category,model,exclude_keywords,interval_minutes,enabled,source,created_at) VALUES (?,?,?,'auto',?,120,0,'live',?)", (name, keyword, category, json.dumps(["求购", "回收", "空盒"], ensure_ascii=False), iso()))
            if not db.execute("SELECT 1 FROM meta WHERE key='phone_preset_v1'").fetchone():
                # One-time addition for existing installations; never overwrite a
                # user's task or recreate the preset after they delete it.
                existing_phone = db.execute("SELECT 1 FROM tasks WHERE category='phone' AND lower(keyword)=lower(?)", ("iPhone 15 256GB",)).fetchone()
                if not existing_phone:
                    db.execute("INSERT INTO tasks(name,keyword,category,model,exclude_keywords,interval_minutes,enabled,source,created_at) VALUES (?,?,?,'auto',?,120,0,'live',?)", ("iPhone 15 256GB 手机", "iPhone 15 256GB", "phone", json.dumps(["求购", "回收", "手机壳", "贴膜"], ensure_ascii=False), iso()))
                db.execute("INSERT INTO meta VALUES ('phone_preset_v1','1')")
        if self.production:
            self.migrate_demo()
        elif first:
            self.seed_demo()

    def migrate_demo(self):
        with self.lock:
            with self.db() as db:
                if db.execute("SELECT 1 FROM meta WHERE key='production_demo_migration_v1'").fetchone():
                    return
                count = db.execute("SELECT count(*) FROM items WHERE source='demo'").fetchone()[0]
                before_live = db.execute("SELECT count(*) FROM items WHERE source='live'").fetchone()[0]
            backup_name = None
            if count:
                directory = Path(self.path).parent / "backups"
                directory.mkdir(parents=True, exist_ok=True)
                backup_name = "before-demo-migration-" + utcnow().strftime("%Y%m%dT%H%M%S%fZ") + ".sqlite3"
                with sqlite3.connect(self.path) as source, sqlite3.connect(directory / backup_name) as target:
                    source.backup(target)
            with self.db() as db:
                db.execute("DELETE FROM items WHERE source='demo'")
                after_live = db.execute("SELECT count(*) FROM items WHERE source='live'").fetchone()[0]
                if after_live != before_live:
                    raise ValidationError("迁移校验失败：真实样本数量发生变化")
                db.execute("INSERT INTO meta(key,value) VALUES ('production_demo_migration_v1',?)", (json.dumps(dict(removed_demo=count, live_preserved=after_live, backup=backup_name, migrated_at=iso())),))

    @staticmethod
    def sampling_config(task):
        # Sampling frequency changes discovery counts even when search filters
        # stay identical. Never compare those runs as one supply time series.
        config = {key: task.get(key) for key in ("keyword", "category", "model", "min_price", "max_price", "exclude_keywords", "interval_minutes")}
        config.update(pages=1, max_items=40)
        serialized = json.dumps(config, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(serialized.encode()).hexdigest(), serialized

    def enqueue_listing(self, task):
        config_hash, config_json = self.sampling_config(task)
        with self.db() as db:
            prior = db.execute("SELECT id FROM listing_jobs WHERE task_id=? AND status IN ('queued','running')", (task["id"],)).fetchone()
            if prior:
                return prior["id"]
            stamp = iso()
            return db.execute("INSERT INTO listing_jobs(task_id,task_snapshot,config_hash,config_json,status,next_attempt_at,created_at,updated_at) VALUES (?,?,?,?,'queued',?,?,?)", (task["id"], json.dumps(task, ensure_ascii=False), config_hash, config_json, stamp, stamp, stamp)).lastrowid

    def pending_listing(self):
        with self.db() as db:
            row = db.execute("SELECT * FROM listing_jobs WHERE status='queued' AND next_attempt_at<=? ORDER BY id LIMIT 1", (iso(),)).fetchone()
        return dict(row) if row else None

    def start_listing(self, job_id):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            job = db.execute("SELECT * FROM listing_jobs WHERE id=?", (job_id,)).fetchone()
            if not job or job["status"] != "queued" or job["attempts"] >= 3:
                return None
            db.execute("UPDATE listing_jobs SET status='running',attempts=attempts+1,lease_expires_at=?,updated_at=? WHERE id=?", (iso(utcnow()+timedelta(minutes=30)), iso(), job_id))
            return json.loads(job["task_snapshot"])

    def finish_listing(self, job_id, run_id, error=None):
        with self.db() as db:
            job = db.execute("SELECT * FROM listing_jobs WHERE id=?", (job_id,)).fetchone()
            if not job:
                return
            status = "success" if not error else "failed" if job["attempts"] >= 3 else "queued"
            db.execute("UPDATE listing_jobs SET status=?,run_id=?,error=?,lease_expires_at=NULL,next_attempt_at=?,updated_at=? WHERE id=?", (status, run_id, error, iso(utcnow()+timedelta(minutes=15)), iso(), job_id))

    @staticmethod
    def item(row):
        if row is None:
            return None
        obj = dict(row)
        obj["quality_flags"] = json.loads(obj["quality_flags"])
        obj["excluded"] = bool(obj["excluded"])
        return obj

    def ingest(self, records, source="live", task=None, *, preserved_flags=None):
        if source not in {"demo", "live"}:
            raise ValidationError("数据源必须是 demo 或 live")
        if not isinstance(records, list) or not 1 <= len(records) <= 5000:
            raise ValidationError("每次须导入1至5000条记录")
        # Normalize everything before opening a transaction, so a bad row never partially imports.
        normalized = []
        for index, record in enumerate(records, 1):
            try:
                if source == "live" and isinstance(record, dict):
                    if record.get("source") == "demo" or str(record.get("item_id", "")).startswith("demo:"):
                        raise ValidationError("演示记录不能导入真实观测；请使用真实商品ID与来源数据")
                item = normalize_record(record, task)
                if source == "live":
                    nested = record.get("商品信息") or record.get("product") or {}
                    if item["item_id"].startswith("demo:") or (isinstance(nested, dict) and nested.get("source") == "demo"):
                        raise ValidationError("演示记录不能导入真实观测；请使用真实商品ID与来源数据")
                # Only internal manual-state changes provide this argument. Imported
                # quality_flags are never trusted to remove or grant eligibility.
                if preserved_flags and item["item_id"] in preserved_flags:
                    item["quality_flags"] = list(dict.fromkeys(item["quality_flags"] + preserved_flags[item["item_id"]]))
                    item["excluded"] = bool(item["quality_flags"])
                normalized.append(item)
            except (ValidationError, TypeError, ValueError, OverflowError) as exc:
                raise ValidationError(f"第{index}条记录：{exc}") from exc
        imported = skipped = 0
        warnings = set()
        with self.db() as db:
            for obs in normalized:
                prior = db.execute("SELECT * FROM items WHERE source=? AND item_id=?", (source, obs["item_id"])).fetchone()
                flags = json.dumps(obs["quality_flags"], ensure_ascii=False)
                if prior:
                    item_pk = prior["id"]
                    old = db.execute("SELECT * FROM observations WHERE item_pk=? AND observed_at=?", (item_pk, obs["observed_at"])).fetchone()
                    if old:
                        keys = ["title", "category", "model", "price", "status", "evidence", "sold_price", "excluded"]
                        if all(old[k] == obs[k] for k in keys) and old["quality_flags"] == flags:
                            skipped += 1
                            continue
                        raise ValidationError(f"商品{obs['item_id']}同一观测时间存在冲突，请修正时间或记录")
                    db.execute("UPDATE items SET first_seen=MIN(first_seen,?), published_at=COALESCE(published_at,?) WHERE id=?", (obs["observed_at"], obs["published_at"], item_pk))
                    if obs["observed_at"] >= prior["last_seen"]:
                        db.execute("UPDATE items SET title=?,url=?,category=?,model=?,price=?,status=?,evidence=?,sold_price=?,quality_flags=?,excluded=?,last_seen=?,published_at=COALESCE(?,published_at) WHERE id=?", (obs["title"], obs["url"] or prior["url"], obs["category"], obs["model"], obs["price"], obs["status"], obs["evidence"], obs["sold_price"], flags, obs["excluded"], obs["observed_at"], obs["published_at"], item_pk))
                else:
                    cur = db.execute("INSERT INTO items(source,item_id,title,url,category,model,price,status,evidence,sold_price,quality_flags,excluded,first_seen,last_seen,published_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (source, obs["item_id"], obs["title"], obs["url"], obs["category"], obs["model"], obs["price"], obs["status"], obs["evidence"], obs["sold_price"], flags, obs["excluded"], obs["observed_at"], obs["observed_at"], obs["published_at"]))
                    item_pk = cur.lastrowid
                observation_id = db.execute("INSERT INTO observations(item_pk,observed_at,title,category,model,price,status,evidence,sold_price,quality_flags,excluded,task_id,run_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", (item_pk, obs["observed_at"], obs["title"], obs["category"], obs["model"], obs["price"], obs["status"], obs["evidence"], obs["sold_price"], flags, obs["excluded"], task.get("id") if task else None, task.get("_run_id") if task else None)).lastrowid
                if source == "live" and getattr(self, "price_audit", None):
                    self.price_audit.record_in(db, item_pk, obs["item_id"], observation_id, obs["title"], obs["category"], obs["model"])
                imported += 1
                if obs["excluded"]:
                    warnings.add("含质量排除记录：已保存，但默认不计入行情与成交占比")
                if not obs["published_at"]:
                    warnings.add("部分记录无可靠发布时间；新增指标按首次观测计算")
        return {"imported": imported, "skipped": skipped, "warnings": sorted(warnings)}

    def get_item(self, item_pk):
        with self.db() as db:
            row = db.execute("SELECT * FROM items WHERE id=?", (item_pk,)).fetchone()
        item = self.item(row)
        return self.price_audit.annotate_items([item])[0] if item else None

    def history(self, item_pk):
        item = self.get_item(item_pk)
        if not item:
            return None
        with self.db() as db:
            rows = db.execute("SELECT observed_at,price,status,evidence,sold_price,title,quality_flags FROM observations WHERE item_pk=? ORDER BY observed_at DESC,id DESC", (item_pk,)).fetchall()
        observations = [dict(row) for row in rows]
        for obs in observations:
            obs["quality_flags"] = json.loads(obs["quality_flags"])
        return {"item": item, "observations": observations}

    def set_status(self, item_pk, status, evidence, sold_price=None):
        item = self.get_item(item_pk)
        if not item:
            return None
        record = {**item, "status": status, "evidence": evidence, "sold_price": sold_price, "observed_at": iso()}
        self.ingest([record], item["source"], preserved_flags={item["item_id"]: item.get("raw_quality_flags", item["quality_flags"])})
        return self.get_item(item_pk)

    def filtered_items(self, source="demo", days=30, category="all", model="all", status="all", q="", include_excluded=False, window=True):
        sql, params = "SELECT * FROM items WHERE source=?", [source]
        for field, value in [("category", category), ("model", model), ("status", status)]:
            if value and value != "all":
                sql += f" AND {field}=?"
                params.append(value)
        if q:
            sql += " AND (instr(lower(title),lower(?))>0 OR instr(lower(model),lower(?))>0 OR instr(item_id,?)>0)"
            params.extend([q, q, q])
        if not include_excluded:
            sql += " AND excluded=0"
        if window:
            sql += " AND last_seen>=?"
            params.append(self.window_start(days))
        sql += " ORDER BY last_seen DESC,id DESC"
        with self.db() as db:
            rows = db.execute(sql, params).fetchall()
        items = self.price_audit.annotate_items([self.item(row) for row in rows])
        return items if include_excluded else [item for item in items if not item["excluded"]]

    @staticmethod
    def window_start(days):
        today = utcnow().astimezone(SHANGHAI).date()
        return iso(datetime.combine(today - timedelta(days=days - 1), time(), SHANGHAI))

    def overview(self, source="demo", days=30, category="all", model="all"):
        all_items = self.filtered_items(source, days, category, model, include_excluded=True)
        items = [item for item in all_items if not item["excluded"]]
        start, today = self.window_start(days), utcnow().astimezone(SHANGHAI).date()
        with self.db() as db:
            observations = [dict(row) for row in db.execute("SELECT o.* FROM observations o JOIN items i ON i.id=o.item_pk WHERE i.source=? ORDER BY o.observed_at,o.id", (source,)).fetchall()]
        if source == "live":
            observations = self.price_audit.mask_observations(observations)
        ids = {item["id"] for item in items}
        observations = [obs for obs in observations if obs["item_pk"] in ids]
        first_sold = {}
        daily = defaultdict(dict)
        for obs in observations:
            if not obs["excluded"] and obs["status"] == "sold" and obs["evidence"]:
                first_sold.setdefault(obs["item_pk"], obs["observed_at"])
            if obs["observed_at"] >= start:
                daily[local_day(obs["observed_at"])][obs["item_pk"]] = obs
        cohort = [item for item in items if item["first_seen"] >= start]
        confirmed = sum(item["id"] in first_sold for item in cohort)
        prices = [item["price"] for item in items if item["status"] == "on_sale"]
        summary = dict(total_items=len(items), active_items=len(prices), new_items=len(cohort), confirmed_sold=confirmed,
                       sold_ratio=ratio(confirmed, len(cohort)), median_price=quantile(prices, .5),
                       p25_price=quantile(prices, .25), p75_price=quantile(prices, .75), excluded_items=len(all_items) - len(items),
                       published_items=sum(bool(item["published_at"] and item["published_at"] >= start) for item in items),
                       last_observed_at=max((i["last_seen"] for i in all_items), default=None))
        trend = []
        first_days = Counter(local_day(item["first_seen"]) for item in items)
        sold_days = Counter(local_day(stamp) for stamp in first_sold.values())
        for offset in range(days - 1, -1, -1):
            date = (today - timedelta(days=offset)).isoformat()
            vals = [obs["price"] for obs in daily[date].values() if obs["status"] == "on_sale" and not obs["excluded"]]
            trend.append(dict(date=date, median_price=quantile(vals, .5), new_items=first_days[date], confirmed_sold=sold_days[date], samples=len(vals)))
        distribution = []
        if prices:
            lo, hi = min(prices), max(prices)
            if hi == lo:
                distribution = [dict(label=f"¥{lo:g}", count=len(prices), min=lo, max=hi)]
            else:
                width = (hi - lo) / 6
                for index in range(6):
                    a, b = lo + width * index, lo + width * (index + 1)
                    count = sum(a <= p < b if index < 5 else a <= p <= b for p in prices)
                    distribution.append(dict(label=f"{a:,.0f}–{b:,.0f}", count=count, min=round(a, 2), max=round(b, 2)))
        groups = defaultdict(list)
        for item in items:
            groups[(item["category"], item["model"])].append(item)
        models = []
        for (cat, mod), group in groups.items():
            p = [item["price"] for item in group if item["status"] == "on_sale"]
            new = [item for item in group if item["first_seen"] >= start]
            sold = sum(item["id"] in first_sold for item in new)
            models.append(dict(category=cat, model=mod, active_items=len(p), median_price=quantile(p, .5), new_items=len(new), confirmed_sold=sold, sold_ratio=ratio(sold, len(new))))
        models.sort(key=lambda m: (-m["active_items"], m["model"]))
        return dict(source=source, days=days, summary=summary, trend=trend, distribution=distribution, models=models,
                    coverage=dict(message="演示数据，仅用于体验功能，不代表真实市场行情。" if source == "demo" else "仅覆盖所选时间内最近观测的本地样本；搜索排序、登录和采集范围会影响覆盖率。在售为最近一次观测状态，未复查不等于实时在售。",
                                  price_basis="正常质量样本的最近一次在售挂牌价；趋势为每日去重观测挂牌中位价，非成交价。不同型号汇总只反映采样构成，请筛选型号后比较。",
                                  ratio_basis=f"{days}日内首次观测的正常样本为同一队列；明确售出件数 ÷ 队列件数。下架和未知不计售出，新增观测不等于全市场新发布。"))

    def price_trends(self, source="live", days=180, category="all", model="all"):
        """Daily listing prices from actual snapshots, using historical metadata.

        First choose each item's final observation that day, then evaluate its
        state and quality. Filtering before this choice would incorrectly retain
        an earlier on-sale snapshot when it was sold/excluded later that day.
        """
        start = self.window_start(days)
        today = utcnow().astimezone(SHANGHAI).date()
        dates = [(today - timedelta(days=offset)).isoformat() for offset in range(days - 1, -1, -1)]
        with self.db() as db:
            rows = db.execute("SELECT o.* FROM observations o JOIN items i ON i.id=o.item_pk WHERE i.source=? AND o.observed_at>=? ORDER BY o.observed_at,o.id", (source, start)).fetchall()
        if source == "live":
            rows = self.price_audit.mask_observations(rows)
        daily_last = {}
        for row in rows:
            daily_last[(local_day(row["observed_at"]), row["item_pk"])] = dict(row)
        eligible = []
        for (date, _), obs in daily_last.items():
            if date > dates[-1] or obs["excluded"] or obs["status"] != "on_sale":
                continue
            if category != "all" and obs["category"] != category:
                continue
            if model != "all" and obs["model"] != model:
                continue
            eligible.append(obs)

        def points(observations):
            by_date = defaultdict(list)
            for obs in observations:
                by_date[local_day(obs["observed_at"])].append(obs["price"])
            result = []
            for date in dates:
                values = by_date[date]
                result.append(dict(date=date, samples=len(values), min_price=min(values) if values else None,
                                   p25_price=quantile(values, .25), median_price=quantile(values, .5),
                                   mean_price=round(sum(values) / len(values), 2) if values else None,
                                   p75_price=quantile(values, .75), max_price=max(values) if values else None))
            return result

        groups = defaultdict(list)
        for obs in eligible:
            groups[(obs["category"], obs["model"])].append(obs)
        series = []
        for (cat, mod), observations in sorted(groups.items()):
            daily_points = points(observations)
            observed_points = [point for point in daily_points if point["samples"]]
            first_price, latest_price = observed_points[0]["median_price"], observed_points[-1]["median_price"]
            series.append(dict(category=cat, model=mod, observed_days=len(observed_points),
                               total_items=len({obs["item_pk"] for obs in observations}), latest_price=latest_price,
                               change_pct=round((latest_price / first_price - 1) * 100, 2) if len(observed_points) >= 2 and first_price > 0 else None,
                               points=daily_points))
        observed_dates = {local_day(obs["observed_at"]) for obs in eligible}
        stamps = [obs["observed_at"] for obs in eligible]
        actual = len(observed_dates)
        message = f"请求{days}天 / 实际有效挂牌观测{actual}天。空白日期无观测，未以当前价格或发布时间回填；挂牌样本不能代替官方成交行情。"
        if source == "demo":
            message = "演示数据，不代表真实历史行情。" + message
        return dict(source=source, days=days, basis="listing", coverage=dict(
            start_date=dates[0], end_date=dates[-1], observed_days=actual, requested_days=days,
            first_observed_at=min(stamps, default=None), last_observed_at=max(stamps, default=None), message=message),
            aggregate=points(eligible), series=series)

    @staticmethod
    def task(row):
        if row is None:
            return None
        obj = dict(row)
        obj["exclude_keywords"] = json.loads(obj["exclude_keywords"])
        obj["enabled"] = bool(obj["enabled"])
        return obj

    def tasks(self):
        with self.db() as db:
            return [self.task(row) for row in db.execute("SELECT * FROM tasks ORDER BY id")]

    def get_task(self, task_id):
        with self.db() as db:
            return self.task(db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone())

    def save_task(self, payload, task_id=None):
        old = self.get_task(task_id) if task_id else None
        if task_id and not old:
            return None
        allowed = {"name", "keyword", "category", "model", "min_price", "max_price", "exclude_keywords", "interval_minutes", "enabled", "source"}
        if set(payload) - allowed:
            raise ValidationError("任务包含未知字段")
        data = dict(name="", keyword="", category="component", model="auto", min_price=None, max_price=None,
                    exclude_keywords=[], interval_minutes=120, enabled=False, source="live")
        if old:
            data.update({k: old[k] for k in data})
        data.update(payload)
        for key in ("name", "keyword", "model"):
            if not isinstance(data[key], str) or not data[key].strip() or len(data[key]) > 160:
                raise ValidationError(f"{key} 须为1至160字文本")
            data[key] = data[key].strip()
        if not isinstance(data["category"], str) or data["category"] not in CATEGORIES or data["source"] != "live":
            raise ValidationError("任务须选择有效硬件分类，且仅使用 live 数据源")
        n = data["interval_minutes"]
        if isinstance(n, bool) or not isinstance(n, int) or not 30 <= n <= 10080:
            raise ValidationError("采集间隔须为30至10080分钟的整数")
        if not isinstance(data["enabled"], bool):
            raise ValidationError("enabled 必须是布尔值")
        if not isinstance(data["exclude_keywords"], list) or len(data["exclude_keywords"]) > 100 or any(not isinstance(x, str) or len(x) > 100 for x in data["exclude_keywords"]):
            raise ValidationError("排除关键词须为文本数组，最多100项")
        from .normalization import number
        for key in ("min_price", "max_price"):
            data[key] = number(data[key], key, nullable=True)
        if data["min_price"] is not None and data["max_price"] is not None and data["min_price"] > data["max_price"]:
            raise ValidationError("最低价不能大于最高价")
        data["exclude_keywords"] = json.dumps(data["exclude_keywords"], ensure_ascii=False)
        next_run = iso(utcnow() + timedelta(minutes=data["interval_minutes"])) if data["enabled"] else None
        with self.db() as db:
            if task_id:
                fields = list(data)
                db.execute(f"UPDATE tasks SET {','.join(f'{key}=?' for key in fields)},next_run_at=? WHERE id=?", [data[key] for key in fields] + [next_run, task_id])
                if payload.get("enabled") is False:
                    db.execute("UPDATE listing_jobs SET status='cancelled',error='操作员已停用任务，待执行轮次取消',updated_at=? WHERE task_id=? AND status='queued'", (iso(), task_id))
            else:
                fields = list(data)
                cur = db.execute(f"INSERT INTO tasks({','.join(fields)},next_run_at,created_at) VALUES ({','.join('?' for _ in range(len(fields)+2))})", [data[key] for key in fields] + [next_run, iso()])
                task_id = cur.lastrowid
        return self.get_task(task_id)

    def delete_task(self, task_id):
        with self.db() as db:
            db.execute("UPDATE listing_jobs SET status='cancelled',error='任务已删除，待执行轮次取消',updated_at=? WHERE task_id=? AND status='queued'", (iso(), task_id))
            return bool(db.execute("DELETE FROM tasks WHERE id=?", (task_id,)).rowcount)

    def tracked_items(self, task):
        with self.db() as db:
            rows = db.execute("SELECT DISTINCT i.* FROM items i LEFT JOIN observations o ON o.item_pk=i.id WHERE i.source='live' AND i.excluded=0 AND i.status IN ('on_sale','unknown') AND (o.task_id=? OR (i.category=? AND instr(lower(i.title),lower(?))>0)) ORDER BY i.last_seen LIMIT 10", (task["id"], task["category"], task["keyword"])).fetchall()
        return [self.item(row) for row in rows]

    def begin_run(self, task, listing_job_id=None):
        stamp = iso()
        config_hash, config_json = self.sampling_config(task)
        with self.db() as db:
            initial = not db.execute("SELECT 1 FROM runs WHERE task_id=? AND sampling_config_hash=? AND status='success' LIMIT 1", (task["id"], config_hash)).fetchone()
            cur = db.execute("INSERT INTO runs(task_id,task_name,status,started_at,sampling_config_hash,config_json,initial_scan,listing_job_id) VALUES (?,?,'running',?,?,?,?,?)", (task["id"], task["name"], stamp, config_hash, config_json, int(initial), listing_job_id))
            db.execute("UPDATE tasks SET last_run_at=?,next_run_at=?,last_error=NULL WHERE id=?", (stamp, iso(utcnow() + timedelta(minutes=task["interval_minutes"])), task["id"]))
            return cur.lastrowid

    def finish_run(self, run_id, task_id, seen=0, saved=0, error=None):
        with self.db() as db:
            db.execute("UPDATE runs SET status=?,finished_at=?,items_seen=?,items_saved=?,error=? WHERE id=?", ("failed" if error else "success", iso(), seen, saved, error, run_id))
            db.execute("UPDATE tasks SET last_error=? WHERE id=?", (error, task_id))

    def runs(self, limit=20):
        with self.db() as db:
            return [dict(row) for row in db.execute("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,))]

    def export_csv(self, **filters):
        items = self.filtered_items(**filters)
        out = io.StringIO()
        fields = ["source", "item_id", "title", "category", "model", "price", "status", "sold_price", "evidence", "published_at", "first_seen", "last_seen", "quality_flags", "url"]
        writer = csv.DictWriter(out, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for item in items:
            item["quality_flags"] = ";".join(item["quality_flags"])
            # Prevent spreadsheet formula injection from untrusted listing titles/evidence.
            for key in fields:
                value = item.get(key)
                if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@", "\t", "\r")):
                    item[key] = "'" + value
            writer.writerow(item)
        return "\ufeff" + out.getvalue()

    def seed_demo(self):
        rng, now = random.Random(27182), utcnow()
        today = now.astimezone(SHANGHAI).date()
        presets = [("gpu", "RTX 4070 SUPER 12GB", "微星 RTX 4070 SUPER 12G 自用显卡", 3400),
                   ("gpu", "RTX 4060 Ti 8GB", "华硕 RTX 4060 Ti 8G 游戏显卡", 2100),
                   ("gpu", "RX 7800 XT 16GB", "蓝宝石 RX 7800 XT 16G 显卡", 2700),
                   ("ram", "DDR5 32GB (16GB×2) 6000", "金士顿 DDR5 16G×2 6000 内存套条", 620),
                   ("ram", "DDR4 16GB 3200", "威刚 DDR4 16G 3200 台式机内存", 160),
                   ("cpu", "Ryzen 7800X3D", "AMD R7 7800X3D 散片处理器", 1950),
                   ("motherboard", "B650M", "微星 B650M 迫击炮 主板", 680),
                   ("storage", "SSD 2TB", "三星 990 PRO 2TB NVMe SSD 固态硬盘", 800),
                   ("computer", "办公整机 i5-12400", "办公台式电脑整机 i5-12400 16G", 1500),
                   ("component", "STM32F103 开发板", "STM32F103 最小系统开发板 元件", 25)]
        rows = []
        for group, (category, model, title, base) in enumerate(presets):
            for j in range(18):
                first_date = today - timedelta(days=rng.randint(1, 27))
                first = datetime.combine(first_date, time(9, rng.randrange(60)), SHANGHAI).astimezone(timezone.utc)
                factor = rng.uniform(.85, 1.16)
                end_state = "sold" if j in {0, 4, 9, 13} else "delisted" if j in {3, 14} else "unknown" if j == 6 else "on_sale"
                faulty = j == 17
                obs_time = first
                while obs_time < now:
                    age = (obs_time - first).total_seconds() / 86400
                    status = end_state if age >= 3 else "on_sale"
                    price = round(base * factor * (1 - min(age, 25) * .002) / (10 if base > 100 else 1)) * (10 if base > 100 else 1)
                    if faulty:
                        price = round(price * .28, 2)
                    rows.append(dict(item_id=f"demo:{group}:{j}", title=title + (" 故障维修件" if faulty else " 自用闲置"), category=category, model=model, price=price,
                                     status=status, evidence="演示：商品详情页明确显示已售出" if status == "sold" else "演示：商品详情页明确显示已下架" if status == "delisted" else "演示：暂未复查状态" if status == "unknown" else "演示：搜索页观察到挂牌",
                                     published_at=iso(first - timedelta(hours=rng.randint(0, 20))) if j % 3 else None, observed_at=iso(obs_time)))
                    obs_time += timedelta(days=3)
                if rows and rows[-1]["item_id"] == f"demo:{group}:{j}":
                    final = {**rows[-1], "observed_at": iso(now - timedelta(minutes=j + group)), "status": end_state}
                    final["evidence"] = "演示：人工核实已售出（无实际成交价）" if end_state == "sold" else "演示：已下架，不代表成交" if end_state == "delisted" else "演示：未能确认当前状态" if end_state == "unknown" else "演示：最新挂牌观测"
                    rows.append(final)
        # Normalize before removal, preserving live rows on any error.
        normalized = [normalize_record(row) for row in rows]
        with self.lock:
            with self.db() as db:
                db.execute("DELETE FROM items WHERE source='demo'")
            self.ingest(normalized, source="demo")
        return {"ok": True, "items": 180, "message": "演示样本已重建；真实采集数据保持不变"}
