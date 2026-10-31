"""Single local project entry point. No UI, phone driver, or model API calls."""
from __future__ import annotations

import argparse
import ipaddress
import json
import math
import os
import re
import sqlite3
import sys
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_BASE = "http://127.0.0.1:8765"
MAX_FILE_BYTES = 5*1024*1024
TERMINAL = {"success", "failed", "cancelled", "interrupted"}


class CLIError(ValueError):
    def __init__(self, code, message, **details):
        super().__init__(message)
        self.code, self.message, self.details = code, message, details


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise CLIError("invalid_arguments", message)


def positive_int(value):
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError("须为正整数") from exc
    if number <= 0:
        raise argparse.ArgumentTypeError("须为正整数")
    return number


def finite_json(text):
    def reject(value):
        raise CLIError("invalid_json", "JSON不接受NaN/Infinity等非有限值")
    try:
        return json.loads(text, parse_constant=reject)
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise CLIError("invalid_json", "内容不是有效UTF-8 JSON") from exc


def validate_base_url(value):
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        parsed.port  # Validate malformed ports without opening a connection.
        loopback = host == "localhost" or bool(host and ipaddress.ip_address(host).is_loopback)
    except (ValueError, TypeError):
        raise CLIError("local_http_only", "base-url只允许无凭据、无路径的本机回环HTTP地址") from None
    if (parsed.scheme not in {"http", "https"} or not loopback or parsed.username or parsed.password or
            parsed.path not in {"", "/"} or parsed.query or parsed.fragment or not host):
        raise CLIError("local_http_only", "base-url只允许无凭据、无路径的本机回环HTTP地址")
    return value.rstrip("/")


class LocalHTTP:
    def __init__(self, base_url=DEFAULT_BASE, timeout=15):
        self.base = validate_base_url(base_url)
        self.timeout = timeout
        origin = urlsplit(self.base)

        class LocalRedirect(HTTPRedirectHandler):
            def redirect_request(self, request, fp, code, msg, headers, newurl):
                target = urlsplit(newurl)
                if (target.scheme, target.netloc) != (origin.scheme, origin.netloc):
                    raise CLIError("redirect_refused", "拒绝将本机控制请求重定向到其他地址")
                return super().redirect_request(request, fp, code, msg, headers, newurl)

        # Local control must stay local even when a shell has proxy variables.
        self.opener = build_opener(ProxyHandler({}), LocalRedirect())

    def request(self, method, path, body=None):
        if not path.startswith("/api/") or "\\" in path:
            raise CLIError("invalid_route", "仅允许已定义的本机API路径")
        data = None if body is None else json.dumps(body, ensure_ascii=False, allow_nan=False).encode()
        request = Request(self.base+path, data=data, method=method,
                          headers={"Accept":"application/json", "Content-Type":"application/json"})
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                payload = response.read(10*1024*1024+1)
        except HTTPError as exc:
            raw = exc.read(MAX_FILE_BYTES)
            try:
                detail = finite_json(raw.decode("utf-8")).get("detail", "本机API拒绝请求")
            except (CLIError, AttributeError):
                detail = "本机API拒绝请求"
            raise CLIError("http_error", str(detail), http_status=exc.code, route=path) from None
        except (URLError, TimeoutError, OSError):
            raise CLIError("service_unavailable", "本机服务不可用或请求超时，请先启动项目服务", route=path) from None
        if len(payload) > 10*1024*1024:
            raise CLIError("response_too_large", "本机API响应超过大小限制")
        result = finite_json(payload.decode("utf-8"))
        if not isinstance(result, dict):
            raise CLIError("invalid_response", "本机API须返回JSON对象")
        return result


def save_json(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix+".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False)+"\n")
    os.chmod(temporary, 0o600)
    temporary.replace(path)
    return str(path.resolve())


def safe_file(path, *, json_only=False, maximum=MAX_FILE_BYTES):
    path = Path(path).expanduser()
    if path.is_symlink():
        raise CLIError("invalid_path", "输入必须是明确的本地普通文件，不接受符号链接")
    resolved = path.resolve()
    forbidden = {".ssh", ".aws", ".azure", ".kube", "browser_profile"}
    if forbidden.intersection(resolved.parts) or resolved.name.lower() in {
        "session_meta.json", "cookies.json", "storage_state.json", "credentials.json", "token.json", "secrets.json", ".env"
    }:
        raise CLIError("credential_path_refused", "此入口不读取凭据或登录态文件")
    if json_only and resolved.suffix.lower() != ".json":
        raise CLIError("invalid_path", "复核导入只接受.json普通文件")
    if not resolved.is_file():
        raise CLIError("invalid_path", "指定文件不存在或不是普通文件")
    if maximum is not None and resolved.stat().st_size > maximum:
        raise CLIError("file_too_large", "输入文件超过5MB")
    return resolved


def status(http):
    health = http.request("GET", "/api/health")
    workflow = http.request("GET", "/api/workflow/status")
    queue = http.request("GET", "/api/workflow/listing/queue?limit=100")
    return dict(ok=True, command="status", health=health, workflow=workflow,
                listing_queue=queue, external_ai_called=False, mobile_actions=False)


def collect(http, args):
    task_ids = sorted(set(args.task_id or []))
    if len(task_ids) > 100:
        raise CLIError("invalid_arguments", "单次最多排队100个已启用任务")
    body = {"task_ids": task_ids} if task_ids else {}
    queued = http.request("POST", "/api/workflow/listing/queue", body)
    targets = queued.get("queued")
    if not isinstance(targets, list) or any(not isinstance(row, dict) or isinstance(row.get("listing_job_id"), bool) or not isinstance(row.get("listing_job_id"), int) or row["listing_job_id"] <= 0 for row in targets):
        raise CLIError("invalid_response", "排队接口缺少持久listing_job_id")
    result = dict(ok=True, command="collect", state="queued" if targets else "idle", collection_complete=False,
                  queue=queued, waited=False, external_ai_called=False, mobile_actions=False)
    if not args.wait or not targets:
        result["message"] = "持久任务已提交或复用；尚未表示采集完成" if targets else "没有可排队的已启用任务"
        return result
    result["waited"] = True
    wanted = {row["listing_job_id"] for row in targets}
    queue_path = "/api/workflow/listing/queue?job_ids="+",".join(str(key) for key in sorted(wanted))+"&limit=100"
    terminal_seen = {}
    deadline = time.monotonic()+args.wait_seconds
    while True:
        rows = http.request("GET", queue_path).get("jobs", [])
        if not isinstance(rows, list):
            raise CLIError("invalid_response", "持久队列jobs须为数组")
        if any(not isinstance(row,dict) or isinstance(row.get("id"),bool) or not isinstance(row.get("id"),int) or not isinstance(row.get("status"),str) for row in rows):
            raise CLIError("invalid_response", "持久队列须包含整数id及明确status")
        matching = {row["id"]:row for row in rows if isinstance(row,dict) and row.get("id") in wanted}
        terminal_seen.update({key:row for key,row in matching.items() if row["status"] in TERMINAL})
        matching = {**matching, **terminal_seen}
        result["jobs"] = [matching[key] for key in sorted(matching)]
        result["missing_job_ids"] = sorted(wanted-matching.keys())
        if len(matching) == len(wanted) and all(row.get("status") in TERMINAL for row in matching.values()):
            successful = all(row["status"] == "success" for row in matching.values())
            result.update(ok=successful, state="complete" if successful else "failed", collection_complete=successful,
                          message="选定网页采集任务均已成功结束" if successful else "存在失败或取消的采集任务；未声明采集完整")
            return result
        if time.monotonic() >= deadline:
            result.update(ok=False, state="pending", wait_timed_out=True, message="等待到时；持久队列仍保留，未完成任务不计为成功")
            return result
        time.sleep(args.poll_seconds)


def review_export(http, args, root):
    batch = http.request("POST", "/api/workflow/ai-review-batches", {"limit":args.limit})
    if batch.get("status") == "empty" and batch.get("id") is None:
        return dict(ok=True, command="review-export", state="empty", item_count=0, path=None,
                    external_ai_called=False, automatic_apply=False)
    raw_id = batch.get("id")
    if isinstance(raw_id, bool) or not isinstance(raw_id, int) or raw_id <= 0:
        raise CLIError("invalid_batch_id", "服务器返回的固定批次ID不是安全文件名")
    batch_id = str(raw_id)
    if not isinstance(batch.get("content_hash"), str) or not re.fullmatch(r"[a-f0-9]{64}", batch["content_hash"]):
        raise CLIError("invalid_response", "批次缺少可校验的内容哈希")
    destination = root/"output"/"review_batches"/(batch_id+".json")
    existing = False
    if destination.exists():
        old = finite_json(safe_file(destination,json_only=True).read_text(encoding="utf-8"))
        if not isinstance(old,dict) or old.get("content_hash") != batch.get("content_hash"):
            raise CLIError("batch_file_conflict", "固定批次文件已存在但内容哈希不同；为保护复核结果未覆盖")
        path, existing = str(destination.resolve()), True
    else:
        path = save_json(destination, batch)
    return dict(ok=True, command="review-export", state="exported", id=batch.get("id"), item_count=batch.get("item_count"),
                content_hash=batch.get("content_hash"), path=path, existing_file_preserved=existing,
                external_ai_called=False, automatic_apply=False,
                message="已保存真实原文、decision_template及schema，供人工/Codex/本机模型离线提出建议；未调用AI")


def review_import(http, args):
    path = safe_file(args.file, json_only=True)
    try:
        payload = finite_json(path.read_text(encoding="utf-8"))
    except UnicodeError:
        raise CLIError("invalid_json", "复核文件须为UTF-8 JSON") from None
    if not isinstance(payload,dict) or set(payload) != {"batch_id", "content_hash", "reviewer", "decisions"}:
        raise CLIError("invalid_review_payload", "请将填完整的decision_template另存为JSON，仅保留batch_id/content_hash/reviewer/decisions")
    if isinstance(payload["batch_id"],bool) or not isinstance(payload["batch_id"],int) or payload["batch_id"] <= 0:
        raise CLIError("invalid_review_payload", "batch_id须为真实正整数")
    if not isinstance(payload["content_hash"],str) or not re.fullmatch(r"[a-f0-9]{64}",payload["content_hash"]):
        raise CLIError("invalid_review_payload", "须保留导出批次的content_hash")
    reviewer, decisions = payload["reviewer"], payload["decisions"]
    if not isinstance(reviewer,dict) or set(reviewer) != {"kind","name"} or not isinstance(reviewer["kind"],str) or reviewer["kind"] not in {"human","codex","local_ai"} or not isinstance(reviewer["name"],str) or not 1 <= len(reviewer["name"].strip()) <= 120 or reviewer["name"] == "填写实际复核者":
        raise CLIError("invalid_review_payload", "须填写实际复核者kind与name")
    if not isinstance(decisions,list) or not 1 <= len(decisions) <= 40:
        raise CLIError("invalid_review_payload", "每次仅导入1至40条建议")
    required={"observation_id","source_hash","decision","quote","confidence","reason_codes"}
    allowed=required|{"proposed_category","proposed_model","explanation"}
    for item in decisions:
        if not isinstance(item,dict) or not required.issubset(item) or set(item)-allowed:
            raise CLIError("invalid_review_payload", "建议字段无效，不接受价格、日期或原始记录修改")
        confidence=item["confidence"]
        if not isinstance(item["decision"],str) or item["decision"] not in {"keep","exclude","needs_review"} or not isinstance(item["quote"],str) or len(item["quote"]) < 2 or isinstance(confidence,bool) or not isinstance(confidence,(int,float)) or not math.isfinite(confidence) or not 0 <= confidence <= 1:
            raise CLIError("invalid_review_payload", "建议须含明确decision、逐字quote及0至1有限confidence")
        if isinstance(item["observation_id"],bool) or not isinstance(item["observation_id"],int) or item["observation_id"] <= 0 or not isinstance(item["source_hash"],str) or not re.fullmatch(r"[a-f0-9]{64}",item["source_hash"]) or not isinstance(item["reason_codes"],list) or not all(isinstance(code,str) and code for code in item["reason_codes"]):
            raise CLIError("invalid_review_payload", "须保留真实观测ID、source_hash及字符串reason_codes数组")
    result = http.request("POST", "/api/workflow/review-decisions", payload)
    if result.get("applied") != 0 or result.get("status") != "suggestions_recorded":
        raise CLIError("unexpected_apply", "建议接口意外返回已应用状态，须人工检查；CLI没有提交任何应用请求")
    return dict(ok=True,command="review-import",result=result,recommendations_only=True,
                external_ai_called=False,automatic_apply=False)


def local_audit(command, args, root):
    from .service import Store
    db_path = safe_file(args.db or root/"data"/"market.sqlite3", maximum=None)
    if db_path.suffix.lower() not in {".sqlite3",".sqlite",".db"}:
        raise CLIError("invalid_path", "审计入口只接受项目SQLite数据库")
    with db_path.open("rb") as file:
        if file.read(16) != b"SQLite format 3\x00":
            raise CLIError("invalid_path", "指定文件不是SQLite数据库；未初始化或修改文件")
    store = Store(db_path,production=True,initialize=False)
    audit = store.price_audit
    if command == "audit-preview":
        report = audit.preview()
        path = save_json(root/"output"/"price_audit_preview.json", report)
        return dict(ok=True,command=command,path=path,summary={key:value for key,value in report.items() if key!="changes"})
    if command in {"audit-apply","audit-state","audit-rollback"}:
        method={"audit-apply":"apply","audit-state":"state","audit-rollback":"rollback"}[command]
        report=getattr(audit,method)()
        if command!="audit-state":
            save_json(root/"output"/("price_audit_applied.json" if command=="audit-apply" else "price_audit_rollback.json"),report)
        return dict(ok=True,command=command,audit=report)
    from .indices import IndexService
    result=IndexService(store).rebuild(days=args.days,category=args.category)
    return saved_index(result,root)


def saved_index(result,root):
    if isinstance(result.get("build_id"),bool) or not isinstance(result.get("build_id"),int) or result["build_id"] <= 0:
        raise CLIError("invalid_response", "重算未返回真实持久build_id")
    path=save_json(root/"output"/"index_builds"/(str(result["build_id"])+".json"),result)
    return dict(ok=True,command="rebuild-index",path=path,build_id=result["build_id"],version=result["version"],
                input_hash=result["input_hash"],status="built",coverage=result.get("coverage"),
                composite_score=result.get("composite_score"),composite_status="ready" if result.get("composite_score") is not None else "insufficient_data",
                message="仅新建真实输入的版本化计算；insufficient_data不表示可发布指数或完整综合分")


def daily(http,args,root):
    collection=collect(http,args)
    steps={"collection":collection}
    if args.wait and not collection["collection_complete"]:
        return dict(ok=False,command="daily",state="collection_incomplete",steps=steps,
                    complete=False,external_ai_called=False,mobile_actions=False)
    operation="apply" if args.wait else "preview"
    # Every data-changing stage uses the same service/database as collection.
    # A custom local --db remains available only on explicit audit-* commands.
    audit=http.request("POST","/api/workflow/price-audit/"+operation,{})
    audit_path=save_json(root/"output"/("price_audit_applied.json" if args.wait else "price_audit_preview.json"),audit)
    steps["audit"]={"ok":True,"operation":operation,"path":audit_path,"version":audit.get("version")}
    steps["review_export"]=review_export(http,args,root)
    steps["quality_status"]=http.request("GET","/api/workflow/status")
    gate=steps["quality_status"].get("quality_gate",{})
    if args.wait and collection["collection_complete"] and gate.get("can_rebuild") is True:
        steps["index"]=saved_index(http.request("POST","/api/indices/rebuild",{"days":args.days,"category":args.category}),root)
    else:
        steps["index"]={"status":"deferred","reason":"等待采集真实完成及明确can_rebuild质量门；没有自动应用AI建议"}
    rebuilt = steps["index"].get("ok") is True
    return dict(ok=True,command="daily",state=("processed" if rebuilt else "quality_deferred") if args.wait else "queued_and_prepared",steps=steps,
                complete=bool(args.wait and collection["collection_complete"] and rebuilt),
                collection_complete=collection["collection_complete"],
                can_publish_composite=gate.get("can_publish_composite") is True,
                external_ai_called=False,automatic_apply_ai=False,mobile_actions=False)


def parser():
    root=Parser(add_help=False)
    root.add_argument("--base-url",default=DEFAULT_BASE)
    root.add_argument("--http-timeout",type=float,default=15)
    commands=root.add_subparsers(dest="command",required=True,parser_class=Parser)
    commands.add_parser("status")
    def collection_arguments(command):
        command.add_argument("--task-id",type=positive_int,action="append")
        command.add_argument("--wait",action="store_true")
        command.add_argument("--wait-seconds",type=float,default=300)
        command.add_argument("--poll-seconds",type=float,default=2)
    collection_arguments(commands.add_parser("collect"))
    export=commands.add_parser("review-export");export.add_argument("--limit",type=positive_int,default=40)
    imp=commands.add_parser("review-import");imp.add_argument("file")
    for name in ["audit-preview","audit-apply","audit-state","audit-rollback","rebuild-index"]:
        command=commands.add_parser(name);command.add_argument("--db",type=Path)
        if name=="rebuild-index":
            command.add_argument("--days",type=positive_int,default=180);command.add_argument("--category",choices=["all","gpu","ram","cpu","motherboard","storage","computer","component","phone"],default="all")
    day=commands.add_parser("daily");collection_arguments(day)
    day.add_argument("--limit",type=positive_int,default=40)
    day.add_argument("--days",type=positive_int,default=180);day.add_argument("--category",choices=["all","gpu","ram","cpu","motherboard","storage","computer","component","phone"],default="all")
    return root


def main(argv=None, *, http=None, project_root=None, stdout=None):
    argv=list(sys.argv[1:] if argv is None else argv)
    stream=stdout or sys.stdout
    try:
        if not argv:
            argv=["status"]
        if any(value in {"--help","-h"} for value in argv):
            result=dict(ok=True,command="help",commands=["status","collect [--task-id N] [--wait]","review-export [--limit 40]","review-import FILE.json","audit-preview","audit-apply","audit-state","audit-rollback","rebuild-index","daily [--wait]"],mobile_actions=False,external_ai_called=False)
        else:
            args=parser().parse_args(argv)
            validate_base_url(args.base_url)
            if not math.isfinite(args.http_timeout) or not 0 < args.http_timeout <= 60:
                raise CLIError("invalid_arguments","http-timeout须为0至60秒有限值")
            if hasattr(args,"limit") and args.limit>40:
                raise CLIError("invalid_arguments","离线复核每批limit不能超过40")
            if hasattr(args,"days") and args.days>365:
                raise CLIError("invalid_arguments","days不能超过365")
            if hasattr(args,"wait_seconds") and (not math.isfinite(args.wait_seconds) or not 0 < args.wait_seconds <= 86400 or not math.isfinite(args.poll_seconds) or not .05 <= args.poll_seconds <= 30):
                raise CLIError("invalid_arguments","wait-seconds须为正且最多86400；poll-seconds须为0.05至30")
            root=Path(project_root or ROOT).resolve()
            transport=http or LocalHTTP(args.base_url,args.http_timeout)
            if args.command=="status":result=status(transport)
            elif args.command=="collect":result=collect(transport,args)
            elif args.command=="review-export":result=review_export(transport,args,root)
            elif args.command=="review-import":result=review_import(transport,args)
            elif args.command=="daily":result=daily(transport,args,root)
            else:result=local_audit(args.command,args,root)
        print(json.dumps(result,ensure_ascii=False,allow_nan=False),file=stream)
        return 0 if result.get("ok") else 3 if result.get("state")=="pending" else 2
    except CLIError as exc:
        result=dict(ok=False,error=exc.code,message=exc.message,**exc.details)
    except (OSError,ValueError,sqlite3.Error) as exc:
        result=dict(ok=False,error="local_operation_failed",message=str(exc))
    print(json.dumps(result,ensure_ascii=False,allow_nan=False),file=stream)
    return 2


if __name__=="__main__":
    raise SystemExit(main())
