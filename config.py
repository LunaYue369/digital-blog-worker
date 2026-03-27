"""配置加载器 — 从 .env 读取所有配置项

所有配置通过环境变量注入，支持 .env 文件自动加载。
频道与商家的映射关系在 merchants/{id}/merchant.json 中定义。
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

# ── 项目根目录 ──────────────────────────────────────────────
BASE_DIR = Path(__file__).resolve().parent

# 加载 .env
load_dotenv(BASE_DIR / ".env")


def _env(key: str, default: str = "") -> str:
    return os.getenv(key, default)


def _env_bool(key: str, default: bool = False) -> bool:
    val = os.getenv(key, "").strip().lower()
    if not val:
        return default
    return val in ("true", "1", "yes")


def _env_int(key: str, default: int = 0) -> int:
    val = os.getenv(key, "").strip()
    if not val:
        return default
    try:
        return int(val)
    except ValueError:
        return default


# ── Slack ────────────────────────────────────────────────────
SLACK_BOT_TOKEN: str = _env("SLACK_BOT_TOKEN")
SLACK_APP_TOKEN: str = _env("SLACK_APP_TOKEN")

# ── OpenAI ───────────────────────────────────────────────────
OPENAI_API_KEY: str = _env("OPENAI_API_KEY")
BLOG_MODEL: str = _env("BLOG_MODEL", "gpt-4.1")
RESEARCH_MODEL: str = _env("RESEARCH_MODEL", "gpt-4.1-mini")

# ── 火山引擎 Seedream（AI 生图）────────────────────────────
VOLCENGINE_API_KEY: str = _env("VOLCENGINE_API_KEY")
SEEDREAM_MODEL: str = _env("SEEDREAM_MODEL", "doubao-seedream-4-5-251128")

# ── 发帖调度 ─────────────────────────────────────────────────
# 默认发帖时间点（逗号分隔，24 小时制）
DEFAULT_POST_TIMES: str = _env("POST_TIMES", "09:00,13:00,17:00")

# ── 审稿设置 ─────────────────────────────────────────────────
REVIEWER_MAX_ROUNDS: int = _env_int("REVIEWER_MAX_ROUNDS", 3)
REVIEWER_MIN_SCORE: int = _env_int("REVIEWER_MIN_SCORE", 80)

# ── 预览服务 ─────────────────────────────────────────────────
PREVIEW_HOST: str = _env("PREVIEW_HOST", "0.0.0.0")
PREVIEW_PORT: int = _env_int("PREVIEW_PORT", 8900)
PREVIEW_BASE_URL: str = _env("PREVIEW_BASE_URL", "http://localhost:8900")

# ── 热词爬取 ─────────────────────────────────────────────────
SCRAPE_MAX_ITEMS: int = _env_int("SCRAPE_MAX_ITEMS", 30)

# ── 时区 ─────────────────────────────────────────────────────
TIMEZONE: str = _env("TIMEZONE", "America/Los_Angeles")

# ── 目录 ─────────────────────────────────────────────────────
OUTPUT_DIR: Path = BASE_DIR / "output"
STORE_DIR: Path = BASE_DIR / "store"
MERCHANTS_DIR: Path = BASE_DIR / "merchants"
TEMPLATES_DIR: Path = BASE_DIR / "templates"
LOGS_DIR: Path = BASE_DIR / "logs"

# 确保目录存在
for _d in (OUTPUT_DIR, STORE_DIR, LOGS_DIR):
    _d.mkdir(parents=True, exist_ok=True)
