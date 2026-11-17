"""单库生成的核心逻辑，HTTP 层和批量任务共用。

流程：取库内海报 -> 指纹判断是否可跳过 -> 缓存/并发下载 -> 渲染 -> 写回。
"""
from __future__ import annotations

import gc
import hashlib
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from app import store
from app.cache import fetch_posters
from app.compat.log import logger
from app.config import EN_FONT, ZH_FONT
from app.emby import EmbyClient
from app.render import (
    ANIMATED_STYLES,
    REQUIRED_IMAGES,
    SINGLE_IMAGE_STYLES,
    RenderError,
    render_cover,
)
from app.settings import DEFAULT_PARAMS, Server


@dataclass
class GenerateResult:
    data: Optional[bytes] = None
    ext: str = ".png"
    fingerprint: str = ""
    skipped: bool = False
    seconds: float = 0.0
    meta: Dict[str, Any] = field(default_factory=dict)


def fonts() -> Tuple[str, str]:
    for p in (ZH_FONT, EN_FONT):
        if not Path(p).is_file():
            raise RenderError(f"字体文件不存在: {p}")
    return ZH_FONT, EN_FONT


def build_params(style: str, resolution: str, overrides: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """把样式/分辨率和覆盖项合并成完整渲染参数。"""
    params = dict(DEFAULT_PARAMS)
    params.update(overrides or {})
    params["style"] = style
    params["resolution"] = resolution
    return params


def fingerprint(
    server: Server,
    library_id: str,
    style: str,
    resolution: str,
    title_zh: str,
    title_en: str,
    color: str,
    params: Dict[str, Any],
    items: List[Dict[str, Any]],
) -> str:
    payload = {
        "v": 1,
        "server": server.url,
        "library": library_id,
        "style": style,
        "resolution": resolution,
        "title": [title_zh, title_en],
        "color": color,
        "params": params,
        "posters": [[it.get("id"), it.get("tag")] for it in items],
    }
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def generate_one(
    *,
    server: Server,
    library_id: str,
    library_name: str = "",
    style: str = "animated_1",
    resolution: str = "1080p",
    title_zh: str = "",
    title_en: str = "",
    color: str = "",
    apply: bool = False,
    force: bool = False,
    params: Optional[Dict[str, Any]] = None,
) -> GenerateResult:
    t0 = time.time()
    style = (style or "animated_1").lower()
    params = build_params(style, resolution, params)

    want = 1 if style in SINGLE_IMAGE_STYLES else REQUIRED_IMAGES.get(style, 9)
    fetch_need = max(want, 3)

    zh_font, en_font = fonts()
    with EmbyClient(server.url, server.api_key) as client:
        items = client.poster_items(library_id, limit=max(fetch_need * 2, 8))
        fp = fingerprint(
            server, library_id, style, resolution,
            title_zh, title_en, color, params, items,
        )

        if apply and not force and items:
            entry = store.get_fingerprint(server.id, library_id)
            if entry and entry.get("hash") == fp and entry.get("applied"):
                logger.info(
                    "跳过未变化的库 server=%s library=%s", server.id, library_name or library_id
                )
                return GenerateResult(
                    fingerprint=fp,
                    skipped=True,
                    seconds=round(time.time() - t0, 1),
                    meta={"posters": len(items)},
                )

        images = fetch_posters(client, server.id, items, fetch_need)
        if not images:
            raise RenderError("该库没有可用海报（先刮削出 Primary 图片）")
        if style in SINGLE_IMAGE_STYLES:
            images = images[:1]

        bg_mode = str(params.get("bg_color_mode") or "auto")
        custom_bg = str(params.get("custom_bg_color") or "") or None
        if color and (bg_mode == "auto" or not custom_bg):
            bg_mode = "custom"
            custom_bg = color

        data, ext = render_cover(
            style,
            images,
            title_zh or library_name or "媒体库",
            title_en or "",
            zh_font=zh_font,
            en_font=en_font,
            resolution=resolution,
            zh_font_size=float(params.get("zh_font_size") or 170),
            en_font_size=float(params.get("en_font_size") or 80),
            title_scale=float(params.get("title_scale") or 1.0),
            blur_size=int(params.get("blur_size") or 50),
            color_ratio=float(params.get("color_ratio") or 0.4),
            bg_mode=bg_mode,
            custom_bg_color=custom_bg,
            animation_format=str(params.get("animation_format") or "apng"),
            animation_fps=int(params.get("animation_fps") or 24),
            animation_duration=int(params.get("animation_duration") or 8),
            animation_resolution=str(params.get("animation_resolution") or "320x180"),
            animation_reduce_colors=bool(params.get("animation_reduce_colors")),
        )

        if apply:
            mime = "image/apng" if ext == ".apng" else "image/png"
            client.upload_cover(library_id, data, mime)
            store.set_fingerprint(server.id, library_id, {
                "hash": fp,
                "at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "applied": True,
                "bytes": len(data),
                "style": style,
            })
            logger.info(
                "封面已更新 server=%s library=%s style=%s bytes=%d %.1fs",
                server.id, library_name or library_id, style, len(data), time.time() - t0,
            )

    return GenerateResult(
        data=data,
        ext=ext,
        fingerprint=fp,
        skipped=False,
        seconds=round(time.time() - t0, 1),
        meta={"posters": len(images), "animated": style in ANIMATED_STYLES},
    )


def run_and_collect(**kwargs: Any) -> GenerateResult:
    """批量任务用：确保 PIL 大图及时回收。"""
    try:
        return generate_one(**kwargs)
    finally:
        gc.collect()
