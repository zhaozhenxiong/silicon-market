"""The project entry point only calls mocked local HTTP or temporary SQLite."""
import copy
import io
import json
import os
import subprocess
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request

import pytest

from app import project_cli as cli


class HTTP:
    def __init__(self, responses=()):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, path, body=None):
        self.calls.append((method, path, copy.deepcopy(body)))
        assert self.responses, f"Unexpected local request: {method} {path}"
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return copy.deepcopy(response)


def run(arguments, tmp_path, responses=(), http=None):
    http = http or HTTP(responses)
    output = io.StringIO()
    code = cli.main(arguments, http=http, project_root=tmp_path, stdout=output)
    result = json.loads(output.getvalue())
    return code, result, http


def batch(**changes):
    return dict(id=7, status="exported", item_count=1, content_hash="a"*64,
                items=[dict(title="i9-14900KF 故障维修件", observation_id=5)],
                decision_template={}, json_schema={}, external_ai_called=False, **changes)


def review():
    return dict(batch_id=7, content_hash="a"*64,
                reviewer=dict(kind="codex", name="本机复核"), decisions=[
                    dict(observation_id=5, source_hash="b"*64, decision="exclude",
                         quote="故障维修件", confidence=.92, reason_codes=["fault_or_accessory"])])


def review_file(tmp_path, payload=None, name="decisions.json"):
    path = tmp_path/name
    path.write_text(json.dumps(review() if payload is None else payload, ensure_ascii=False))
    return path


@pytest.mark.parametrize("url", ["https://example.org", "http://127.0.0.1.evil.com", "file:///tmp/x", "http://user:secret@localhost:8765", "http://localhost:8765/api", "http://localhost:8765?key=x", "http://[broken", "http://localhost:nope", "http://0.0.0.0:8765"])
def test_refuses_nonlocal_urls_without_http(url, tmp_path):
    code, result, http = run(["--base-url", url, "status"], tmp_path)
    assert code == 2 and result["error"] == "local_http_only"
    assert http.calls == []


@pytest.mark.parametrize("url", ["http://127.0.0.1:8765", "http://localhost:8765/", "http://[::1]:8765"])
def test_valid_loopback_addresses(url):
    assert cli.validate_base_url(url) == url.rstrip("/")


def test_status_only_reads_local_source_state_and_does_not_activate_phone(tmp_path):
    code, result, http = run([], tmp_path, [dict(ok=True), dict(mobile={"operator_paused":True}), dict(jobs=[])])
    assert code == 0 and result["workflow"]["mobile"]["operator_paused"]
    assert http.calls == [("GET", "/api/health", None), ("GET", "/api/workflow/status", None),
                          ("GET", "/api/workflow/listing/queue?limit=100", None)]
    assert result["external_ai_called"] is False and result["mobile_actions"] is False


def test_service_failure_is_json_not_traceback(tmp_path):
    code, result, _ = run(["status"], tmp_path, [cli.CLIError("service_unavailable", "本机服务不可用")])
    assert code == 2 and result == dict(ok=False, error="service_unavailable", message="本机服务不可用")


@pytest.mark.parametrize("arguments", [["review-export", "--limit", "41"], ["review-export", "--limit", "0"],
    ["collect", "--task-id", "-2"], ["collect", "--wait-seconds", "nan"], ["collect", "--poll-seconds", "0"],
    ["--http-timeout", "inf", "status"], ["rebuild-index", "--days", "366"],
    ["daily", "--category", "crypto"], ["unknown"]])
def test_argument_errors_are_json_without_side_effects(arguments, tmp_path):
    code, result, http = run(arguments, tmp_path)
    assert code == 2 and result["error"] == "invalid_arguments"
    assert http.calls == []


def test_collect_default_is_one_durable_queue_submission_without_false_completion(tmp_path):
    response = dict(queued=[dict(task_id=3, listing_job_id=81, status="running")], count=1)
    code, result, http = run(["collect"], tmp_path, [response])
    assert code == 0 and result["collection_complete"] is False and result["state"] == "queued"
    assert http.calls == [("POST", "/api/workflow/listing/queue", {})]


def test_collect_explicit_ids_are_deduplicated_without_enabling_tasks(tmp_path):
    code, result, http = run(["collect", "--task-id", "3", "--task-id", "1", "--task-id", "3"], tmp_path,
                             [dict(queued=[], count=0)])
    assert code == 0 and result["state"] == "idle"
    assert http.calls == [("POST", "/api/workflow/listing/queue", {"task_ids":[1,3]})]


def test_collect_wait_polls_exact_durable_jobs_once_and_requires_all_success(tmp_path, monkeypatch):
    monkeypatch.setattr(cli.time, "sleep", lambda _: None)
    responses = [dict(queued=[dict(task_id=3, listing_job_id=81), dict(task_id=2, listing_job_id=80)]),
                 dict(jobs=[dict(id=999,status="success"), dict(id=81,status="running"), dict(id=80,status="success")]),
                 dict(jobs=[dict(id=81,status="success",run_id=12), dict(id=80,status="success",run_id=11)])]
    code, result, http = run(["collect", "--wait"], tmp_path, responses)
    assert code == 0 and result["collection_complete"] is True and result["waited"]
    assert [row["id"] for row in result["jobs"]] == [80,81]
    assert sum(method=="POST" for method,_,_ in http.calls) == 1
    assert all(path=="/api/workflow/listing/queue?job_ids=80,81&limit=100" for method,path,_ in http.calls if method=="GET")


def test_wait_preserves_seen_success_when_another_snapshot_omits_it(tmp_path,monkeypatch):
    monkeypatch.setattr(cli.time,"sleep",lambda _:None)
    code,result,http=run(["collect","--wait"],tmp_path,[
        dict(queued=[dict(listing_job_id=80),dict(listing_job_id=81)]),
        dict(jobs=[dict(id=80,status="success"),dict(id=81,status="running")]),
        dict(jobs=[dict(id=81,status="success")])])
    assert code==0 and result["collection_complete"] and result["missing_job_ids"]==[]


def test_exact_job_filter_covers_target_older_than_recent_hundred_jobs(tmp_path):
    class OldJob:
        def request(self,method,path,body=None):
            if method=="POST":return dict(queued=[dict(listing_job_id=4)])
            assert path=="/api/workflow/listing/queue?job_ids=4&limit=100"
            return dict(jobs=[dict(id=4,status="success")])
    code,result,_=run(["collect","--wait"],tmp_path,http=OldJob())
    assert code==0 and result["collection_complete"]


@pytest.mark.parametrize("state", ["failed", "cancelled", "interrupted"])
def test_failed_or_cancelled_job_is_never_collection_success(state, tmp_path):
    code, result, _ = run(["collect", "--wait"], tmp_path,
                          [dict(queued=[dict(listing_job_id=81)]),dict(jobs=[dict(id=81,status=state,error="blocked")])])
    assert code == 2 and result["collection_complete"] is False and result["state"] == "failed"


def test_missing_old_job_or_active_queue_times_out_honestly(tmp_path, monkeypatch):
    ticks = iter([0,2])
    monkeypatch.setattr(cli.time, "monotonic", lambda: next(ticks))
    code, result, http = run(["collect", "--wait", "--wait-seconds", "1"], tmp_path,
                             [dict(queued=[dict(listing_job_id=81)]),dict(jobs=[dict(id=99,status="success")])])
    assert code == 3 and result["state"] == "pending" and not result["collection_complete"]
    assert result["missing_job_ids"] == [81]
    assert len(http.calls) == 2


def test_collect_rejects_nonpersistent_server_id(tmp_path):
    code, result, _ = run(["collect"], tmp_path, [dict(queued=[dict(listing_job_id=True)])])
    assert code == 2 and result["error"] == "invalid_response"


def test_review_export_fixed_batch_path_and_idempotent_file_preservation(tmp_path):
    code, result, http = run(["review-export", "--limit", "12"], tmp_path, [batch()])
    path = tmp_path/"output/review_batches/7.json"
    assert code == 0 and Path(result["path"]) == path and path.is_file()
    assert http.calls == [("POST", "/api/workflow/ai-review-batches", {"limit":12})]
    assert json.loads(path.read_text())["items"][0]["title"] == "i9-14900KF 故障维修件"
    edited = batch(); edited["local_notes"] = "已经人工读过，不得覆盖"
    path.write_text(json.dumps(edited))
    code, result, _ = run(["review-export"], tmp_path, [batch()])
    assert code == 0 and result["existing_file_preserved"] is True
    assert json.loads(path.read_text())["local_notes"] == edited["local_notes"]
    changed = batch(); changed["content_hash"] = "c"*64
    code, result, _ = run(["review-export"], tmp_path, [changed])
    assert code == 2 and result["error"] == "batch_file_conflict"


@pytest.mark.parametrize("bad_id", [None, True, "../escape", 0])
def test_review_export_rejects_unsafe_or_missing_id(tmp_path, bad_id):
    response=batch(); response["id"]=bad_id
    code, result, _ = run(["review-export"], tmp_path, [response])
    assert code == 2 and result["error"] == "invalid_batch_id"
    assert not (tmp_path/"output").exists()


def test_empty_review_export_creates_no_fake_batch_or_file(tmp_path):
    code,result,_=run(["review-export"],tmp_path,[dict(status="empty",id=None,item_count=0)])
    assert code == 0 and result["state"] == "empty" and result["path"] is None
    assert not (tmp_path/"output").exists()


def test_review_import_posts_exact_suggestions_and_has_no_apply_action(tmp_path):
    payload=review(); path=review_file(tmp_path,payload)
    code,result,http=run(["review-import",str(path)],tmp_path,
        [dict(status="suggestions_recorded",applied=0,review_status="suggested",raw_unchanged=True)])
    assert code == 0 and result["recommendations_only"] and not result["automatic_apply"]
    assert http.calls == [("POST","/api/workflow/review-decisions",payload)]


@pytest.mark.parametrize("change", ["price", "date", "bad_decision", "bad_kind", "nan", "count", "bad_hash", "boolean_id"])
def test_invalid_review_fields_are_refused_before_post(tmp_path,change):
    payload=review()
    if change=="price": payload["decisions"][0]["price"]=1000
    elif change=="date": payload["decisions"][0]["observed_at"]="2025-01-01"
    elif change=="bad_decision": payload["decisions"][0]["decision"]=[]
    elif change=="bad_kind": payload["reviewer"]["kind"]={}
    elif change=="nan": payload["decisions"][0]["confidence"]=float("nan")
    elif change=="count": payload["decisions"]*=41
    elif change=="bad_hash": payload["decisions"][0]["source_hash"]="tampered"
    elif change=="boolean_id": payload["decisions"][0]["observation_id"]=True
    path=review_file(tmp_path,payload)
    code,result,http=run(["review-import",str(path)],tmp_path)
    assert code==2 and result["error"] in {"invalid_review_payload","invalid_json"}
    assert not http.calls


@pytest.mark.parametrize("kind",["missing","extension","credential","symlink","too_large"])
def test_invalid_review_paths_are_rejected_without_reading_credentials(tmp_path,kind):
    if kind=="missing": path=tmp_path/"missing.json"
    elif kind=="extension": path=review_file(tmp_path,name="review.txt")
    elif kind=="credential": path=review_file(tmp_path,name="session_meta.json")
    elif kind=="symlink":
        target=review_file(tmp_path);path=tmp_path/"link.json";path.symlink_to(target)
    else:
        path=tmp_path/"large.json";path.write_bytes(b" "*(cli.MAX_FILE_BYTES+1))
    code,result,http=run(["review-import",str(path)],tmp_path)
    assert code==2 and result["error"] in {"invalid_path","credential_path_refused","file_too_large"}
    assert not http.calls


def test_server_cannot_claim_review_was_applied_silently(tmp_path):
    code,result,_=run(["review-import",str(review_file(tmp_path))],tmp_path,
                      [dict(status="suggestions_recorded",applied=1)])
    assert code==2 and result["error"]=="unexpected_apply"


def test_daily_default_only_queues_previews_and_exports_no_index_or_ai_apply(tmp_path,monkeypatch):
    monkeypatch.setattr(cli,"local_audit",lambda *_:pytest.fail("daily must use its service database"))
    code,result,http=run(["daily"],tmp_path,[dict(queued=[dict(listing_job_id=3)]),dict(version="audit-v2",changes=[]),batch(),
        dict(quality_gate=dict(can_rebuild=True,can_publish_composite=False))])
    assert code==0 and not result["complete"] and not result["collection_complete"]
    assert result["state"]=="queued_and_prepared"
    assert http.calls[1]==("POST","/api/workflow/price-audit/preview",{})
    assert result["steps"]["index"]["status"]=="deferred"
    assert not result["automatic_apply_ai"] and not result["mobile_actions"]


@pytest.mark.parametrize("can_rebuild",[False,True])
def test_daily_waits_for_real_success_then_only_rebuilds_when_explicit_quality_gate_allows(tmp_path,monkeypatch,can_rebuild):
    monkeypatch.setattr(cli,"local_audit",lambda *_:pytest.fail("daily must use its service database"))
    responses=[dict(queued=[dict(listing_job_id=3)]),dict(jobs=[dict(id=3,status="success")]),
        dict(version="audit-v2",raw_unchanged=True),batch(),dict(quality_gate=dict(can_rebuild=can_rebuild,can_publish_composite=False))]
    if can_rebuild:responses.append(dict(build_id=9,version="indices-v9",input_hash="c"*64,composite_score=None))
    code,result,http=run(["--base-url","http://127.0.0.1:8999","daily","--wait"],tmp_path,responses)
    assert code==0 and result["collection_complete"]
    assert http.calls[2]==("POST","/api/workflow/price-audit/apply",{})
    assert any(path=="/api/indices/rebuild" for _,path,_ in http.calls) is can_rebuild
    assert result["complete"] is can_rebuild
    assert result["can_publish_composite"] is False
    if can_rebuild:assert result["steps"]["index"]["composite_status"]=="insufficient_data"


def test_daily_refuses_local_db_option_before_queueing(tmp_path):
    code,result,http=run(["daily","--db",str(tmp_path/"other.sqlite3")],tmp_path)
    assert code==2 and result["error"]=="invalid_arguments" and not http.calls


def test_daily_failure_does_not_apply_audit_or_export_partial_review(tmp_path,monkeypatch):
    monkeypatch.setattr(cli,"local_audit",lambda *_:pytest.fail("must await actual collection"))
    code,result,http=run(["daily","--wait"],tmp_path,[dict(queued=[dict(listing_job_id=3)]),
        dict(jobs=[dict(id=3,status="failed")])])
    assert code==2 and result["state"]=="collection_incomplete" and not result["complete"]
    assert len(http.calls)==2


def test_local_audit_uses_existing_db_without_store_initialization_or_raw_changes(tmp_path,monkeypatch):
    from app.service import Store
    store=Store(tmp_path/"market.sqlite3",production=True)
    store.ingest([dict(item_id="cli-live",title="CPU: R7 7800X3D 主板:B650M 内存:DDR5 32G",price=10900)],"live")
    with store.db() as db:
        db.execute("UPDATE tasks SET enabled=1 WHERE id=1")
        before={table:[tuple(row) for row in db.execute(f"SELECT * FROM {table} ORDER BY rowid")]
                for table in ("items","observations","tasks")}
        # The production database already exceeds 5 MB; SQLite is not a review-file upload.
        db.execute("CREATE TABLE cli_large_fixture(payload BLOB)")
        db.execute("INSERT INTO cli_large_fixture VALUES (zeroblob(?))",(cli.MAX_FILE_BYTES+1,))
    monkeypatch.setattr(Store,"initialize",lambda *_:pytest.fail("must preserve user task settings"))
    for command in ("audit-preview","audit-apply","audit-state","audit-rollback"):
        code,result,http=run([command,"--db",str(store.path)],tmp_path)
        assert code==0 and result["ok"] and not http.calls
    with store.db() as db:
        assert {table:[tuple(row) for row in db.execute(f"SELECT * FROM {table} ORDER BY rowid")]
                for table in before}==before
    assert json.loads((tmp_path/"output/price_audit_applied.json").read_text())["raw_unchanged"]


def test_fake_database_file_is_not_initialized(tmp_path):
    path=tmp_path/"not_database.sqlite3";path.write_text("not SQLite")
    code,result,http=run(["audit-state","--db",str(path)],tmp_path)
    assert code==2 and result["error"]=="invalid_path" and not http.calls
    assert path.read_text()=="not SQLite"


def test_local_http_disables_environment_proxies_and_cross_origin_redirects(monkeypatch):
    captured=[]
    monkeypatch.setattr(cli,"build_opener",lambda *handlers:captured.extend(handlers))
    cli.LocalHTTP("http://127.0.0.1:8765")
    proxy=next(handler for handler in captured if isinstance(handler,ProxyHandler))
    redirect=next(handler for handler in captured if isinstance(handler,HTTPRedirectHandler))
    assert proxy.proxies=={}
    for target in ("https://example.org/exfil", "http://127.0.0.1:9000/api/status", "https://127.0.0.1:8765/api/status"):
        with pytest.raises(cli.CLIError,match="重定向"):
            redirect.redirect_request(Request("http://127.0.0.1:8765/api/health"),None,302,"",{},target)
    same=redirect.redirect_request(Request("http://127.0.0.1:8765/api/health"),None,302,"",{},"http://127.0.0.1:8765/api/workflow/status")
    assert same.full_url=="http://127.0.0.1:8765/api/workflow/status"


def test_local_http_serializes_exact_json_to_local_origin_without_credentials(monkeypatch):
    class Response:
        def __enter__(self):return self
        def __exit__(self,*_):pass
        def read(self,_):return b'{"applied":0}'
    calls=[]
    class Opener:
        def open(self,request,timeout):
            calls.append((request,timeout))
            return Response()
    monkeypatch.setattr(cli,"build_opener",lambda *_:Opener())
    transport=cli.LocalHTTP("http://localhost:8765",timeout=5)
    assert transport.request("POST","/api/workflow/review-decisions",{"quote":"真实原文"})=={"applied":0}
    request,timeout=calls[0]
    assert request.full_url=="http://localhost:8765/api/workflow/review-decisions" and request.method=="POST" and timeout==5
    assert json.loads(request.data)=={"quote":"真实原文"}
    assert request.get_header("Authorization") is None and request.get_header("Cookie") is None
    with pytest.raises(cli.CLIError):transport.request("GET","https://example.org")


@pytest.mark.parametrize("failure,code",[(URLError("offline"),"service_unavailable"),
    (HTTPError("http://localhost",422,"invalid",{},io.BytesIO(b'{"detail":"disabled task"}')),"http_error")])
def test_http_failures_have_structured_json_errors_without_retries(failure,code,tmp_path,monkeypatch):
    class Opener:
        def open(self,*_,**__):raise failure
    monkeypatch.setattr(cli,"build_opener",lambda *_:Opener())
    code_number,result,_=run(["collect"],tmp_path,http=cli.LocalHTTP())
    assert code_number==2 and result["error"]==code
    if code=="http_error":assert result["http_status"]==422


def test_truncated_sqlite_is_json_failure_not_schema_reinitialization(tmp_path):
    path=tmp_path/"truncated.sqlite3";path.write_bytes(b"SQLite format 3\x00broken")
    original=path.read_bytes()
    code,result,_=run(["audit-state","--db",str(path)],tmp_path)
    assert code==2 and result["error"]=="local_operation_failed"
    assert path.read_bytes()==original


def test_shell_entry_is_executable_and_help_never_networks_or_operates_phone():
    entry=Path(cli.ROOT)/"project.sh"
    assert os.access(entry,os.X_OK)
    process=subprocess.run([str(entry),"--help"],cwd=cli.ROOT,capture_output=True,text=True,timeout=10)
    assert process.returncode==0
    result=json.loads(process.stdout)
    assert result["command"]=="help" and not result["mobile_actions"] and not result["external_ai_called"]
