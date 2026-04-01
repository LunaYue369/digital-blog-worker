"""博客草稿持久化存储 — 每个商家独立 JSON 文件，线程安全

每个商家的草稿存储在单独的文件中：
    store/drafts_thouseirvine.json
    store/drafts_sorensen_hvac.json

用于去重（避免重复主题）和状态查询。
"""

import json
import logging
import threading
import time
from pathlib import Path

import config as cfg

log = logging.getLogger(__name__)

# 每个商家一把锁，互不影响
_locks: dict[str, threading.Lock] = {}
_locks_lock = threading.Lock()


def _get_lock(merchant_id: str) -> threading.Lock:
    """获取指定商家的存储锁（懒创建，线程安全）"""
    with _locks_lock:
        if merchant_id not in _locks:
            _locks[merchant_id] = threading.Lock()
        return _locks[merchant_id]


def _store_path(merchant_id: str) -> Path:
    """获取商家的草稿存储文件路径"""
    return cfg.STORE_DIR / f"drafts_{merchant_id}.json"


def _load(merchant_id: str) -> list[dict]:
    """加载指定商家的草稿列表"""
    path = _store_path(merchant_id)
    if not path.exists():
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        log.error("[%s] 草稿存储加载失败: %s", merchant_id, e)
        return []


def _save(merchant_id: str, drafts: list[dict]) -> None:
    """保存指定商家的草稿列表"""
    path = _store_path(merchant_id)
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(drafts, f, indent=2, ensure_ascii=False)
    except OSError as e:
        log.error("[%s] 草稿存储保存失败: %s", merchant_id, e)


def save_draft(
    merchant_id: str,
    title: str,
    filename: str,
    preview_url: str,
    blog_data: dict,
    review_score: int = 0,
    session_id: str = "",
) -> None:
    """保存一条博客草稿记录

    Args:
        merchant_id: 商家标识
        title: 博客标题
        filename: HTML 文件名
        preview_url: 预览链接
        blog_data: 完整博客数据（title, excerpt, tags, seo_slug 等）
        review_score: Reviewer 评分
        session_id: 生成会话 ID
    """
    record = {
        "title": title,
        "filename": filename,
        "preview_url": preview_url,
        "content_html": blog_data.get("content_html", ""),
        "excerpt": blog_data.get("excerpt", ""),
        "tags": blog_data.get("tags", []),
        "seo_slug": blog_data.get("seo_slug", ""),
        "review_score": review_score,
        "session_id": session_id,
        "status": "draft",
        "created_at": time.time(),
    }

    lock = _get_lock(merchant_id)
    with lock:
        drafts = _load(merchant_id)
        drafts.append(record)
        _save(merchant_id, drafts)

    log.info("[%s] 草稿已保存: %s (score=%d)", merchant_id, title, review_score)


def get_recent_titles(merchant_id: str, limit: int = 10) -> list[str]:
    """获取商家最近发布的博客标题（用于去重）

    Args:
        merchant_id: 商家标识
        limit: 最多返回数量

    Returns:
        标题列表（最新的在前）
    """
    lock = _get_lock(merchant_id)
    with lock:
        drafts = _load(merchant_id)

    drafts.sort(key=lambda x: x.get("created_at", 0), reverse=True)
    return [d["title"] for d in drafts[:limit]]


def get_drafts(merchant_id: str, limit: int = 20) -> list[dict]:
    """获取商家的草稿列表

    Args:
        merchant_id: 商家标识
        limit: 最多返回数量

    Returns:
        草稿记录列表（最新的在前）
    """
    lock = _get_lock(merchant_id)
    with lock:
        drafts = _load(merchant_id)

    drafts.sort(key=lambda x: x.get("created_at", 0), reverse=True)
    return drafts[:limit]
