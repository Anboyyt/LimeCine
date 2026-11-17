"""业务配置：config.yaml -> AppConfig。

查找顺序：
  1. LC_CONFIG 指定的文件
  2. /app/config.yaml（compose 挂载）
  3. <data>/config.yaml

都不存在时：
  - 有旧的 data/settings.json -> 自动迁移并写出一份 <data>/config.yaml
  - 有 LC_URL / LC_API_KEY    -> 单服务器兜底

配置里所有字符串都支持 ${ENV} 和 ${ENV:-默认值} 展开，Key 不必写进仓库。
"""
from __future__ import annotations

import os
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

import yaml

from app.compat.log import logger
from app.config import APP_NAME, CONFIG_CANDIDATES, DATA_DIR, DEFAULT_RESOLUTION, VERSION

DEFAULT_SUBTITLE = {
    "movies": "MOVIES",
    "tvshows": "TV SHOWS",
    "collections": "COLLECTIONS",
    "boxsets": "COLLECTIONS",
    "music": "MUSIC",
    "musicvideos": "MUSIC VIDEOS",
    "homevideos": "HOME VIDEOS",
    "photos": "PHOTOS",
}

DEFAULT_PARAMS: Dict[str, Any] = {
    "style": "animated_1",
    "resolution": DEFAULT_RESOLUTION,
    "zh_font_size": 170,
    "en_font_size": 80,
    "title_scale": 1.0,
    "blur_size": 50,
    "color_ratio": 0.4,
    "bg_color_mode": "auto",
    "custom_bg_color": "",
    "animation_duration": 8,
    "animation_fps": 24,
    "animation_format": "apng",
    "animation_resolution": "320x180",
    "animation_reduce_colors": False,
}

DEFAULT_SCHEDULE: Dict[str, Any] = {
    "enabled": False,
    "time": "04:00",
}

_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


@dataclass
class Server:
    id: str
    url: str
    api_key: str = ""
    name: str = ""
    enabled: bool = True
    write_back: bool = True
    schedule: Dict[str, Any] = field(default_factory=dict)

    def public(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name or self.id,
            "url": self.url,
            "has_key": bool(self.api_key),
            "enabled": self.enabled,
            "write_back": self.write_back,
        }


@dataclass
class AppConfig:
    servers: List[Server] = field(default_factory=list)
    defaults: Dict[str, Any] = field(default_factory=dict)
    schedule: Dict[str, Any] = field(default_factory=dict)
    libraries: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    source: str = ""
    error: str = ""

    def server(self, server_id: str) -> Optional[Server]:
        for s in self.servers:
            if s.id == server_id:
                return s
        return None

    def enabled_servers(self) -> List[Server]:
        return [s for s in self.servers if s.enabled]


# ---------------------------------------------------------------- 基础工具


def _expand(value: Any, env: Mapping[str, str]) -> Any:
    if isinstance(value, str):
        def repl(m: re.Match) -> str:
            name, default = m.group(1), m.group(2)
            return env.get(name, default if default is not None else "")
        return _ENV_RE.sub(repl, value)
    if isinstance(value, dict):
        return {k: _expand(v, env) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand(v, env) for v in value]
    return value


def _slug(value: Any) -> str:
    s = re.sub(r"^[A-Za-z][A-Za-z0-9+.-]*://", "", str(value or "").lower())
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return (s or "server")[:40]


def _norm_url(value: Any) -> str:
    url = str(value or "").strip().rstrip("/")
    if url and "://" not in url:
        url = "http://" + url
    return url


def _bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on", "y")


def _float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _int(value: Any, default: int) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _norm_time(value: Any, default: str) -> str:
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", str(value or "").strip())
    if not m:
        return default
    hour, minute = int(m.group(1)), int(m.group(2))
    if 0 <= hour < 24 and 0 <= minute < 60:
        return f"{hour:02d}:{minute:02d}"
    return default


def _normalize_library(value: Any) -> Dict[str, Any]:
    """标题配置支持三种写法，统一成 dict。"""
    if value is None:
        return {}
    if isinstance(value, str):
        return {"title": value}
    if isinstance(value, (list, tuple)):
        out: Dict[str, Any] = {}
        if len(value) > 0 and value[0]:
            out["title"] = str(value[0])
        if len(value) > 1 and value[1]:
            out["subtitle"] = str(value[1])
        if len(value) > 2 and value[2]:
            out["color"] = str(value[2])
        return out
    if isinstance(value, dict):
        return {str(k): v for k, v in value.items() if v is not None}
    return {}


def _normalize_params(raw: Any) -> Dict[str, Any]:
    out = dict(DEFAULT_PARAMS)
    if not isinstance(raw, dict):
        return out
    if "style" in raw and raw["style"]:
        out["style"] = str(raw["style"])
    if "resolution" in raw and raw["resolution"]:
        out["resolution"] = str(raw["resolution"])
    out["zh_font_size"] = _float(raw.get("zh_font_size"), out["zh_font_size"])
    out["en_font_size"] = _float(raw.get("en_font_size"), out["en_font_size"])
    out["title_scale"] = _float(raw.get("title_scale"), out["title_scale"])
    out["blur_size"] = _int(raw.get("blur_size"), out["blur_size"])
    out["color_ratio"] = _float(raw.get("color_ratio"), out["color_ratio"])
    if raw.get("bg_color_mode"):
        out["bg_color_mode"] = str(raw["bg_color_mode"])
    if raw.get("custom_bg_color") is not None:
        out["custom_bg_color"] = str(raw["custom_bg_color"])
    out["animation_duration"] = _int(raw.get("animation_duration"), out["animation_duration"])
    out["animation_fps"] = _int(raw.get("animation_fps"), out["animation_fps"])
    if raw.get("animation_format"):
        out["animation_format"] = str(raw["animation_format"])
    if raw.get("animation_resolution"):
        out["animation_resolution"] = str(raw["animation_resolution"])
    out["animation_reduce_colors"] = _bool(
        raw.get("animation_reduce_colors"), out["animation_reduce_colors"]
    )
    return out


# ---------------------------------------------------------------- 解析


def _parse_servers(raw: Any) -> List[Server]:
    items: List[Tuple[Any, Dict[str, Any]]] = []
    if isinstance(raw, dict):
        pairs: Iterable[Tuple[Any, Any]] = raw.items()
    elif isinstance(raw, list):
        pairs = ((None, item) for item in raw)
    else:
        pairs = []

    for key, val in pairs:
        if isinstance(val, str):
            val = {"url": val}
        if not isinstance(val, dict):
            continue
        url = _norm_url(val.get("url") or val.get("base_url"))
        if not url:
            logger.warning("忽略没有 url 的服务器配置: %s", key)
            continue
        # dict 写法的 key 就是服务器 id，name 只作显示名
        sid = str(val.get("id") or key or val.get("name") or _slug(url))
        items.append((sid, {**val, "url": url}))

    seen: Dict[str, int] = {}
    servers: List[Server] = []
    for sid, val in items:
        base = _slug(sid)
        seen[base] = seen.get(base, 0) + 1
        sid = base if seen[base] == 1 else f"{base}-{seen[base]}"
        sched = val.get("schedule")
        if isinstance(sched, str):
            sched = {"enabled": True, "time": sched}
        servers.append(Server(
            id=sid,
            url=val["url"],
            api_key=str(val.get("api_key") or val.get("key") or ""),
            name=str(val.get("name") or sid),
            enabled=_bool(val.get("enabled"), True),
            write_back=_bool(val.get("write_back"), True),
            schedule=dict(sched) if isinstance(sched, dict) else {},
        ))
    return servers


def _parse_libraries(raw: Any) -> Dict[str, Dict[str, Any]]:
    if not isinstance(raw, dict):
        return {}
    out: Dict[str, Dict[str, Any]] = {}
    for key, val in raw.items():
        key = str(key).strip()
        if not key:
            continue
        item = _normalize_library(val)
        if item:
            out[key] = item
    return out


def _parse_schedule(raw: Any, legacy: Optional[dict] = None) -> Dict[str, Any]:
    sched = dict(DEFAULT_SCHEDULE)
    if isinstance(raw, dict):
        sched["enabled"] = _bool(raw.get("enabled"), sched["enabled"])
        sched["time"] = _norm_time(raw.get("time"), sched["time"])
    if legacy:
        sched["enabled"] = _bool(legacy.get("schedule_enabled"), sched["enabled"])
        sched["time"] = _norm_time(legacy.get("schedule_time"), sched["time"])
    return sched


def _legacy_settings(path: Path) -> Optional[Dict[str, Any]]:
    if not path.is_file():
        return None
    try:
        import json
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception as e:  # noqa: BLE001
        logger.warning("读取旧 settings.json 失败: %s", e)
        return None


def _from_legacy(legacy: Dict[str, Any]) -> Tuple[List[Server], Dict[str, Any], Dict[str, Any], Dict[str, Dict[str, Any]]]:
    servers: List[Server] = []
    url = _norm_url(legacy.get("base_url"))
    if url:
        servers.append(Server(
            id="lime",
            url=url,
            api_key=str(legacy.get("api_key") or ""),
            name="lime",
        ))

    defaults = _normalize_params(legacy)

    title_text = legacy.get("title_config") or ""
    libraries: Dict[str, Dict[str, Any]] = {}
    if title_text:
        try:
            parsed = yaml.safe_load(title_text) or {}
            libraries = _parse_libraries(parsed)
        except Exception as e:  # noqa: BLE001
            logger.warning("旧 title_config 解析失败: %s", e)

    schedule = _parse_schedule(None, legacy)
    return servers, defaults, schedule, libraries


def _write_migrated(
    path: Path,
    servers: List[Server],
    defaults: Dict[str, Any],
    schedule: Dict[str, Any],
    libraries: Dict[str, Dict[str, Any]],
) -> None:
    data = {
        "servers": {
            s.id: {
                "id": s.id,
                "url": s.url,
                "api_key": s.api_key,
                "name": s.name,
                "write_back": s.write_back,
            }
            for s in servers
        },
        "defaults": defaults,
        "schedule": schedule,
        "libraries": libraries,
    }
    header = (
        "# LimeCine 配置（由旧 data/settings.json 自动迁移）\n"
        "# 可以直接手改；api_key 支持 ${ENV} 引用。\n\n"
    )
    text = header + yaml.safe_dump(data, allow_unicode=True, sort_keys=False)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        logger.info("已把旧设置迁移到 %s", path)
    except Exception as e:  # noqa: BLE001
        logger.warning("写出迁移配置失败: %s", e)


def load_config(
    path: Optional[Path] = None,
    *,
    env: Optional[Mapping[str, str]] = None,
    legacy_path: Optional[Path] = None,
    write_migrated: bool = True,
) -> AppConfig:
    """读取业务配置。path 仅用于测试或显式指定。"""
    env = env if env is not None else os.environ
    legacy_path = legacy_path or (DATA_DIR / "settings.json")

    found: Optional[Path] = None
    if path is not None:
        if path.is_file():
            found = path
        else:
            return AppConfig(error=f"配置文件不存在: {path}")
    else:
        for candidate in CONFIG_CANDIDATES:
            if candidate.is_file():
                found = candidate
                break

    if found:
        try:
            raw = yaml.safe_load(found.read_text(encoding="utf-8")) or {}
        except Exception as e:  # noqa: BLE001
            logger.error("配置文件解析失败 %s: %s", found, e)
            return AppConfig(source=str(found), error=f"配置解析失败: {e}")
        if not isinstance(raw, dict):
            return AppConfig(source=str(found), error="配置文件根节点必须是映射")
        raw = _expand(raw, env)
        servers = _parse_servers(raw.get("servers"))
        defaults = _normalize_params({
            **{k: raw.get(k) for k in DEFAULT_PARAMS if k in raw},
            **(raw.get("defaults") or {}),
        })
        schedule = _parse_schedule(raw.get("schedule"))
        libraries = _parse_libraries(raw.get("libraries"))
        source = str(found)
    else:
        legacy = _legacy_settings(legacy_path)
        if legacy:
            servers, defaults, schedule, libraries = _from_legacy(legacy)
            source = f"{legacy_path}（旧格式）"
            if write_migrated and not (DATA_DIR / "config.yaml").exists():
                _write_migrated(DATA_DIR / "config.yaml", servers, defaults, schedule, libraries)
        else:
            servers, defaults, schedule, libraries = [], dict(DEFAULT_PARAMS), dict(DEFAULT_SCHEDULE), {}
            source = ""

    # 配置文件里没写服务器时，允许用环境变量兜底
    if not servers and env.get("LC_URL"):
        url = _norm_url(env.get("LC_URL"))
        servers = [Server(id=_slug(url), url=url, api_key=env.get("LC_API_KEY", ""), name="default")]

    # 校验
    for s in servers:
        if not s.api_key:
            logger.warning("服务器 %s 没有配置 api_key", s.id)
    if not servers:
        logger.warning("没有配置任何服务器：请在 config.yaml 的 servers 段添加")

    return AppConfig(
        servers=servers,
        defaults=defaults,
        schedule=schedule,
        libraries=libraries,
        source=source,
    )


# ---------------------------------------------------------------- 匹配


def resolve_library(cfg: AppConfig, server: Server, lib: Dict[str, Any]) -> Dict[str, Any]:
    """把库 + 全局默认 + 库级覆盖合并成一次生成需要的参数。

    匹配顺序（越靠后越具体）：
      type:<CollectionType> -> <库名/库ID> -> <server>/<库名/库ID>
    """
    name = str(lib.get("name") or "")
    lib_id = str(lib.get("id") or "")
    ctype = str(lib.get("type") or "").lower()

    candidates: List[str] = []
    if ctype:
        candidates.append(f"type:{ctype}")
    if name:
        candidates.append(name)
    if lib_id:
        candidates.append(lib_id)
    if name:
        candidates.append(f"{server.id}/{name}")
    if lib_id:
        candidates.append(f"{server.id}/{lib_id}")

    merged: Dict[str, Any] = dict(cfg.defaults)
    matched = ""
    for key in candidates:
        item = cfg.libraries.get(key)
        if isinstance(item, dict) and item:
            merged.update(item)
            matched = key

    title = merged.get("title")
    title_zh = ""
    title_en = ""
    if isinstance(title, (list, tuple)):
        if len(title) > 0 and title[0]:
            title_zh = str(title[0])
        if len(title) > 1 and title[1]:
            title_en = str(title[1])
    elif title:
        title_zh = str(title)

    title_zh = title_zh or name or lib_id or "媒体库"
    title_en = str(merged.get("subtitle") or title_en or DEFAULT_SUBTITLE.get(ctype, ""))
    color = str(merged.get("color") or "").strip()

    params = _normalize_params({
        **{k: merged.get(k) for k in DEFAULT_PARAMS if k in merged},
        "style": merged.get("style") or cfg.defaults.get("style"),
        "resolution": merged.get("resolution") or cfg.defaults.get("resolution"),
    })

    return {
        "server_id": server.id,
        "library_id": lib_id,
        "library_name": name or lib_id,
        "title_zh": title_zh,
        "title_en": title_en,
        "color": color,
        "params": params,
        "write_back": _bool(merged.get("write_back"), server.write_back),
        "enabled": _bool(merged.get("enabled"), True),
        "matched": matched,
    }


def server_schedule(cfg: AppConfig, server: Server) -> Dict[str, Any]:
    sched = dict(cfg.schedule)
    if isinstance(server.schedule, dict):
        if "enabled" in server.schedule:
            sched["enabled"] = _bool(server.schedule.get("enabled"), sched["enabled"])
        if "time" in server.schedule:
            sched["time"] = _norm_time(server.schedule.get("time"), sched["time"])
    return sched


def view(cfg: AppConfig) -> Dict[str, Any]:
    """给前端的配置视图：绝不含 api_key。"""
    return {
        "name": APP_NAME,
        "version": VERSION,
        "source": cfg.source,
        "error": cfg.error,
        "servers": [s.public() for s in cfg.servers],
        "defaults": cfg.defaults,
        "schedule": cfg.schedule,
        "libraries": cfg.libraries,
    }


# ---------------------------------------------------------------- 缓存


_lock = threading.RLock()
_config: Optional[AppConfig] = None
_signature: Tuple[str, float] = ("", 0.0)


def find_config_path() -> Optional[Path]:
    for candidate in CONFIG_CANDIDATES:
        if candidate.is_file():
            return candidate
    return None


def get_config(reload: bool = False) -> AppConfig:
    """读取配置；文件 mtime 变化时自动重新加载，手改 YAML 不用重启。"""
    global _config, _signature
    with _lock:
        path = find_config_path()
        try:
            mtime = path.stat().st_mtime if path else 0.0
        except OSError:
            mtime = 0.0
        sig = (str(path) if path else "", mtime)
        if _config is None or reload or sig != _signature:
            _config = load_config()
            _signature = sig
        return _config


def reload_config() -> AppConfig:
    return get_config(reload=True)
