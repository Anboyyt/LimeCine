"""批量生成任务 + 每日定时调度。

一次任务可以覆盖多台服务器；默认串行（单张封面峰值约 275MB），
LC_CONCURRENCY 可调。同一时刻只允许一个批量任务。
"""
from __future__ import annotations

import gc
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

from app import schedules, settings, store
from app.compat.log import logger
from app.config import CONCURRENCY
from app.emby import EmbyClient
from app.service import run_and_collect
from app.settings import AppConfig, Server


@dataclass
class JobOptions:
    servers: List[Server]
    cfg: AppConfig
    trigger: str = "manual"
    apply: bool = True
    force: bool = False
    style: str = ""
    resolution: str = ""
    library_ids: List[str] = field(default_factory=list)


def _matches_library(only: List[str], server: Server, lib: Dict[str, Any]) -> bool:
    if not only:
        return True
    lid = str(lib.get("id") or "")
    name = str(lib.get("name") or "")
    candidates = {lid, name, f"{server.id}/{lid}", f"{server.id}/{name}"}
    return bool(candidates & set(only))


@dataclass
class Task:
    server: Server
    library_id: str
    library_name: str
    plan: Dict[str, Any]
    style: str
    resolution: str


class JobManager:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._job: Optional[Dict[str, Any]] = None
        last = store.load().get("last_job")
        if isinstance(last, dict):
            last = dict(last)
            last["running"] = False
            self._job = last

    def snapshot(self) -> Optional[Dict[str, Any]]:
        with self._lock:
            if not self._job:
                return None
            job = dict(self._job)
            job["items"] = list(self._job.get("items") or [])
            return job

    def running(self) -> bool:
        with self._lock:
            return bool(self._job and self._job.get("running"))

    def start(self, options: JobOptions) -> Dict[str, Any]:
        with self._lock:
            if self._job and self._job.get("running"):
                raise RuntimeError("已有批量任务在跑")
            job: Dict[str, Any] = {
                "id": uuid.uuid4().hex[:12],
                "running": True,
                "trigger": options.trigger,
                "started_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "finished_at": None,
                "total": 0,
                "done": 0,
                "ok": 0,
                "failed": 0,
                "skipped": 0,
                "current": "",
                "servers": [s.id for s in options.servers],
                "items": [],
                "errors": [],
            }
            self._job = job
        threading.Thread(target=self._run, args=(job, options), daemon=True).start()
        return self.snapshot() or {}

    # ---- 状态 ----
    def _update(self, job: Dict[str, Any], **kw: Any) -> None:
        with self._lock:
            job.update(kw)

    def _append(self, job: Dict[str, Any], item: Dict[str, Any]) -> None:
        with self._lock:
            job["items"].append(item)

    def _inc(self, job: Dict[str, Any], **counters: int) -> None:
        with self._lock:
            for name, delta in counters.items():
                job[name] = int(job.get(name, 0) or 0) + delta

    def _finish(self, job: Dict[str, Any], **kw: Any) -> None:
        with self._lock:
            job.update(kw)
            job["running"] = False
            job["finished_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            job["current"] = ""
            snap = dict(job)
            snap["items"] = list(job.get("items") or [])
        try:
            store.set_last_job(snap)
        except Exception:  # noqa: BLE001
            pass

    # ---- 执行 ----
    def _run(self, job: Dict[str, Any], options: JobOptions) -> None:
        tasks: List[Task] = []
        server_errors: List[Dict[str, Any]] = []

        for server in options.servers:
            if not server.api_key:
                server_errors.append({
                    "server": server.id, "server_name": server.name or server.id,
                    "library_id": "", "name": "（整台服务器）", "ok": False, "skipped": False,
                    "bytes": 0, "seconds": 0, "error": "没有配置 api_key",
                })
                continue
            try:
                with EmbyClient(server.url, server.api_key, timeout=20) as client:
                    libs = client.libraries()
            except Exception as e:  # noqa: BLE001
                logger.warning("取库列表失败 server=%s: %s", server.id, e)
                server_errors.append({
                    "server": server.id, "server_name": server.name or server.id,
                    "library_id": "", "name": "（整台服务器）", "ok": False, "skipped": False,
                    "bytes": 0, "seconds": 0, "error": str(e)[:300],
                })
                continue

            for lib in libs:
                if not _matches_library(options.library_ids, server, lib):
                    continue
                plan = settings.resolve_library(options.cfg, server, lib)
                if not plan["enabled"]:
                    logger.info("跳过已禁用库 server=%s library=%s", server.id, plan["library_name"])
                    continue
                style = options.style or str(plan["params"].get("style") or "animated_1")
                resolution = options.resolution or str(plan["params"].get("resolution") or "1080p")
                tasks.append(Task(
                    server=server,
                    library_id=plan["library_id"],
                    library_name=plan["library_name"],
                    plan=plan,
                    style=style,
                    resolution=resolution,
                ))

        self._update(job, total=len(tasks) + len(server_errors))
        for item in server_errors:
            self._append(job, item)
            self._inc(job, failed=1, done=1)

        logger.info(
            "批量封面开始 trigger=%s servers=%s libraries=%d errors=%d style=%s concurrency=%d",
            options.trigger, [s.id for s in options.servers], len(tasks), len(server_errors),
            options.style or "per-library", CONCURRENCY,
        )

        if not tasks:
            self._finish(job, current="")
            return

        if CONCURRENCY <= 1:
            for task in tasks:
                self._run_task(job, task, options)
                gc.collect()
        else:
            with ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
                futures = [pool.submit(self._run_task, job, task, options) for task in tasks]
                for fut in as_completed(futures):
                    try:
                        fut.result()
                    except Exception as e:  # noqa: BLE001
                        logger.warning("批量任务线程异常: %s", e)

        self._finish(job)
        logger.info(
            "批量封面结束 ok=%d failed=%d skipped=%d",
            job["ok"], job["failed"], job["skipped"],
        )

    def _run_task(self, job: Dict[str, Any], task: Task, options: JobOptions) -> None:
        plan = task.plan
        label = f"{task.server.name or task.server.id} / {task.library_name}"
        self._update(job, current=label)
        t0 = time.time()
        applied = bool(options.apply and plan["write_back"])
        try:
            result = run_and_collect(
                server=task.server,
                library_id=task.library_id,
                library_name=task.library_name,
                style=task.style,
                resolution=task.resolution,
                title_zh=plan["title_zh"],
                title_en=plan["title_en"],
                color=plan["color"],
                apply=applied,
                force=options.force,
                params=plan["params"],
            )
            item = {
                "server": task.server.id,
                "server_name": task.server.name or task.server.id,
                "library_id": task.library_id,
                "name": task.library_name,
                "ok": True,
                "applied": applied,
                "skipped": result.skipped,
                "bytes": len(result.data or b""),
                "seconds": result.seconds or round(time.time() - t0, 1),
                "error": "",
            }
            self._append(job, item)
            if result.skipped:
                self._inc(job, skipped=1)
            else:
                self._inc(job, ok=1)
        except Exception as e:  # noqa: BLE001
            logger.warning("封面生成失败 %s: %s", label, e)
            self._append(job, {
                "server": task.server.id,
                "server_name": task.server.name or task.server.id,
                "library_id": task.library_id,
                "name": task.library_name,
                "ok": False,
                "skipped": False,
                "bytes": 0,
                "seconds": round(time.time() - t0, 1),
                "error": str(e)[:300],
            })
            self._inc(job, failed=1)
        finally:
            self._inc(job, done=1)


# ---------------------------------------------------------------- 定时


def start_scheduler(manager: JobManager) -> None:
    threading.Thread(target=_scheduler_loop, args=(manager,), daemon=True).start()
    logger.info("定时任务已启动")


SCHEDULE_GRACE_MINUTES = 30


def _in_window(target: str, now: datetime) -> bool:
    """到点后 30 分钟内都算，容器重启/任务排队后还能补跑。"""
    try:
        hour, minute = (int(x) for x in str(target).split(":", 1))
    except ValueError:
        return False
    delta = (now.hour * 60 + now.minute) - (hour * 60 + minute)
    return 0 <= delta <= SCHEDULE_GRACE_MINUTES


def _scheduler_loop(manager: JobManager) -> None:
    while True:
        time.sleep(30)
        try:
            cfg = settings.get_config()
            tasks = schedules.load(cfg)
            now = datetime.now()
            today = now.strftime("%Y-%m-%d")

            for task in tasks:
                if not task.get("enabled"):
                    continue
                if not _in_window(str(task.get("time") or ""), now):
                    continue
                if store.get_last_run(f"task:{task['id']}") == today:
                    continue

                servers = schedules.task_servers(cfg, task)
                if not servers:
                    continue
                if manager.running():
                    logger.info("定时任务等待：已有批量任务在跑 task=%s", task["id"])
                    break

                options = JobOptions(
                    servers=servers,
                    cfg=cfg,
                    trigger=f"schedule:{task['id']}",
                    apply=True,
                    force=bool(task.get("force")),
                    style=str(task.get("style") or ""),
                    resolution=str(task.get("resolution") or ""),
                    library_ids=list(task.get("libraries") or []),
                )
                manager.start(options)
                store.set_last_run(f"task:{task['id']}", today)
                logger.info(
                    "定时任务已启动 task=%s servers=%s libraries=%s",
                    task["id"], [s.id for s in servers], len(task.get("libraries") or []),
                )
                # 一次只起一个任务，其余到点任务下一轮继续
                break
        except Exception as e:  # noqa: BLE001
            logger.warning("定时循环异常: %s", e)
