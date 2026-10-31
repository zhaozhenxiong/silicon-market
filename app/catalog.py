"""Versioned target identities discovered only from real observations/tasks."""
import hashlib
import json
import re

from .normalization import CATEGORIES, ValidationError, infer_hardware, iso, unknown_model


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def identity(value):
    return re.sub(r"\s+", "", str(value)).casefold()


def scope_payload(scope):
    if not isinstance(scope, dict):
        raise ValidationError("scope 必须是对象")
    if len(canonical(scope)) > 6000:
        raise ValidationError("scope 太长")
    # Verification is provenance, not a change in the economic scope.
    return {key: value for key, value in scope.items() if key not in {"verified", "evidence", "verified_at", "aliases"}}


class Catalog:
    def __init__(self, store):
        self.store = store
        with store.db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS catalog (
                    id INTEGER PRIMARY KEY, category TEXT NOT NULL, model TEXT NOT NULL,
                    query TEXT NOT NULL, scope TEXT NOT NULL, market_scope_id TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1, status TEXT NOT NULL DEFAULT 'pending',
                    last_error TEXT, version INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    UNIQUE(category,model,market_scope_id));
                CREATE TABLE IF NOT EXISTS catalog_revisions (
                    id INTEGER PRIMARY KEY,catalog_id INTEGER NOT NULL,version INTEGER NOT NULL,
                    data TEXT NOT NULL,created_at TEXT NOT NULL);
            """)

    @staticmethod
    def scope_id(category, model, scope):
        payload = scope_payload(scope)
        if "model" in payload:
            payload["model"] = identity(payload["model"])
        if isinstance(payload.get("storage"), str):
            payload["storage"] = identity(payload["storage"])
        return hashlib.sha256(canonical([category, identity(model), payload]).encode()).hexdigest()

    @staticmethod
    def decode(row):
        if row is None:
            return None
        result = dict(row)
        result["scope"] = json.loads(result["scope"])
        result["enabled"] = bool(result["enabled"])
        return result

    def get(self, catalog_id):
        with self.store.db() as db:
            return self.decode(db.execute("SELECT * FROM catalog WHERE id=?", (catalog_id,)).fetchone())

    def sync(self):
        candidates = []
        with self.store.db() as db:
            candidates.extend((row["category"], row["model"], row["model"]) for row in db.execute("SELECT DISTINCT category,model FROM items WHERE source='live' AND excluded=0"))
        for task in self.store.tasks():
            if task.get("source", "live") != "live":
                continue
            explicit = task["model"] != "auto"
            if explicit:
                model = task["model"]
            else:
                _, model = infer_hardware(task["keyword"], task["category"])
            if not unknown_model(model):
                candidates.append((task["category"], model, task["keyword"]))
        added = 0
        with self.store.db() as db:
            for category, model, query in candidates:
                if unknown_model(model):
                    continue
                capacity = re.search(r"(?<![A-Z0-9])(\d+(?:\.\d+)?(?:GB|TB))", model.upper())
                scope = {"model": model, "storage": capacity[1] if capacity else None, "condition": None, "filters": {}, "verified": False}
                scope_id = self.scope_id(category, model, scope)
                if db.execute("SELECT 1 FROM catalog WHERE category=? AND market_scope_id=?", (category, scope_id)).fetchone():
                    continue
                stamp = iso()
                cur = db.execute("INSERT INTO catalog(category,model,query,scope,market_scope_id,created_at,updated_at) VALUES (?,?,?,?,?,?,?)", (category, model, query, canonical(scope), scope_id, stamp, stamp))
                db.execute("INSERT INTO catalog_revisions(catalog_id,version,data,created_at) VALUES (?,1,?,?)", (cur.lastrowid, canonical(dict(category=category, model=model, query=query, scope=scope)), stamp))
                added += 1
            total = db.execute("SELECT count(*) FROM catalog").fetchone()[0]
        return {"added": added, "total": total}

    def patch(self, catalog_id, payload):
        old = self.get(catalog_id)
        if old is None:
            return None
        if not isinstance(payload, dict) or set(payload) - {"query", "scope", "enabled", "status", "last_error"}:
            raise ValidationError("目录修改包含未知字段；型号和分类身份不可静默修改")
        merged = {**old, **payload}
        if not isinstance(merged["query"], str) or not 1 <= len(merged["query"].strip()) <= 160:
            raise ValidationError("query 须为1至160字")
        if not isinstance(merged["enabled"], bool):
            raise ValidationError("enabled 必须为布尔值")
        if merged["status"] not in {"pending", "ready", "no_market", "unsupported", "needs_review", "waiting_device", "complete", "partial"}:
            raise ValidationError("目录状态无效")
        scope_payload(merged["scope"])
        if "verified" in merged["scope"] and not isinstance(merged["scope"]["verified"], bool):
            raise ValidationError("scope.verified 必须是人工核对后的布尔值")
        aliases = merged["scope"].get("aliases", [])
        if not isinstance(aliases, list) or len(aliases) > 30 or any(not isinstance(alias, str) or not 1 <= len(alias) <= 160 for alias in aliases):
            raise ValidationError("scope.aliases 须为最多30个明确完整型号别名")
        if "model" in merged["scope"] and identity(merged["scope"]["model"]) != identity(old["model"]):
            raise ValidationError("scope.model 不可与目录型号身份不符")
        target_capacity = re.search(r"(?<![A-Z0-9])(\d+(?:\.\d+)?(?:GB|TB))", old["model"].upper())
        if target_capacity and identity(merged["scope"].get("storage")) != identity(target_capacity[1]):
            raise ValidationError("不能把宽容量行情标为该细分容量；请保留原规格并标待复核")
        if merged.get("last_error") is not None and (not isinstance(merged["last_error"], str) or len(merged["last_error"]) > 2000):
            raise ValidationError("last_error 须为短文本")
        version = old["version"] + 1
        with self.store.db() as db:
            db.execute("UPDATE catalog SET query=?,scope=?,market_scope_id=?,enabled=?,status=?,last_error=?,version=?,updated_at=? WHERE id=?", (merged["query"].strip(), canonical(merged["scope"]), self.scope_id(old["category"], old["model"], merged["scope"]), merged["enabled"], merged["status"], merged.get("last_error"), version, iso(), catalog_id))
            db.execute("INSERT INTO catalog_revisions(catalog_id,version,data,created_at) VALUES (?,?,?,?)", (catalog_id, version, canonical(merged), iso()))
        return self.get(catalog_id)

    def list(self):
        with self.store.db() as db:
            rows = db.execute("SELECT * FROM catalog ORDER BY category,model,id").fetchall()
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            result = []
            for row in rows:
                item = self.decode(row)
                counts = db.execute("SELECT count(*) AS n,sum(status='verified') AS verified FROM mobile_jobs WHERE catalog_id=? AND market_scope_id=? AND batch_id=(SELECT max(batch_id) FROM mobile_jobs WHERE catalog_id=? AND market_scope_id=?)", (item["id"], item["market_scope_id"], item["id"], item["market_scope_id"])).fetchone() if "mobile_jobs" in tables else None
                target, verified = (counts["n"], counts["verified"] or 0) if counts else (0, 0)
                item.update(target_days=target, verified_days=verified, missing_days=target-verified, coverage_pct=round(verified/target*100, 2) if target else 0)
                reviewed = item["scope"].get("verified") is True and item["status"] not in {"needs_review", "no_market", "unsupported"}
                if reviewed and target and target == verified:
                    item["status"] = "complete"
                elif reviewed and verified:
                    item["status"] = "partial"
                item["legacy_points"] = db.execute("SELECT count(*) FROM official_points p JOIN official_snapshots s ON s.id=p.snapshot_id WHERE s.category=? AND s.model=?", (item["category"], item["model"])).fetchone()[0] if "official_points" in tables else 0
                result.append(item)
        return {"items": result, "total": len(result)}
