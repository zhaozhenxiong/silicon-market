"""Phone collection is authorized by enabled live monitor tasks, never items.

Catalog/history remain intact. This resolver chooses one exact model/scope per
monitor and the jobs guard rechecks authorization in the final read transaction.
"""
import hashlib
import re
import unicodedata

from .catalog import Catalog, canonical, identity
from .jobs import LeaseError, MobileJobs
from .normalization import infer_hardware, iso, unknown_model

POLICY = "enabled_live_monitor_tasks_only"
BLOCKED = {"needs_review", "no_market", "unsupported"}
READY_STATUSES = {"ready", "partial", "complete"}
# Exact aliases only: each input is one known SKU, never a family prefix or
# substring. Unlisted numbers require an explicit model/prefix from the monitor.
BARE_RTX_ALIASES = {"4060", "4060TI", "4070", "4070SUPER", "4070TI", "4070TISUPER",
                    "4080", "4080SUPER", "4090", "5070TI"}


def task_model(task):
    """Return a single requested target; never expand a broad family to variants."""
    explicit = str(task.get("model") or "auto").strip()
    is_explicit = explicit.casefold() not in {"auto", "all", "自动识别"}
    text = explicit if is_explicit else task["keyword"]
    normalized = unicodedata.normalize("NFKC", text).upper()
    if unknown_model(text) or re.search(r"[/|或]|系列|多型号", normalized):
        return None
    # More than one named SKU cannot silently become the first regex match.
    named = set(re.findall(r"(?<![A-Z0-9])(?:RTX|GTX|RX)\s*[- ]?\d{3,4}\s*(?:TI\s*SUPER|SUPER|TI|XTX|XT|GRE)?(?![A-Z0-9])",normalized))
    cpu_models = set(re.findall(r"(?<![A-Z0-9])(?:I[3579]\s*[- ]?)?(?:14900(?:KF|KS|K|F)?)(?![A-Z0-9])|(?<![A-Z0-9])(?:I[3579]\s*[- ]?\d{4,5}[A-Z]{0,3}|R[3579]\s*[- ]?[3579]\d{3}(?:X3D|XT|X|G|F)?)(?![A-Z0-9])",normalized))
    if len(named)+len(cpu_models) > 1:
        return None
    category = task["category"]
    if category == "computer" and not is_explicit:
        # A CPU/GPU configuration in a generic whole-PC query is not an App SKU.
        return None
    _, model = infer_hardware(text, category)
    if category == "gpu" and unknown_model(model):
        alias = re.sub(r"\s+", "", normalized)
        if alias in BARE_RTX_ALIASES:
            _, model = infer_hardware("RTX "+alias,"gpu")
    if category == "cpu" and unknown_model(model):
        # The monitor keyword '14900' is specifically the unsuffixed 14900.
        # Do not derive K/KF/KS/F from observed listings or prefix matches.
        match = re.search(r"(?<![A-Z0-9])14900(KF|KS|K|F)?(?![A-Z0-9])", normalized)
        if match:
            model = "i9-14900"+(match[1] or "")
    if is_explicit:
        if category == "computer" or unknown_model(model):
            # Explicit custom SKUs remain operator-provided targets. Generic
            # labels without a model digit cannot authorize an arbitrary model.
            if not re.search(r"[A-Z0-9]*\d[A-Z0-9]*", normalized):
                return None
            model = explicit
    if unknown_model(model):
        return None
    return model, "explicit_model" if is_explicit else "keyword"


class MobileTargets:
    def __init__(self, store, catalog):
        self.store, self.catalog = store, catalog

    @staticmethod
    def _scope(model):
        capacity = re.search(r"(?<![A-Z0-9])(\d+(?:\.\d+)?(?:GB|TB))", model.upper())
        return dict(model=model, storage=capacity[1] if capacity else None,
                    condition=None, filters={}, verified=False)

    def _resolve(self, db, *, create):
        tasks = [dict(row) for row in db.execute("SELECT * FROM tasks WHERE enabled=1 AND source='live' ORDER BY id")]
        catalogs = [Catalog.decode(row) for row in db.execute("SELECT * FROM catalog ORDER BY id")]
        targets, skipped = {}, []
        for task in tasks:
            def skip(reason, message):
                skipped.append(dict(task_id=task["id"], task_name=task["name"], keyword=task["keyword"], category=task["category"], reason=reason, message=message))
            inferred = task_model(task)
            if inferred is None:
                skip("needs_model", "监控任务尚不能确定唯一App型号；请明确型号，不会从商品扩展变体")
                continue
            model, mapping = inferred
            matching = [row for row in catalogs if row["category"] == task["category"] and identity(row["model"]) == identity(model)]
            enabled = [row for row in matching if row["enabled"]]
            if matching and not enabled:
                skip("catalog_disabled", "该型号目录已停用；保留设置，不新建目录绕过停用")
                continue
            verified = [row for row in enabled if row["scope"].get("verified") is True]
            if len({row["market_scope_id"] for row in verified}) > 1:
                skip("needs_scope", "同型号有多个已核对经济范围，须明确选择，不任取成色/筛选范围")
                continue
            default_scope = self._scope(model)
            default_id = Catalog.scope_id(task["category"], model, default_scope)
            candidates = verified or [row for row in enabled if row["market_scope_id"] == default_id] or enabled
            if len({row["market_scope_id"] for row in candidates}) > 1:
                skip("needs_scope", "同型号有多个未确定范围，须先核对目标范围")
                continue
            chosen = candidates[0] if candidates else None
            if chosen is None and create:
                stamp = iso()
                # Only a currently enabled task may add this derived catalog.
                # Query is the exact canonical SKU, not an observed variant.
                cur = db.execute("INSERT INTO catalog(category,model,query,scope,market_scope_id,created_at,updated_at) VALUES (?,?,?,?,?,?,?)",
                    (task["category"],model,model,canonical(default_scope),default_id,stamp,stamp))
                db.execute("INSERT INTO catalog_revisions(catalog_id,version,data,created_at) VALUES (?,1,?,?)",
                    (cur.lastrowid,canonical(dict(category=task["category"],model=model,query=model,scope=default_scope,source_task_id=task["id"])),stamp))
                chosen = Catalog.decode(db.execute("SELECT * FROM catalog WHERE id=?",(cur.lastrowid,)).fetchone())
                catalogs.append(chosen)
            if chosen is None:
                continue
            key = chosen["id"]
            if key not in targets:
                targets[key] = dict(catalog_id=key,catalog_version=chosen["version"],category=chosen["category"],model=chosen["model"],query=chosen["query"],
                    scope=chosen["scope"],market_scope_id=chosen["market_scope_id"],scope_verified=chosen["scope"].get("verified") is True,
                    status=chosen["status"],enabled=True,readable=chosen["scope"].get("verified") is True and chosen["status"] in READY_STATUSES,
                    task_ids=[],task_names=[],mapping_sources=[])
            target = targets[key]
            target["task_ids"].append(task["id"])
            target["task_names"].append(task["name"])
            target["mapping_sources"].append(mapping)
        items = sorted(targets.values(),key=lambda row:row["catalog_id"])
        version = hashlib.sha256(canonical([items,skipped]).encode()).hexdigest()
        return dict(targets=items,count=len(items),enabled_task_count=len(tasks),skipped_tasks=skipped,policy=POLICY,whitelist_version=version)

    def list(self):
        with self.store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            result = self._resolve(db,create=True)
        coverage = {item["id"]:item for item in self.catalog.list()["items"]}
        for target in result["targets"]:
            row = coverage.get(target["catalog_id"],{})
            target.update({key:row[key] for key in ("target_days","verified_days","missing_days","coverage_pct","legacy_points") if key in row})
        return result

    def require_catalog(self, db, catalog_id):
        result = self._resolve(db,create=False)
        for target in result["targets"]:
            if target["catalog_id"] == catalog_id:
                return target
        raise LeaseError("该目录不属于当前已启用的真实监控任务；已暂停、删除或型号范围已改变，禁止读取")


class MonitoredMobileJobs(MobileJobs):
    """Keep legacy jobs/history, but enforce the monitor policy at execution."""
    def __init__(self, store, catalog, official, targets):
        super().__init__(store,catalog,official)
        self.targets = targets

    def _current_catalog(self, db, job):
        # Base submit invokes this again after BEGIN IMMEDIATE immediately
        # before persisting readings, so disabling a monitor revokes its lease.
        self.targets.require_catalog(db,job["catalog_id"])
        return super()._current_catalog(db,job)

    def claim(self,payload):
        if not isinstance(payload,dict) or set(payload)-{"worker_id","catalog_id","limit"}:
            raise LeaseError("领取只接受worker_id/catalog_id/limit，不接受其他目标范围")
        requested = payload.get("catalog_id")
        if requested is not None and (isinstance(requested,bool) or not isinstance(requested,int) or requested <= 0):
            raise LeaseError("catalog_id须为白名单目录正整数")
        with self.store.lock:
            report = self.targets.list()
            allowed = {target["catalog_id"] for target in report["targets"]}
            if not allowed:
                raise LeaseError("没有已启用且可确定型号的真实监控任务；未领取任何手机日期任务")
            if requested is not None and requested not in allowed:
                raise LeaseError("该目录不属于当前已启用的真实监控任务，禁止领取")
            if requested is None:
                with self.store.db() as db:
                    pending = db.execute("SELECT j.catalog_id FROM mobile_jobs j JOIN catalog c ON c.id=j.catalog_id AND c.market_scope_id=j.market_scope_id WHERE j.status IN ('pending','waiting_device') AND j.attempts<? AND c.enabled=1 AND c.status NOT IN ('needs_review','no_market','unsupported') AND json_extract(c.scope,'$.verified')=1 ORDER BY j.id",(self.MAX_ATTEMPTS,)).fetchall()
                requested = next((row["catalog_id"] for row in pending if row["catalog_id"] in allowed), -1)
            result = super().claim({**payload,"catalog_id":requested})
            if result["catalog"] is not None:
                with self.store.db() as db:
                    self.targets.require_catalog(db,result["catalog"]["id"])
            return result
