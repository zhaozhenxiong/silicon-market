"""Versioned derived eligibility for comparable *unit* listing prices.

Raw observations are never edited. Applying/rolling back selects an immutable
rules version; fresh ingestion records the same rule decisions independently.
"""
from __future__ import annotations

import argparse
import hashlib
import inspect
import json
import re
import threading
import unicodedata
from functools import lru_cache
from pathlib import Path

from .normalization import ValidationError, iso

VERSION = "listing-price-eligibility-v2"
PARTS = {"cpu", "gpu", "ram", "motherboard", "storage"}


def assess(title, category, model=""):
    """Conservative sale-intent/quotation-unit rules, never a price heuristic."""
    text = unicodedata.normalize("NFKC", str(title)).upper()
    reasons = []
    quote = re.sub(r"(?:不是|并非|非|不按|不以)(?:单|每)[根条个](?:(?:的)?(?:价格|价)|\s*\d+(?:\.\d+)?元)", " ", text)
    unit_quote = bool(re.search(r"标价(?:为|是)?\s*(?:1|一|单)[颗根条张套个]|(?:单|每)[颗根条张套个](?:的)?(?:价格|价)|(?:价格|标价)(?:为|是)\s*(?:1|一)[颗根条张套个]|元\s*(?:1|一)[颗根条张套个]|(?:单|每)[颗根条张套个]\s*[:：]?(?:[¥￥]\d+(?:\.\d+)?|\d+(?:\.\d+)?\s*元)|\d+(?:\.\d+)?元\s*/\s*[颗根条张套个]", quote))
    kit = bool(re.search(r"\d+\s*G(?:B)?\s*[X×*+]\s*(?:2|\d+\s*G)|\d+\s*G(?:B)?\s*\(\s*\d+\s*[*X×]\s*2\s*\)|两[条根].{0,4}共\s*\d+\s*G|套条|内存套装", text)) or "×2" in str(model)
    single_module_quote = bool(re.search(r"标价(?:为|是)?\s*(?:1|一|单)[根条个]|(?:单|每)[根条个](?:的)?(?:价格|价)|(?:价格|标价)(?:为|是)\s*(?:1|一)[根条个]|元\s*(?:1|一)[根条个]|(?:单|每)[根条个]\s*[:：]?(?:[¥￥]\d+(?:\.\d+)?|\d+(?:\.\d+)?\s*元)|\d+(?:\.\d+)?元\s*/\s*[根条个]", quote))
    if category == "ram" and kit and single_module_quote:
        reasons.append(("kit_unit_quote_conflict", "套装型号与单条/单个报价冲突，不能计入套装价格"))

    quantities = [(int(match[1]),match[2]) for match in re.finditer(r"(?<![A-Z0-9])([2-9]\d*|1\d+)\s*([颗个根条张套])(?=打包|一起|整批|批量|出售|出|[\s。！!，,、]|$)", text)]
    chinese_numbers = {"两":2,"二":2,"三":3,"四":4,"五":5,"六":6,"七":7,"八":8,"九":9,"十":10}
    quantities.extend((chinese_numbers[match[1]],match[2]) for match in re.finditer(r"([两二三四五六七八九十])\s*([颗个根条张套])(?=打包|一起|整批|批量|出售|出|共|[\s。！!，,、]|$)", text))
    if quantities and not unit_quote and any(q > (2 if category == "ram" and kit and unit in {"条","根"} else 1) for q,unit in quantities):
        reasons.append(("bulk_quote_unit_unknown", "多件/整批报价未明确单件计价，报价单位待核实"))

    if category not in PARTS:
        return reasons
    # Mentioning a source computer is different from offering that computer.
    sale = re.sub(r"(?:从)?(?:整机|主机|电脑)(?:上|中|里)?(?:正常|升级|刚刚|刚)?(?:拆机下来|拆机下|拆下来|拆下|拆出|取下|升级拆下|拆机取下|拆机件|拆机|购买|购入)", " ", text)
    sale = re.sub(r"(?:适合|适用于|用于|可用于|用来)[^。；;!！]{0,35}?(?:主机|整机)(?:用|使用|升级)?", " ", sale)
    sale = re.sub(r"(?:本来|之前|原本|当初).{0,16}?(?:组|买)(?:整机|主机).{0,12}?(?:没组|未组|没装|没买|取消|放弃)", " ", sale)
    components = r"(?:CPU|处理器|内存|显卡|主板|固态|硬盘)"
    explicit_single = bool(re.search(r"(?:只|仅)(?:单独)?(?:出售|卖出|销售|售卖|出|卖|售)(?:的|这颗|单颗|这一颗|一颗)?\s*"+components+r"(?!\s*[+＋/、和及])|(?:单独|单)"+components+r"(?:出售|售卖|销售|出|卖)|"+components+r"单独(?:出售|售卖|销售|出|卖)|裸卡(?:出售|售卖|出)", sale))
    if explicit_single:
        return reasons
    if re.search(r"板\s*U\s*(?:套装|套餐|整套|组合)|CPU\s*[+＋]\s*主板.{0,8}(?:套装|套餐|整套|组合)", sale):
        reasons.append(("multi_component_offer", "板U/多部件组合报价，不能作为独立部件挂牌价"))
        return reasons
    signal_text = re.sub(r"(?:标价|价格|售价|报价)[:：为是\s]*[¥￥]?\d+(?:\.\d+)?(?:元)?|[¥￥]?\d+(?:\.\d+)?\s*元", " ", sale)
    signal_text = re.sub(r"(?:[单双四两124](?:条|个)?\s*)?(?:DDR\s*[345]\s*)?内存(?:插)?槽|(?:M\.?2\s*)?固态(?:硬盘)?(?:接口|槽|位)", " ", signal_text)
    signals = {
        "cpu": bool(re.search(r"(?<![A-Z0-9])(?:CPU|I[3579][ -]?\d{4,5}|R[3579][ -]?\d{4}|RYZEN)|处理器", signal_text)),
        "gpu": bool(re.search(r"显卡|GEFORCE|(?<![A-Z0-9])(?:RTX|GTX|RX)\s*\d{3,4}|(?<![A-Z0-9])[345](?:060|070|080|090)\s*(?:TI|SUPER)?(?!\d)", signal_text)),
        "motherboard": bool(re.search(r"主板|(?<![A-Z0-9])[ABHXZ][345678]\d{2}[MI]?(?!\d)", signal_text)),
        "ram": bool(re.search(r"内存|DDR\s*[345]|(?<![A-Z0-9])D5(?![A-Z0-9])", signal_text)),
        "storage": bool(re.search(r"固态|硬盘|NVME|(?<![A-Z0-9])SSD(?![A-Z0-9])", signal_text)),
    }
    host = bool(re.search(r"整台电脑|单主机|组装台机|组装台式机|电脑整机|整机配置|(?:出自用|出售|转让|出|卖)(?:自用|高配|组装|全新|二手|游戏|电竞|办公|台式|电脑|一台|一套|\s){0,6}(?:主机|整机)|(?:海景房|高配|游戏|电竞|ITX|迷你)\s*主机|主机.{0,6}(?:出售|一台|整套)", sale))
    host = host or (sum(signals.values()) >= 2 and bool(re.search(r"主机|整机", sale)))
    host = host or (signals["cpu"] and signals["gpu"] and bool(re.search(r"准新机器|机器整套", sale)))
    if host:
        reasons.append(("complete_computer_offer", "明确售卖主机/整机，不能作为独立部件挂牌价"))
        return reasons
    compatibility = bool(re.search(r"兼容|适用|可搭配|建议搭配|推荐搭配|仅作展示|仅供展示|仅展示|不出售|不卖|另售|(?:主板|CPU|平台)支持|支持\s*(?:INTEL|A\s*M\s*D|RYZEN|锐龙|D[345]|DDR|LGA|AM[345]|XMP|EXPO|主板|内存|CPU|处理器|平台|显卡)", sale))
    formatted = len(re.findall(r"(?:CPU|处理器|主板|内存|显卡|硬盘|固态)\s*[:：/]", sale)) >= 2
    explicit_bundle = bool(re.search(r"配置清单|整套配置|整套出售|整套价格|打包出售|开机即用|到手即用", sale))
    if sum(signals.values()) >= 3 and (not compatibility or formatted or explicit_bundle):
        reasons.append(("multi_component_offer", "CPU/主板/显卡/内存等配置清单组合报价，单品价格不可比"))
    return reasons


@lru_cache(maxsize=1)
def _rule_hash():
    return hashlib.sha256(json.dumps([VERSION, sorted(PARTS), inspect.getsource(assess)], ensure_ascii=False).encode()).hexdigest()


class PriceAudit:
    def __init__(self, store):
        self.store = store
        with store.db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS price_audit_versions (
                    version TEXT PRIMARY KEY,rule_hash TEXT NOT NULL,created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS price_audit_state (
                    id INTEGER PRIMARY KEY CHECK(id=1),active_version TEXT,revision INTEGER NOT NULL DEFAULT 0);
                INSERT OR IGNORE INTO price_audit_state(id) VALUES (1);
                CREATE TABLE IF NOT EXISTS price_eligibility_audit (
                    id INTEGER PRIMARY KEY,item_pk INTEGER NOT NULL,item_id TEXT NOT NULL,
                    observation_id INTEGER NOT NULL,version TEXT NOT NULL,eligible INTEGER NOT NULL,
                    reasons TEXT NOT NULL,created_at TEXT NOT NULL,UNIQUE(observation_id,version));
                CREATE INDEX IF NOT EXISTS price_audit_lookup ON price_eligibility_audit(version,observation_id);
                CREATE TABLE IF NOT EXISTS price_audit_changes (
                    id INTEGER PRIMARY KEY,revision INTEGER NOT NULL,event TEXT NOT NULL,
                    previous_version TEXT,active_version TEXT,created_at TEXT NOT NULL);
            """)

    @staticmethod
    def record_in(db, item_pk, item_id, observation_id, title, category, model):
        row = db.execute("SELECT rule_hash FROM price_audit_versions WHERE version=?", (VERSION,)).fetchone()
        if row and row["rule_hash"] != _rule_hash():
            raise ValidationError("价格资格规则代码已变化，请使用新审计版本，不能改写旧版本")
        if not row:
            db.execute("INSERT INTO price_audit_versions VALUES (?,?,?)", (VERSION, _rule_hash(), iso()))
        reasons = assess(title, category, model)
        db.execute("INSERT OR IGNORE INTO price_eligibility_audit(item_pk,item_id,observation_id,version,eligible,reasons,created_at) VALUES (?,?,?,?,?,?,?)", (item_pk, item_id, observation_id, VERSION, not reasons, json.dumps(reasons, ensure_ascii=False), iso()))

    def state(self):
        with self.store.db() as db:
            state = dict(db.execute("SELECT active_version AS version,revision FROM price_audit_state WHERE id=1").fetchone())
            version = db.execute("SELECT rule_hash FROM price_audit_versions WHERE version=?", (state["version"],)).fetchone()
            rows = [tuple(row) for row in db.execute("SELECT observation_id,eligible,reasons FROM price_eligibility_audit WHERE version=? ORDER BY observation_id", (state["version"],))]
        state["rule_hash"] = version["rule_hash"] if version else None
        state["decisions"] = len(rows)
        state["excluded_observations"] = sum(not row[1] for row in rows)
        state["input_hash"] = hashlib.sha256(json.dumps([state, rows], ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        return state

    def preview(self):
        with self.store.db() as db:
            rows = db.execute("SELECT o.*,i.item_id,i.source FROM observations o JOIN items i ON i.id=o.item_pk WHERE i.source='live' ORDER BY o.id").fetchall()
        changes = []
        for row in rows:
            reasons = assess(row["title"], row["category"], row["model"])
            if reasons:
                changes.append(dict(item_pk=row["item_pk"], item_id=row["item_id"], observation_id=row["id"], version=VERSION,
                                    title=row["title"], category=row["category"], model=row["model"], price=row["price"],
                                    observed_at=row["observed_at"], raw_excluded=bool(row["excluded"]), raw_quality_flags=json.loads(row["quality_flags"]),
                                    audit_eligible=False, effective_excluded=True, reason_codes=[r[0] for r in reasons], reasons=[r[1] for r in reasons]))
        return dict(version=VERSION, rule_hash=_rule_hash(), generated_at=iso(), observed_rows=len(rows), affected_observations=len(changes),
                    affected_items=len({row["item_pk"] for row in changes}), changes=changes, current_state=self.state(),
                    message="仅派生价格资格预览；保留全部原始标题、价格、状态及观测时间，不拆分或推断单价。")

    def apply(self):
        with self.store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            def raw_fingerprint():
                raw = {table: [tuple(row) for row in db.execute(f"SELECT * FROM {table} ORDER BY rowid")]
                       for table in ("items", "observations")}
                return hashlib.sha256(json.dumps(raw, ensure_ascii=False, default=str).encode()).hexdigest()
            raw_before = raw_fingerprint()
            rows = db.execute("SELECT o.*,i.item_id FROM observations o JOIN items i ON i.id=o.item_pk WHERE i.source='live' ORDER BY o.id").fetchall()
            for row in rows:
                self.record_in(db, row["item_pk"], row["item_id"], row["id"], row["title"], row["category"], row["model"])
            old = db.execute("SELECT * FROM price_audit_state WHERE id=1").fetchone()
            if old["active_version"] != VERSION:
                revision = old["revision"]+1
                db.execute("UPDATE price_audit_state SET active_version=?,revision=? WHERE id=1", (VERSION, revision))
                db.execute("INSERT INTO price_audit_changes(revision,event,previous_version,active_version,created_at) VALUES (?,'apply',?,?,?)", (revision, old["active_version"], VERSION, iso()))
            raw_after = raw_fingerprint()
            if raw_before != raw_after:
                raise ValidationError("审计应用中原始表发生变化，已回滚派生提交")
        return {**self.state(), "raw_unchanged": True, "raw_input_hash": raw_before}

    def rollback(self):
        with self.store.db() as db:
            old = db.execute("SELECT * FROM price_audit_state WHERE id=1").fetchone()
            if old["active_version"] is not None:
                revision = old["revision"]+1
                db.execute("UPDATE price_audit_state SET active_version=NULL,revision=? WHERE id=1", (revision,))
                db.execute("INSERT INTO price_audit_changes(revision,event,previous_version,active_version,created_at) VALUES (?,'rollback',?,NULL,?)", (revision, old["active_version"], iso()))
        return self.state()

    def _decisions(self):
        with self.store.db() as db:
            active = db.execute("SELECT active_version FROM price_audit_state WHERE id=1").fetchone()[0]
            rows = db.execute("SELECT observation_id,eligible,reasons FROM price_eligibility_audit WHERE version=?", (active,)).fetchall() if active else []
        return active, {row["observation_id"]: row for row in rows}

    def mask_observations(self, observations):
        active, decisions = self._decisions()
        result = []
        for original in observations:
            row = dict(original)
            decision = decisions.get(row["id"])
            flags = [reason[1] for reason in json.loads(decision["reasons"])] if decision else []
            # Missing stored decisions for a newly arrived row use the exact
            # active implementation. Other historical versions never borrow it.
            if decision is None and active == VERSION and row.get("source") != "demo":
                flags = [reason[1] for reason in assess(row["title"], row["category"], row["model"])]
            row["raw_excluded"] = bool(row["excluded"])
            row["price_audit_flags"] = flags
            row["price_audit_version"] = active
            row["excluded"] = bool(row["excluded"] or flags)
            result.append(row)
        return result

    def annotate_items(self, items):
        if not items:
            return []
        active, decisions = self._decisions()
        with self.store.db() as db:
            latest = {row["item_pk"]: row["id"] for row in db.execute("SELECT item_pk,id FROM observations ORDER BY observed_at,id")}
        result = []
        for original in items:
            row = dict(original)
            decision = decisions.get(latest.get(row["id"]))
            flags = [reason[1] for reason in json.loads(decision["reasons"])] if decision else []
            if decision is None and active == VERSION and row.get("source") == "live":
                flags = [reason[1] for reason in assess(row["title"], row["category"], row["model"])]
            row["raw_excluded"] = bool(row["excluded"])
            row["price_audit_flags"], row["price_audit_version"] = flags, active
            row["excluded"] = bool(row["excluded"] or flags)
            original_flags = row["quality_flags"] if isinstance(row["quality_flags"], list) else json.loads(row["quality_flags"])
            row["raw_quality_flags"] = list(original_flags)
            row["quality_flags"] = list(dict.fromkeys(original_flags+flags))
            result.append(row)
        return result


def main():
    parser = argparse.ArgumentParser(description="只新增派生价格资格审计；不修改原始观察")
    parser.add_argument("command", choices=["preview", "apply", "rollback", "state", "rebuild-index"])
    parser.add_argument("--db", default=str(Path(__file__).resolve().parent.parent/"data/market.sqlite3"))
    parser.add_argument("--output")
    parser.add_argument("--days", type=int, default=180)
    parser.add_argument("--category", default="all")
    args = parser.parse_args()
    from .service import Store
    audit = PriceAudit(Store(args.db, production=True, initialize=False))
    if args.command == "rebuild-index":
        from .indices import IndexService
        result = IndexService(audit.store).rebuild(days=args.days, category=args.category)
        destination = Path(args.output) if args.output else Path(__file__).resolve().parent.parent/"output/indices_after_price_audit.json"
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps(result, ensure_ascii=False, indent=2))
        print(json.dumps({key: result.get(key) for key in ("id", "version", "input_hash", "status", "coverage")}, ensure_ascii=False))
    elif args.command == "preview":
        result = audit.preview()
        destination = Path(args.output) if args.output else Path(__file__).resolve().parent.parent/"output/price_audit_preview.json"
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps(result, ensure_ascii=False, indent=2))
        print(json.dumps({key: value for key, value in result.items() if key != "changes"}, ensure_ascii=False))
    else:
        print(json.dumps(getattr(audit, args.command)(), ensure_ascii=False))


if __name__ == "__main__":
    main()
