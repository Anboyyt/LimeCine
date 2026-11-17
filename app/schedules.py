"""定时任务：data/schedules.json（面板可编辑）。

没有这个文件时，从 config.yaml 的 schedule 段生成只读任务；
在面板保存一次后，文件成为唯一来源，config.yaml 里的 schedule 不再参与调度。
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.compat.log import logger
from app.config import DATA_DIR
from app.render import ALL_STYLES
from app.settings import AppConfig, Server, server_schedule

SCHEDULES_FILE = Path(os.getenv("LC_SCHEDULE_FILE", str(DATA_DIR / "schedules.json")))

_lock = threading.RLock()

TIME_RE = re.compile(r"(\d{1,2}):(\d{2})")


def new_id() -> str:
    return uuid.uuid4().hex[:8]


def normalize_time(value: Any, default: str = "04:00") -> str:
    m = TIME_RE.fullmatch(str(value or "").strip())
    if not m:
        return default
    hour, minute = int(m.group(1)), int(m.group(2))
    if 0 <= hour < 24 and 0 <= minute < 60:
        return f"{hour:02d}:{minute:02d}"
    return default


def _task(
    *,
    task_id: str,
    name: str,
    time: str,
    servers: Optional[List[str]] = None,
    libraries: Optional[List[str]] = None,
    style: str = "",
    resolution: str = "",
    force: bool = False,
    enabled: bool = True,
) -> Dict[str, Any]:
    return {
        "id": task_id,
        "name": name,
        "enabled": bool(enabled),
        "time": normalize_time(time),
        "servers": list(servers or []),
        "libraries": list(libraries or []),
        "style": style if style in ALL_STYLES else "",
        "resolution": resolution,
        "force": bool(force),
    }


def seed_from_config(cfg: AppConfig) -> List[Dict[str, Any]]:
    """把 config.yaml 的 schedule 段映射成任务。"""
    tasks: List[Dict[str, Any]] = []
    global_sched = cfg.schedule or {}

    explicit_ids = {
        s.id for s in cfg.servers
        if isinstance(s.schedule, dict) and "enabled" in s.schedule
    }
    if global_sched.get("enabled"):
        servers = [
            s.id for s in cfg.servers
            if s.enabled and s.id not in explicit_ids
        ]
        if servers:
            tasks.append(_task(
                task_id="default",
                name="默认定时",
                time=str(global_sched.get("time") or "04:00"),
                servers=servers,
            ))

    for server in cfg.servers:
        if not isinstance(server.schedule, dict) or not server.schedule.get("enabled"):
            continue
        sched = server_schedule(cfg, server)
        tasks.append(_task(
            task_id=f"server-{server.id}",
            name=server.name or server.id,
            time=str(sched.get("time") or "04:00"),
            servers=[server.id],
        ))
    return tasks


def has_file() -> bool:
    return SCHEDULES_FILE.is_file()


def _normalize(raw: Dict[str, Any]) -> Dict[str, Any]:
    style = str(raw.get("style") or "")
    return _task(
        task_id=str(raw.get("id") or new_id()),
        name=str(raw.get("name") or "定时任务")[:60],
        time=str(raw.get("time") or "04:00"),
        enabled=bool(raw.get("enabled", True)),
        servers=[str(x) for x in (raw.get("servers") or []) if str(x)],
        libraries=[str(x) for x in (raw.get("libraries") or []) if str(x)],
        style=style if style in ALL_STYLES else "",
        resolution=str(raw.get("resolution") or ""),
        force=bool(raw.get("force", False)),
    )


def load(cfg: AppConfig) -> List[Dict[str, Any]]:
    if not SCHEDULES_FILE.is_file():
        return seed_from_config(cfg)
    try:
        data = json.loads(SCHEDULES_FILE.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        logger.warning("读取 schedules.json 失败: %s", e)
        return []
    raw_tasks = data.get("tasks") if isinstance(data, dict) else None
    if not isinstance(raw_tasks, list):
        return []
    return [_normalize(t) for t in raw_tasks if isinstance(t, dict)]


def save(tasks: List[Dict[str, Any]]) -> None:
    SCHEDULES_FILE.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "version": 1,
        "tasks": [_normalize(t) for t in tasks],
    }
    tmp = None
    with _lock:
        try:
            fd, tmp = tempfile.mkstemp(dir=str(SCHEDULES_FILE.parent), prefix=".schedules-")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.chmod(tmp, 0o600)
            os.replace(tmp, SCHEDULES_FILE)
        except Exception as e:  # noqa: BLE001
            logger.warning("保存 schedules.json 失败: %s", e)
            if tmp and os.path.exists(tmp):
                os.remove(tmp)
            raise


def validate(raw: Dict[str, Any], cfg: AppConfig, task_id: Optional[str] = None) -> Dict[str, Any]:
    """校验并规范化前端提交的任务，失败抛 ValueError。"""
    time_value = str(raw.get("time") or "").strip()
    if not TIME_RE.fullmatch(time_value):
        raise ValueError("时间格式应为 HH:MM")
    hour, minute = (int(x) for x in time_value.split(":", 1))
    if not (0 <= hour < 24 and 0 <= minute < 60):
        raise ValueError("时间超出范围")

    style = str(raw.get("style") or "").strip()
    if style and style not in ALL_STYLES:
        raise ValueError(f"未知样式: {style}")

    known = {s.id for s in cfg.servers}
    servers = [str(x) for x in (raw.get("servers") or []) if str(x)]
    unknown = [x for x in servers if x not in known]
    if unknown:
        raise ValueError(f"未知服务器: {', '.join(unknown)}")

    return _task(
        task_id=task_id or str(raw.get("id") or new_id()),
        name=str(raw.get("name") or "定时任务")[:60],
        time=f"{hour:02d}:{minute:02d}",
        enabled=bool(raw.get("enabled", True)),
        servers=servers,
        libraries=[str(x) for x in (raw.get("libraries") or []) if str(x)],
        style=style,
        resolution=str(raw.get("resolution") or ""),
        force=bool(raw.get("force", False)),
    )


def next_run(task: Dict[str, Any], now: Optional[datetime] = None) -> str:
    """给面板显示的人类可读时间。"""
    now = now or datetime.now()
    try:
        hour, minute = (int(x) for x in str(task.get("time") or "04:00").split(":", 1))
    except ValueError:
        hour, minute = 4, 0
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    label = "今天" if target.date() == now.date() else "明天"
    return f"{label} {target.strftime('%H:%M')}"


def task_servers(cfg: AppConfig, task: Dict[str, Any]) -> List[Server]:
    wanted = set(task.get("servers") or [])
    out: List[Server] = []
    for server in cfg.enabled_servers():
        if not server.api_key:
            continue
        if wanted and server.id not in wanted:
            continue
        out.append(server)
    return out
