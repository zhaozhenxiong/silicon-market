from __future__ import annotations

import asyncio
import contextlib
import importlib
import ipaddress
import logging
import os
import re
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import Body, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from . import __version__
from .normalization import CATEGORIES, SHANGHAI, STATUSES, ValidationError, iso, utcnow
from .catalog import Catalog
from .jobs import LeaseError
from .mobile_targets import MobileTargets, MonitoredMobileJobs
from .official_market import OfficialMarket
from .mobile_runtime import MobileRuntime, MobileRuntimeError
from .mobile_guidance import MobileGuidance
from .mobile_captures import MobileCaptures
from .image_trends import ImageTrends
from .service import Store
from .workflow import Workflow

ROOT = Path(__file__).resolve().parent.parent
logger = logging.getLogger("xianyu")


def collector():
    try:
        return importlib.import_module("app.collector")
    except ImportError:
        return None


def collector_status():
    module = collector()
    if module:
        return module.collector_status()
    return dict(available=False, session_exists=False, browser_installed=False,
                login_running=False, message="采集器未安装，请安装 Playwright 与 Chromium 后重启服务")


def create_app(db_path=None, production=None):
    production = db_path is None if production is None else production
    store = Store(db_path or os.environ.get("XIANYU_DB_PATH") or ROOT / "data" / "market.sqlite3", production=production)
    official_market = OfficialMarket(store)
    catalog = Catalog(store)
    mobile_targets = MobileTargets(store,catalog)
    mobile_jobs = MonitoredMobileJobs(store, catalog, official_market,mobile_targets)
    mobile_runtime = MobileRuntime(ROOT, catalog.list, enabled=production,target_provider=mobile_targets.list)
    guidance_root = ROOT if production else Path(store.path).resolve().parent / "mobile-runtime"
    mobile_guidance = MobileGuidance(guidance_root, mobile_targets.list, official_market.evidence_file)
    from .indices import IndexService
    indices = IndexService(store)
    workflow = Workflow(store, catalog, mobile_jobs, indices)
    mobile_captures = MobileCaptures(store,mobile_targets,official_market,workflow.is_mobile_paused)
    image_trends = ImageTrends(store,mobile_targets,mobile_captures)
    jobs = set()
    active_task_ids = set()
    stopping = False

    async def run_task(task, job_id):
        persisted = store.start_listing(job_id)
        if persisted is None:
            active_task_ids.discard(task["id"])
            return
        task = persisted
        run_id = store.begin_run(task, job_id)
        task["_run_id"] = run_id
        try:
            module = collector()
            if module is None:
                raise ValidationError("采集器不可用，请安装 Playwright 和 Chromium")
            task["tracked_items"] = store.tracked_items(task)
            records = await module.collect(task)
            # A verified empty response is a successful observation with no new rows.
            result = store.ingest(records, "live", task) if records else {"imported": 0}
            store.finish_run(run_id, task["id"], len(records), result["imported"])
            store.finish_listing(job_id, run_id)
        except asyncio.CancelledError:
            store.finish_run(run_id, task["id"], error="服务停止，采集已中断")
            store.finish_listing(job_id, run_id, error="服务停止，等待持久队列续跑")
            raise
        except Exception as exc:
            safe = str(exc) if isinstance(exc, ValidationError) or type(exc).__name__ == "CollectorError" else "采集失败，请检查本机浏览器和登录状态后重试"
            logger.warning("Collection failed for task %s (%s)", task["id"], type(exc).__name__)
            store.finish_run(run_id, task["id"], error=safe[:2000])
            store.finish_listing(job_id, run_id, error=safe[:2000])
        finally:
            active_task_ids.discard(task["id"])

    def launch(task, job_id=None):
        job_id = job_id or store.enqueue_listing(task)
        active_task_ids.add(task["id"])
        job = asyncio.create_task(run_task(task, job_id))
        jobs.add(job)
        job.add_done_callback(jobs.discard)

    async def scheduler():
        while not stopping:
            await asyncio.sleep(15)
            if active_task_ids:
                continue
            pending = store.pending_listing()
            if pending:
                task = store.get_task(pending["task_id"])
                if task:
                    launch(task, pending["id"])
                    continue
            for task in store.tasks():
                if task["enabled"] and task["next_run_at"] and task["next_run_at"] <= iso():
                    launch(task)
                    break

    @asynccontextmanager
    async def lifespan(app):
        nonlocal stopping
        stopping = False
        scheduler_job = asyncio.create_task(scheduler())
        yield
        stopping = True
        scheduler_job.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await scheduler_job
        for job in list(jobs):
            job.cancel()
        if jobs:
            await asyncio.gather(*list(jobs), return_exceptions=True)

    app = FastAPI(title="硅价 · 闲鱼硬件市场观察", version=__version__, lifespan=lifespan)
    app.state.store = store
    app.state.official_market = official_market
    app.state.catalog = catalog
    app.state.mobile_targets = mobile_targets
    app.state.mobile_jobs = mobile_jobs
    app.state.mobile_runtime = mobile_runtime
    app.state.mobile_guidance = mobile_guidance
    app.state.mobile_captures = mobile_captures
    app.state.image_trends = image_trends
    app.state.workflow = workflow
    app.state.production = production
    app.state.active_task_ids = active_task_ids
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1", "[::1]", "testserver"])

    @app.middleware("http")
    async def local_write_guard(request: Request, call_next):
        if request.method in {"POST", "PATCH", "DELETE", "PUT"}:
            origin = request.headers.get("origin")
            if origin:
                parsed = urlsplit(origin)
                if parsed.netloc.lower() != request.headers.get("host", "").lower() or parsed.scheme not in {"http", "https"}:
                    return JSONResponse({"detail": "本地系统拒绝跨站写入，请从本机页面操作"}, status_code=403)
        if request.url.path == "/api/import":
            length = request.headers.get("content-length", "0")
            try:
                if int(length) > 10 * 1024 * 1024:
                    return JSONResponse({"detail": "导入内容不能超过10MB"}, status_code=413)
            except ValueError:
                return JSONResponse({"detail": "无效的请求大小"}, status_code=400)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(ValidationError)
    async def validation_handler(request, exc):
        return JSONResponse(status_code=422, content={"detail": str(exc)})

    @app.exception_handler(MobileRuntimeError)
    async def runtime_handler(request, exc):
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.exception_handler(LeaseError)
    async def lease_handler(request, exc):
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    def filters(source, days, category, model):
        if source not in {"demo", "live"}:
            raise ValidationError("数据源须为 demo 或 live")
        if production and source == "demo":
            raise ValidationError("生产环境只提供真实数据；演示数据仅用于隔离测试")
        if category != "all" and category not in CATEGORIES:
            raise ValidationError("不支持的硬件分类")
        return dict(source=source, days=days, category=category, model=model)

    @app.get("/api/health")
    def health():
        return {"ok": True}

    @app.get("/api/overview")
    def overview(source: str = "live" if production else "demo", days: int = Query(30, ge=1, le=365), category: str = "all", model: str = "all"):
        return store.overview(**filters(source, days, category, model))

    @app.get("/api/price-trends")
    def price_trends(source: str = "live", days: int = Query(180, ge=1, le=365), category: str = "all", model: str = "all"):
        return store.price_trends(**filters(source, days, category, model))

    @app.get("/api/official-market")
    def official_snapshots(days: int = Query(180, ge=1, le=365), category: str = "all", model: str = "all"):
        response = official_market.list_snapshots(days, category, model)
        response["series"].extend(mobile_jobs.official_series(days, category, model))
        return response

    @app.post("/api/official-market/import")
    def import_official_snapshot(payload: dict = Body(...)):
        return official_market.import_snapshot(payload)

    @app.get("/api/official-market/{snapshot_id}/evidence")
    def official_evidence(snapshot_id: int):
        try:
            path = official_market.get_evidence(snapshot_id)
        except ValidationError:
            raise HTTPException(404, "官方行情证据图片不可用") from None
        if path is None:
            raise HTTPException(404, "官方行情快照不存在")
        jpeg = path.suffix.lower() in {".jpg", ".jpeg"}
        media_type = "image/jpeg" if jpeg else "image/png"
        filename = "market-evidence.jpg" if jpeg else "market-evidence.png"
        return FileResponse(path, media_type=media_type, headers={"Content-Disposition": f'inline; filename="{filename}"'})

    @app.get("/api/items")
    def items(source: str = "live" if production else "demo", days: int = Query(30, ge=1, le=365), category: str = "all", model: str = "all",
              status: str = "all", q: str = Query("", max_length=200), page: int = Query(1, ge=1),
              page_size: int = Query(20, ge=1, le=200), include_excluded: bool = False):
        if status != "all" and status not in STATUSES:
            raise ValidationError("不支持的商品状态")
        records = store.filtered_items(**filters(source, days, category, model), status=status, q=q, include_excluded=include_excluded)
        return dict(items=records[(page - 1) * page_size:page * page_size], total=len(records), page=page, page_size=page_size)

    @app.get("/api/items/{item_id}/history")
    def history(item_id: int):
        result = store.history(item_id)
        if result is None:
            raise HTTPException(404, "商品不存在")
        return result

    @app.patch("/api/items/{item_id}/status")
    def set_status(item_id: int, payload: dict = Body(...)):
        if set(payload) - {"status", "evidence", "sold_price"}:
            raise ValidationError("状态更新包含未知字段")
        status = payload.get("status")
        if not isinstance(status, str) or status not in STATUSES:
            raise ValidationError("不支持的商品状态")
        evidence = str(payload.get("evidence") or "").strip()
        if not evidence:
            raise ValidationError("人工更新须填写核实依据（evidence）")
        result = store.set_status(item_id, status, "人工核实：" + evidence, payload.get("sold_price"))
        if result is None:
            raise HTTPException(404, "商品不存在")
        return result

    @app.get("/api/tasks")
    def tasks():
        return {"tasks": store.tasks()}

    @app.post("/api/tasks", status_code=201)
    def add_task(payload: dict = Body(...)):
        return store.save_task(payload)

    @app.patch("/api/tasks/{task_id}")
    def edit_task(task_id: int, payload: dict = Body(...)):
        if task_id in active_task_ids:
            raise HTTPException(409, "任务采集中，请完成后再修改")
        result = store.save_task(payload, task_id)
        if result is None:
            raise HTTPException(404, "任务不存在")
        return result

    @app.delete("/api/tasks/{task_id}")
    def delete_task(task_id: int):
        if task_id in active_task_ids:
            raise HTTPException(409, "任务采集中，请完成后再删除")
        if not store.delete_task(task_id):
            raise HTTPException(404, "任务不存在")
        return {"ok": True}

    @app.post("/api/tasks/{task_id}/run")
    async def run(task_id: int):
        task = store.get_task(task_id)
        if task is None:
            raise HTTPException(404, "任务不存在")
        if active_task_ids:
            raise HTTPException(409, "已有采集任务运行中，请稍后再试")
        status = collector_status()
        if not status["available"] or status.get("login_running"):
            raise HTTPException(409, status["message"])
        launch(task)
        return {"ok": True, "message": "已开始本机采集，运行结果可在采集日志中查看"}

    @app.get("/api/runs")
    def runs(limit: int = Query(20, ge=1, le=100)):
        return {"runs": store.runs(limit)}

    @app.get("/api/collector/status")
    def status():
        return collector_status()

    @app.post("/api/collector/login")
    def login():
        if active_task_ids:
            raise HTTPException(409, "采集进行中，请等待任务完成后打开登录窗口")
        module = collector()
        if module is None:
            raise HTTPException(409, "采集器不可用，请安装 Playwright 与 Chromium")
        try:
            return module.start_login()
        except Exception as exc:
            safe = str(exc) if type(exc).__name__ == "CollectorError" else "无法打开登录浏览器，请确认 Chromium 已安装且本机有桌面环境"
            raise HTTPException(409, safe) from None

    @app.post("/api/import")
    def import_records(payload: dict = Body(...)):
        if payload.get("source", "live") != "live":
            raise ValidationError("导入只写入真实观测 live；演示数据由独立重建入口维护")
        if set(payload) - {"source", "records", "task_id"}:
            raise ValidationError("导入请求包含未知字段")
        task = None
        if payload.get("task_id") is not None:
            if isinstance(payload["task_id"], bool) or not isinstance(payload["task_id"], int):
                raise ValidationError("task_id 必须是整数")
            task = store.get_task(payload["task_id"])
            if task is None:
                raise HTTPException(404, "导入关联任务不存在")
        return store.ingest(payload.get("records"), "live", task)

    @app.get("/api/export")
    def export(source: str = "live" if production else "demo", days: int = Query(30, ge=1, le=365), category: str = "all", model: str = "all", include_excluded: bool = False):
        content = store.export_csv(**filters(source, days, category, model), include_excluded=include_excluded)
        filename = f"hardware-market-{source}-{utcnow().astimezone(SHANGHAI).date().isoformat()}.csv"
        return Response(content=content, media_type="text/csv; charset=utf-8", headers={"Content-Disposition": f'attachment; filename="{filename}"'})

    @app.post("/api/demo/reset")
    def reset_demo():
        if production:
            raise HTTPException(404, "生产环境没有演示重置入口")
        return store.seed_demo()

    @app.get("/api/catalog")
    def list_catalog():
        return catalog.list()

    @app.post("/api/catalog/sync")
    def sync_catalog():
        return catalog.sync()

    @app.patch("/api/catalog/{catalog_id}")
    def patch_catalog(catalog_id: int, payload: dict = Body(...)):
        result = catalog.patch(catalog_id, payload)
        if result is None:
            raise HTTPException(404, "目录不存在")
        mobile_jobs.sync_catalog_state(catalog_id)
        return result

    @app.post("/api/mobile/batches")
    def create_mobile_batch(payload: dict = Body(...)):
        return mobile_jobs.create_batch(payload)

    @app.get("/api/mobile/targets")
    def monitored_mobile_targets():
        return mobile_targets.list()

    @app.get("/api/mobile/batches")
    def list_mobile_batches():
        return mobile_jobs.batches()

    @app.get("/api/mobile/jobs")
    def list_mobile_jobs(batch_id: int | None = None, catalog_id: int | None = None, limit: int = Query(100, ge=1, le=1000), offset: int = Query(0, ge=0)):
        return mobile_jobs.jobs(batch_id, catalog_id, limit, offset)

    @app.post("/api/mobile/claim")
    def claim_mobile(payload: dict = Body(...)):
        if workflow.is_mobile_paused():
            raise HTTPException(409, "手机支线已人工暂停；手机可用后从项目流程明确恢复")
        return mobile_jobs.claim(payload)

    @app.post("/api/mobile/heartbeat")
    def heartbeat_mobile(payload: dict = Body(...)):
        if workflow.is_mobile_paused() and payload.get("device_status") in {"ready", "reading"}:
            raise HTTPException(409, "手机支线已人工暂停；不接受自动恢复心跳")
        return mobile_jobs.heartbeat(payload)

    @app.post("/api/mobile/readings")
    def readings_mobile(payload: dict = Body(...)):
        if workflow.is_mobile_paused():
            raise HTTPException(409, "手机支线已人工暂停；不接受暂停前租约继续提交读数")
        return mobile_jobs.submit(payload)

    @app.post("/api/mobile/jobs/status")
    def status_mobile_jobs(payload: dict = Body(...)):
        return mobile_jobs.set_status(payload)

    @app.post("/api/mobile/batches/{batch_id}/retry")
    def retry_mobile_batch(batch_id: int):
        result = mobile_jobs.retry(batch_id)
        if result is None:
            raise HTTPException(404, "批次不存在")
        return result

    @app.get("/api/mobile/status")
    def mobile_status():
        return mobile_jobs.status()

    def local_runtime_request(request: Request):
        try:
            local = request.client is not None and ipaddress.ip_address(request.client.host).is_loopback
        except ValueError:
            local = False
        if not local:
            raise HTTPException(403, "手机进程控制仅允许本机回环连接")
        origin = request.headers.get("origin")
        if origin:
            parsed = urlsplit(origin)
            if parsed.scheme != "http" or parsed.netloc.lower() != request.headers.get("host", "").lower():
                raise HTTPException(403, "拒绝跨站访问手机采集进程")
        if request.headers.get("sec-fetch-site") == "cross-site":
            raise HTTPException(403, "拒绝跨站访问手机采集进程")
        if request.method != "GET" and request.headers.get("content-type", "").split(";")[0].strip().lower() != "application/json":
            raise HTTPException(415, "手机进程控制只接受本机JSON请求")

    @app.get("/api/mobile/worker/status")
    def native_mobile_worker_status(request: Request):
        local_runtime_request(request)
        result = app.state.mobile_runtime.status()
        pause = workflow.mobile_pause()
        result["operator_paused"] = pause["operator_paused"]
        if pause["operator_paused"] and not result["running"]:
            result.update(phase="operator_paused", connected=False, device_status="waiting_user",
                          message=pause["pause_reason"] or "手机支线已暂停，保留原图及断点；已保存图片仍可继续识别", navigation=None)
        return result

    @app.get("/api/mobile/guidance")
    def mobile_guidance_status(request: Request):
        local_runtime_request(request)
        return app.state.mobile_guidance.status()

    @app.post("/api/mobile/capture-batches")
    def create_mobile_capture_batch(request: Request, payload: dict = Body(...)):
        local_runtime_request(request)
        return mobile_captures.create_batch(payload)

    @app.post("/api/image-trends")
    def import_image_trend(request: Request,payload: dict = Body(...)):
        local_runtime_request(request)
        return image_trends.import_extract(payload)

    @app.get("/api/image-trends")
    def list_image_trends(request: Request,category: str="all",model: str="all"):
        local_runtime_request(request)
        if set(request.query_params)-{"category","model"}:
            raise ValidationError("图像曲线只支持分类/型号筛选，不按days裁切原图窗口")
        return image_trends.list(category,model)

    @app.get("/api/trend-indices")
    def get_estimated_trend_indices(request: Request):
        local_runtime_request(request)
        if request.query_params:
            raise ValidationError("图像趋势指数是全局当前监控任务篮子，不按days/model裁切")
        return image_trends.get()

    @app.post("/api/trend-indices/rebuild")
    def rebuild_estimated_trend_indices(request: Request,payload: dict=Body(default={})):
        local_runtime_request(request)
        if payload!={}:
            raise ValidationError("估算趋势重算不接受外部配置、路径、价格或命令")
        return image_trends.rebuild()

    @app.get("/api/trend-indices/builds/{build_id}")
    def replay_estimated_trend_indices(build_id: int,request: Request):
        local_runtime_request(request)
        result=image_trends.replay(build_id)
        if result is None:raise HTTPException(404,"趋势构建不存在")
        return result

    @app.get("/api/mobile/capture-batches/{batch_id}/control")
    def control_mobile_capture_batch(batch_id: int,request: Request):
        local_runtime_request(request)
        result=mobile_captures.control(batch_id)
        if result is None:
            raise HTTPException(404,"截图批次不存在")
        return result

    @app.post("/api/mobile/captures")
    def register_mobile_captures(request: Request, payload: dict = Body(...)):
        local_runtime_request(request)
        return mobile_captures.register(payload)

    @app.get("/api/mobile/captures/status")
    def mobile_capture_status(request: Request):
        local_runtime_request(request)
        return mobile_captures.status()

    @app.get("/api/mobile/captures/pending")
    def pending_mobile_captures(request: Request,batch_id: int | None = Query(None,ge=1),limit: int = Query(200,ge=1,le=200),offset: int = Query(0,ge=0)):
        local_runtime_request(request)
        return mobile_captures.pending(batch_id,limit,offset)

    @app.get("/api/mobile/captures/frames")
    def archived_mobile_captures(request: Request,batch_id: int | None = Query(None,ge=1),status: str | None = None,limit: int = Query(200,ge=1,le=200),offset: int = Query(0,ge=0)):
        local_runtime_request(request)
        return mobile_captures.frames(batch_id,status,limit,offset)

    @app.post("/api/mobile/captures/recognitions")
    def register_mobile_capture_recognitions(request: Request,payload: dict = Body(...)):
        local_runtime_request(request)
        return mobile_captures.register_recognitions(payload)

    @app.get("/api/mobile/captures/recognitions")
    def mobile_capture_recognitions(request: Request,frame_id: int | None = Query(None,ge=1),limit: int = Query(100,ge=1,le=100),offset: int = Query(0,ge=0)):
        local_runtime_request(request)
        return mobile_captures.recognitions(frame_id,limit,offset)

    @app.get("/api/mobile/captures/{frame_id}/evidence")
    def mobile_capture_evidence(frame_id: int,request: Request):
        local_runtime_request(request)
        path = mobile_captures.evidence(frame_id)
        if path is None:
            raise HTTPException(404,"截图记录不存在")
        return FileResponse(path,media_type="image/png" if path.suffix.lower()==".png" else "image/jpeg",headers={"Cache-Control":"no-store","X-Content-Type-Options":"nosniff"})

    @app.get("/api/mobile/guidance/{request_id}/evidence")
    def mobile_guidance_evidence(request_id: str, request: Request):
        local_runtime_request(request)
        path = app.state.mobile_guidance.evidence(request_id)
        return FileResponse(path, media_type="image/png" if path.suffix.lower() == ".png" else "image/jpeg", headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})

    @app.post("/api/mobile/guidance")
    def submit_mobile_guidance(request: Request, payload: dict = Body(default={})):
        local_runtime_request(request)
        if workflow.is_mobile_paused():
            raise HTTPException(409, "手机支线已人工暂停，不接受导航动作；先明确恢复项目手机支线")
        return app.state.mobile_guidance.submit(payload)

    @app.post("/api/mobile/worker/start")
    def start_native_mobile_worker(request: Request, payload: dict = Body(default={})):
        local_runtime_request(request)
        device = mobile_jobs.status()
        if device.get("connected") and device.get("worker_id") not in {None, MobileRuntime.WORKER_ID}:
            raise HTTPException(409, "其他采集端仍持有新鲜设备心跳，请先结束当前手机采集会话")
        port = request.url.port or 80
        prior_pause = workflow.mobile_pause()
        workflow.resume_mobile("操作员明确启动手机支线；仍需设备实际可用")
        try:
            return app.state.mobile_runtime.start(f"http://127.0.0.1:{port}", payload)
        except Exception:
            if prior_pause["operator_paused"]:
                workflow.pause_mobile(prior_pause["pause_reason"] or "手机启动未完成，保留暂停状态")
            raise

    @app.post("/api/mobile/worker/stop")
    def stop_native_mobile_worker(request: Request, payload: dict = Body(default={})):
        local_runtime_request(request)
        result = app.state.mobile_runtime.stop(payload)
        workflow.pause_mobile("操作员停止手机支线，保留已核验日期和断点")
        return result

    @app.get("/api/workflow/status")
    def project_workflow_status():
        return workflow.status()

    @app.get("/api/workflow/ai-review-batches")
    def review_batches(limit: int = Query(20, ge=1, le=100)):
        return workflow.review_batches(limit)

    @app.post("/api/workflow/ai-review-batches")
    def create_review_batch(request: Request, payload: dict = Body(default={})):
        local_runtime_request(request)
        if set(payload) - {"limit"}:
            raise ValidationError("复核批次只接受limit，不接受模型、命令、路径或密钥")
        return workflow.create_review_batch(payload.get("limit", 40))

    @app.get("/api/workflow/ai-review-batches/{batch_id}")
    def get_review_batch(batch_id: int):
        return workflow.review_batch(batch_id)

    @app.post("/api/workflow/review-decisions")
    def import_review_suggestions(request: Request, payload: dict = Body(...)):
        local_runtime_request(request)
        return workflow.import_review_decisions(payload)

    @app.get("/api/workflow/review-decisions")
    def get_review_suggestions(limit: int = Query(40, ge=1, le=100), batch_id: int | None = Query(None, ge=1)):
        return workflow.review_decisions(limit, batch_id)

    @app.post("/api/workflow/mobile/pause")
    def pause_mobile_branch(request: Request, payload: dict = Body(default={})):
        local_runtime_request(request)
        if set(payload) - {"reason"}:
            raise ValidationError("手机暂停只接受原因")
        reason = payload.get("reason", "手机暂不可用，人工暂停历史回填并保留断点")
        # Validate and persist first, so a forced process exit cannot permit a
        # reconnecting independent worker to resume phone input.
        pause = workflow.pause_mobile(reason)
        if production:
            app.state.mobile_runtime.stop({})
        return {"ok": True, "mobile": pause, "message": "手机支线已暂停；网页挂牌与本地处理继续运行"}

    @app.post("/api/workflow/mobile/resume")
    def resume_mobile_branch(request: Request,payload: dict = Body(default={})):
        local_runtime_request(request)
        if not isinstance(payload,dict) or set(payload)-{"reason"} or request.query_params:
            raise ValidationError("手机恢复只接受reason，不接受进程、命令、路径或其他参数")
        state = workflow.resume_mobile(payload.get("reason","本人明确恢复手机截图归档；设备状态仍须实际检查"))
        return {"ok":True,"mobile":state,"native_worker_started":False,"device_connection_checked":False,
                "message":"已明确恢复手机支线许可；未启动独立worker，未截图或输入，也未据此认定设备连接成功"}

    @app.get("/api/workflow/listing/queue")
    def project_listing_queue(limit: int = Query(100, ge=1, le=100), job_ids: str | None = Query(None, max_length=1999)):
        selected = []
        if job_ids is not None:
            if not re.fullmatch(r"[1-9][0-9]{0,18}(?:,[1-9][0-9]{0,18}){0,99}", job_ids):
                raise ValidationError("job_ids须为逗号分隔的最多100个正整数")
            selected = sorted({int(value) for value in job_ids.split(",")})
            if any(value > 9223372036854775807 for value in selected):
                raise ValidationError("job_ids超出数据库整数范围")
        where = " AND j.id IN ("+",".join("?" for _ in selected)+")" if selected else ""
        with store.db() as db:
            rows = db.execute("""SELECT j.id,j.task_id,t.name AS task_name,j.status,j.attempts,
                j.run_id,j.error,j.next_attempt_at,j.created_at,j.updated_at FROM listing_jobs j
                JOIN tasks t ON t.id=j.task_id WHERE t.source='live'"""+where+" ORDER BY j.id DESC LIMIT ?", (*selected, limit)).fetchall()
        return {"jobs": [dict(row) for row in rows]}

    @app.post("/api/workflow/price-audit/{operation}")
    def project_price_audit(operation: str, request: Request, payload: dict = Body(default={})):
        local_runtime_request(request)
        methods = {"preview": store.price_audit.preview, "apply": store.price_audit.apply,
                   "state": store.price_audit.state, "rollback": store.price_audit.rollback}
        if operation not in methods:
            raise ValidationError("价格审计只允许preview/apply/state/rollback")
        if payload != {} or request.query_params:
            raise ValidationError("价格审计不接受body字段、路径或命令参数；只处理当前服务数据库")
        return methods[operation]()

    @app.post("/api/workflow/listing/queue")
    def enqueue_project_listing(request: Request, payload: dict = Body(default={})):
        local_runtime_request(request)
        if set(payload) - {"task_ids"}:
            raise ValidationError("挂牌排队只接受task_ids；采样配置由监控任务管理")
        enabled = {task["id"]: task for task in store.tasks() if task["enabled"] and task["source"] == "live"}
        ids = payload.get("task_ids", sorted(enabled))
        if not isinstance(ids, list) or len(ids) > 100 or any(isinstance(i, bool) or not isinstance(i, int) or i <= 0 for i in ids):
            raise ValidationError("task_ids须为最多100个正整数")
        if any(i not in enabled for i in ids):
            raise ValidationError("只允许已启用的真实采集任务；此操作不会启用已暂停任务")
        queued = []
        for task_id in sorted(set(ids)):
            job_id = store.enqueue_listing(enabled[task_id])
            with store.db() as db:
                status = db.execute("SELECT status FROM listing_jobs WHERE id=?", (job_id,)).fetchone()[0]
            queued.append({"task_id": task_id, "listing_job_id": job_id, "status": status})
        return {"queued": queued, "count": len(queued), "message": "已持久入队，调度器将串行采集；入队不等于完成"}

    @app.get("/api/mobile/evidence/{reading_id}")
    def mobile_evidence(reading_id: int):
        try:
            path = mobile_jobs.evidence(reading_id)
        except ValidationError:
            raise HTTPException(404, "证据不可用或哈希不符") from None
        if path is None:
            raise HTTPException(404, "读数不存在")
        jpeg = path.suffix.lower() in {".jpg", ".jpeg"}
        return FileResponse(path, media_type="image/jpeg" if jpeg else "image/png")

    @app.get("/api/indices")
    def get_indices(days: int = Query(180, ge=1, le=365), category: str = "all"):
        filters("live", days, category, "all")
        return indices.get(days=days, category=category)

    @app.post("/api/indices/rebuild")
    def rebuild_indices(payload: dict = Body(default={})):
        days, category = payload.get("days", 180), payload.get("category", "all")
        if isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= 365:
            raise ValidationError("days 须为1至365")
        filters("live", days, category, "all")
        return indices.rebuild(days=days, category=category)

    static = ROOT / "app" / "static"
    if static.is_dir():
        app.mount("/", StaticFiles(directory=static, html=True), name="ui")
    return app


app = create_app()
