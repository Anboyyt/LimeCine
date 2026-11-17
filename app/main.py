"""LimeCine：连 Emby / Jellyfin / LiMe，拉库内海报生成整张库封面。

配置在 config.yaml（见 README），这里只提供 HTTP 接口和预览页。
"""
from __future__ import annotations

import gc
import secrets
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app import schedules, settings
from app.compat.log import logger
from app.config import ADMIN_TOKEN, APP_NAME, DEFAULT_RESOLUTION, VERSION
from app.emby import EmbyClient, EmbyError
from app.jobs import JobManager, JobOptions, start_scheduler
from app.render import ALL_STYLES, RenderError
from app.service import generate_one
from app.settings import Server

BASE_DIR = Path(__file__).resolve().parent.parent
INDEX = BASE_DIR / "templates" / "index.html"

manager = JobManager()
_probe_lock = threading.Lock()
_probe_cache: Dict[str, tuple] = {}
PROBE_TTL = 30


@asynccontextmanager
async def lifespan(_app: FastAPI):
    cfg = settings.get_config()
    logger.info(
        "%s %s 启动：servers=%s source=%s",
        APP_NAME, VERSION,
        [s.id for s in cfg.servers] or "（无）",
        cfg.source or "环境变量/未配置",
    )
    start_scheduler(manager)
    yield


app = FastAPI(title=APP_NAME, version=VERSION, lifespan=lifespan)

STATIC_DIR = BASE_DIR / "static"
if STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# ---------------------------------------------------------------- 鉴权


@app.middleware("http")
async def token_guard(request: Request, call_next):
    if ADMIN_TOKEN and request.url.path.startswith("/api"):
        token = (
            request.headers.get("X-LC-Token")
            or request.headers.get("X-Auth-Token")
            or ""
        )
        auth = request.headers.get("Authorization") or ""
        if not token and auth.lower().startswith("bearer "):
            token = auth[7:].strip()
        if not token:
            token = request.query_params.get("token") or ""
        if not secrets.compare_digest(token, ADMIN_TOKEN):
            return JSONResponse({"detail": "需要访问令牌（LC_TOKEN）"}, status_code=401)
    return await call_next(request)


# ---------------------------------------------------------------- 请求模型


class GenerateRequest(BaseModel):
    server: str = ""
    base_url: str = ""
    api_key: str = ""
    library_id: str
    library_name: str = ""
    style: str = ""
    resolution: str = ""
    title_zh: str = ""
    title_en: str = ""
    color: str = ""
    force: bool = False
    apply: bool = False
    params: Dict[str, Any] = Field(default_factory=dict)
    # 兼容旧前端/脚本的扁平参数
    zh_font_size: Optional[float] = None
    en_font_size: Optional[float] = None
    title_scale: Optional[float] = None
    blur_size: Optional[int] = None
    color_ratio: Optional[float] = None
    bg_mode: Optional[str] = None
    custom_bg_color: Optional[str] = None
    animation_format: Optional[str] = None
    animation_fps: Optional[int] = None
    animation_duration: Optional[int] = None
    animation_resolution: Optional[str] = None
    animation_reduce_colors: Optional[bool] = None


class GenerateAllRequest(BaseModel):
    servers: List[str] = Field(default_factory=list)
    base_url: str = ""
    api_key: str = ""
    style: str = ""
    resolution: str = ""
    apply: bool = True
    force: bool = False
    library_ids: List[str] = Field(default_factory=list)


class ScheduleIn(BaseModel):
    name: str = "定时任务"
    enabled: bool = True
    time: str = "04:00"
    servers: List[str] = Field(default_factory=list)
    libraries: List[str] = Field(default_factory=list)
    style: str = ""
    resolution: str = ""
    force: bool = False


class ServerIn(BaseModel):
    id: str = ""
    name: str = ""
    url: str
    api_key: str = ""
    enabled: bool = True
    write_back: bool = True


# ---------------------------------------------------------------- 辅助


def _ephemeral(base_url: str, api_key: str) -> Server:
    return Server(id="_manual", url=settings._norm_url(base_url), api_key=api_key, name="手动填写")


def _pick_server(server_id: str = "", base_url: str = "", api_key: str = "") -> Server:
    cfg = settings.get_config()
    if base_url:
        return _ephemeral(base_url, api_key)
    if server_id:
        server = cfg.server(server_id)
        if not server:
            raise HTTPException(status_code=400, detail=f"未知服务器: {server_id}")
        return server
    enabled = cfg.enabled_servers()
    if len(enabled) == 1:
        return enabled[0]
    if not enabled:
        raise HTTPException(status_code=400, detail="没有配置服务器，请先编辑 config.yaml")
    raise HTTPException(status_code=400, detail="配置了多台服务器，请指定 server")


def _overrides(req: GenerateRequest) -> Dict[str, Any]:
    out: Dict[str, Any] = dict(req.params or {})
    mapping = {
        "zh_font_size": req.zh_font_size,
        "en_font_size": req.en_font_size,
        "title_scale": req.title_scale,
        "blur_size": req.blur_size,
        "color_ratio": req.color_ratio,
        "bg_color_mode": req.bg_mode,
        "custom_bg_color": req.custom_bg_color,
        "animation_format": req.animation_format,
        "animation_fps": req.animation_fps,
        "animation_duration": req.animation_duration,
        "animation_resolution": req.animation_resolution,
        "animation_reduce_colors": req.animation_reduce_colors,
    }
    for key, value in mapping.items():
        if value is not None:
            out[key] = value
    return out


def _probe(server: Server) -> Dict[str, Any]:
    key = server.id
    now = time.monotonic()
    with _probe_lock:
        cached = _probe_cache.get(key)
        if cached and now - cached[0] < PROBE_TTL and cached[1].get("url") == server.url:
            return cached[1]

    item = {
        **server.public(),
        "ok": False,
        "server_name": "",
        "version": "",
        "product": "",
        "error": "",
    }
    if not server.api_key:
        item["error"] = "没有配置 api_key"
    else:
        try:
            with EmbyClient(server.url, server.api_key, timeout=6) as client:
                info = client.probe()
            item.update(info)
            item["ok"] = True
        except EmbyError as e:
            item["error"] = str(e)
        except Exception as e:  # noqa: BLE001
            item["error"] = str(e)[:200]

    with _probe_lock:
        _probe_cache[key] = (now, item)
    return item


# ---------------------------------------------------------------- 页面与健康


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    html = INDEX.read_text(encoding="utf-8")
    return HTMLResponse(html, headers={"Cache-Control": "no-cache"})


@app.get("/health")
def health() -> dict:
    cfg = settings.get_config()
    return {
        "ok": True,
        "name": APP_NAME,
        "version": VERSION,
        "servers": len(cfg.servers),
        "config_error": cfg.error,
    }


@app.get("/api/styles")
def styles() -> dict:
    return {"styles": ALL_STYLES, "default_resolution": DEFAULT_RESOLUTION}


# ---------------------------------------------------------------- 配置与服务器


@app.get("/api/config")
def get_config() -> dict:
    return settings.view(settings.get_config())


@app.post("/api/config/reload")
def reload_config() -> dict:
    cfg = settings.reload_config()
    with _probe_lock:
        _probe_cache.clear()
    logger.info("配置已重新加载 source=%s servers=%s", cfg.source, [s.id for s in cfg.servers])
    return settings.view(cfg)


@app.get("/api/servers")
def servers() -> dict:
    cfg = settings.get_config()
    enabled = [s for s in cfg.servers if s.enabled]
    results: List[Dict[str, Any]] = []
    if enabled:
        with ThreadPoolExecutor(max_workers=min(8, len(enabled))) as pool:
            futures = [pool.submit(_probe, s) for s in enabled]
            for fut in as_completed(futures):
                results.append(fut.result())
    for server in cfg.servers:
        if not server.enabled:
            results.append({
                **server.public(),
                "ok": False,
                "server_name": "",
                "version": "",
                "product": "",
                "error": "已禁用",
            })
    order = {s.id: i for i, s in enumerate(cfg.servers)}
    results.sort(key=lambda x: order.get(x["id"], 99))
    return {
        "servers": results,
        "source": cfg.source,
        "error": cfg.error,
        "schedule": cfg.schedule,
    }


@app.post("/api/servers")
def add_server(req: ServerIn) -> dict:
    cfg = settings.get_config()
    url = settings._norm_url(req.url)
    if not url:
        raise HTTPException(status_code=400, detail="服务器地址不能为空")
    if not req.api_key:
        raise HTTPException(status_code=400, detail="API Key 不能为空")

    existing = {s.id for s in cfg.servers}
    if req.id:
        sid = settings._slug(req.id)
        if sid in existing:
            raise HTTPException(status_code=409, detail=f"服务器 ID 已存在: {sid}")
    else:
        sid = settings.make_server_id(req.name, url, existing)

    server = settings.Server(
        id=sid,
        url=url,
        api_key=req.api_key,
        name=req.name or sid,
        enabled=req.enabled,
        write_back=req.write_back,
        source="ui",
    )
    ui_servers = settings.load_ui_servers()
    ui_servers.append(server)
    settings.save_ui_servers(ui_servers)
    settings.reload_config()
    with _probe_lock:
        _probe_cache.pop(sid, None)
    logger.info("新增服务器 id=%s url=%s", sid, url)
    return server.public()


@app.put("/api/servers/{server_id}")
def update_server(server_id: str, req: ServerIn) -> dict:
    ui_servers = settings.load_ui_servers()
    target = next((s for s in ui_servers if s.id == server_id), None)
    if not target:
        raise HTTPException(
            status_code=404,
            detail="只能修改面板添加的服务器；config.yaml 里的服务器请直接编辑文件",
        )
    url = settings._norm_url(req.url or target.url)
    if not url:
        raise HTTPException(status_code=400, detail="服务器地址不能为空")
    target.url = url
    target.name = req.name or target.name
    if req.api_key:
        target.api_key = req.api_key
    target.enabled = req.enabled
    target.write_back = req.write_back
    settings.save_ui_servers(ui_servers)
    settings.reload_config()
    with _probe_lock:
        _probe_cache.pop(server_id, None)
    logger.info("更新服务器 id=%s url=%s", server_id, url)
    return target.public()


@app.delete("/api/servers/{server_id}")
def delete_server(server_id: str) -> dict:
    ui_servers = settings.load_ui_servers()
    remaining = [s for s in ui_servers if s.id != server_id]
    if len(remaining) == len(ui_servers):
        raise HTTPException(status_code=404, detail="只能删除面板添加的服务器")
    settings.save_ui_servers(remaining)
    settings.reload_config()
    with _probe_lock:
        _probe_cache.pop(server_id, None)
    logger.info("删除服务器 id=%s", server_id)
    return {"ok": True}


@app.get("/api/libraries")
def libraries(
    request: Request,
    server: str = "",
    base_url: str = "",
    api_key: str = "",
) -> dict:
    # 手动模式的 Key 走请求头，避免出现在访问日志的 URL 里
    api_key = api_key or request.headers.get("X-LimeCine-Key", "")
    cfg = settings.get_config()
    if base_url:
        targets = [_ephemeral(base_url, api_key)]
    elif server:
        s = cfg.server(server)
        if not s:
            raise HTTPException(status_code=400, detail=f"未知服务器: {server}")
        targets = [s]
    else:
        targets = cfg.enabled_servers()
        if not targets:
            raise HTTPException(status_code=400, detail="没有配置服务器，请先编辑 config.yaml")

    out: List[Dict[str, Any]] = []
    errors: List[Dict[str, str]] = []
    for s in targets:
        if not s.api_key:
            errors.append({"server": s.id, "server_name": s.name or s.id, "error": "没有配置 api_key"})
            continue
        try:
            with EmbyClient(s.url, s.api_key, timeout=15) as client:
                libs = client.libraries()
            for lib in libs:
                out.append({
                    **lib,
                    "server": s.id,
                    "server_name": s.name or s.id,
                })
        except EmbyError as e:
            errors.append({"server": s.id, "server_name": s.name or s.id, "error": str(e)})
        except Exception as e:  # noqa: BLE001
            errors.append({"server": s.id, "server_name": s.name or s.id, "error": str(e)[:200]})

    return {"libraries": out, "errors": errors}


# ---------------------------------------------------------------- 生成


@app.post("/api/generate")
def generate(req: GenerateRequest) -> Response:
    if not req.library_id:
        raise HTTPException(status_code=400, detail="缺少 library_id")
    server = _pick_server(req.server, req.base_url, req.api_key)
    if not server.api_key:
        raise HTTPException(status_code=400, detail="缺少 API Key")

    cfg = settings.get_config()
    plan = settings.resolve_library(cfg, server, {
        "id": req.library_id,
        "name": req.library_name,
    })

    style = req.style or str(plan["params"].get("style") or "animated_1")
    resolution = req.resolution or str(plan["params"].get("resolution") or "1080p")
    params = dict(plan["params"])
    params.update(_overrides(req))
    params["style"] = style
    params["resolution"] = resolution
    applied = bool(req.apply and plan["write_back"])

    try:
        result = generate_one(
            server=server,
            library_id=req.library_id,
            library_name=req.library_name or plan["library_name"],
            style=style,
            resolution=resolution,
            title_zh=req.title_zh or plan["title_zh"],
            title_en=req.title_en or plan["title_en"],
            color=req.color or plan["color"],
            apply=applied,
            force=req.force,
            params=params,
        )
    except (EmbyError, RenderError) as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:  # noqa: BLE001
        logger.exception("generate failed")
        raise HTTPException(status_code=500, detail=f"生成失败: {e}")
    finally:
        gc.collect()

    if req.apply:
        return JSONResponse({
            "ok": True,
            "applied": applied,
            "write_back": plan["write_back"],
            "skipped": result.skipped,
            "seconds": result.seconds,
            "bytes": len(result.data or b""),
        })
    if result.data is None:
        raise HTTPException(status_code=500, detail="生成结果为空")
    mime = "image/apng" if result.ext == ".apng" else "image/png"
    return Response(content=result.data, media_type=mime)


@app.post("/api/generate-all")
def generate_all(req: GenerateAllRequest) -> dict:
    cfg = settings.get_config()
    if req.base_url:
        targets = [_ephemeral(req.base_url, req.api_key)]
    elif req.servers:
        targets = []
        for sid in req.servers:
            s = cfg.server(sid)
            if not s:
                raise HTTPException(status_code=400, detail=f"未知服务器: {sid}")
            targets.append(s)
    else:
        targets = cfg.enabled_servers()
    if not targets:
        raise HTTPException(status_code=400, detail="没有可用的服务器，请先编辑 config.yaml")

    options = JobOptions(
        servers=targets,
        cfg=cfg,
        trigger="manual",
        apply=req.apply,
        force=req.force,
        style=req.style,
        resolution=req.resolution,
        library_ids=req.library_ids,
    )
    try:
        return manager.start(options)
    except RuntimeError as e:
        raise HTTPException(status_code=409, detail=str(e))


@app.get("/api/job")
def current_job() -> dict:
    return {"job": manager.snapshot()}


# ---------------------------------------------------------------- 定时任务


def _task_view(task: dict) -> dict:
    return {**task, "next_run": schedules.next_run(task)}


@app.get("/api/schedules")
def list_schedules() -> dict:
    cfg = settings.get_config()
    return {
        "tasks": [_task_view(t) for t in schedules.load(cfg)],
        "source": "file" if schedules.has_file() else "config",
    }


@app.post("/api/schedules")
def create_schedule(req: ScheduleIn) -> dict:
    cfg = settings.get_config()
    try:
        task = schedules.validate(req.model_dump(), cfg)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    tasks = schedules.load(cfg)
    tasks.append(task)
    schedules.save(tasks)
    logger.info("新增定时任务 id=%s time=%s", task["id"], task["time"])
    return _task_view(task)


@app.put("/api/schedules/{task_id}")
def update_schedule(task_id: str, req: ScheduleIn) -> dict:
    cfg = settings.get_config()
    tasks = schedules.load(cfg)
    index = next((i for i, t in enumerate(tasks) if t["id"] == task_id), None)
    if index is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    try:
        task = schedules.validate(req.model_dump(), cfg, task_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    tasks[index] = task
    schedules.save(tasks)
    logger.info("更新定时任务 id=%s time=%s", task["id"], task["time"])
    return _task_view(task)


@app.delete("/api/schedules/{task_id}")
def delete_schedule(task_id: str) -> dict:
    cfg = settings.get_config()
    tasks = schedules.load(cfg)
    remaining = [t for t in tasks if t["id"] != task_id]
    if len(remaining) == len(tasks):
        raise HTTPException(status_code=404, detail="任务不存在")
    schedules.save(remaining)
    logger.info("删除定时任务 id=%s", task_id)
    return {"ok": True}


@app.post("/api/schedules/{task_id}/run")
def run_schedule(task_id: str) -> dict:
    cfg = settings.get_config()
    task = next((t for t in schedules.load(cfg) if t["id"] == task_id), None)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    servers = schedules.task_servers(cfg, task)
    if not servers:
        raise HTTPException(status_code=400, detail="任务没有可用的服务器")
    options = JobOptions(
        servers=servers,
        cfg=cfg,
        trigger=f"manual-schedule:{task_id}",
        apply=True,
        force=bool(task.get("force")),
        style=str(task.get("style") or ""),
        resolution=str(task.get("resolution") or ""),
        library_ids=list(task.get("libraries") or []),
    )
    try:
        return manager.start(options)
    except RuntimeError as e:
        raise HTTPException(status_code=409, detail=str(e))


# ---------------------------------------------------------------- 旧设置接口


@app.get("/api/settings")
def legacy_settings() -> dict:
    cfg = settings.get_config()
    first = cfg.servers[0] if cfg.servers else None
    return {
        **cfg.defaults,
        "base_url": first.url if first else "",
        "has_key": bool(first and first.api_key),
        "schedule_enabled": bool(cfg.schedule.get("enabled")),
        "schedule_time": cfg.schedule.get("time") or "04:00",
        "servers": [s.public() for s in cfg.servers],
        "source": cfg.source,
    }


@app.put("/api/settings")
def legacy_settings_update() -> dict:
    raise HTTPException(
        status_code=410,
        detail="配置已迁移到 config.yaml，请直接修改文件后 GET /api/config/reload",
    )
