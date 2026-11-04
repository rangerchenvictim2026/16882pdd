"""仅监听本机的工作台接口与静态页面。"""
import csv
import io
import json
import os
import sqlite3
import math
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.exceptions import HTTPException as StarletteHTTPException

from .models import Backup, PresetRequest, SaveSelections, SearchFilters, SelectionPatch, Settings, SearchLinkRequest, RetryRequest, timestamp
from .native_search import import_search_url, REFERENCE_URL
from .providers import BrowserProvider, HumanActionRequired, SourceUnavailable
from .store import Store, dump
from .tasks import ACTIVE, TaskManager
from .repairs import repair_existing_records
from .competition_models import CompetitionRequest, SessionValidation
from .competition_tasks import CompetitionManager
from .pdd_chrome import ChromePDDProvider
from .pdd_safari import SafariPDDProvider
from .opportunities import opportunities

ROOT = Path(__file__).resolve().parents[1]


def csv_safe(value):
    text = "" if value is None else str(value)
    return "'" + text if text.lstrip().startswith(("=", "+", "-", "@", "\t", "\r")) else text


def export_csv(rows):
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(["商品ID", "商品名称", "商品链接", "供应商", "价格下限", "价格上限", "上新依据", "一件代发依据", "近30天销量展示值", "销量单位", "销量原文", "包邮条件", "采集时间", "选品状态", "标签", "备注", "平台筛选链接", "平台排序位置", "平台销量展示原文"])
    for row in rows:
        p = row.get("product", row)
        proof = {e["field"]: e["text"] for e in p.get("evidence", [])}
        writer.writerow([csv_safe(v) for v in [p["id"], p["title"], p["url"], p["supplier_name"], p["price_min"], p["price_max"], proof.get("published_at", proof.get("native_new_days", "")), proof.get("dropship", ""), p["sales_30d"], p["sales_unit"], proof.get("sales_30d", ""), p["shipping_condition"], p["observed_at"], row.get("status", ""), "、".join(row.get("tags", [])), row.get("notes", ""), p.get("native_search_url", ""), p.get("native_position", ""), p.get("platform_sales_text", "")]])
    return Response("\ufeff" + output.getvalue(), media_type="text/csv; charset=utf-8", headers={"Content-Disposition": "attachment; filename=selections.csv"})


def create_app(data_dir: Path | None = None, provider=None, competition_provider=None, *, item_interval=1.0):
    directory = data_dir or Path(os.environ.get("WORKBENCH_DATA_DIR", ROOT / "data"))
    store = Store(directory)
    source = provider or BrowserProvider(directory)
    manager = TaskManager(store, source, item_interval=item_interval)
    pdd = competition_provider or (ChromePDDProvider(directory) if os.environ.get('WORKBENCH_PDD_BROWSER') == 'chrome' else SafariPDDProvider(directory))
    competition = CompetitionManager(store, pdd, manager)
    manager.on_complete=lambda task: competition.create(CompetitionRequest(source_task_id=task['id']))

    @asynccontextmanager
    async def lifespan(app):
        repair_existing_records(store)
        await manager.start()
        await competition.start()
        try:
            yield
        finally:
            await competition.close()
            await manager.close()

    app = FastAPI(title="电商运营助手", description="本机1688选品服务", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.store, app.state.manager = store, manager
    app.state.competition = competition
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "testserver"])

    @app.middleware("http")
    async def local_write_guard(request: Request, call_next):
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            if request.headers.get("x-workbench") != "1":
                return JSONResponse({"detail": "请通过本机工作台执行此操作"}, status_code=403)
            origin = request.headers.get("origin")
            if origin and origin not in {str(request.base_url).rstrip("/"), "http://127.0.0.1:8765", "http://localhost:8765", "http://127.0.0.1:5173", "http://localhost:5173"}:
                return JSONResponse({"detail": "不允许其他网站操作本机工作台"}, status_code=403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, error):
        errors = error.errors()
        message = "输入格式不正确，请检查必填项、价格区间和数量上限"
        for item in errors:
            context = item.get("ctx", {}).get("error")
            if context and any("\u4e00" <= c <= "\u9fff" for c in str(context)):
                message = str(context)
                break
        return JSONResponse({"detail": message}, status_code=422)

    @app.exception_handler(ValueError)
    async def value_error(request, error):
        message = str(error)
        if not any("\u4e00" <= c <= "\u9fff" for c in message):
            message = "输入资料不符合要求，请检查后重试"
        return JSONResponse({"detail": message}, status_code=400)

    @app.exception_handler(SourceUnavailable)
    async def source_error(request, error):
        return JSONResponse({"detail": str(error)}, status_code=503)

    @app.exception_handler(HumanActionRequired)
    async def browser_action_required(request, error):
        return JSONResponse({"detail": str(error)}, status_code=409)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request, error):
        detail = str(error.detail)
        if not any("\u4e00" <= c <= "\u9fff" for c in detail):
            detail = "请求的地址不存在" if error.status_code == 404 else "此操作方式不受支持" if error.status_code == 405 else "请求未能完成，请检查后重试"
        return JSONResponse({"detail": detail}, status_code=error.status_code)

    @app.exception_handler(Exception)
    async def unexpected_error(request, error):
        return JSONResponse({"detail": "本机服务处理请求时发生异常，请稍后重试"}, status_code=500)

    def get_task(task_id):
        task = store.task(task_id)
        if not task:
            raise HTTPException(404, "任务不存在")
        return task

    @app.get("/api/health")
    async def health():
        return {"ok": True, "time": timestamp(), "local_only": True, "demo": getattr(app.state, 'demo', False)}

    @app.get("/api/overview")
    async def overview():
        tasks, selections = store.tasks(), store.selections()
        return {"tasks": len(tasks), "active": sum(t["status"] in ACTIVE for t in tasks), "saved": len(selections),
                "selected": sum(s["status"] == "已选中" for s in selections), "qualified": sum(t["qualified"] for t in tasks)}

    @app.get("/api/capabilities")
    async def capabilities():
        return source.capabilities()

    @app.post("/api/browser/open")
    async def open_browser():
        await source.open()
        return source.capabilities()

    @app.post("/api/pdd/browser/open")
    async def open_pdd_browser():
        ensure_pdd_idle()
        if hasattr(pdd,'open_login'):
            return await pdd.open_login()
        await pdd.open()
        return {"message": "专用登录窗口已打开", "browser": "Chrome"}

    def ensure_pdd_idle():
        current=store.competition(competition.current) if competition.current else None
        if current and current['status']=='running':
            raise HTTPException(409,'竞品正在后台采集，不能同时切换同一登录资料；请等待暂停或先取消任务')

    @app.get('/api/pdd/session')
    async def pdd_session():
        return pdd.status() if hasattr(pdd,'status') else {'browser':'测试提供器','mode':'closed','verified':False}

    @app.post('/api/pdd/session/confirm')
    async def confirm_pdd_session(body:SessionValidation|None=None):
        ensure_pdd_idle()
        if not hasattr(pdd,'confirm_login'):
            raise HTTPException(409,'当前接入不支持浏览器连接验证')
        return await pdd.confirm_login(body.keyword if body else None)

    @app.post('/api/pdd/diagnostics')
    async def pdd_diagnostics():
        ensure_pdd_idle()
        if not hasattr(pdd,'diagnose'):
            raise HTTPException(409,'当前接入不支持连接检查')
        return await pdd.diagnose()

    def get_competition(job_id):
        job = store.competition(job_id)
        if not job:
            raise HTTPException(404, "竞品任务不存在")
        return job

    @app.get("/api/competitions")
    async def competitions(source_task_id: str | None = None):
        return store.competitions(source_task_id)[:20]

    @app.post("/api/competitions", status_code=201)
    async def create_competition(body: CompetitionRequest):
        return competition.create(body)

    @app.get("/api/competitions/{job_id}")
    async def competition_job(job_id: str):
        return get_competition(job_id)

    @app.get('/api/competitions/{job_id}/opportunities')
    async def opportunity_results(job_id: str,minimum_gap: float=0):
        if not math.isfinite(minimum_gap) or minimum_gap<0:
            raise ValueError('最低货价差必须是大于等于0的金额')
        return opportunities(get_competition(job_id),minimum_gap)

    @app.get('/api/competitions/{job_id}/opportunities/export')
    async def export_opportunities(job_id: str,minimum_gap: float=0):
        data=await opportunity_results(job_id,minimum_gap)
        output=io.StringIO(newline='');writer=csv.writer(output)
        writer.writerow(['货源商品','货源链接','1688最高展示价','竞品商品','竞品链接','拼多多最低展示价','保守货价差','当前单品销量','货源数据时间','竞品数据时间','核对状态','说明'])
        for row in data['rows']:
            writer.writerow([csv_safe(v) for v in [row['source']['title'],row['source']['url'],row['reference_cost'],
                row['competitor']['title'],row['competitor']['url'],row['reference_price'],row['gap_min'],
                row['competitor']['sales_text'],row['source']['observed_at'],row['competitor']['observed_at'],row['verification'],data['note']]])
        return Response('\ufeff'+output.getvalue(),media_type='text/csv; charset=utf-8',headers={'Content-Disposition':'attachment; filename=opportunities.csv'})

    @app.post("/api/competitions/{job_id}/cancel")
    async def cancel_competition(job_id: str):
        get_competition(job_id)
        return competition.cancel(job_id)

    @app.post("/api/competitions/{job_id}/resume")
    async def resume_competition(job_id: str):
        get_competition(job_id)
        return competition.resume(job_id)

    @app.get("/api/competitions/{job_id}/export")
    async def export_competition(job_id: str):
        job = get_competition(job_id)
        output = io.StringIO(newline="")
        writer = csv.writer(output)
        writer.writerow(["货源商品", "货源链接", "搜索关键词", "竞品排名", "竞品商品", "售价下限", "售价上限", "售价原文", "单品销量原文", "竞品链接", "数据时间", "排序范围"])
        for item in job["items"]:
            for rank, row in enumerate(item["matches"], 1):
                writer.writerow([csv_safe(v) for v in [item["source"]["title"], item["source"]["url"], item["keyword"], rank,
                    row["title"], row["price_min"], row["price_max"], row["price_text"], row["sales_text"], row["url"], row["observed_at"], item["sort_scope"]]])
        return Response("\ufeff" + output.getvalue(), media_type="text/csv; charset=utf-8", headers={"Content-Disposition": "attachment; filename=competitors.csv"})

    @app.get("/api/tasks")
    async def tasks():
        return store.tasks()

    @app.post("/api/tasks", status_code=201)
    async def create_task(filters: SearchFilters):
        return manager.create(filters)

    @app.post('/api/screenings',status_code=201)
    async def create_screening(filters:SearchFilters):
        small=SearchFilters.model_validate({**filters.model_dump(),'limit':20,'selection_mode':'platform','native_search_url':''})
        return manager.create(small,compare_after=True)

    @app.get("/api/search-link/reference")
    async def reference_search():
        return {"url": REFERENCE_URL, "filters": import_search_url(REFERENCE_URL)}

    @app.post("/api/search-link/import")
    async def import_search(body: SearchLinkRequest):
        filters = SearchFilters.model_validate(import_search_url(body.url))
        return filters

    @app.get("/api/tasks/{task_id}")
    async def task(task_id: str):
        return get_task(task_id)

    @app.get("/api/tasks/{task_id}/results")
    async def results(task_id: str):
        get_task(task_id)
        return manager.results(task_id)

    @app.post("/api/tasks/{task_id}/cancel")
    async def cancel(task_id: str):
        get_task(task_id)
        return manager.cancel(task_id)

    @app.post("/api/tasks/{task_id}/resume")
    async def resume(task_id: str):
        get_task(task_id)
        manager.resume(task_id)
        return store.task(task_id)

    @app.post("/api/tasks/{task_id}/retry", status_code=201)
    async def retry(task_id: str, body: RetryRequest | None = None):
        old = get_task(task_id)
        if old["status"] in ACTIVE:
            raise HTTPException(409, "请先取消或完成原任务")
        filters = {**old["filters"]}
        if body and body.selection_mode:
            filters["selection_mode"] = body.selection_mode
        return manager.create(SearchFilters.model_validate(filters), retry_of=task_id, refresh_id=old.get("refresh_id"),compare_after=old.get('compare_after',False))

    @app.get("/api/tasks/{task_id}/export")
    async def export_results(task_id: str):
        get_task(task_id)
        return export_csv(manager.results(task_id))

    @app.get("/api/products/{product_id}")
    async def product(product_id: str):
        result = store.product(product_id)
        if not result:
            raise HTTPException(404, "商品不存在")
        return result

    @app.post("/api/products/{product_id}/refresh", status_code=201)
    async def refresh(product_id: str):
        product = store.product(product_id)
        if not product:
            raise HTTPException(404, "商品不存在")
        settings = store.settings()
        previous = store.latest_filters_for_product(product_id)
        filters = SearchFilters.model_validate({**previous, "limit": 1}) if previous else SearchFilters(keyword=product["title"][:100], province=settings["province"], city=settings["city"], limit=1)
        return manager.create(filters, refresh_id=product_id)

    @app.get("/api/selections")
    async def selections():
        return store.selections()

    @app.post("/api/selections")
    async def save_selections(body: SaveSelections):
        store.save_selections(body.product_ids)
        return {"saved": len(set(body.product_ids))}

    @app.patch("/api/selections/{product_id}")
    async def patch_selection(product_id: str, patch: SelectionPatch):
        if not store.patch_selection(product_id, patch):
            raise HTTPException(404, "选品记录不存在")
        return {"ok": True}

    @app.get("/api/selections/export")
    async def export_selections():
        return export_csv(store.selections())

    @app.get("/api/settings")
    async def settings():
        return store.settings()

    @app.put("/api/settings")
    async def save_settings(body: Settings):
        store.put_kv("settings", body.model_dump())
        return body

    @app.get("/api/presets")
    async def presets():
        return store.presets()

    @app.post("/api/presets", status_code=201)
    async def save_preset(body: PresetRequest):
        return {"id": store.save_preset(body.name, body.filters.model_dump())}

    @app.delete("/api/presets/{preset_id}")
    async def delete_preset(preset_id: str):
        store.delete_preset(preset_id)
        return {"ok": True}

    @app.get("/api/backup")
    async def backup():
        return Response(dump(store.backup()), media_type="application/json", headers={"Content-Disposition": "attachment; filename=workbench-backup.json"})

    @app.post("/api/restore")
    async def restore(request: Request):
        if manager.current is not None or not manager.queue.empty() or competition.current is not None or not competition.queue.empty():
            raise HTTPException(409, "请先取消或完成采集任务，等待采集停止后再恢复备份")
        content = bytearray()
        async for chunk in request.stream():
            content.extend(chunk)
            if len(content) > 30 * 1024 * 1024:
                raise HTTPException(413, "备份文件不能超过30MB")
        try:
            parsed = Backup.model_validate_json(bytes(content))
        except ValidationError:
            raise HTTPException(400, "备份文件格式不正确")
        previous = store.backup()
        backup_dir = directory / "backups"
        backup_dir.mkdir(exist_ok=True)
        (backup_dir / (timestamp().replace(":", "-") + ".json")).write_text(dump(previous), encoding="utf-8")
        try:
            store.restore(parsed)
        except (ValueError, KeyError, TypeError, sqlite3.Error):
            raise HTTPException(400, "备份内容不完整或关联记录有误，原有数据未改变")
        return {"ok": True, "message": "恢复完成，恢复前的数据已自动备份"}

    assets = ROOT / "dist" / "assets"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=assets), name="assets")
    demo_assets = ROOT / 'public' / 'demo-assets'
    if demo_assets.is_dir():
        app.mount('/demo-assets', StaticFiles(directory=demo_assets), name='demo-assets')

    @app.get("/{path:path}")
    async def frontend(path: str):
        if path.startswith("api/"):
            raise HTTPException(404, "接口不存在")
        index = ROOT / "dist" / "index.html"
        if not index.is_file():
            return JSONResponse({"detail": "前端尚未构建，请先运行启动脚本"}, status_code=503)
        return FileResponse(index)

    return app


def configured_app():
    if os.environ.get('WORKBENCH_DEMO') == '1':
        from .demo import create_demo
        return create_demo(create_app)
    return create_app()


app = configured_app()
