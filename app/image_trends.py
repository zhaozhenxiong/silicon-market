"""Versioned, estimated image-shape indices; never historical price points.

Levels describe position inside the photographed chart. Neither pixel levels nor
the scores are CNY prices or price returns. All calendar dates are estimates.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter
from datetime import date,datetime,timedelta
from statistics import median

from .catalog import canonical,identity
from .jobs import LeaseError
from .normalization import CATEGORIES,SHANGHAI,ValidationError,iso,utcnow
from .service import quantile

CONFIG=dict(method="image-trend-estimate-v1",mode="partial_estimate",min_points=12,min_x_span=.8,
    min_column_coverage=.6,min_valid_columns=12,max_trace_gap_fraction=.25,max_source_age_days=7,
    min_axis_span_days=28,max_axis_lag_days=31,max_future_tick_days=31,
    shape_weights=dict(overall_shape=.4,recent_momentum=.6),
    shape_formula="0.4*(50+0.5*(median(t>=.8)-median(t<=.2)))+0.6*(50+0.5*(median(t>=.9)-median(.8<=t<.9))); t=(x-min(x))/(max(x)-min(x))",
    composite_weights=dict(image_trend=.7,listing_concentration=.3),listing_window_days=7,listing_min_samples=5,
    listing_formula="100/(1+IQR/median); quote dispersion proxy, not price reliability or a price return",
    coverage_metrics=dict(factor="sum of available nominal factor weights",source="sum(effective factor weight * underlying source coverage)"),
    neutral="near 50 means observed direction is neutral/balanced; missing stays null",unit="relative_shape_score_0_100",
    score_x_basis="observed_trace_extent",
    dates="monthday labels mapped to nearby years only as estimated metadata; never canonical dates",
    member_weights="category equal, then exact catalog/model equal; missing effective weights are explicitly redistributed")
VISIBLE_CONDITION=re.compile(r"少量磕碰划痕|严重磕碰划痕|几乎全新|轻微磕碰划痕|细微使用痕迹|明显使用痕迹|成色\s*[:：]?\s*(?:全新|99新|95新|九成新)")


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def finite(value,lo,hi):
    return type(value) in {int,float} and lo <= value <= hi and math.isfinite(value)


def shape_score(points):
    """Use the extractor's ordered column anchors without fabricating points."""
    if len(points)<2:
        return None,[]
    extent=points[-1]["x"]-points[0]["x"]
    if extent<=0:return None,[]
    sampled=[((point["x"]-points[0]["x"])/extent,point["level"]) for point in points]
    groups=[[level for x,level in sampled if x<=.2],[level for x,level in sampled if x>=.8],
            [level for x,level in sampled if .8<=x<.9],[level for x,level in sampled if x>=.9]]
    if any(not group for group in groups):
        return None,[]
    overall=50+.5*(median(groups[1])-median(groups[0]))
    recent=50+.5*(median(groups[3])-median(groups[2]))
    components=[dict(key="overall_shape",label="可见全段走势",score=round(overall,4),nominal_weight=.4,effective_weight=.4,
                     window="x<=0.2 versus x>=0.8; image window, not fixed calendar days"),
                dict(key="recent_momentum",label="近期曲线动量",score=round(recent,4),nominal_weight=.6,effective_weight=.6,
                     window="0.8<=x<0.9 versus x>=0.9; image window, not fixed calendar days")]
    return round(max(0,min(100,.4*overall+.6*recent)),4),components


def estimated_axis(labels,captured_at):
    captured=datetime.fromisoformat(captured_at).astimezone(SHANGHAI).date()
    parsed=[]
    for row in labels:
        match=re.fullmatch(r"([0-9]{2})[./-]([0-9]{2})",row["label"].strip())
        if not match:continue
        month,day=int(match[1]),int(match[2])
        try:date(2000,month,day)
        except ValueError:continue
        parsed.append((row["x"],month,day))
    result=dict(period_start_estimated=None,period_end_estimated=None,axis_end_lag_days=None,
                span_days_estimated=None,date_precision="estimated_monthday",canonical_dates=False,
                capture_local_date=captured.isoformat(),year_source="nearby capture year; estimate only")
    if len(parsed)<2:return result
    parsed.sort()
    def possibilities(month,day,year):
        values=[]
        for y in (year-1,year,year+1):
            try:values.append(date(y,month,day))
            except ValueError:pass
        return values
    last=parsed[-1]
    candidates=possibilities(last[1],last[2],captured.year)
    if not candidates:return result
    end=min(candidates,key=lambda day:(abs((day-captured).days),day>captured))
    cursor=end
    for _,month,day in reversed(parsed[:-1]):
        previous=[candidate for candidate in possibilities(month,day,cursor.year) if candidate<=cursor]
        if not previous:return result
        cursor=max(previous)
    result.update(period_start_estimated=cursor.isoformat(),period_end_estimated=end.isoformat(),
                  axis_end_lag_days=(captured-end).days,span_days_estimated=(end-cursor).days)
    return result


class ImageTrends:
    def __init__(self,store,targets,captures):
        self.store,self.targets,self.captures=store,targets,captures
        with store.db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS image_trend_extracts (
                    id INTEGER PRIMARY KEY,frame_id INTEGER NOT NULL REFERENCES mobile_capture_frames(id),
                    source_hash TEXT NOT NULL,extractor_version TEXT NOT NULL,payload_hash TEXT NOT NULL,
                    payload TEXT NOT NULL,created_at TEXT NOT NULL,UNIQUE(frame_id,extractor_version));
                CREATE TABLE IF NOT EXISTS image_trend_index_builds (
                    id INTEGER PRIMARY KEY,version TEXT NOT NULL,created_at TEXT NOT NULL,input_hash TEXT NOT NULL,
                    config TEXT NOT NULL,inputs TEXT NOT NULL,result TEXT NOT NULL);
            """)

    @staticmethod
    def _validate(payload):
        fields={"frame_id","source_hash","extractor_version","signal_kind","quality","points","axis_labels","header_price","raw_metric_label","notes"}
        if not isinstance(payload,dict) or set(payload)!=fields:
            raise ValidationError("图像趋势只接受固定来源、几何质量、相对曲线和原文字段；不接受confidence或实际价格历史")
        if type(payload["frame_id"]) is not int or not 0<payload["frame_id"]<=9223372036854775807:
            raise ValidationError("frame_id须为真实截图正整数ID")
        if not isinstance(payload["source_hash"],str) or not re.fullmatch(r"[a-f0-9]{64}",payload["source_hash"]):
            raise ValidationError("source_hash须为真实原图SHA256")
        if not isinstance(payload["extractor_version"],str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,119}",payload["extractor_version"]):
            raise ValidationError("extractor_version须为明确提取器版本，不接受路径或命令")
        if payload["signal_kind"] not in ("price_curve","popularity_only"):
            raise ValidationError("signal_kind只接受price_curve或popularity_only")
        q=payload["quality"]
        if not isinstance(q,dict) or set(q)!={"sampled_columns","valid_columns","column_coverage","trace_gap_max_px"}:
            raise ValidationError("quality须含实测采样列、有效列、覆盖率和最大断点像素")
        if (type(q["sampled_columns"]) is not int or not 0<=q["sampled_columns"]<=100000
                or type(q["valid_columns"]) is not int or not 0<=q["valid_columns"]<=q["sampled_columns"]
                or not finite(q["column_coverage"],0,1) or not finite(q["trace_gap_max_px"],0,max(1,q["sampled_columns"]))):
            raise ValidationError("几何质量须为有效有限实测值，覆盖率范围0至1")
        ratio=q["valid_columns"]/q["sampled_columns"] if q["sampled_columns"] else 0
        if abs(q["column_coverage"]-ratio)>.02:
            raise ValidationError("列覆盖率与有效列/采样列不一致")
        points=payload["points"]
        if not isinstance(points,list) or len(points)>2000 or (payload["signal_kind"]=="price_curve" and len(points)<2):
            raise ValidationError("price_curve须有2至2000个相对点；不足历史仅归档不入指数")
        previous=-1
        for point in points:
            if not isinstance(point,dict) or set(point)!={"x","level"} or not finite(point["x"],0,1) or not finite(point["level"],0,100) or point["x"]<=previous:
                raise ValidationError("曲线x须严格递增且0至1，level须0至100；不能输入人民币或日期点")
            previous=point["x"]
        labels=payload["axis_labels"]
        if not isinstance(labels,list) or len(labels)>40:
            raise ValidationError("axis_labels最多40个实际原图刻度")
        for label in labels:
            if not isinstance(label,dict) or set(label)!={"x","label"} or not finite(label["x"],0,1) or not isinstance(label["label"],str) or not 1<=len(label["label"])<=64:
                raise ValidationError("轴刻度只接受归一化位置和原文label，不确定年份")
        price=payload["header_price"]
        if price is not None and not finite(price,0,100_000_000):
            raise ValidationError("header_price只能为已有屏幕读值或null，不由曲线level换算")
        for key,maximum in (("raw_metric_label",500),("notes",4000)):
            if not isinstance(payload[key],str) or len(payload[key])>maximum:
                raise ValidationError(f"{key}须为最多{maximum}字的真实原文/说明")

    def _frame_current(self,db,frame):
        target=self.targets.require_catalog(db,frame["catalog_id"])
        if (target["catalog_version"]!=frame["catalog_version"] or target["market_scope_id"]!=frame["market_scope_id"]
                or target["task_ids"]!=json.loads(frame["task_ids"]) or target["scope"]!=json.loads(frame["scope"])):
            raise LeaseError("图像来源的监控任务、目录版本或经济范围已变化，不能作为当前范围导入")
        return target

    def import_extract(self,payload):
        self._validate(payload)
        fingerprint=digest(payload)
        with self.store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            frame=db.execute("SELECT * FROM mobile_capture_frames WHERE id=?",(payload["frame_id"],)).fetchone()
            if frame is None or frame["evidence_hash"]!=payload["source_hash"]:
                raise ValidationError("frame_id/source_hash不匹配真实截图")
            self._frame_current(db,frame)  # Phone pause does not prevent image processing.
            self.captures._evidence(frame["evidence_filename"],payload["source_hash"])
            old=db.execute("SELECT * FROM image_trend_extracts WHERE frame_id=? AND extractor_version=?",(payload["frame_id"],payload["extractor_version"])).fetchone()
            if old:
                if old["payload_hash"]!=fingerprint:
                    raise ValidationError("同一提取版本已有不同结果，须用新extractor_version，禁止改写旧曲线")
                return dict(id=old["id"],created=False,source_hash=old["source_hash"],canonical_price_points_created=0)
            cur=db.execute("INSERT INTO image_trend_extracts(frame_id,source_hash,extractor_version,payload_hash,payload,created_at) VALUES (?,?,?,?,?,?)",
                (payload["frame_id"],payload["source_hash"],payload["extractor_version"],fingerprint,canonical(payload),iso()))
        return dict(id=cur.lastrowid,created=True,source_hash=payload["source_hash"],canonical_price_points_created=0)

    def _decode(self,row,frame,current):
        item={**json.loads(row["payload"]),"id":row["id"],"created_at":row["created_at"],
            **{key:frame[key] for key in ("catalog_id","catalog_version","category","model","query","market_scope_id","captured_at")},
            "scope":json.loads(frame["scope"]),"task_ids":json.loads(frame["task_ids"]),
            "evidence_url":f"/api/mobile/captures/{frame['id']}/evidence","canonical_price":False,
            "score_unit":"relative_image_shape_0_100","price_change_pct":None,
            "observed_scope_text":frame.get("observed_scope_text","")}
        item["estimated_scope"]=item["scope"].get("verified") is not True
        score,components=shape_score(item["points"]) if item["signal_kind"]=="price_curve" else (None,[])
        item.update(raw_trend_score=score,score_components=components,score_x_basis="observed_trace_extent")
        reasons=[]
        def reject(code,message):reasons.append(dict(code=code,message=message))
        target=current.get(frame["catalog_id"])
        if target is None:reject("not_monitored","当前没有此范围的已启用真实监控任务")
        elif (target["catalog_version"]!=frame["catalog_version"] or target["market_scope_id"]!=frame["market_scope_id"]
                or target["task_ids"]!=item["task_ids"] or target["scope"]!=item["scope"]):
            reject("scope_changed","冻结目录版本、经济范围或监控集合已变更")
        try:self.captures._evidence(frame["evidence_filename"],item["source_hash"])
        except (ValidationError,OSError):reject("source_unavailable","原图不存在、格式异常或SHA256已改变")
        if item["signal_kind"]!="price_curve":reject("popularity_only","仅搜索热度，不是成交价格曲线，不混入行情趋势")
        else:
            if "近7日成交均价" not in re.sub(r"\s+","",item["raw_metric_label"]):reject("metric_unproven","原图指标未明确为近7日成交均价")
            points=item["points"]
            if len(points)<CONFIG["min_points"] or points[-1]["x"]-points[0]["x"]<CONFIG["min_x_span"] or score is None:
                reject("short_history","可见曲线点数、跨度或近期区间不足")
            q=item["quality"]
            if q["valid_columns"]<CONFIG["min_valid_columns"] or q["column_coverage"]<CONFIG["min_column_coverage"] or q["trace_gap_max_px"]>q["sampled_columns"]*CONFIG["max_trace_gap_fraction"]:
                reject("low_geometric_quality","实际曲线列覆盖不足或断点过大")
        age=(utcnow().astimezone(SHANGHAI).date()-datetime.fromisoformat(frame["captured_at"]).astimezone(SHANGHAI).date()).days
        if age>CONFIG["max_source_age_days"]:reject("stale_source","截图拍摄时间已过当前估算有效期")
        axis=estimated_axis(item["axis_labels"],frame["captured_at"])
        if item["signal_kind"]=="price_curve":
            if axis["period_end_estimated"] is None:reject("axis_unresolved","原图轴刻度不足，无法估计观察区间")
            else:
                if axis["axis_end_lag_days"]>CONFIG["max_axis_lag_days"]:reject("stale_axis","日期轴与拍摄时间的近年估计不一致，当前性无法确认；保留原图，不计当前趋势")
                if axis["axis_end_lag_days"] < -CONFIG["max_future_tick_days"]:reject("future_axis","右端超出允许的常规未来刻度，观察区间待复核")
                if axis["span_days_estimated"]<CONFIG["min_axis_span_days"]:reject("short_axis","原图轴所示历史窗口过短")
        item.update(current_score=score if not reasons else None,
                    eligibility=dict(eligible=not reasons,reasons=reasons,date_axis=axis))
        return item

    def _inputs(self):
        with self.store.lock:
            report=self.targets.list()
            current={row["catalog_id"]:row for row in report["targets"]}
            with self.store.db() as db:
                frames={row["id"]:dict(row) for row in db.execute("SELECT * FROM mobile_capture_frames")}
                observed_scopes={row["frame_id"]:row["scope_text"] for row in db.execute("SELECT r.frame_id,r.scope_text FROM mobile_capture_recognitions r JOIN (SELECT frame_id,max(id) latest FROM mobile_capture_recognitions GROUP BY frame_id) m ON r.id=m.latest")}
                for frame_id,frame in frames.items():frame["observed_scope_text"]=observed_scopes.get(frame_id,"")
                rows=db.execute("SELECT * FROM image_trend_extracts ORDER BY id").fetchall()
                items=[self._decode(row,frames[row["frame_id"]],current) for row in rows if row["frame_id"] in frames]
                cutoff=iso(utcnow()-timedelta(days=CONFIG["listing_window_days"]))
                raw=[self.store.item(row) for row in db.execute("SELECT * FROM items WHERE source='live' AND status='on_sale' AND last_seen>=?",(cutoff,))]
            audited=self.store.price_audit.annotate_items(raw)
            keys={(row["category"],identity(row["model"])) for row in current.values()}
            listing=[{key:row[key] for key in ("id","category","model","price","last_seen")} for row in audited
                     if not row["excluded"] and (row["category"],identity(row["model"])) in keys and finite(row["price"],.01,100_000_000)]
            audit=self.store.price_audit.state()
        return dict(as_of_date=utcnow().astimezone(SHANGHAI).date().isoformat(),targets=report["targets"],
                    skipped_tasks=report["skipped_tasks"],enabled_task_count=report["enabled_task_count"],
                    items=items,listing=listing,price_eligibility_audit=audit)

    def list(self,category="all",model="all"):
        if category not in CATEGORIES|{"all"}:
            raise ValidationError("无效分类")
        inputs=self._inputs()
        latest={}
        for item in sorted(inputs["items"],key=lambda row:(row["captured_at"],row["id"])):
            latest[item["catalog_id"]]=item
        current={target["catalog_id"] for target in inputs["targets"]}
        items=[item for key,item in latest.items() if key in current and category in {"all",item["category"]} and model in {"all",item["model"]}]
        return dict(items=items,total=len(items),enabled_task_count=inputs["enabled_task_count"],policy="enabled_live_monitor_tasks_only",
                    window="original screenshot window; no days clipping or fabricated daily dates",canonical_price_points_created=0)

    @staticmethod
    def _render(inputs):
        targets=inputs["targets"];counts=Counter(row["category"] for row in targets)
        latest={}
        for item in sorted(inputs["items"],key=lambda row:(row["captured_at"],row["id"])):latest[item["catalog_id"]]=item
        members=[];listing_members=[]
        for target in targets:
            weight=1/len(counts)/counts[target["category"]]
            item=latest.get(target["catalog_id"])
            score=item["current_score"] if item else None
            reasons=item["eligibility"]["reasons"] if item else [dict(code="missing_source",message="尚无当前范围的原图曲线提取")]
            members.append(dict(catalog_id=target["catalog_id"],category=target["category"],model=target["model"],score=score,
                nominal_weight=weight,effective_weight=0,eligible=score is not None,reasons=reasons,
                estimated_scope=target["scope"].get("verified") is not True,source_id=item["id"] if item else None,
                score_components=item["score_components"] if item else []))
            prices=[row["price"] for row in inputs["listing"] if row["category"]==target["category"] and identity(row["model"])==identity(target["model"])]
            storage=target["scope"].get("storage")
            capacity_proven=not storage or bool(isinstance(storage,str) and re.search(r"(?<![A-Z0-9])"+re.escape(storage.upper())+r"(?![A-Z0-9])",target["model"].upper()))
            # Source semantics may be narrower than the still-unreviewed target
            # scope. Fixed condition words only reject a proxy; they never become
            # executable filters or grant scope verification.
            observed_text=item.get("observed_scope_text","") if item else ""
            source_notes=item.get("notes","") if item else ""
            visible_specific=bool(VISIBLE_CONDITION.search(observed_text+"\n"+source_notes))
            unreviewed_phone=target["category"]=="phone" and target["scope"].get("verified") is not True
            scope_specific=bool(target["scope"].get("condition") or target["scope"].get("filters") or not capacity_proven or visible_specific or unreviewed_phone)
            value=None;reason=""
            if scope_specific:reason="scope_unmatched：挂牌缺少能证明App成色/筛选同范围的结构化证据，不混合所有成色"
            elif len(prices)<CONFIG["listing_min_samples"]:reason="精确型号的真实合格挂牌样本不足5件"
            else:
                center=median(prices);spread=quantile(prices,.75)-quantile(prices,.25)
                value=round(100/(1+spread/center),4)
            listing_members.append(dict(catalog_id=target["catalog_id"],model=target["model"],score=value,nominal_weight=weight,
                                        effective_weight=0,sample_count=len(prices),reason=reason,
                                        observed_scope_text=observed_text,scope_basis="exact model; actual image scope must be proven; quote dispersion proxy only"))
        def aggregate(rows):
            coverage=sum(row["nominal_weight"] for row in rows if row["score"] is not None)
            for row in rows:row["effective_weight"]=row["nominal_weight"]/coverage if coverage and row["score"] is not None else 0
            return (round(sum(row["score"]*row["effective_weight"] for row in rows if row["score"] is not None),4) if coverage else None),coverage
        market,market_coverage=aggregate(members)
        listing,listing_coverage=aggregate(listing_members)
        components=[dict(key="image_trend",label="截图估算趋势",score=market,nominal_weight=.7,effective_weight=0,available=market is not None,underlying_coverage=market_coverage),
                    dict(key="listing_concentration",label="真实挂牌报价一致度",score=listing,nominal_weight=.3,effective_weight=0,available=listing is not None,underlying_coverage=listing_coverage)]
        composite,coverage=aggregate(components) if market is not None else (None,0)
        source_coverage=sum(row["effective_weight"]*row["underlying_coverage"] for row in components)
        partial=market_coverage<.999999 or coverage<.999999 or (listing is not None and listing_coverage<.999999)
        status="insufficient_data" if market is None else "partial_estimate" if partial else "estimated"
        source_keys=("frame_id","source_hash","extractor_version","catalog_id","catalog_version","model","category","evidence_url","captured_at","signal_kind","estimated_scope","quality","observed_scope_text")
        source_metadata=[{**{key:item[key] for key in source_keys},"eligible":item["eligibility"]["eligible"],"reasons":item["eligibility"]["reasons"],"date_axis":item["eligibility"]["date_axis"]} for key,item in latest.items() if key in {row["catalog_id"] for row in targets}]
        return dict(status=status,market_index=dict(score=market,coverage=market_coverage,coverage_pct=round(market_coverage*100,2),members=members,
            excluded=[row for row in members if not row["eligible"]],available_member_count=sum(row["eligible"] for row in members),member_count=len(members),
            expected_target_count=len(members),available_price_curve_count=sum(row["eligible"] for row in members),
            captured_price_curve_count=sum(latest.get(row["catalog_id"],{}).get("signal_kind")=="price_curve" for row in targets),
            task_coverage=sum(row["eligible"] for row in members)/len(members) if members else 0,mode="partial_estimate"),
            composite_index=dict(score=composite,status=status,coverage=coverage,coverage_pct=round(coverage*100,2),
                factor_coverage_pct=round(coverage*100,2),source_coverage_pct=round(source_coverage*100,2),
                source_coverage_basis="effective factor weights × each factor's frozen source coverage",components=components,
                listing_members=listing_members,listing_coverage=listing_coverage,mode="partial_estimate",requires_image_trend=True),
            config=json.loads(canonical(CONFIG)),source_metadata=source_metadata,as_of_date=inputs["as_of_date"],
            label="图像估算行情趋势 / 估算趋势综合指数",canonical_price=False,canonical_price_points_created=0,
            message="0–100图内相对趋势分，不是人民币价格指数或涨跌百分比；缺失按公开有效权重重分配，不补50；日期仅估算，原图和旧严格指数保留")

    def rebuild(self):
        inputs=self._inputs();config=json.loads(canonical(CONFIG))
        fingerprint=digest(dict(inputs=inputs,config=config))
        result=self._render(inputs);stamp=iso()
        with self.store.db() as db:
            cur=db.execute("INSERT INTO image_trend_index_builds(version,created_at,input_hash,config,inputs,result) VALUES (?,?,?,?,?,?)",
                           (CONFIG["method"],stamp,fingerprint,canonical(config),canonical(inputs),canonical(result)))
            result.update(build_id=cur.lastrowid,version=f"{CONFIG['method']}:{cur.lastrowid}",input_hash=fingerprint,created_at=stamp,pending_rebuild=False)
            db.execute("UPDATE image_trend_index_builds SET version=?,result=? WHERE id=?",(result["version"],canonical(result),cur.lastrowid))
        return result

    def get(self):
        inputs=self._inputs();current_hash=digest(dict(inputs=inputs,config=CONFIG))
        with self.store.db() as db:
            row=db.execute("SELECT * FROM image_trend_index_builds ORDER BY id DESC LIMIT 1").fetchone()
        if row is None:
            result=self._render(inputs)
            result.update(build_id=None,version="unbuilt",input_hash=current_hash,created_at=None,pending_rebuild=True)
            result["market_index"]["score"]=result["composite_index"]["score"]=None
            result["status"]="pending_rebuild"
            return result
        result=self._frozen_result(row)
        if row["input_hash"]!=current_hash:
            result["frozen_scores"]={"market":result["market_index"]["score"],"composite":result["composite_index"]["score"]}
            result["market_index"]["score"]=result["composite_index"]["score"]=None
            result.update(pending_rebuild=True,status="pending_rebuild",current_input_hash=current_hash,
                          message="任务、来源、审计、日期或输入已变化；旧构建保留，当前指数须显式重算，不沿用旧范围得分")
        return result

    def replay(self,build_id):
        if type(build_id) is not int or build_id<=0:raise ValidationError("构建ID须为正整数")
        with self.store.db() as db:
            row=db.execute("SELECT * FROM image_trend_index_builds WHERE id=?",(build_id,)).fetchone()
        return self._frozen_result(row) if row else None

    @staticmethod
    def _frozen_result(row):
        inputs,config,result=json.loads(row["inputs"]),json.loads(row["config"]),json.loads(row["result"])
        if (digest(dict(inputs=inputs,config=config))!=row["input_hash"] or result.get("input_hash")!=row["input_hash"]
                or result.get("config")!=config):
            raise ValidationError("冻结输入/配置哈希校验失败，不能展示为可审计构建")
        return {**result,"replay_kind":"frozen_result","frozen_integrity_verified":True}
