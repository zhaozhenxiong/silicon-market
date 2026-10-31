"""Archive actual phone screenshots for later recognition, never price points.

The frozen scope describes the intended target, not a verified reading of the
image. Unverified scope screenshots stay queued_scope until a separate review.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import date, datetime

from .catalog import canonical
from .jobs import LeaseError
from .normalization import SHANGHAI, ValidationError, iso, utcnow


class MobileCaptures:
    PENDING = ("queued", "queued_scope")

    def __init__(self, store, targets, official, paused=lambda: False):
        self.store, self.targets, self.official, self.paused = store, targets, official, paused
        with store.db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS mobile_capture_batches (
                    id INTEGER PRIMARY KEY, signature TEXT NOT NULL UNIQUE,
                    catalog_id INTEGER NOT NULL, catalog_version INTEGER NOT NULL,
                    category TEXT NOT NULL,model TEXT NOT NULL,query TEXT NOT NULL,
                    market_scope_id TEXT NOT NULL,scope TEXT NOT NULL,task_ids TEXT NOT NULL,
                    task_names TEXT NOT NULL,scope_verified INTEGER NOT NULL,status TEXT NOT NULL,
                    label TEXT NOT NULL,created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS mobile_capture_frames (
                    id INTEGER PRIMARY KEY,batch_id INTEGER NOT NULL REFERENCES mobile_capture_batches(id),
                    catalog_id INTEGER NOT NULL,catalog_version INTEGER NOT NULL,
                    category TEXT NOT NULL,model TEXT NOT NULL,query TEXT NOT NULL,
                    market_scope_id TEXT NOT NULL,scope TEXT NOT NULL,task_ids TEXT NOT NULL,
                    evidence_filename TEXT NOT NULL,evidence_hash TEXT NOT NULL,
                    captured_at TEXT NOT NULL,chart_x REAL,screen_date TEXT,
                    status TEXT NOT NULL,duplicate_hash_of INTEGER,created_at TEXT NOT NULL,
                    UNIQUE(batch_id,evidence_filename));
                CREATE INDEX IF NOT EXISTS mobile_capture_pending ON mobile_capture_frames(status,id);
                CREATE INDEX IF NOT EXISTS mobile_capture_hash ON mobile_capture_frames(batch_id,evidence_hash);
                CREATE TABLE IF NOT EXISTS mobile_capture_recognition_calls (
                    id INTEGER PRIMARY KEY,request_id TEXT NOT NULL UNIQUE,model TEXT NOT NULL,
                    adapter TEXT NOT NULL,input_hash TEXT NOT NULL,results_count INTEGER NOT NULL,
                    created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS mobile_capture_recognitions (
                    id INTEGER PRIMARY KEY,call_id INTEGER NOT NULL REFERENCES mobile_capture_recognition_calls(id),
                    request_id TEXT NOT NULL,frame_id INTEGER NOT NULL REFERENCES mobile_capture_frames(id),
                    evidence_hash TEXT NOT NULL,model TEXT NOT NULL,adapter TEXT NOT NULL,
                    observed_model TEXT NOT NULL,metric TEXT NOT NULL,displayed_mmdd TEXT,proposed_price REAL,
                    currency TEXT NOT NULL,readable INTEGER NOT NULL,scope_text TEXT NOT NULL,
                    raw_label TEXT NOT NULL,notes TEXT NOT NULL,status TEXT NOT NULL,
                    raw_result TEXT NOT NULL,created_at TEXT NOT NULL,UNIQUE(request_id,frame_id));
                CREATE INDEX IF NOT EXISTS mobile_capture_recognition_frame ON mobile_capture_recognitions(frame_id,id);
            """)

    @staticmethod
    def _positive(value, name):
        if type(value) is not int or not 0 < value <= 9223372036854775807:
            raise ValidationError(f"{name}须为正整数")
        return value

    def _writable(self):
        if self.paused():
            raise LeaseError("手机支线已人工暂停，不接受新的截图批次或截图登记")

    @staticmethod
    def _batch(row):
        if row is None:
            return None
        result = dict(row)
        for key in ("scope", "task_ids", "task_names"):
            result[key] = json.loads(result[key])
        result["scope_verified"] = bool(result["scope_verified"])
        result.update(price_points_created=0, verified_days_added=0, capture_complete=False,
                      scope_is_target_only=True)
        return result

    @staticmethod
    def _frame(row, recognition=None):
        result = dict(row)
        for key in ("scope", "task_ids"):
            result[key] = json.loads(result[key])
        result.update(evidence_url=f"/api/mobile/captures/{result['id']}/evidence",
                      chart_x_basis="image_width", price=None,
                      screen_date_verified=False, scope_is_target_only=True,recognition=recognition)
        return result

    @staticmethod
    def _recognition(row):
        result=dict(row)
        result["readable"]=bool(result["readable"])
        result["raw_result"]=json.loads(result["raw_result"])
        result.update(date_year=None,displayed_date=None,canonical_verified=False,proposal_only=True,
                      source="trusted_local_result_registration")
        return result

    def _frames(self,db,rows):
        if not rows:
            return []
        ids=[row["id"] for row in rows]
        marks=",".join("?" for _ in ids)
        latest={row["frame_id"]:self._recognition(row) for row in db.execute(
            "SELECT r.* FROM mobile_capture_recognitions r JOIN (SELECT frame_id,max(id) latest FROM mobile_capture_recognitions WHERE frame_id IN ("+marks+") GROUP BY frame_id) v ON r.id=v.latest",ids)}
        return [self._frame(row,latest.get(row["id"])) for row in rows]

    def _current(self, db, batch):
        self._writable()
        target = self.targets.require_catalog(db, batch["catalog_id"])
        if (target["catalog_version"] != batch["catalog_version"]
                or target["market_scope_id"] != batch["market_scope_id"]
                or target["task_ids"] != batch["task_ids"]
                or target["scope"] != batch["scope"]):
            raise LeaseError("截图批次的目录版本、经济范围或监控任务已变化；须建立新批次，不改写旧截图")
        return target

    def control(self,batch_id):
        """Cheap per-frame authorization; no progress scans, files or device IO."""
        self._positive(batch_id,"batch_id")
        with self.store.db() as db:
            batch=self._batch(db.execute("SELECT * FROM mobile_capture_batches WHERE id=?",(batch_id,)).fetchone())
            if batch is None:
                return None
            paused=self.paused()
            try:
                self._current(db,batch)
                allowed,reason=True,"当前批次仍获已启用真实监控任务授权；不代表设备已连接或价格已核验"
            except LeaseError as exc:
                allowed,reason=False,str(exc)
        return dict(allowed=allowed,operator_paused=paused,batch_id=batch_id,catalog_id=batch["catalog_id"],
                    catalog_version=batch["catalog_version"],market_scope_id=batch["market_scope_id"],
                    task_ids=batch["task_ids"],reason=reason,checked_at=iso(),device_connection_checked=False)

    def create_batch(self, payload):
        if not isinstance(payload, dict) or set(payload)-{"catalog_id", "label"}:
            raise ValidationError("截图批次只接受catalog_id和可选label，不接受价格、日期或外部路径")
        catalog_id = self._positive(payload.get("catalog_id"), "catalog_id")
        label = payload.get("label", "")
        if not isinstance(label, str) or len(label) > 160 or any(ord(c) < 32 for c in label):
            raise ValidationError("label须为最多160字普通文本")
        self._writable()
        self.targets.list()  # Only enabled live tasks may create a missing target.
        with self.store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            self._writable()
            target = self.targets.require_catalog(db, catalog_id)
            signature = hashlib.sha256(canonical([catalog_id,target["catalog_version"],target["market_scope_id"],target["task_ids"],label]).encode()).hexdigest()
            prior = db.execute("SELECT * FROM mobile_capture_batches WHERE signature=?", (signature,)).fetchone()
            if prior:
                return {**self._batch(prior), "created":False}
            reviewed = target["scope_verified"] is True and target["readable"] is True
            cur = db.execute("""INSERT INTO mobile_capture_batches(signature,catalog_id,catalog_version,category,model,query,
                market_scope_id,scope,task_ids,task_names,scope_verified,status,label,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (signature,catalog_id,target["catalog_version"],target["category"],target["model"],target["query"],
                 target["market_scope_id"],canonical(target["scope"]),canonical(target["task_ids"]),canonical(target["task_names"]),
                 reviewed,"queued" if reviewed else "needs_scope",label,iso()))
            return {**self._batch(db.execute("SELECT * FROM mobile_capture_batches WHERE id=?",(cur.lastrowid,)).fetchone()),"created":True}

    @staticmethod
    def _metadata(frame):
        if not isinstance(frame, dict) or set(frame)-{"evidence_filename","evidence_hash","captured_at","chart_x","screen_date"}:
            raise ValidationError("截图仅接受证据文件/哈希/拍摄时间/归一化横坐标/可选屏幕日期，不接受price或识别结果")
        digest = frame.get("evidence_hash")
        if not isinstance(digest,str) or not re.fullmatch(r"[a-f0-9]{64}",digest):
            raise ValidationError("evidence_hash须为实际图片SHA256")
        raw_time = frame.get("captured_at")
        try:
            stamp = datetime.fromisoformat(raw_time.replace("Z","+00:00")) if isinstance(raw_time,str) else None
            if stamp is None or stamp.tzinfo is None or stamp.timestamp() > utcnow().timestamp()+30:
                raise ValueError
        except (ValueError,TypeError,OverflowError):
            raise ValidationError("captured_at须为含时区的实际拍摄时间，不能是未来时间") from None
        x = frame.get("chart_x")
        if x is not None and (type(x) not in {int,float} or not 0 <= x <= 1 or not math.isfinite(x)):
            raise ValidationError("chart_x须为0至1有限值，按整张图片宽度归一，不代表日期")
        day = frame.get("screen_date")
        if day is not None:
            try:
                if not isinstance(day,str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}",day):
                    raise ValueError
                actual = datetime.strptime(day,"%Y-%m-%d").date()
                if actual > stamp.astimezone(SHANGHAI).date():
                    raise ValueError
            except ValueError:
                raise ValidationError("screen_date须为实际可见的完整日期或null，不可未来或从坐标推算") from None
        return dict(evidence_filename=frame.get("evidence_filename"),evidence_hash=digest,captured_at=iso(stamp),chart_x=x,screen_date=day)

    def _evidence(self, filename, digest):
        if isinstance(filename,str) and (self.official.evidence_dir / filename).is_symlink():
            raise ValidationError("截图证据不可使用符号链接")
        path = self.official.evidence_file(filename)
        if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise ValidationError("截图证据的实际SHA256与登记不符，未登记任何图片")
        return path

    def register(self, payload):
        if not isinstance(payload,dict) or set(payload)!={"batch_id","frames"}:
            raise ValidationError("截图登记只接受batch_id和frames")
        batch_id = self._positive(payload["batch_id"],"batch_id")
        frames = payload["frames"]
        if not isinstance(frames,list) or not 1 <= len(frames) <= 200:
            raise ValidationError("每次登记1至200张截图")
        prepared = [self._metadata(frame) for frame in frames]
        if any(not isinstance(frame["evidence_filename"],str) for frame in prepared) or len({frame["evidence_filename"] for frame in prepared}) != len(prepared):
            raise ValidationError("本次截图文件名须为互不重复的普通图片名")
        registered, existing, result = 0, 0, []
        with self.store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            batch = self._batch(db.execute("SELECT * FROM mobile_capture_batches WHERE id=?",(batch_id,)).fetchone())
            if batch is None:
                raise ValidationError("截图批次不存在")
            self._current(db,batch)
            status = "queued" if batch["scope_verified"] else "queued_scope"
            for frame in prepared:
                self._evidence(frame["evidence_filename"],frame["evidence_hash"])
                old = db.execute("SELECT * FROM mobile_capture_frames WHERE batch_id=? AND evidence_filename=?",(batch_id,frame["evidence_filename"])).fetchone()
                if old:
                    if any(old[key] != value for key,value in frame.items()):
                        raise ValidationError("相同批次截图已有不同元数据，保留原记录，不覆盖")
                    existing += 1
                    result.extend(self._frames(db,[old]))
                    continue
                duplicate = db.execute("SELECT id FROM mobile_capture_frames WHERE batch_id=? AND evidence_hash=? ORDER BY id LIMIT 1",(batch_id,frame["evidence_hash"])).fetchone()
                cur = db.execute("""INSERT INTO mobile_capture_frames(batch_id,catalog_id,catalog_version,category,model,query,
                    market_scope_id,scope,task_ids,evidence_filename,evidence_hash,captured_at,chart_x,screen_date,status,duplicate_hash_of,created_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (batch_id,batch["catalog_id"],batch["catalog_version"],batch["category"],batch["model"],batch["query"],
                     batch["market_scope_id"],canonical(batch["scope"]),canonical(batch["task_ids"]),frame["evidence_filename"],
                     frame["evidence_hash"],frame["captured_at"],frame["chart_x"],frame["screen_date"],status,duplicate["id"] if duplicate else None,iso()))
                result.append(self._frame(db.execute("SELECT * FROM mobile_capture_frames WHERE id=?",(cur.lastrowid,)).fetchone()))
                registered += 1
        return dict(ok=True,batch_id=batch_id,registered=registered,existing=existing,frames=result,
                    price_points_created=0,verified_days_added=0,message="实际截图已归档并排队待识别；没有登记价格或核验日期，不代表半年数据已齐")

    def pending(self,batch_id=None,limit=200,offset=0):
        sql,args = " WHERE status IN ('queued','queued_scope')",[]
        if batch_id is not None:
            sql += " AND batch_id=?";args.append(self._positive(batch_id,"batch_id"))
        if type(limit) is not int or not 1 <= limit <= 200 or type(offset) is not int or offset < 0:
            raise ValidationError("limit须为1至200，offset须为非负整数")
        with self.store.db() as db:
            total = db.execute("SELECT count(*) FROM mobile_capture_frames"+sql,args).fetchone()[0]
            rows = db.execute("SELECT * FROM mobile_capture_frames"+sql+" ORDER BY id LIMIT ? OFFSET ?",args+[limit,offset]).fetchall()
            frames=self._frames(db,rows)
        return dict(frames=frames,total=total,limit=limit,offset=offset,
                    price_points_created=0,verified_days_added=0,source="actual_screenshots_pending_recognition")

    def frames(self,batch_id=None,status=None,limit=200,offset=0):
        if type(limit) is not int or not 1 <= limit <= 200 or type(offset) is not int or offset < 0:
            raise ValidationError("limit须为1至200，offset须为非负整数")
        where,args=[],[]
        if batch_id is not None:
            where.append("batch_id=?");args.append(self._positive(batch_id,"batch_id"))
        if status is not None:
            if status not in {"queued","queued_scope","parsed","needs_review"}:
                raise ValidationError("截图状态无效")
            where.append("status=?");args.append(status)
        sql=" WHERE "+" AND ".join(where) if where else ""
        with self.store.db() as db:
            total=db.execute("SELECT count(*) FROM mobile_capture_frames"+sql,args).fetchone()[0]
            rows=db.execute("SELECT * FROM mobile_capture_frames"+sql+" ORDER BY id LIMIT ? OFFSET ?",args+[limit,offset]).fetchall()
            frames=self._frames(db,rows)
        return dict(frames=frames,total=total,limit=limit,offset=offset,price_points_created=0,verified_days_added=0)

    @staticmethod
    def _recognition_result(result):
        fields={"frame_id","evidence_hash","observed_model","metric","displayed_mmdd","price","currency","readable","scope_text","raw_label","notes"}
        if not isinstance(result,dict) or set(result)!=fields:
            raise ValidationError("识别结果须使用固定字段；不接受年份、完整日期、scope核验、路径或命令")
        MobileCaptures._positive(result["frame_id"],"frame_id")
        if not isinstance(result["evidence_hash"],str) or not re.fullmatch(r"[a-f0-9]{64}",result["evidence_hash"]):
            raise ValidationError("识别结果须绑定实际截图SHA256")
        if result["metric"]!="rolling_7d_sold_mean" or result["currency"]!="CNY" or type(result["readable"]) is not bool:
            raise ValidationError("只接受近7日成交均价/CNY与明确readable布尔值")
        for name,maximum in (("observed_model",200),("scope_text",4000),("raw_label",4000),("notes",4000)):
            if not isinstance(result[name],str) or len(result[name])>maximum:
                raise ValidationError(f"{name}须为最多{maximum}字原文或说明")
        price=result["price"]
        if price is not None and (type(price) not in {int,float} or not 0 <= price <= 100_000_000 or not math.isfinite(price)):
            raise ValidationError("识别价格只能为有限非负数值或null；它仍仅是价格建议")
        day=result["displayed_mmdd"]
        if day is not None:
            if not isinstance(day,str) or not re.fullmatch(r"[0-9]{2}[./-][0-9]{2}",day):
                raise ValidationError("displayed_mmdd只接受精确MM.DD/MM-DD/MM/DD或null，不推断年份")
            month,day_number=int(day[:2]),int(day[3:])
            try:
                date(2000,month,day_number)  # Calendar validation only; no year is stored/inferred.
            except ValueError:
                raise ValidationError("屏幕月日不是真实日历月日") from None
            day=f"{month:02d}.{day_number:02d}"
        status="parsed" if result["readable"] and day is not None and price is not None and price>0 else "needs_review"
        return {**result,"displayed_mmdd":day,"status":status}

    def register_recognitions(self,payload):
        # This stage deliberately works after the phone/monitor is paused.
        # It never promotes a proposal to mobile_points or changes scope proof.
        if not isinstance(payload,dict) or set(payload)!={"model","adapter","request_id","results"}:
            raise ValidationError("识别登记只接受model/adapter/request_id/results")
        if payload["model"]!="gpt-6.1-sol" or not isinstance(payload["adapter"],str) or payload["adapter"] not in {"codex_session","codex_cli_existing_connection"}:
            raise ValidationError("只登记gpt-6.1-sol现有连接的明确adapter结果，不接受其他调用命令或配置")
        request_id=payload["request_id"]
        if not isinstance(request_id,str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,159}",request_id):
            raise ValidationError("request_id须为明确请求标识，不接受路径或命令")
        results=payload["results"]
        if not isinstance(results,list) or not 1 <= len(results) <= 100:
            raise ValidationError("每次登记1至100条AI识别结果")
        prepared=[self._recognition_result(result) for result in results]
        if len({row["frame_id"] for row in prepared})!=len(prepared):
            raise ValidationError("同一请求的frame_id不能重复")
        digest=hashlib.sha256(canonical(payload).encode()).hexdigest()
        with self.store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            for row in prepared:
                frame=db.execute("SELECT * FROM mobile_capture_frames WHERE id=?",(row["frame_id"],)).fetchone()
                if frame is None or frame["evidence_hash"]!=row["evidence_hash"]:
                    raise ValidationError("识别结果的frame_id或证据哈希不匹配")
                self._evidence(frame["evidence_filename"],row["evidence_hash"])
            prior=db.execute("SELECT * FROM mobile_capture_recognition_calls WHERE request_id=?",(request_id,)).fetchone()
            if prior:
                if prior["input_hash"]!=digest:
                    raise ValidationError("此request_id已有不同结果，保留原建议不覆盖；新识别须用新请求ID")
                saved=[self._recognition(row) for row in db.execute("SELECT * FROM mobile_capture_recognitions WHERE call_id=? ORDER BY id",(prior["id"],))]
                return dict(ok=True,request_id=request_id,registered=0,existing=len(saved),results=saved,proposal_only=True,
                            price_points_created=0,verified_days_added=0)
            stamp=iso()
            call_id=db.execute("INSERT INTO mobile_capture_recognition_calls(request_id,model,adapter,input_hash,results_count,created_at) VALUES (?,?,?,?,?,?)",
                (request_id,payload["model"],payload["adapter"],digest,len(prepared),stamp)).lastrowid
            saved=[]
            for raw,row in zip(results,prepared):
                cur=db.execute("""INSERT INTO mobile_capture_recognitions(call_id,request_id,frame_id,evidence_hash,model,adapter,
                    observed_model,metric,displayed_mmdd,proposed_price,currency,readable,scope_text,raw_label,notes,status,raw_result,created_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (call_id,request_id,row["frame_id"],row["evidence_hash"],payload["model"],payload["adapter"],row["observed_model"],row["metric"],
                     row["displayed_mmdd"],row["price"],row["currency"],row["readable"],row["scope_text"],row["raw_label"],row["notes"],row["status"],canonical(raw),stamp))
                db.execute("UPDATE mobile_capture_frames SET status=? WHERE id=?",(row["status"],row["frame_id"]))
                saved.append(self._recognition(db.execute("SELECT * FROM mobile_capture_recognitions WHERE id=?",(cur.lastrowid,)).fetchone()))
        return dict(ok=True,request_id=request_id,registered=len(saved),existing=0,results=saved,proposal_only=True,
                    price_points_created=0,verified_days_added=0,
                    message="实际模型结果已登记为建议；未确定年份、核实范围或写入正式价格点")

    def recognitions(self,frame_id=None,limit=100,offset=0):
        if type(limit) is not int or not 1 <= limit <= 100 or type(offset) is not int or offset<0:
            raise ValidationError("limit须为1至100，offset须为非负整数")
        sql,args="",[]
        if frame_id is not None:
            sql=" WHERE frame_id=?";args.append(self._positive(frame_id,"frame_id"))
        with self.store.db() as db:
            total=db.execute("SELECT count(*) FROM mobile_capture_recognitions"+sql,args).fetchone()[0]
            rows=db.execute("SELECT * FROM mobile_capture_recognitions"+sql+" ORDER BY id DESC LIMIT ? OFFSET ?",args+[limit,offset]).fetchall()
        return dict(results=[self._recognition(row) for row in rows],total=total,limit=limit,offset=offset,
                    proposal_only=True,price_points_created=0,verified_days_added=0)

    def status(self):
        with self.store.db() as db:
            counts = {row["status"]:row["n"] for row in db.execute("SELECT status,count(*) n FROM mobile_capture_frames GROUP BY status")}
            batch_count = db.execute("SELECT count(*) FROM mobile_capture_batches").fetchone()[0]
            batches = []
            for row in db.execute("SELECT b.*,count(f.id) frame_count FROM mobile_capture_batches b LEFT JOIN mobile_capture_frames f ON f.batch_id=b.id GROUP BY b.id ORDER BY b.id DESC LIMIT 50"):
                batches.append(self._batch(row))
            calls=db.execute("SELECT count(*) FROM mobile_capture_recognition_calls").fetchone()[0]
            result_count=db.execute("SELECT count(*) FROM mobile_capture_recognitions").fetchone()[0]
            last=db.execute("SELECT * FROM mobile_capture_recognition_calls ORDER BY id DESC LIMIT 1").fetchone()
            recognition=dict(model=last["model"] if last else None,adapter=last["adapter"] if last else None,
                request_id=last["request_id"] if last else None,last_registered_at=last["created_at"] if last else None,
                last_request_succeeded=bool(last),results_count=last["results_count"] if last else 0,
                registered_call_count=calls,result_count=result_count,state_basis="accepted_local_result_registration",
                model_calls_executed_by_this_service=0,persistent_connection_verified=False)
        return dict(batches=batches,batch_count=batch_count,frame_count=sum(counts.values()),pending_frames=sum(counts.get(key,0) for key in self.PENDING),
                    frame_status_counts=counts,price_points_created=0,verified_days_added=0,recognition=recognition,
                    message="截图数量不等于唯一日期覆盖或有效价格点；待识别及待范围核对均不计成交行情")

    def evidence(self,frame_id):
        self._positive(frame_id,"frame_id")
        with self.store.db() as db:
            row = db.execute("SELECT * FROM mobile_capture_frames WHERE id=?",(frame_id,)).fetchone()
        if row is None:
            return None
        return self._evidence(row["evidence_filename"],row["evidence_hash"])
