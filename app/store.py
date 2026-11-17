"""运行状态持久化（data/state.json）。

只存「跑过没有」和「上次生成的指纹」，不存任何配置和 Key。
配置在 config.yaml，见 app/settings.py。
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Any, Dict, Optional

from app.compat.log import logger
from app.config import STATE_FILE

DEFAULTS: Dict[str, Any] = {
    "last_runs": {},      # server_id -> YYYY-MM-DD
    "fingerprints": {},   # "server/library" -> {"hash": ..., "at": ..., "applied": true}
    "last_job": None,
}

_lock = threading.RLock()
_state: Optional[Dict[str, Any]] = None


def load() -> Dict[str, Any]:
    global _state
    with _lock:
        if _state is not None:
            return _state
        data = {k: (v.copy() if isinstance(v, dict) else v) for k, v in DEFAULTS.items()}
        try:
            if STATE_FILE.is_file():
                loaded = json.loads(STATE_FILE.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    for key in DEFAULTS:
                        if key in loaded:
                            data[key] = loaded[key]
        except Exception as e:  # noqa: BLE001
            logger.warning("读取 state.json 失败: %s", e)
        _state = data
        return _state


def save(data: Dict[str, Any]) -> None:
    global _state
    with _lock:
        merged = {k: (data.get(k) if data.get(k) is not None else DEFAULTS[k]) for k in DEFAULTS}
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = None
        try:
            fd, tmp = tempfile.mkstemp(dir=str(STATE_FILE.parent), prefix=".state-")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(merged, f, ensure_ascii=False, indent=2)
            os.chmod(tmp, 0o600)
            os.replace(tmp, STATE_FILE)
            _state = merged
        except Exception as e:  # noqa: BLE001
            logger.warning("保存 state.json 失败: %s", e)
            if tmp and os.path.exists(tmp):
                os.remove(tmp)
            raise


def get_last_run(server_id: str) -> str:
    return str(load().get("last_runs", {}).get(server_id, "") or "")


def set_last_run(server_id: str, day: str) -> None:
    with _lock:
        data = load()
        runs = dict(data.get("last_runs") or {})
        runs[server_id] = day
        data["last_runs"] = runs
        save(data)


def key(server_id: str, library_id: str) -> str:
    return f"{server_id}/{library_id}"


def get_fingerprint(server_id: str, library_id: str) -> Optional[Dict[str, Any]]:
    entry = load().get("fingerprints", {}).get(key(server_id, library_id))
    return entry if isinstance(entry, dict) else None


def set_fingerprint(server_id: str, library_id: str, value: Dict[str, Any]) -> None:
    with _lock:
        data = load()
        fps = dict(data.get("fingerprints") or {})
        fps[key(server_id, library_id)] = value
        # 防止长期运行无限增长：只保留最近 5000 条
        if len(fps) > 5000:
            ordered = sorted(fps.items(), key=lambda kv: str(kv[1].get("at", "")), reverse=True)
            fps = dict(ordered[:5000])
        data["fingerprints"] = fps
        save(data)


def set_last_job(job: Dict[str, Any]) -> None:
    with _lock:
        data = load()
        data["last_job"] = job
        save(data)


def clear() -> None:
    global _state
    with _lock:
        _state = None
        try:
            STATE_FILE.unlink(missing_ok=True)
        except OSError:
            pass
