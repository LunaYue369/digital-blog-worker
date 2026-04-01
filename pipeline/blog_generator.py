"""博客生成流水线 — 编排各 Agent 协作生成完整 SEO 博客

完整流程：
1. Researcher 爬取热词 → 选出 TOP N 主题
2. Copywriter 根据主题撰写 HTML 博客
3. Reviewer 审核（最多 3 轮，不合格则打回重写）
4. Artist 美化图片 prompt
5. Seedream 生成配图
6. 组装 HTML → 保存文件 → 返回预览链接

线程安全：
- 每个商家有独立的生成锁，同一商家同一时刻只允许一个生成流水线运行
- 防止用户连续发 auto 指令或定时任务与手动指令并发导致主题重复
"""

import logging
import threading
import time
import uuid
from pathlib import Path

import config as cfg
from agents.researcher import analyze_and_pick_topics
from agents.copywriter import write_blog, rewrite_blog
from agents.reviewer import review_blog
from agents.artist import enhance_image_prompts
from core.merchant_config import get_seed_keywords
from pipeline.trend_scraper import scrape_trending
from pipeline.preview_server import render_blog_html
from pipeline.web_researcher import research_topic
from services.template_selector import pick_template_and_layout
from services.seedream_client import SeedreamClient
from services.usage_tracker import record_usage, format_usage_report, save_to_disk, set_current_session
from services.wordpress_publisher import WordPressPublisher
from store.blog_store import save_draft, get_recent_titles

log = logging.getLogger(__name__)

# ── 商家级生成锁 — 防止同一商家并发生成导致主题重复或数据竞争 ──
_merchant_locks: dict[str, threading.Lock] = {}
_locks_lock = threading.Lock()  # 保护 _merchant_locks 字典本身


def _get_merchant_lock(merchant_id: str) -> threading.Lock:
    """获取指定商家的生成锁（懒创建，线程安全）"""
    with _locks_lock:
        if merchant_id not in _merchant_locks:
            _merchant_locks[merchant_id] = threading.Lock()
        return _merchant_locks[merchant_id]


# ── 内部生成逻辑（不加锁，由外部调用方负责加锁）──────────────

def _generate_single_inner(
    merchant_id: str,
    merchant_cfg: dict,
    session_id: str,
    topic_index: int = 0,
    pre_scraped_topics: list[dict] | None = None,
    exclude_template: str | None = None,
    exclude_layout: str | None = None,
    progress_cb=None,
    auto_publish: bool = True,
) -> dict:
    """单篇博客生成的核心逻辑（不加锁）
    此函数不持有任何锁，由 generate_single_blog / generate_multiple_blogs 负责加锁后调用。
    在 auto n指令里的generate multiple blogs里被使用
    也在auto on指令里定时发送一篇blog的generate single blog里被使用
    exclude_template: 上一篇用的模板文件名，批量生成时避免连续相同
    exclude_layout: 上一篇用的布局名称，批量生成时避免连续相同
    """
    output_dir = Path(merchant_cfg.get("output_dir", str(cfg.OUTPUT_DIR / merchant_id)))
    output_dir.mkdir(parents=True, exist_ok=True)

    # 设置当前线程的 session_id，所有 agent 的 record_usage 自动关联
    set_current_session(session_id)

    # 计时器
    _t_start = time.time()
    _t_stages: dict[str, float] = {}
    _t_last = _t_start

    # 进度回调辅助（同时记录每阶段耗时）
    def _progress(stage: str, extra: str = ""):
        nonlocal _t_last
        now = time.time()
        if _t_last != _t_start:
            # 记录上一个阶段的耗时
            pass
        _t_stages[stage] = now
        _t_last = now
        if progress_cb:
            try:
                progress_cb(stage, extra)
            except Exception:
                pass

    try:
        # ── Step 1: 获取候选主题 ──────────────────────────────
        if pre_scraped_topics:
            topics = pre_scraped_topics
            log.info("[%s] 使用预爬取的 %d 个候选主题", merchant_id, len(topics))
        else:
            _progress("scrape")
            log.info("[%s] Step 1: 爬取热词...", merchant_id)
            seed_keywords = get_seed_keywords(merchant_id)
            trending_data = scrape_trending(seed_keywords, max_total=cfg.SCRAPE_MAX_ITEMS)
            log.info("[%s] 爬取到 %d 条热词数据", merchant_id, len(trending_data))

            _progress("research")
            recent_titles = get_recent_titles(merchant_id)
            topics, _ = analyze_and_pick_topics(
                merchant_id, trending_data, recent_titles, count=max(3, topic_index + 1)
            )

        if not topics or topic_index >= len(topics):
            return {
                "success": False, "error": "No suitable topics found",
                "session_id": session_id, "title": "", "preview_url": "",
            }

        chosen_topic = topics[topic_index]
        topic_title = chosen_topic.get("title", "")
        log.info("[%s] Step 1 完成 — 选题: %s", merchant_id, topic_title)

        # ── Step 1.5: 选择 HTML 模板 + 内容布局风格 ─────────────
        _progress("template", f"Topic: {topic_title}")
        style_choice = pick_template_and_layout(
            merchant_id,
            merchant_cfg=merchant_cfg,
            exclude_template=exclude_template,
            exclude_layout=exclude_layout,
        )
        log.info("[%s] 风格选择 — 模板: %s | 布局: %s",
                 merchant_id, style_choice["template_name"], style_choice["layout_label"])

        # ── Step 1.8: Web Research — 搜索竞品文章提取要点 ────
        _progress("web", f"Template: {style_choice['template_name']} | Layout: {style_choice['layout_label']}")
        log.info("[%s] Step 1.8: Web Research 调研竞品...", merchant_id)
        research_brief = research_topic(merchant_id, chosen_topic)
        if research_brief:
            log.info("[%s] Step 1.8 完成 — 调研摘要 %d 字符", merchant_id, len(research_brief))
        else:
            log.info("[%s] Step 1.8 跳过 — 无调研数据，继续生成", merchant_id)

        # ── Step 2: Copywriter 撰写博客 ──────────────────────
        _progress("write", f"Topic: {topic_title}")
        log.info("[%s] Step 2: Copywriter 撰写博客...", merchant_id)
        blog_data, _ = write_blog(
            merchant_id, chosen_topic,
            layout_prompt=style_choice["layout_prompt"],
            research_brief=research_brief,
        )
        log.info("[%s] Step 2 完成 — '%s'", merchant_id, blog_data.get("title", ""))

        # ── Step 3: Reviewer 审核（最多 N 轮）────────────────
        review_score = 0
        review_rounds = 0
        max_rounds = cfg.REVIEWER_MAX_ROUNDS
        min_score = cfg.REVIEWER_MIN_SCORE

        for round_num in range(1, max_rounds + 1):
            _progress("review", f"Review round {round_num}/{max_rounds}")
            log.info("[%s] Step 3: Reviewer 审核 (round %d/%d)...", merchant_id, round_num, max_rounds)
            feedback, _ = review_blog(merchant_id, blog_data, chosen_topic, round_num)
            review_score = feedback.get("score", 0)
            review_rounds = round_num

            if feedback.get("passed", False) and review_score >= min_score:
                log.info("[%s] Step 3 通过 — 评分 %d/100 (round %d)", merchant_id, review_score, round_num)
                break

            if round_num < max_rounds:
                _progress("rewrite", f"Score {review_score}/100 — revising (round {round_num})")
                log.info("[%s] Step 3 未通过 (%d/100)，打回重写...", merchant_id, review_score)
                blog_data, _ = rewrite_blog(merchant_id, blog_data, feedback, round_num)
            else:
                log.warning("[%s] Step 3 最终轮仍未通过 (%d/100)，使用当前版本", merchant_id, review_score)

        # ── Step 4: Artist 美化图片 prompt ───────────────────
        _progress("artist", f"Score: {review_score}/100")
        log.info("[%s] Step 4: Artist 美化图片 prompt...", merchant_id)
        raw_image_prompts = blog_data.get("image_prompts", {})
        if isinstance(raw_image_prompts, dict) and raw_image_prompts:
            enhanced_prompts, _ = enhance_image_prompts(
                merchant_id, raw_image_prompts,
                blog_data.get("title", ""), blog_data.get("excerpt", ""),
            )
        else:
            enhanced_prompts = {
                "hero": f"Professional high-quality image related to {chosen_topic.get('primary_keyword', '')}",
                "mid": f"Detail shot illustrating {chosen_topic.get('primary_keyword', '')}",
                "end": "Professional business environment, trust and reliability",
            }
        log.info("[%s] Step 4 完成", merchant_id)

        # ── Step 5: Seedream 生成配图 ─────────────────────────
        _progress("image", "Generating 3 images (hero, mid, end)")
        log.info("[%s] Step 5: Seedream 生成配图...", merchant_id)
        image_paths = {}
        try:
            seedream = SeedreamClient()
            for slot, prompt in enhanced_prompts.items():
                if not prompt:
                    continue
                log.info("[%s] 生成 %s 图片...", merchant_id, slot)
                paths = seedream.text_to_image(prompt, output_dir)
                if paths:
                    image_paths[slot] = paths[0]
                    record_usage(
                        merchant_id, "seedream", cfg.SEEDREAM_MODEL,
                        image_count=1, session_id=session_id,
                    )
        except Exception as img_err:
            log.error("[%s] Seedream 生图失败: %s", merchant_id, img_err)

        log.info("[%s] Step 5 完成 — 生成 %d 张图片", merchant_id, len(image_paths))

        # ── Step 6: 组装 HTML + 保存 ─────────────────────────
        _progress("render", f"{len(image_paths)} images ready")
        log.info("[%s] Step 6: 组装 HTML 并保存...", merchant_id)
        timestamp = int(time.time())
        filename = f"{merchant_id}_{timestamp}_{uuid.uuid4().hex[:4]}.html"
        output_path = output_dir / filename

        preview_url = render_blog_html(
            blog_data=blog_data,
            image_paths=image_paths,
            merchant_cfg=merchant_cfg,
            output_path=output_path,
            template_file=style_choice["template_file"],
        )

        # 保存草稿记录
        save_draft(
            merchant_id=merchant_id,
            title=blog_data.get("title", "Untitled"),
            filename=filename,
            preview_url=preview_url,
            blog_data=blog_data,
            review_score=review_score,
            session_id=session_id,
        )
        log.info("[%s] Step 6 完成 — 预览: %s", merchant_id, preview_url)

        # ── Step 7: 发布到 WordPress（auto_publish=True 且商家配了 WP 时）─
        wp_result = {}
        wp_url = merchant_cfg.get("wordpress_url", "")
        if wp_url and auto_publish:
            _progress("publish", "Uploading images & publishing to WordPress")
            log.info("[%s] Step 7: 发布到 WordPress...", merchant_id)
            try:
                publisher = WordPressPublisher(merchant_id, merchant_cfg)
                wp_result = publisher.publish_blog(
                    blog_data=blog_data,
                    image_paths=image_paths,
                    status="private",
                )
                if wp_result.get("success"):
                    log.info("[%s] Step 7 完成 — WP ID=%d URL=%s",
                             merchant_id, wp_result.get("post_id", 0), wp_result.get("post_url", ""))
                else:
                    log.warning("[%s] Step 7 WordPress 发布失败: %s",
                                merchant_id, wp_result.get("error", "Unknown"))
            except Exception as wp_err:
                log.error("[%s] Step 7 WordPress 发布异常: %s", merchant_id, wp_err)
                wp_result = {"success": False, "error": str(wp_err)}
        elif not wp_url:
            log.info("[%s] 跳过 WordPress 发布（未配置 wordpress_url）", merchant_id)
        else:
            log.info("[%s] 跳过 WordPress 发布（手动模式，等待用户确认）", merchant_id)

        usage_report = format_usage_report(session_id)
        total_seconds = int(time.time() - _t_start)
        total_minutes = total_seconds // 60
        total_secs = total_seconds % 60
        generation_time = f"{total_minutes}m {total_secs}s" if total_minutes else f"{total_secs}s"
        log.info("[%s] 全流程完成 (耗时 %s)", merchant_id, generation_time)

        return {
            "success": True,
            "title": blog_data.get("title", "Untitled"),
            "preview_url": preview_url,
            "blog_data": blog_data,
            "review_score": review_score,
            "review_rounds": review_rounds,
            "session_id": session_id,
            "usage_report": usage_report,
            "generation_time": generation_time,
            # WordPress 发布结果
            "wp_published": wp_result.get("success", False),
            "wp_post_url": wp_result.get("post_url", ""),
            "wp_edit_url": wp_result.get("edit_url", ""),
            "wp_post_id": wp_result.get("post_id", 0),
            # 记录本篇使用的模板和布局，供批量生成时排除避免连续相同
            "template_file": style_choice["template_file"],
            "template_name": style_choice["template_name"],
            "layout_name": style_choice["layout_name"],
            "layout_label": style_choice["layout_label"],
        }

    except Exception as exc:
        log.exception("[%s] 博客生成失败: %s", merchant_id, exc)
        return {
            "success": False,
            "error": str(exc),
            "session_id": session_id,
            "title": "",
            "preview_url": "",
            "usage_report": format_usage_report(session_id),
        }


# ── 公共 API（加锁包装）─────────────────────────────────────

def generate_single_blog(
    merchant_id: str,
    merchant_cfg: dict,
    session_id: str = "",
    topic_index: int = 0,
    pre_scraped_topics: list[dict] | None = None,
    progress_cb=None,
    auto_publish: bool = True,
) -> dict:
    """生成单篇 SEO 博客（线程安全）

    自动获取商家级锁，同一商家同一时刻只允许一个生成任务运行。
    如果锁已被占用（另一个生成正在进行），立即返回错误而非阻塞等待。
    在auto on启动scheduler的时候使用，也会调用generate_single_inner

    Args:
        merchant_id: 商家标识
        merchant_cfg: 商家配置字典
        session_id: 会话 ID（用于追踪用量）
        topic_index: 使用第几个候选主题（0-based）
        pre_scraped_topics: 预先爬取好的候选主题（多篇生成时复用）

    Returns:
        生成结果字典
    """
    if not session_id:
        session_id = f"{merchant_id}_{int(time.time())}_{uuid.uuid4().hex[:6]}"

    merchant_lock = _get_merchant_lock(merchant_id)
    if not merchant_lock.acquire(timeout=5):
        log.warning("[%s] 生成锁已被占用，当前有其他生成任务正在运行", merchant_id)
        return {
            "success": False,
            "error": "Another generation is already running for this merchant. Please wait.",
            "session_id": session_id, "title": "", "preview_url": "",
        }

    try:
        return _generate_single_inner(
            merchant_id, merchant_cfg, session_id, topic_index, pre_scraped_topics,
            exclude_template=None, exclude_layout=None,
            progress_cb=progress_cb,
            auto_publish=auto_publish,
        )
    finally:
        merchant_lock.release()
        # 每次生成完毕持久化用量数据（防止崩溃丢失）
        try:
            save_to_disk()
        except Exception:
            log.warning("[%s] 用量数据持久化失败", merchant_id)


def generate_multiple_blogs(
    merchant_id: str,
    merchant_cfg: dict,
    count: int = 1,
    progress_cb=None,
    auto_publish: bool = True,
) -> list[dict]:
    """为某一个商家生成多篇 SEO 博客（线程安全）

    先统一爬取热词和选题，再逐篇生成，每篇选不同的主题。
    整个批量过程持有商家锁，防止定时任务与手动指令并发。

    Args:
        merchant_id: 商家标识
        merchant_cfg: 商家配置字典
        count: 生成数量

    Returns:
        结果列表
    """
    log.info("[%s] 开始批量生成 %d 篇博客", merchant_id, count)

    merchant_lock = _get_merchant_lock(merchant_id)
    if not merchant_lock.acquire(timeout=5):
        log.warning("[%s] 生成锁已被占用，跳过批量生成", merchant_id)
        return [{"success": False, "error": "Another generation is already running.", "title": "", "preview_url": ""}]

    try:
        # 统一爬取热搜词条，保留前SCRAPE_MAX_ITEMS
        seed_keywords = get_seed_keywords(merchant_id)
        trending_data = scrape_trending(seed_keywords, max_total=cfg.SCRAPE_MAX_ITEMS)
        log.info("[%s] 爬取到 %d 条热词数据", merchant_id, len(trending_data))

        # 查到最近生成的前10个blogs，避免重复
        recent_titles = get_recent_titles(merchant_id)
        # 把商家id，google search suggetions，最近生成的主题，和想给这个商家生成的blogs的个数都传给researcher人格
        # 来挑选出最佳topics，[{title, keywords, angle, why}, ...]
        topics, _ = analyze_and_pick_topics(
            merchant_id, trending_data, recent_titles, count=count,
        )

        if not topics:
            log.error("[%s] 未能选出任何主题", merchant_id)
            return [{"success": False, "error": "No topics found", "title": "", "preview_url": ""}]

        # 根据每个topic逐篇生成（已持有锁，直接调用内部逻辑避免重复抢锁）
        # 记录上一篇的模板和布局，传给下一篇避免连续重复
        results = []
        prev_template = None
        prev_layout = None
        for i in range(min(count, len(topics))):
            log.info("[%s] 正在生成第 %d/%d 篇...", merchant_id, i + 1, count)
            session_id = f"{merchant_id}_{int(time.time())}_{uuid.uuid4().hex[:6]}"

            # 批量生成时，包装 progress_cb 加入篇数信息
            def _batch_progress(stage, extra="", _idx=i):
                if progress_cb:
                    progress_cb(stage, extra, post_index=_idx + 1, post_total=count)

            result = _generate_single_inner(
                merchant_id=merchant_id,
                merchant_cfg=merchant_cfg,
                session_id=session_id,
                topic_index=i,
                pre_scraped_topics=topics,
                exclude_template=prev_template,
                exclude_layout=prev_layout,
                progress_cb=_batch_progress,
                auto_publish=auto_publish,
            )
            results.append(result)

            # 记录本篇的模板和布局，下一篇排除
            if result.get("success"):
                prev_template = result.get("template_file")
                prev_layout = result.get("layout_name")

        log.info("[%s] 批量生成完成: %d/%d 成功",
                 merchant_id, sum(1 for r in results if r.get("success")), count)

        return results
    finally:
        merchant_lock.release()
        try:
            save_to_disk()
        except Exception:
            log.warning("[%s] 用量数据持久化失败", merchant_id)
