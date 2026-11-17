"""LimeCine 进程级运行配置（环境变量，前缀 LC_）。

业务配置（服务器、媒体库、样式覆盖、定时）全部在 config.yaml，
见 app/settings.py。这里只放容器/进程级参数。
"""
from __future__ import annotations

import os
from pathlib import Path

APP_NAME = "LimeCine"
VERSION = "0.5.0"

BASE_DIR = Path(__file__).resolve().parent.parent
FONT_DIR = Path(os.getenv("LC_FONT_DIR", str(BASE_DIR / "fonts")))

# 与 TgtoDrive / MoviePilot 保持一致：标题统一用方正综艺
ZH_FONT = os.getenv("LC_ZH_FONT", str(FONT_DIR / "fangzheng.ttf"))
EN_FONT = os.getenv("LC_EN_FONT", str(FONT_DIR / "fangzheng.ttf"))

DEFAULT_RESOLUTION = os.getenv("LC_RESOLUTION", "1080p")
LISTEN_PORT = int(os.getenv("LC_PORT", "887") or 887)

DATA_DIR = Path(os.getenv("LC_DATA_DIR", "/app/data"))
CACHE_DIR = Path(os.getenv("LC_CACHE_DIR", str(DATA_DIR / "cache")))
STATE_FILE = DATA_DIR / "state.json"

# 配置文件查找顺序：LC_CONFIG > 项目根 config.yaml > 数据目录 config.yaml
_candidates = []
if os.getenv("LC_CONFIG"):
    _candidates.append(Path(os.environ["LC_CONFIG"]))
_candidates.extend([BASE_DIR / "config.yaml", DATA_DIR / "config.yaml"])
CONFIG_CANDIDATES = _candidates

# 可选：给 /api/* 加共享令牌。对外暴露时一定要设。
ADMIN_TOKEN = os.getenv("LC_TOKEN", "").strip()

# 批量并发。单张封面峰值约 275MB，默认串行最稳。
CONCURRENCY = max(1, int(os.getenv("LC_CONCURRENCY", "1") or 1))

# 海报缓存目录；0 表示不缓存
POSTER_CACHE = os.getenv("LC_POSTER_CACHE", "1").strip().lower() not in ("0", "false", "no", "off")
