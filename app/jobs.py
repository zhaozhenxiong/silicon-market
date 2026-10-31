"""Durable mobile date jobs, one real device lease and immutable evidence reads."""
import hashlib
import json
import secrets
from datetime import datetime, timedelta

from .catalog import canonical, identity, scope_payload
from .normalization import SHANGHAI, ValidationError, iso, number, utcnow
from .official_market import METRIC, exact_date


class LeaseError(ValidationError):
    pass


class MobileJobs:
    LEASE_SECONDS = 180
    MAX_ATTEMPTS = 6

    def __init__(self, store, catalog, official):
        self.store, self.catalog, self.official = store, catalog, official
        with store.db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS mobile_batches (
                    id INTEGER PRIMARY KEY,signature TEXT UNIQUE NOT NULL,days INTEGER NOT NULL,
                    end_date TEXT NOT NULL,created_at TEXT NOT NULL,catalog_version TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS mobile_jobs (
                    id INTEGER PRIMARY KEY,batch_id INTEGER NOT NULL REFERENCES mobile_batches(id),
                    catalog_id INTEGER NOT NULL,category TEXT NOT NULL,model TEXT NOT NULL,
                    query TEXT NOT NULL,scope TEXT NOT NULL,market_scope_id TEXT NOT NULL,
                    target_date TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0,price REAL,error TEXT,
                    lease_token TEXT,worker_id TEXT,lease_expires_at TEXT,reading_id INTEGER,
                    updated_at TEXT NOT NULL,UNIQUE(batch_id,catalog_id,target_date));
                CREATE INDEX IF NOT EXISTS mobile_job_claim ON mobile_jobs(status,catalog_id,target_date);
                CREATE INDEX IF NOT EXISTS mobile_job_scope ON mobile_jobs(catalog_id,market_scope_id,status);
                CREATE TABLE IF NOT EXISTS mobile_device (
                    id INTEGER PRIMARY KEY CHECK(id=1),worker_id TEXT,lease_token TEXT,
                    lease_expires_at TEXT,last_heartbeat TEXT,device_status TEXT NOT NULL DEFAULT 'disconnected',message TEXT);
                INSERT OR IGNORE INTO mobile_device(id) VALUES (1);
                CREATE TABLE IF NOT EXISTS mobile_readings (
                    id INTEGER PRIMARY KEY,job_id INTEGER NOT NULL,market_scope_id TEXT NOT NULL,
                    date TEXT NOT NULL,price REAL NOT NULL,evidence_filename TEXT NOT NULL,
                    evidence_hash TEXT NOT NULL,read_method TEXT NOT NULL,observed_model TEXT NOT NULL,
                    scope TEXT NOT NULL,ocr_text TEXT,parser_version TEXT,verification_count INTEGER NOT NULL,
                    status TEXT NOT NULL,created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS mobile_points (
                    market_scope_id TEXT NOT NULL,metric TEXT NOT NULL,date TEXT NOT NULL,
                    price REAL NOT NULL,reading_id INTEGER NOT NULL REFERENCES mobile_readings(id),
                    verified_at TEXT NOT NULL,PRIMARY KEY(market_scope_id,metric,date));
                CREATE TABLE IF NOT EXISTS mobile_job_events (
                    id INTEGER PRIMARY KEY,job_id INTEGER,status TEXT NOT NULL,message TEXT,created_at TEXT NOT NULL);
            """)

    @staticmethod
    def worker(value):
        if not isinstance(value, str) or not 1 <= len(value) <= 120:
            raise ValidationError("worker_id 须为1至120字")
        return value

    def _lease(self, db, worker_id, token):
        row = db.execute("SELECT * FROM mobile_device WHERE id=1").fetchone()
        if not token or row["worker_id"] != worker_id or row["lease_token"] != token or not row["lease_expires_at"] or row["lease_expires_at"] <= iso():
            raise LeaseError("设备租约无效或过期，请重新确认设备状态后领取")
        return row

    @staticmethod
    def _current_catalog(db, job):
        current = db.execute("SELECT * FROM catalog WHERE id=?", (job["catalog_id"],)).fetchone()
        if not current or current["market_scope_id"] != job["market_scope_id"]:
            raise ValidationError("目录范围已修订，须重建本范围批次；冻结任务不接受新范围读数")
        if not current["enabled"] or current["status"] in {"needs_review", "no_market", "unsupported"}:
            raise ValidationError("目录已停用或仍待审核，不能接受读数")
        scope = json.loads(current["scope"])
        if scope.get("verified") is not True:
            raise ValidationError("目录的实际App筛选范围尚未核对；须先确认scope后再接受读数")
        return current, scope

    @staticmethod
    def _review_obsolete_scopes(db, stamp):
        outdated = db.execute("""SELECT j.id,j.lease_token FROM mobile_jobs j
            LEFT JOIN catalog c ON c.id=j.catalog_id
            WHERE j.status IN ('pending','waiting_device','reading')
            AND (c.id IS NULL OR c.market_scope_id!=j.market_scope_id)""").fetchall()
        tokens = set()
        message = "目录范围已修订，须重建本范围批次"
        for job in outdated:
            if job["lease_token"]:
                tokens.add(job["lease_token"])
            db.execute("UPDATE mobile_jobs SET status='needs_review',error=?,lease_token=NULL,worker_id=NULL,lease_expires_at=NULL,updated_at=? WHERE id=?", (message, stamp, job["id"]))
            db.execute("INSERT INTO mobile_job_events(job_id,status,message,created_at) VALUES (?,'needs_review',?,?)", (job["id"], message, stamp))
        for token in tokens:
            if not db.execute("SELECT 1 FROM mobile_jobs WHERE status='reading' AND lease_token=?", (token,)).fetchone():
                db.execute("UPDATE mobile_device SET lease_token=NULL,device_status=CASE WHEN device_status='reading' THEN 'ready' ELSE device_status END WHERE id=1 AND lease_token=?", (token,))

    @staticmethod
    def decode_job(row):
        item = dict(row)
        item["scope"] = json.loads(item["scope"])
        item["evidence_url"] = f"/api/mobile/evidence/{item['reading_id']}" if item["reading_id"] else None
        item.pop("lease_token", None)
        return item

    def sync_catalog_state(self, catalog_id):
        """Propagate scope review to date tasks without manufacturing readings."""
        current = self.catalog.get(catalog_id)
        if current is None:
            return
        marker = "目录核对等待："
        blocked = not current["enabled"] or current["status"] in {"needs_review", "no_market", "unsupported"}
        approved = current["enabled"] and not blocked and current["scope"].get("verified") is True
        stamp = iso()
        with self.store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            self._review_obsolete_scopes(db, stamp)
            if blocked:
                message = marker + (current.get("last_error") or "实际App型号或筛选范围尚待复核；未读取日期价格")[:1800]
                rows = db.execute("SELECT id FROM mobile_jobs WHERE catalog_id=? AND market_scope_id=? AND status IN ('pending','waiting_device','reading')", (catalog_id, current["market_scope_id"])).fetchall()
                for row in rows:
                    db.execute("UPDATE mobile_jobs SET status='needs_review',error=?,lease_token=NULL,worker_id=NULL,lease_expires_at=NULL,updated_at=? WHERE id=?", (message, stamp, row["id"]))
                    db.execute("INSERT INTO mobile_job_events(job_id,status,message,created_at) VALUES (?,'needs_review',?,?)", (row["id"], message, stamp))
            elif approved:
                rows = db.execute("SELECT id FROM mobile_jobs WHERE catalog_id=? AND market_scope_id=? AND status='needs_review' AND error LIKE ? AND attempts<?", (catalog_id, current["market_scope_id"], marker+'%', self.MAX_ATTEMPTS)).fetchall()
                for row in rows:
                    db.execute("UPDATE mobile_jobs SET status='pending',error=NULL,updated_at=? WHERE id=?", (stamp, row["id"]))
                    db.execute("INSERT INTO mobile_job_events(job_id,status,message,created_at) VALUES (?,'pending',?,?)", (row["id"], "目录同一范围已完成核对，等待实际手机读数；未重置逐日冲突或失败记录", stamp))
            if not db.execute("SELECT 1 FROM mobile_jobs WHERE status='reading' AND lease_token=(SELECT lease_token FROM mobile_device WHERE id=1)").fetchone():
                db.execute("UPDATE mobile_device SET lease_token=NULL,device_status=CASE WHEN device_status='reading' THEN 'ready' ELSE device_status END WHERE id=1")

    def create_batch(self, payload):
        if not isinstance(payload, dict) or set(payload) - {"catalog_ids", "days", "end_date"}:
            raise ValidationError("批次包含未知字段")
        days = payload.get("days", 180)
        if isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= 365:
            raise ValidationError("days 须为1至365整数")
        yesterday = utcnow().astimezone(SHANGHAI).date() - timedelta(days=1)
        end = exact_date(payload.get("end_date", yesterday.isoformat()), "end_date")
        if end > yesterday:
            raise ValidationError("完整日批次截止日最晚为上海昨天")
        ids = payload.get("catalog_ids")
        if ids is None:
            targets = [item for item in self.catalog.list()["items"] if item["enabled"]]
        else:
            if not isinstance(ids, list) or any(isinstance(x, bool) or not isinstance(x, int) for x in ids):
                raise ValidationError("catalog_ids 须为整数数组")
            targets = [self.catalog.get(item_id) for item_id in sorted(set(ids))]
            if any(target is None for target in targets):
                raise ValidationError("目录不存在")
        if not targets:
            raise ValidationError("没有可采集的目标目录，请先同步目录")
        versions = [[item["id"], item["version"], item["market_scope_id"]] for item in targets]
        signature = hashlib.sha256(canonical([versions, days, end.isoformat()]).encode()).hexdigest()
        dates = [(end - timedelta(days=offset)).isoformat() for offset in range(days-1, -1, -1)]
        stamp = iso()
        with self.store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            prior = db.execute("SELECT id FROM mobile_batches WHERE signature=?", (signature,)).fetchone()
            if prior:
                batch_id = prior["id"]
            else:
                cur = db.execute("INSERT INTO mobile_batches(signature,days,end_date,created_at,catalog_version) VALUES (?,?,?,?,?)", (signature, days, end.isoformat(), stamp, canonical(versions)))
                batch_id = cur.lastrowid
                for target in targets:
                    for day in dates:
                        point = db.execute("SELECT * FROM mobile_points WHERE market_scope_id=? AND metric=? AND date=?", (target["market_scope_id"], METRIC, day)).fetchone()
                        db.execute("INSERT INTO mobile_jobs(batch_id,catalog_id,category,model,query,scope,market_scope_id,target_date,status,price,reading_id,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (batch_id, target["id"], target["category"], target["model"], target["query"], canonical(target["scope"]), target["market_scope_id"], day, "verified" if point else "pending", point["price"] if point else None, point["reading_id"] if point else None, stamp))
        for target in targets:
            self.sync_catalog_state(target["id"])
        summary = next(batch for batch in self.batches()["batches"] if batch["id"] == batch_id)
        return {**summary, "existing_verified": summary["verified_days"]}

    def batches(self):
        with self.store.db() as db:
            batches = []
            for row in db.execute("SELECT * FROM mobile_batches ORDER BY id DESC").fetchall():
                counts = {r["status"]: r["n"] for r in db.execute("SELECT status,count(*) n FROM mobile_jobs WHERE batch_id=? GROUP BY status", (row["id"],))}
                total, verified = sum(counts.values()), counts.get("verified", 0)
                pending = sum(counts.get(key, 0) for key in ("pending", "reading", "waiting_device"))
                review = counts.get("needs_review", 0) + counts.get("failed", 0)
                status = "complete" if verified == total and total else "partial" if pending == 0 else "waiting_device" if counts.get("waiting_device") and not counts.get("reading") else "running" if counts.get("reading") else "pending"
                targets = db.execute("SELECT count(DISTINCT catalog_id) FROM mobile_jobs WHERE batch_id=?", (row["id"],)).fetchone()[0]
                batches.append(dict(id=row["id"], status=status, days=row["days"], end_date=row["end_date"], total_targets=targets, total_days=total, verified_days=verified, pending_days=pending, review_days=review, unavailable_days=counts.get("unavailable", 0), coverage_pct=round(verified/total*100, 2) if total else 0, created_at=row["created_at"]))
        return {"batches": batches}

    def jobs(self, batch_id=None, catalog_id=None, limit=100, offset=0):
        sql, args = "FROM mobile_jobs WHERE 1=1", []
        for field, value in (("batch_id", batch_id), ("catalog_id", catalog_id)):
            if value is not None:
                sql += f" AND {field}=?"
                args.append(value)
        with self.store.db() as db:
            count = db.execute("SELECT count(*) " + sql, args).fetchone()[0]
            rows = db.execute("SELECT * " + sql + " ORDER BY id LIMIT ? OFFSET ?", args+[limit, offset]).fetchall()
        return {"jobs": [self.decode_job(row) for row in rows], "total": count}

    def heartbeat(self, payload):
        worker = self.worker(payload.get("worker_id"))
        status = payload.get("device_status")
        if not isinstance(status, str) or status not in {"ready", "locked", "verification", "disconnected", "reading", "waiting_user"}:
            raise ValidationError("device_status 无效")
        message = str(payload.get("message") or "")[:1000]
        token = payload.get("lease_token")
        stamp, expiry = iso(), iso(utcnow()+timedelta(seconds=self.LEASE_SECONDS))
        with self.store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT * FROM mobile_device WHERE id=1").fetchone()
            live = old["lease_token"] and old["lease_expires_at"] and old["lease_expires_at"] > stamp
            if live and (old["worker_id"] != worker or old["lease_token"] != token):
                raise LeaseError("设备正在由另一个有效租约占用")
            if status in {"locked", "verification", "disconnected", "waiting_user"}:
                if old["lease_token"]:
                    db.execute("UPDATE mobile_jobs SET status='waiting_device',error=?,lease_token=NULL,worker_id=NULL,lease_expires_at=NULL,updated_at=? WHERE status='reading' AND lease_token=?", (message or status, stamp, old["lease_token"]))
                db.execute("UPDATE mobile_device SET worker_id=?,lease_token=NULL,lease_expires_at=?,last_heartbeat=?,device_status=?,message=? WHERE id=1", (worker, expiry, stamp, status, message))
            else:
                # A no-token initial heartbeat is an explicit worker report of a
                # real connection; it does not claim any reading jobs.
                db.execute("UPDATE mobile_device SET worker_id=?,lease_token=?,lease_expires_at=?,last_heartbeat=?,device_status=?,message=? WHERE id=1", (worker, old["lease_token"] if live else None, expiry, stamp, status, message))
                if live:
                    db.execute("UPDATE mobile_jobs SET lease_expires_at=? WHERE lease_token=? AND status='reading'", (expiry, token))
        return {"ok": True}

    def claim(self, payload):
        worker = self.worker(payload.get("worker_id"))
        limit = payload.get("limit", 40)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 40:
            raise ValidationError("limit 须为1至40")
        if payload.get("catalog_id") is not None and (isinstance(payload["catalog_id"], bool) or not isinstance(payload["catalog_id"], int)):
            raise ValidationError("catalog_id 须为整数")
        stamp, expiry = iso(), iso(utcnow()+timedelta(seconds=self.LEASE_SECONDS))
        with self.store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            self._review_obsolete_scopes(db, stamp)
            device = db.execute("SELECT * FROM mobile_device WHERE id=1").fetchone()
            fresh = device["last_heartbeat"] and device["lease_expires_at"] and device["lease_expires_at"] > stamp
            if not fresh or device["device_status"] not in {"ready", "reading"} or device["worker_id"] != worker:
                raise LeaseError("请由已连接设备的worker先发送ready心跳，锁屏或离线不能领取")
            if device["lease_token"] and device["lease_expires_at"] > stamp:
                raise LeaseError("设备已有有效租约，请完成或释放当前任务后再领取")
            db.execute("UPDATE mobile_jobs SET status=CASE WHEN attempts>=? THEN 'failed' ELSE 'pending' END,lease_token=NULL,worker_id=NULL,lease_expires_at=NULL,error='租约过期，等待续跑',updated_at=? WHERE status='reading' AND lease_expires_at<=?", (self.MAX_ATTEMPTS, stamp, stamp))
            args = [self.MAX_ATTEMPTS]
            sql = """SELECT j.*,c.scope AS current_scope FROM mobile_jobs j
                JOIN catalog c ON c.id=j.catalog_id AND c.market_scope_id=j.market_scope_id
                WHERE j.status IN ('pending','waiting_device') AND j.attempts<? AND c.enabled=1
                AND c.status NOT IN ('needs_review','no_market','unsupported')"""
            if payload.get("catalog_id") is not None:
                sql += " AND j.catalog_id=?"
                args.append(payload["catalog_id"])
            eligible = [row for row in db.execute(sql+" ORDER BY j.id", args).fetchall()
                        if json.loads(row["current_scope"]).get("verified") is True]
            first = eligible[0] if eligible else None
            if first is None:
                return {"lease_token": None, "jobs": [], "catalog": None, "lease_expires_at": None}
            rows = sorted([row for row in eligible if row["catalog_id"] == first["catalog_id"] and row["market_scope_id"] == first["market_scope_id"]], key=lambda row: (row["target_date"], row["id"]))[:limit]
            rows = list({row["target_date"]: row for row in reversed(rows)}.values())
            rows.sort(key=lambda row: row["target_date"])
            token = secrets.token_urlsafe(32)
            for row in rows:
                db.execute("UPDATE mobile_jobs SET status='reading',attempts=attempts+1,worker_id=?,lease_token=?,lease_expires_at=?,updated_at=? WHERE id=?", (worker, token, expiry, stamp, row["id"]))
            db.execute("UPDATE mobile_device SET lease_token=?,lease_expires_at=?,device_status='reading' WHERE id=1", (token, expiry))
            catalog = dict(id=first["catalog_id"], category=first["category"], model=first["model"], query=first["query"], scope=json.loads(first["current_scope"]), market_scope_id=first["market_scope_id"])
            result = [self.decode_job(db.execute("SELECT * FROM mobile_jobs WHERE id=?", (row["id"],)).fetchone()) for row in rows]
        return {"lease_token": token, "jobs": result, "catalog": catalog, "lease_expires_at": expiry}

    def submit(self, payload):
        worker, token = self.worker(payload.get("worker_id")), payload.get("lease_token")
        readings = payload.get("readings")
        if not isinstance(readings, list) or not 1 <= len(readings) <= 100:
            raise ValidationError("readings 须为1至100条")
        prepared = []
        seen_jobs = set()
        with self.store.db() as db:
            self._lease(db, worker, token)
            for reading in readings:
                if not isinstance(reading, dict):
                    raise ValidationError("读数须为对象")
                if isinstance(reading.get("job_id"), bool) or not isinstance(reading.get("job_id"), int) or reading["job_id"] in seen_jobs:
                    raise ValidationError("job_id 须为不重复整数")
                seen_jobs.add(reading["job_id"])
                job = db.execute("SELECT * FROM mobile_jobs WHERE id=?", (reading.get("job_id"),)).fetchone()
                if job is None or job["lease_token"] != token or job["worker_id"] != worker or job["status"] != "reading":
                    raise LeaseError("任务不属于当前有效租约")
                day = exact_date(reading.get("displayed_date"), "displayed_date")
                if day.isoformat() != job["target_date"] or day >= utcnow().astimezone(SHANGHAI).date():
                    raise ValidationError("屏幕实际日期须精确等于目标完整日，不能从坐标推断")
                if reading.get("read_method") != "mobile_screen_ocr" or reading.get("verification_count") != 2:
                    raise ValidationError("读数须为mobile_screen_ocr且完成两次一致复读")
                frozen_scope = json.loads(job["scope"])
                _, proven_scope = self._current_catalog(db, job)
                allowed_models = {identity(job["model"])}
                # Query/aliases form explicit operator-approved equivalence,
                # only after the exact App filters have been scope-verified.
                if proven_scope.get("verified") is True:
                    allowed_models.add(identity(job["query"]))
                    allowed_models.update(identity(alias) for alias in proven_scope.get("aliases", []))
                if identity(reading.get("observed_model")) not in allowed_models:
                    raise ValidationError("屏幕实际型号与目录不符，禁止继承宽口径或其他变体")
                actual_scope = reading.get("scope")
                if not isinstance(actual_scope, dict) or scope_payload(actual_scope) != scope_payload(frozen_scope):
                    raise ValidationError("App实际筛选范围与冻结目标scope不符，请标待复核")
                if actual_scope.get("verified") is not True:
                    raise ValidationError("须明确核对App实际scope，不能把未知范围声明为已核实")
                if reading.get("observed_query") is not None and identity(reading["observed_query"]) != identity(job["query"]):
                    raise ValidationError("观察到的查询与任务不符")
                raw_price = reading.get("price")
                if isinstance(raw_price, bool) or not isinstance(raw_price, (int, float)):
                    raise ValidationError("价格须为实际读取的有限数字")
                price = number(raw_price)
                path = self.official.evidence_file(reading.get("evidence_filename"))
                evidence_hash = hashlib.sha256(path.read_bytes()).hexdigest()
                ocr = str(reading.get("ocr_text") or "")
                parser = str(reading.get("parser_version") or "unknown")
                if len(ocr) > 5000 or len(parser) > 120:
                    raise ValidationError("OCR文本或解析版本过长")
                prepared.append((dict(job), reading, price, evidence_hash, ocr, parser))
        accepted = conflicts = 0
        with self.store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            self._lease(db, worker, token)
            for job, reading, price, digest, ocr, parser in prepared:
                current = db.execute("SELECT * FROM mobile_jobs WHERE id=?", (job["id"],)).fetchone()
                if current["status"] != "reading" or current["lease_token"] != token:
                    raise LeaseError("任务租约状态已变化")
                self._current_catalog(db, current)
                prior = db.execute("SELECT * FROM mobile_points WHERE market_scope_id=? AND metric=? AND date=?", (job["market_scope_id"], METRIC, job["target_date"])).fetchone()
                conflict = prior is not None and prior["price"] != price
                status, stamp = ("conflict" if conflict else "verified"), iso()
                cur = db.execute("INSERT INTO mobile_readings(job_id,market_scope_id,date,price,evidence_filename,evidence_hash,read_method,observed_model,scope,ocr_text,parser_version,verification_count,status,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (job["id"], job["market_scope_id"], job["target_date"], price, reading["evidence_filename"], digest, "mobile_screen_ocr", reading["observed_model"], canonical(reading["scope"]), ocr, parser, 2, status, stamp))
                reading_id = cur.lastrowid
                if conflict:
                    db.execute("UPDATE mobile_jobs SET status='needs_review',error='同范围同日读数冲突；保留原值与新证据',reading_id=?,updated_at=? WHERE id=?", (reading_id, stamp, job["id"]))
                    conflicts += 1
                else:
                    if prior is None:
                        db.execute("INSERT INTO mobile_points VALUES (?,?,?,?,?,?)", (job["market_scope_id"], METRIC, job["target_date"], price, reading_id, stamp))
                    db.execute("UPDATE mobile_jobs SET status='verified',price=?,reading_id=?,error=NULL,updated_at=? WHERE market_scope_id=? AND target_date=? AND status!='verified'", (price, reading_id, stamp, job["market_scope_id"], job["target_date"]))
                    accepted += 1
                db.execute("INSERT INTO mobile_job_events(job_id,status,message,created_at) VALUES (?,?,?,?)", (job["id"], status, "双次屏幕读数入库" if not conflict else "证据冲突，未覆盖原值", stamp))
            left = db.execute("SELECT count(*) FROM mobile_jobs WHERE lease_token=? AND status='reading'", (token,)).fetchone()[0]
            if not left:
                db.execute("UPDATE mobile_device SET lease_token=NULL,device_status='ready' WHERE id=1")
        return {"ok": True, "accepted": accepted, "conflicts": conflicts}

    def set_status(self, payload):
        worker, token = self.worker(payload.get("worker_id")), payload.get("lease_token")
        status, ids = payload.get("status"), payload.get("job_ids")
        if not isinstance(status, str) or status not in {"needs_review", "unavailable", "waiting_device", "pending"} or not isinstance(ids, list) or not ids or any(isinstance(i, bool) or not isinstance(i, int) for i in ids):
            raise ValidationError("任务状态或job_ids无效")
        message = str(payload.get("message") or "").strip()[:1000]
        if not message:
            raise ValidationError("须填写真实失败/等待原因")
        with self.store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            self._lease(db, worker, token)
            for job_id in ids:
                job = db.execute("SELECT * FROM mobile_jobs WHERE id=?", (job_id,)).fetchone()
                if not job or job["lease_token"] != token or job["status"] != "reading":
                    raise LeaseError("任务不属于当前租约")
                db.execute("UPDATE mobile_jobs SET status=?,error=?,lease_token=NULL,worker_id=NULL,lease_expires_at=NULL,updated_at=? WHERE id=?", (status, message, iso(), job_id))
                db.execute("INSERT INTO mobile_job_events(job_id,status,message,created_at) VALUES (?,?,?,?)", (job_id, status, message, iso()))
            if not db.execute("SELECT 1 FROM mobile_jobs WHERE lease_token=? AND status='reading'", (token,)).fetchone():
                db.execute("UPDATE mobile_device SET lease_token=NULL,device_status='ready' WHERE id=1")
        return {"ok": True}

    def retry(self, batch_id):
        with self.store.db() as db:
            if not db.execute("SELECT 1 FROM mobile_batches WHERE id=?", (batch_id,)).fetchone():
                return None
            cur = db.execute("UPDATE mobile_jobs SET status='pending',lease_token=NULL,worker_id=NULL,lease_expires_at=NULL,updated_at=? WHERE batch_id=? AND status IN ('needs_review','failed','waiting_device') AND attempts<? AND EXISTS (SELECT 1 FROM catalog c WHERE c.id=mobile_jobs.catalog_id AND c.market_scope_id=mobile_jobs.market_scope_id AND c.enabled=1 AND c.status NOT IN ('needs_review','no_market','unsupported') AND json_extract(c.scope,'$.verified')=1)", (iso(), batch_id, self.MAX_ATTEMPTS))
            count = cur.rowcount
        return {"ok": True, "retried": count, "max_attempts": self.MAX_ATTEMPTS}

    def status(self):
        with self.store.db() as db:
            row = db.execute("SELECT * FROM mobile_device WHERE id=1").fetchone()
            active = db.execute("SELECT batch_id,catalog_id FROM mobile_jobs WHERE status='reading' AND lease_expires_at>? ORDER BY id LIMIT 1", (iso(),)).fetchone()
        fresh = bool(row["last_heartbeat"] and row["lease_expires_at"] and row["lease_expires_at"] > iso())
        return dict(device_status=row["device_status"] if fresh else "disconnected", worker_id=row["worker_id"], last_heartbeat=row["last_heartbeat"], message=row["message"] if fresh else "未收到有效设备心跳；等待实际worker连接", active_batch=active["batch_id"] if fresh and active else None, active_catalog=active["catalog_id"] if fresh and active else None, connected=fresh and row["device_status"] != "disconnected")

    def evidence(self, reading_id):
        with self.store.db() as db:
            row = db.execute("SELECT evidence_filename,evidence_hash FROM mobile_readings WHERE id=?", (reading_id,)).fetchone()
        if row is None:
            return None
        path = self.official.evidence_file(row["evidence_filename"])
        if hashlib.sha256(path.read_bytes()).hexdigest() != row["evidence_hash"]:
            raise ValidationError("证据图片哈希不符，文件可能已改变")
        return path

    def official_series(self, days=180, category="all", model="all"):
        today = utcnow().astimezone(SHANGHAI).date()
        cutoff = (today-timedelta(days=days)).isoformat()
        end = (today-timedelta(days=1)).isoformat()
        result = []
        with self.store.db() as db:
            catalogs = db.execute("SELECT * FROM catalog ORDER BY id").fetchall()
            seen = set()
            for cat in catalogs:
                if cat["market_scope_id"] in seen or category not in {"all", cat["category"]} or model not in {"all", cat["model"]}:
                    continue
                rows = db.execute("SELECT p.*,r.created_at,r.scope AS observed_scope FROM mobile_points p JOIN mobile_readings r ON r.id=p.reading_id WHERE p.market_scope_id=? AND p.metric=? AND p.date>=? AND p.date<=? ORDER BY p.date", (cat["market_scope_id"], METRIC, cutoff, end)).fetchall()
                if not rows:
                    continue
                seen.add(cat["market_scope_id"])
                points = [dict(date=row["date"], price=row["price"]) for row in rows]
                first, last = points[0]["price"], points[-1]["price"]
                result.append(dict(id=f"mobile-{cat['id']}", query=cat["query"], model=cat["model"], category=cat["category"], metric=METRIC, captured_at=max(row["created_at"] for row in rows), period_start=points[0]["date"], period_end=points[-1]["date"], read_method="mobile_screen_ocr", market_scope_id=cat["market_scope_id"], scope=json.loads(rows[-1]["observed_scope"]), evidence_url=f"/api/mobile/evidence/{rows[-1]['reading_id']}", points=points, sampled_points=len(points), first_price=first, last_price=last, change_pct=round((last/first-1)*100, 2) if len(points)>1 and first else None, coverage_message=f"手机屏幕双次复读{len(points)}个日期，每点有独立证据；缺日不插值。"))
        return result
