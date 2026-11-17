"""封面渲染编排。

样式实现来自 MoviePilot 插件 mediacovergenerator（GPL-3.0），
本文件只负责准备输入、调用样式函数、返回图片字节。
"""
from __future__ import annotations

import base64
import shutil
import tempfile
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

from app.compat.log import logger
from app.styles.style_animated_1 import create_style_animated_1
from app.styles.style_animated_2 import create_style_animated_2
from app.styles.style_animated_3 import create_style_animated_3
from app.styles.style_animated_4 import create_style_animated_4
from app.styles.style_static_1 import create_style_static_1
from app.styles.style_static_2 import create_style_static_2
from app.styles.style_static_3 import create_style_static_3
from app.styles.style_static_4 import create_style_static_4
from app.utils.image_manager import ResolutionConfig

# 单图样式直接用第一张海报；其余样式把图片摆进目录。
SINGLE_IMAGE_STYLES = {"static_1", "static_2", "static_4"}
ANIMATED_STYLES = {"animated_1", "animated_2", "animated_3", "animated_4"}

# 各样式需要的图片数量
REQUIRED_IMAGES = {
    "static_3": 9,
    "animated_1": 5,
    "animated_2": 9,
    "animated_3": 9,
    "animated_4": 5,
}

ALL_STYLES = [
    "static_1", "static_2", "static_3", "static_4",
    "animated_1", "animated_2", "animated_3", "animated_4",
]


class RenderError(Exception):
    pass


def _resolution(name: str) -> ResolutionConfig:
    if not name:
        return ResolutionConfig("1080p")
    if "x" in name.lower():
        try:
            w, h = name.lower().split("x", 1)
            return ResolutionConfig((int(w), int(h)))
        except ValueError:
            pass
    return ResolutionConfig(name)


def _decode(data) -> bytes:
    if not data:
        raise RenderError("renderer returned empty result")
    if isinstance(data, bytes):
        return data
    if isinstance(data, str):
        return base64.b64decode(data)
    raise RenderError(f"unexpected renderer result type: {type(data).__name__}")


def _prepare_dir(images: Sequence[str], need: int, workdir: Path) -> Path:
    """把图片复制成 1.jpg..N.jpg；图不够时循环复用。"""
    if not images:
        raise RenderError("no images to render")
    d = workdir / "images"
    d.mkdir(parents=True, exist_ok=True)
    for i in range(need):
        src = images[i % len(images)]
        shutil.copyfile(src, d / f"{i + 1}.jpg")
    return d


def render_cover(
    style: str,
    images: Sequence[str],
    title_zh: str,
    title_en: str = "",
    *,
    zh_font: str,
    en_font: str,
    resolution: str = "1080p",
    zh_font_size: float = 170,
    en_font_size: float = 75,
    title_scale: float = 1.0,
    zh_font_offset: float = 0,
    title_spacing: float = 40,
    en_line_spacing: float = 40,
    blur_size: int = 50,
    color_ratio: float = 0.8,
    bg_mode: str = "auto",
    custom_bg_color: Optional[str] = None,
    animation_format: str = "apng",
    animation_fps: int = 15,
    animation_duration: int = 10,
    animation_resolution: str = "320x180",
    animation_reduce_colors: bool = False,
) -> Tuple[bytes, str]:
    """渲染一张库封面，返回 (图片字节, 扩展名)。"""
    style = (style or "static_1").lower()
    if style not in ALL_STYLES:
        raise RenderError(f"unknown style: {style}")
    if not images:
        raise RenderError("no images to render")

    rc = _resolution(resolution)

    if style in ANIMATED_STYLES:
        zh_size = float(zh_font_size) * title_scale
        en_size = float(en_font_size) * title_scale
    else:
        zh_size = rc.get_font_size(int(zh_font_size)) * title_scale
        en_size = rc.get_font_size(int(en_font_size)) * title_scale

    font_path = (str(zh_font), str(en_font))
    font_size = (float(zh_size), float(en_size))
    font_offset = (float(zh_font_offset), float(title_spacing), float(en_line_spacing))
    title = (title_zh or "", title_en or "")
    bg_color_config = {
        "mode": bg_mode or "auto",
        "custom_color": custom_bg_color,
        "config_color": None,
    }

    logger.info("render style=%s resolution=%s images=%d", style, rc, len(images))

    if style in SINGLE_IMAGE_STYLES:
        fn = {
            "static_1": create_style_static_1,
            "static_2": create_style_static_2,
            "static_4": create_style_static_4,
        }[style]
        data = fn(
            images[0], title, font_path,
            font_size=font_size,
            font_offset=font_offset,
            blur_size=blur_size,
            color_ratio=color_ratio,
            resolution_config=rc,
            bg_color_config=bg_color_config,
        )
        return _decode(data), ".png"

    need = REQUIRED_IMAGES.get(style, 9)
    with tempfile.TemporaryDirectory(prefix="lime-cover-") as tmp:
        workdir = Path(tmp)
        lib_dir = _prepare_dir(images, need, workdir)

        if style == "static_3":
            data = create_style_static_3(
                lib_dir, title, font_path,
                font_size=font_size,
                font_offset=font_offset,
                is_blur=False,
                blur_size=blur_size,
                color_ratio=color_ratio,
                resolution_config=rc,
                bg_color_config=bg_color_config,
            )
            return _decode(data), ".png"

        if style == "animated_3":
            data = create_style_animated_3(
                lib_dir, title, font_path,
                font_size=font_size,
                font_offset=font_offset,
                is_blur=False,
                blur_size=blur_size,
                color_ratio=color_ratio,
                resolution_config=rc,
                bg_color_config=bg_color_config,
                animation_duration=animation_duration,
                animation_fps=animation_fps,
                animation_format=animation_format,
                animation_resolution=animation_resolution,
                animation_reduce_colors="strong" if animation_reduce_colors else "off",
            )
        elif style == "animated_1":
            data = create_style_animated_1(
                lib_dir, title, font_path,
                font_size=font_size,
                font_offset=font_offset,
                is_blur=False,
                blur_size=blur_size,
                color_ratio=color_ratio,
                resolution_config=rc,
                bg_color_config=bg_color_config,
                animation_duration=animation_duration,
                animation_fps=animation_fps,
                animation_format=animation_format,
                animation_resolution=animation_resolution,
                animation_reduce_colors="strong" if animation_reduce_colors else "off",
                image_count=need,
            )
        elif style == "animated_2":
            data = create_style_animated_2(
                lib_dir, title, font_path,
                font_size=font_size,
                font_offset=font_offset,
                is_blur=False,
                blur_size=blur_size,
                color_ratio=color_ratio,
                resolution_config=rc,
                bg_color_config=bg_color_config,
                animation_duration=animation_duration,
                animation_fps=animation_fps,
                animation_format=animation_format,
                animation_resolution=animation_resolution,
                animation_reduce_colors="strong" if animation_reduce_colors else "off",
                image_count=need,
            )
        else:  # animated_4
            data = create_style_animated_4(
                lib_dir, title, font_path,
                font_size=font_size,
                font_offset=font_offset,
                is_blur=False,
                blur_size=blur_size,
                color_ratio=color_ratio,
                resolution_config=rc,
                bg_color_config=bg_color_config,
                animation_duration=animation_duration,
                animation_fps=animation_fps,
                animation_format=animation_format,
                animation_resolution=animation_resolution,
                animation_reduce_colors="strong" if animation_reduce_colors else "off",
                image_count=need,
            )
        return _decode(data), ".apng"
