"""Versioned real-data indices. This module never seeds or rewrites source data.

GET computes against the last explicitly frozen basket/calibration. Rebuild is
the only operation that proposes a new version and persists its exact inputs.
"""
from __future__ import annotations

import hashlib
import json
import math
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
from statistics import median, pstdev

from .normalization import CATEGORIES, SHANGHAI, ValidationError, utcnow

FACTORS = (
    ("momentum", "28日参考价格趋势", .30, 1),
    ("liquidity", "成熟队列14日明确售出比例", .25, 1),
    ("supply", "可比7日新增供给变化", .20, -1),
    ("concentration", "挂牌报价相对四分位距", .15, -1),
    ("stability", "28日平滑参考价格波动", .10, -1),
)
PARAMETERS = dict(method="real-index-v1", baseline_days=7, listing_min_samples=20,
                  cohort_days=14, cohort_window_days=28, cohort_min_samples=20,
                  tracking_min_coverage=.8, calibration_min_days=30,
                  momentum_days=28, stability_returns=28, supply_window_days=7,
                  calibration_quantiles=[.05, .95], missing="null_no_renormalization")


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def percentile(values, q):
    values = sorted(values)
    pos = (len(values) - 1) * q
    lo, hi = math.floor(pos), math.ceil(pos)
    return values[lo] + (values[hi] - values[lo]) * (pos - lo)


def dates(start, end):
    return [(start + timedelta(days=i)).isoformat() for i in range((end - start).days + 1)]


def day_of(stamp):
    return datetime.fromisoformat(stamp.replace("Z", "+00:00")).astimezone(SHANGHAI).date().isoformat()


def valid_price(value):
    return isinstance(value, (float, int)) and not isinstance(value, bool) and math.isfinite(value) and 0 < value <= 100_000_000


def eligible_model(value):
    return bool(value and not any(word in value for word in ("待确认", "未识别", "自动识别")) and value.lower() not in {"all", "auto"})


def equal_weights(members):
    """Equal category weights, then equal distinct scopes/models within category."""
    counts = defaultdict(int)
    for member in members:
        counts[member["category"]] += 1
    return [{**member, "weight": 1 / len(counts) / counts[member["category"]]} for member in members]


def qualified(inputs, category, model, scope_id=None):
    """A discovered title is a candidate, not a verified basket identity.

    A store with no catalogue retains the standalone legacy import workflow.
    Once an operator catalogue exists, every formal member must pass its current
    explicit scope review. Old manual scopes are never mapped to a SKU by name.
    """
    catalog = inputs.get("catalog", [])
    if not catalog:
        return True
    identity = lambda value: "".join(str(value).casefold().split())
    matches = [c for c in catalog if c["category"] == category and identity(c["model"]) == identity(model)
               and (scope_id is None or c["market_scope_id"] == scope_id)]
    for item in matches:
        try:
            scope = json.loads(item["scope"]) if isinstance(item["scope"], str) else item["scope"]
        except (TypeError, ValueError):
            continue
        if (item.get("enabled") and item.get("status") not in {"needs_review", "no_market", "unsupported"}
                and isinstance(scope, dict) and scope.get("verified") is True):
            return True
    return False


def common_baseline(series, members):
    if not members:
        return None
    common = set.intersection(*(set(series[m["id"]]["prices"]) for m in members))
    for start in sorted(common):
        window = dates(date.fromisoformat(start), date.fromisoformat(start) + timedelta(days=6))
        if set(window).issubset(common):
            return dict(start=window[0], end=window[-1], prices={
                m["id"]: median(series[m["id"]]["prices"][d] for d in window) for m in members})
    return None


def calibration(values, cutoff):
    """Freeze the first 30 valid factor days strictly before a scoring date."""
    points = [(d, item["value"]) for d, item in sorted(values.items()) if d < cutoff and item["eligible"]]
    if len(points) < PARAMETERS["calibration_min_days"]:
        return None
    points = points[:PARAMETERS["calibration_min_days"]]
    numbers = [v for _, v in points]
    return dict(start=points[0][0], end=points[-1][0], samples=len(points),
                q05=percentile(numbers, .05), q95=percentile(numbers, .95),
                constant=math.isclose(min(numbers), max(numbers), rel_tol=1e-12, abs_tol=1e-12),
                input_hash=digest(points))


def standardize(raw, frozen, direction, score_date):
    if frozen is None or score_date <= frozen["end"]:
        return None
    lo, hi = frozen["q05"], frozen["q95"]
    # Fifty is reserved for a genuinely observed constant calibration series.
    if math.isclose(lo, hi, rel_tol=1e-12, abs_tol=1e-12):
        return 50.0 if frozen.get("constant") else None
    score = 100 * min(1, max(0, (raw - lo) / (hi - lo)))
    return round(score if direction > 0 else 100 - score, 6)


class IndexService:
    def __init__(self, store):
        self.store = store
        with store.db() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS index_builds (
                id INTEGER PRIMARY KEY, category TEXT NOT NULL, days INTEGER NOT NULL,
                created_at TEXT NOT NULL, version TEXT NOT NULL, input_hash TEXT NOT NULL,
                config_json TEXT NOT NULL, inputs_json TEXT NOT NULL, result_json TEXT NOT NULL)""")

    @staticmethod
    def _validate(days, category):
        if isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= 365:
            raise ValidationError("指数窗口须为1至365天")
        if category != "all" and category not in CATEGORIES:
            raise ValidationError("指数硬件分类无效")

    def _inputs(self, category):
        end = (utcnow().astimezone(SHANGHAI).date() - timedelta(days=1)).isoformat()
        with self.store.db() as db:
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            def rows(table):
                return [dict(r) for r in db.execute(f"SELECT * FROM {table} ORDER BY rowid")] if table in tables else []
            items = {r["id"]: r for r in rows("items") if r["source"] == "live"}
            obs = [r for r in rows("observations") if r["item_pk"] in items and day_of(r["observed_at"]) <= end]
            snapshots = {r["id"]: r for r in rows("official_snapshots")
                         if r.get("source", "live") != "demo" and r["metric"] == "rolling_7d_sold_mean"}
            official = []
            for p in rows("official_points"):
                s = snapshots.get(p["snapshot_id"])
                if s and p["date"] <= end and valid_price(p["price"]):
                    # The legacy format has no verified SKU filter. Query defines
                    # an unknown-width scope, never silently a catalogue variant.
                    scope = s.get("market_scope_id") or "legacy:" + digest([s["category"], "".join(s["query"].lower().split())])[:24]
                    official.append(dict(scope=scope, category=s["category"], model=s["model"], date=p["date"],
                                         price=p["price"], verified_at=s["captured_at"], source="screen_verified",
                                         evidence=s["evidence_filename"], query=s["query"]))
            catalogs = rows("catalog")
            by_scope = defaultdict(list)
            for c in catalogs:
                if c.get("enabled", True) and c.get("market_scope_id"):
                    by_scope[c["market_scope_id"]].append(c)
            for p in rows("mobile_points"):
                choices = by_scope.get(p["market_scope_id"], [])
                if p.get("metric") != "rolling_7d_sold_mean" or not choices or p["date"] > end or not valid_price(p["price"]):
                    continue
                c = sorted(choices, key=lambda x: (x["category"], x["model"], x["id"]))[0]
                official.append(dict(scope=p["market_scope_id"], category=c["category"], model=c["model"],
                                     date=p["date"], price=p["price"], verified_at=p["verified_at"],
                                     reading_id=p["reading_id"], source="mobile_screen_ocr", query=c.get("query", "")))
            runs = [r for r in rows("runs") if day_of(r["started_at"]) <= end]
        from .price_audit import PriceAudit
        audit = getattr(self.store, "price_audit", None) or PriceAudit(self.store)
        obs = audit.mask_observations(obs)
        audit_state = audit.state()
        # Keep input scope in the hash; exclude demo at the SQL-boundary equivalent.
        return dict(end_date=end, category=category, official=official, observations=obs, runs=runs, catalog=catalogs,
                    price_eligibility_audit=audit_state)

    @staticmethod
    def _series(inputs, parameters=None):
        parameters = parameters or PARAMETERS
        category, end = inputs["category"], inputs["end_date"]
        official, listing = {}, {}
        canonical_models = {(r["category"], r["model"]) for r in inputs["official"] if r["source"] == "mobile_screen_ocr"}
        for row in sorted(inputs["official"], key=lambda r: (r["verified_at"], r.get("reading_id", 0))):
            if (category != "all" and row["category"] != category) or not eligible_model(row["model"]):
                continue
            if row["source"] == "screen_verified" and (row["category"], row["model"]) in canonical_models:
                continue  # keep legacy proof in its source table; never union an unverified old scope into the canonical one
            key = "official:" + row["scope"]
            entry = official.setdefault(key, dict(id=key, category=row["category"], model=row["model"],
                                                  market_scope_id=row["scope"], prices={}, conflicts=set()))
            if row["date"] in entry["prices"] and entry["prices"][row["date"]] != row["price"]:
                entry["conflicts"].add(row["date"])
            entry["prices"][row["date"]] = row["price"]
        for entry in official.values():
            # Different legacy snapshots cannot silently resolve contradictory evidence.
            for day in entry.pop("conflicts"):
                entry["prices"].pop(day, None)
        daily_last = {}
        for row in sorted(inputs["observations"], key=lambda r: (r["observed_at"], r["id"])):
            daily_last[(day_of(row["observed_at"]), row["item_pk"])] = row
        for (day, _), row in daily_last.items():
            if row["excluded"] or row["status"] != "on_sale" or not valid_price(row["price"]):
                continue
            if (category != "all" and row["category"] != category) or not eligible_model(row["model"]):
                continue
            key = "listing:" + canonical([row["category"], row["model"]])
            entry = listing.setdefault(key, dict(id=key, category=row["category"], model=row["model"], prices={}, samples=defaultdict(list)))
            entry["samples"][day].append(row["price"])
        for entry in listing.values():
            entry["samples"] = dict(entry["samples"])
            entry["prices"] = {day: median(prices) for day, prices in entry["samples"].items()
                               if len(prices) >= parameters["listing_min_samples"]}
        return official, listing

    @staticmethod
    def _factor_series(inputs, official, listing, parameters=None):
        """Compute facts first. Eligibility controls calibration, never raw facts."""
        parameters = parameters or PARAMETERS
        horizon, cohort_window = parameters["cohort_days"], parameters["cohort_window_days"]
        groups = defaultdict(lambda: dict(official=[], listing=None))
        for entry in official.values():
            groups[(entry["category"], entry["model"])]["official"].append(entry)
        for entry in listing.values():
            groups[(entry["category"], entry["model"])]["listing"] = entry
        history = defaultdict(list)
        runs_by_id = {run["id"]: run for run in inputs["runs"]}
        for obs in sorted(inputs["observations"], key=lambda r: (r["observed_at"], r["id"])):
            history[obs["item_pk"]].append(obs)
        end = date.fromisoformat(inputs["end_date"])
        result = {}
        for (category, model), group in sorted(groups.items()):
            fs = {f[0]: {} for f in FACTORS}
            prices = group["official"][0]["prices"] if len(group["official"]) == 1 else {}
            for day, price in sorted(prices.items()):
                stamp = date.fromisoformat(day)
                prior = prices.get((stamp - timedelta(days=parameters["momentum_days"])).isoformat())
                if prior:
                    fs["momentum"][day] = dict(value=100 * (price / prior - 1), eligible=True)
                window = dates(stamp - timedelta(days=parameters["stability_returns"]), stamp)
                if all(d in prices for d in window):
                    changes = [math.log(prices[b] / prices[a]) for a, b in zip(window, window[1:])]
                    fs["stability"][day] = dict(value=pstdev(changes), eligible=True)
            sample_days = group["listing"]["samples"] if group["listing"] else {}
            for day, values in sample_days.items():
                fs["concentration"][day] = dict(value=(percentile(values, .75) - percentile(values, .25)) / median(values),
                    eligible=len(values) >= parameters["listing_min_samples"], samples=len(values))
            histories = [h for h in history.values() if h[0]["category"] == category and h[0]["model"] == model]
            if histories:
                candidate_days = {inputs["end_date"]}
                for h in histories:
                    first = date.fromisoformat(day_of(h[0]["observed_at"]))
                    candidate_days.update(dates(first + timedelta(days=horizon), min(first + timedelta(days=horizon + cohort_window - 1), end)))
                candidate_days.update(day_of(r["started_at"]) for r in inputs["runs"] if day_of(r["started_at"]) <= inputs["end_date"])
                for day in sorted(candidate_days):
                    current = date.fromisoformat(day)
                    cutoff = datetime.combine(current + timedelta(days=1), time(), SHANGHAI).astimezone(timezone.utc)
                    cohort = []
                    for h in histories:
                        first = h[0]
                        initial = datetime.fromisoformat(first["observed_at"])
                        age = (current - date.fromisoformat(day_of(first["observed_at"]))).days
                        if first["status"] == "on_sale" and not first["excluded"] and horizon <= age < horizon + cohort_window and initial + timedelta(days=horizon) < cutoff:
                            cohort.append(h)
                    if cohort:
                        sold = tracked = 0
                        for h in cohort:
                            deadline = datetime.fromisoformat(h[0]["observed_at"]) + timedelta(days=horizon)
                            visible = [o for o in h if datetime.fromisoformat(o["observed_at"]) < cutoff]
                            sale = any(not o["excluded"] and o["status"] == "sold" and o["evidence"]
                                       and datetime.fromisoformat(o["observed_at"]) <= deadline for o in visible)
                            sold += sale
                            tracked += sale or any(datetime.fromisoformat(o["observed_at"]) >= deadline for o in visible)
                        coverage = tracked / len(cohort)
                        fs["liquidity"][day] = dict(value=100 * sold / len(cohort), eligible=len(cohort) >= parameters["cohort_min_samples"] and coverage >= parameters["tracking_min_coverage"],
                            samples=len(cohort), confirmed_sold=sold, tracking_coverage_pct=round(coverage * 100, 2))
                    # A single task/config must cover both complete seven-day windows.
                    task_ids = {o["task_id"] for h in histories for o in h
                                if o.get("run_id") in runs_by_id and o.get("task_id") == runs_by_id[o["run_id"]].get("task_id")
                                and day_of(o["observed_at"]) <= day}
                    window = dates(current - timedelta(days=2 * parameters["supply_window_days"] - 1), current)
                    candidates = defaultdict(set)
                    earlier = set()
                    for run in inputs["runs"]:
                        key = (run.get("task_id"), run.get("sampling_config_hash"))
                        if key[0] not in task_ids or not key[1] or run["status"] != "success":
                            continue
                        finished = run.get("finished_at")
                        if not finished or day_of(finished) > day:
                            continue
                        rd = day_of(run["started_at"])
                        if rd < window[0]:
                            earlier.add(key)
                        if not run.get("initial_scan", True) and rd in window:
                            candidates[key].add(rd)
                    comparable = [key for key in sorted(candidates, key=str) if key in earlier and candidates[key] == set(window)
                        and not any(r.get("task_id") == key[0] and r["status"] == "success"
                                    and window[0] <= day_of(r["started_at"]) <= day
                                    and (r.get("sampling_config_hash") != key[1] or r.get("initial_scan", True))
                                    for r in inputs["runs"])]
                    if comparable:
                        task_id, config_hash = comparable[0]
                        start_current = (current - timedelta(days=parameters["supply_window_days"] - 1)).isoformat()
                        counts = [0, 0]
                        for h in histories:
                            first = h[0]
                            fd = day_of(first["observed_at"])
                            run = runs_by_id.get(first.get("run_id"), {})
                            if (not first["excluded"] and first["status"] == "on_sale" and first.get("task_id") == task_id
                                    and run.get("task_id") == task_id and run.get("sampling_config_hash") == config_hash
                                    and run.get("status") == "success" and not run.get("initial_scan", True)
                                    and day_of(run["started_at"]) == fd and window[0] <= fd <= day):
                                counts[fd >= start_current] += 1
                        if counts[0]:
                            fs["supply"][day] = dict(value=100 * (counts[1] / counts[0] - 1), eligible=True,
                                previous_new=counts[0], current_new=counts[1], sampling_config_hash=config_hash, task_id=task_id)
            result[canonical([category, model])] = dict(category=category, model=model, values=fs,
                official_scope_count=len(group["official"]))
        return result

    @staticmethod
    def _propose(inputs, official, listing, factors):
        config = dict(parameters=PARAMETERS, factor_definitions=[dict(key=k, label=l, weight=w, direction=d) for k, l, w, d in FACTORS],
                      baskets={}, feature_basket=[], calibration={})
        config["price_eligibility_audit"] = inputs.get("price_eligibility_audit")
        for basis, series in (("official", official), ("listing", listing)):
            members = [dict(id=key, category=e["category"], model=e["model"], **({"market_scope_id": e["market_scope_id"]} if basis == "official" else {}))
                       for key, e in sorted(series.items()) if common_baseline(series, [dict(id=key)])
                       and qualified(inputs, e["category"], e["model"], e.get("market_scope_id"))]
            # If individually eligible series have disjoint coverage, no common
            # baseline exists; never silently drop one merely to publish a line.
            members = equal_weights(members)
            config["baskets"][basis] = dict(members=members, baseline=common_baseline(series, members))
        feature_members = [dict(id=key, category=e["category"], model=e["model"]) for key, e in sorted(factors.items())]
        config["feature_basket"] = equal_weights([m for m in feature_members if qualified(inputs, m["category"], m["model"])])
        config["raw_models"] = feature_members
        for key, entry in factors.items():
            config["calibration"][key] = {factor: calibration(values, inputs["end_date"]) for factor, values in entry["values"].items()}
        return config

    def _render(self, inputs, config, days, version, build_id=None):
        official, listing = self._series(inputs, config["parameters"])
        factors = self._factor_series(inputs, official, listing, config["parameters"])
        end = date.fromisoformat(inputs["end_date"])
        displayed = dates(end - timedelta(days=days - 1), end)
        price_indices = []
        for basis, series, name in (("official", official, "App近7日成交均价参考指数"), ("listing", listing, "实际观测挂牌中位价指数")):
            frozen = config["baskets"][basis]
            members, baseline = frozen["members"], frozen["baseline"]
            reviewed = {m["id"] for m in members if qualified(inputs, m["category"], m["model"], m.get("market_scope_id"))}
            points = []
            for day in displayed:
                missing = [m["model"] for m in members if m["id"] not in reviewed or day not in series.get(m["id"], {}).get("prices", {})]
                available_weight = sum(m["weight"] for m in members if m["id"] in reviewed and day in series.get(m["id"], {}).get("prices", {}))
                value = None
                if members and baseline and day >= baseline["end"] and not missing:
                    value = round(100 * math.exp(sum(m["weight"] * math.log(series[m["id"]]["prices"][day] / baseline["prices"][m["id"]]) for m in members)), 6)
                points.append(dict(date=day, value=value, coverage_pct=round(available_weight * 100, 2), missing_models=missing))
            price_indices.append(dict(id=basis, basis=basis, name=name, status="ready" if baseline and len(reviewed) == len(members) else "insufficient_data",
                basket=members, base_start=baseline["start"] if baseline else None, base_end=baseline["end"] if baseline else None,
                base_prices=baseline["prices"] if baseline else {}, points=points,
                metric="rolling_7d_sold_mean" if basis == "official" else "daily_listing_median",
                message="固定类别/型号等权；待复核/无支持范围不入正式篮子。缺失日不填补、不重分配权重。" if baseline and len(reviewed) == len(members) else "尚未达到已核对固定篮子共同连续7日基期；挂牌另须每型号每日至少20件正常在售样本。"))
        feature_rows = []
        missing_defaults = dict(momentum="缺少相隔28日的同口径App参考价", stability="缺少连续29个实际核实参考价以计算28个日变化",
            liquidity="尚无成熟14日首见在售队列", supply="缺少run_id可追溯采样、窗口前预热及连续14日同配置成功采样，或前7日新增为零",
            concentration="尚无正常在售挂牌观测")
        formal_members = {m["id"] for m in config["feature_basket"]}
        members_to_show = config["feature_basket"] + [{**m, "weight": 0} for m in config.get("raw_models", []) if m["id"] not in formal_members]
        for member in members_to_show:
            reviewed = member["id"] in formal_members and qualified(inputs, member["category"], member["model"])
            entry = factors.get(member["id"], {})
            values = entry.get("values", {})
            rows, reasons = [], []
            for definition in config["factor_definitions"]:
                key, label, weight, direction = (definition[k] for k in ("key", "label", "weight", "direction"))
                available = values.get(key, {})
                latest = max(available, default=None)
                raw = available[latest] if latest else None
                frozen = config["calibration"].get(member["id"], {}).get(key)
                score = None
                reason = missing_defaults[key]
                status = "missing"
                if raw:
                    status = "insufficient_data"
                    reason = "样本量或跟踪覆盖未达门槛" if not raw["eligible"] else "校准须有至少30个有效因子日，且全部早于评分日期"
                    if raw["eligible"] and frozen and latest == inputs["end_date"]:
                        score = standardize(raw["value"], frozen, direction, latest)
                        if score is not None:
                            status, reason = "ready", ""
                        elif math.isclose(frozen["q05"], frozen["q95"], rel_tol=1e-12, abs_tol=1e-12) and not frozen.get("constant"):
                            reason = "校准分位点重合但原始历史非常量，需复核校准；不填中性50"
                    if latest != inputs["end_date"]:
                        reason = f"最新原始因子仅至{latest}，不能冒充当前日期"
                        status = "stale"
                if key in {"momentum", "stability"} and entry.get("official_scope_count", 0) > 1:
                    reason = "该型号有多个不同官方范围，尚未选定唯一范围"
                if not reviewed:
                    score, status, reason = None, "needs_review", "型号/当前行情范围尚未核对、已禁用或不在此冻结篮子；原始因子仅供复核"
                row = dict(key=key, label=label, weight=weight, raw_value=round(raw["value"], 8) if raw else None,
                           raw_date=latest, score=score, contribution=round(score * weight, 8) if score is not None else None,
                           status=status, reason=reason, calibration=frozen,
                           raw_details={k: v for k, v in (raw or {}).items() if k not in {"value", "eligible"}})
                rows.append(row)
                if score is None:
                    reasons.append(f"{label}：{reason}")
            complete = all(r["score"] is not None for r in rows)
            feature_rows.append(dict(category=member["category"], model=member["model"], weight=member["weight"],
                status="ready" if complete else "waiting_data" if reviewed else "needs_review", basket_eligible=reviewed,
                score=round(sum(r["contribution"] for r in rows), 6) if complete else None,
                factors=rows, missing_reasons=reasons, as_of=inputs["end_date"]))
        aggregate = None
        indexed_rows = [f for f in feature_rows if f["basket_eligible"]]
        if indexed_rows and len(indexed_rows) == len(formal_members) and all(f["score"] is not None for f in indexed_rows):
            aggregate = round(sum(f["score"] * f["weight"] for f in indexed_rows), 6)
        return dict(version=version, build_id=build_id, input_hash=digest(inputs), category=inputs["category"], days=days,
            methodology=dict(**config["parameters"], price_aggregation="100×exp(Σw×ln(P/Pbase))", model_weights="类别等权，类别内独立型号/行情范围等权",
                             source="仅live挂牌与已核实App点；排除demo；完整自然日截至昨天（Asia/Shanghai）", calibration="冻结首30个有效因子日Q05/Q95；评分日期严格在其后",
                             price_eligibility_audit=inputs.get("price_eligibility_audit")),
            weights={f["key"]: f["weight"] for f in config["factor_definitions"]}, price_indices=price_indices, features=feature_rows,
            composite_score=aggregate, coverage=dict(end_date=inputs["end_date"], requested_days=days,
                official_scopes=len(official), listing_models=len(listing), feature_models=len(feature_rows),
                indexed_feature_models=len(indexed_rows),
                ready_feature_models=sum(f["score"] is not None for f in feature_rows),
                message="仅固定观察目录与真实样本；App滚动成交参考和挂牌分线。无完整因子不发布综合分，不伪造历史供给/售出记录。"))

    def get(self, days=180, category="all"):
        self._validate(days, category)
        inputs = self._inputs(category)
        with self.store.db() as db:
            saved = db.execute("SELECT * FROM index_builds WHERE category=? ORDER BY id DESC LIMIT 1", (category,)).fetchone()
        if saved:
            config, version = json.loads(saved["config_json"]), saved["version"]
            build_id = saved["id"]
            frozen_audit = config.get("price_eligibility_audit") or {}
            active_audit = inputs.get("price_eligibility_audit") or {}
            if any(frozen_audit.get(key, 0 if key == "revision" else None) != active_audit.get(key, 0 if key == "revision" else None)
                   for key in ("version", "revision", "rule_hash")):
                # A derived eligibility revision cannot silently change a saved
                # build. Old inputs remain replayable; publish a new build.
                saved = None
        if not saved:
            official, listing = self._series(inputs)
            config = self._propose(inputs, official, listing, self._factor_series(inputs, official, listing))
            version, build_id = "unpublished", None
        result = self._render(inputs, config, days, version, build_id)
        result["coverage"]["version_status"] = "frozen" if saved else "pending_rebuild"
        return result

    overview = get

    def rebuild(self, days=180, category="all"):
        self._validate(days, category)
        inputs = self._inputs(category)
        official, listing = self._series(inputs)
        config = self._propose(inputs, official, listing, self._factor_series(inputs, official, listing))
        version = "real-v1-" + digest(config)[:16]
        result = self._render(inputs, config, days, version)
        with self.store.db() as db:
            cur = db.execute("INSERT INTO index_builds(category,days,created_at,version,input_hash,config_json,inputs_json,result_json) VALUES (?,?,?,?,?,?,?,?)",
                (category, days, utcnow().isoformat(), version, digest(inputs), canonical(config), canonical(inputs), canonical(result)))
            result["id"] = result["build_id"] = cur.lastrowid
            db.execute("UPDATE index_builds SET result_json=? WHERE id=?", (canonical(result), cur.lastrowid))
        result["status"] = "ready" if any(s["status"] == "ready" for s in result["price_indices"]) else "insufficient_data"
        return result

    def replay(self, build_id):
        """Recalculate a saved build using its exact frozen source inputs."""
        with self.store.db() as db:
            row = db.execute("SELECT * FROM index_builds WHERE id=?", (build_id,)).fetchone()
        if row is None:
            raise ValidationError("指数构建不存在")
        return self._render(json.loads(row["inputs_json"]), json.loads(row["config_json"]), row["days"], row["version"], row["id"])


Indices = IndexService
