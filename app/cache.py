"""海报磁盘缓存 + 并发下载。

同一个条目（itemId+tag）只下载一次。海报的 tag 变了才重新拉，
所以每天定时生成时几乎不需要走网络。
"""
from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.compat.log import logger
from app.config import CACHE_DIR, POSTER_CACHE
from app.emby import EmbyClient

_SAFE_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _safe(value: Any, limit: int = 80) -> str:
    text = _SAFE_RE.sub("_", str(value or "")).strip("_")
    return (text or "x")[:limit]


def poster_path(server_id: str, item_id: str, tag: Optional[str], root: Optional[Path] = None) -> Path:
    base = root or CACHE_DIR
    return base / "posters" / _safe(server_id, 40) / f"{_safe(item_id, 60)}_{_safe(tag or '0', 40)}.img"


def _usable(path: Path) -> bool:
    try:
        return path.is_file() and path.stat().st_size > 1024
    except OSError:
        return False


def fetch_posters(
    client: EmbyClient,
    server_id: str,
    items: List[Dict[str, Any]],
    need: int,
    *,
    root: Optional[Path] = None,
    workers: int = 6,
) -> List[str]:
    """按顺序返回最多 need 张海报的本地路径。

    items 来自 EmbyClient.poster_items，包含 id/tag。
    """
    if need <= 0:
        return []

    seen: set[str] = set()
    ordered: List[Dict[str, Any]] = []
    for it in items:
        iid, tag = str(it.get("id") or ""), str(it.get("tag") or "")
        if not iid:
            continue
        key = f"{iid}:{tag}"
        if key in seen:
            continue
        seen.add(key)
        ordered.append({"id": iid, "tag": tag})

    if not ordered:
        return []

    # 先看缓存
    ready: Dict[str, Path] = {}
    todo: List[Dict[str, Any]] = []
    for it in ordered:
        path = poster_path(server_id, it["id"], it["tag"], root)
        if _usable(path):
            ready[f"{it['id']}:{it['tag']}"] = path
        elif POSTER_CACHE:
            todo.append({**it, "path": path})

    # 缓存够用就不下载
    ready_in_order = [ready[k] for k in (f"{it['id']}:{it['tag']}" for it in ordered) if k in ready]
    if len(ready_in_order) >= need or not POSTER_CACHE:
        return [str(p) for p in ready_in_order[:need]]

    # 并发补齐缺的
    if todo:
        todo = todo[: max(need * 2, 6)]
        logger.debug("下载海报 server=%s count=%d", server_id, len(todo))

        def work(it: Dict[str, Any]) -> bool:
            url = client.poster_url(it["id"], it.get("tag"))
            return client.download(url, str(it["path"]))

        with ThreadPoolExecutor(max_workers=max(1, min(workers, len(todo)))) as pool:
            futures = [pool.submit(work, it) for it in todo]
            for fut in as_completed(futures):
                try:
                    fut.result()
                except Exception as e:  # noqa: BLE001
                    logger.debug("海报下载异常: %s", e)

        for it in todo:
            if _usable(it["path"]):
                ready[f"{it['id']}:{it['tag']}"] = it["path"]

    paths = [ready[k] for k in (f"{it['id']}:{it['tag']}" for it in ordered) if k in ready]
    return [str(p) for p in paths[:need]]
