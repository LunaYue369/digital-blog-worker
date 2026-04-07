"""Slack Block Kit 消息构建器 — 构建博客预览卡片和调度状态消息

所有发送到 Slack 的富消息都通过这里构建。
"""

import logging
import time

log = logging.getLogger(__name__)


def build_blog_result_blocks(
    result: dict,
    index: int = 1,
    total: int = 1,
) -> list[dict]:
    """构建单篇博客生成结果的 Slack Block Kit 消息"""
    if not result.get("success"):
        return [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f":x: *Blog #{index}/{total} — Generation Failed*\n\n"
                            f"Error: {result.get('error', 'Unknown error')}",
                },
            },
            {"type": "divider"},
        ]

    title = result.get("title", "Untitled")
    preview_url = result.get("preview_url", "")
    score = result.get("review_score", 0)
    rounds = result.get("review_rounds", 0)
    usage = result.get("usage_report", "")
    blog_data = result.get("blog_data", {})
    excerpt = blog_data.get("excerpt", "")
    tags = blog_data.get("tags", [])
    template_name = result.get("template_name", "N/A")
    layout_label = result.get("layout_label", "N/A")
    gen_time = result.get("generation_time", "N/A")
    wp_published = result.get("wp_published", False)
    wp_post_url = result.get("wp_post_url", "")
    wp_edit_url = result.get("wp_edit_url", "")

    # 标签格式化
    tags_text = "  ".join(f"`{t}`" for t in tags[:5]) if tags else "N/A"

    # 评分 emoji
    if score >= 90:
        score_emoji = ":star2:"
    elif score >= 80:
        score_emoji = ":white_check_mark:"
    else:
        score_emoji = ":warning:"

    blocks = [
        # ── 标题 ──
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": f":notebook_with_decorative_cover: Blog Post {index}/{total}",
            },
        },
        # ── 博客标题 ──
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*{title}*",
            },
        },
        # ── 摘要 ──
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"_{excerpt}_",
            },
        },
        # ── SEO 关键词信息 ──
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f":mag: *Primary Keyword:*  {blog_data.get('seo_slug', 'N/A').replace('-', ' ')}\n"
                    f":link: *SEO Slug:*  `{blog_data.get('seo_slug', 'N/A')}`"
                ),
            },
        },
        {"type": "divider"},
        # ── 详细信息（纵向单列） ──
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"{score_emoji} *Review Score:*  {score}/100  (round {rounds})\n"
                    f":label: *Tags:*  {tags_text}\n"
                    f":art: *Template:*  {template_name}\n"
                    f":page_facing_up: *Layout:*  {layout_label}\n"
                    f":stopwatch: *Generation Time:*  {gen_time}"
                ),
            },
        },
        {"type": "divider"},
        # ── 预览链接（醒目） ──
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f":eyes:  *<{preview_url}|:point_right: Open Preview in Browser>*",
            },
        },
    ]

    # ── WordPress 发布状态 / 发布按钮 ──
    if wp_published and wp_post_url:
        # 已发布 — 显示链接
        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f":outbox_tray: *WordPress:*  Published as `private`\n"
                    f":link: *Post:*  <{wp_post_url}|View on WordPress>\n"
                    f":pencil2: *Edit:*  <{wp_edit_url}|Open in WP Admin>"
                ),
            },
        })
    elif not wp_published and result.get("blog_data"):
        # 未发布 — 显示 Publish 按钮
        # action_id 里编码 merchant_id 和 session_id，按钮点击时用来找到对应草稿
        session_id = result.get("session_id", "")
        blocks.append({
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": ":outbox_tray: Publish to WordPress"},
                    "style": "primary",
                    "action_id": f"wp_publish_{session_id}",
                },
            ],
        })

    # ── 用量信息 ──
    if usage:
        blocks.append({"type": "divider"})
        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f":bar_chart: *Cost Breakdown*\n{usage}",
            },
        })

    blocks.append({"type": "divider"})
    return blocks


def build_batch_summary_blocks(results: list[dict], merchant_name: str) -> list[dict]:
    """构建批量生成结果的汇总消息"""
    success_count = sum(1 for r in results if r.get("success"))
    total_count = len(results)

    # 汇总 emoji
    if success_count == total_count:
        status_emoji = ":tada:"
    elif success_count > 0:
        status_emoji = ":white_check_mark:"
    else:
        status_emoji = ":x:"

    # 收集成功文章的标题
    titles = [r.get("title", "Untitled") for r in results if r.get("success")]
    titles_text = "\n".join(f"  {i+1}. {t}" for i, t in enumerate(titles))

    # 收集总用量
    total_cost = 0.0
    for r in results:
        usage_text = r.get("usage_report", "")
        # 从 usage_report 里提取 total cost（简单解析）
        if "Total Cost:" in usage_text:
            try:
                cost_str = usage_text.split("Total Cost: $")[1].split("*")[0].strip()
                total_cost += float(cost_str)
            except (IndexError, ValueError):
                pass

    summary_lines = [
        f":memo: Generated *{success_count}/{total_count}* blog post{'s' if total_count > 1 else ''} successfully.",
    ]
    if titles_text:
        summary_lines.append(f"\n:page_facing_up: *Articles:*\n{titles_text}")
    # 总耗时
    gen_times = [r.get("generation_time", "") for r in results if r.get("generation_time")]
    if gen_times:
        summary_lines.append(f"\n:stopwatch: *Generation Time:* {', '.join(gen_times)}")
    if total_cost > 0:
        summary_lines.append(f":moneybag: *Total Cost:* ${total_cost:.4f}")

    blocks = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": f"{status_emoji} Generation Complete — {merchant_name}",
            },
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": "\n".join(summary_lines),
            },
        },
        {"type": "divider"},
    ]

    for i, result in enumerate(results, 1):
        blocks.extend(build_blog_result_blocks(result, index=i, total=total_count))

    return blocks


def build_schedule_status_blocks(
    merchant_name: str,
    is_active: bool,
    times: list[str],
    recent_drafts: list[dict],
) -> list[dict]:
    """构建调度状态消息

    Args:
        merchant_name: 商家名称
        is_active: 调度是否激活
        times: 当前调度时间点列表
        recent_drafts: 最近的草稿列表

    Returns:
        Slack Block Kit blocks 列表
    """
    status_emoji = ":white_check_mark:" if is_active else ":no_entry_sign:"
    status_text = "Active" if is_active else "Inactive"
    times_text = ", ".join(times) if times else "None"

    blocks = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": f"Schedule Status — {merchant_name}",
            },
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"{status_emoji} *Status:* {status_text}\n"
                    f":clock3: *Schedule:* {times_text}\n"
                    f":newspaper: *Posts per day:* {len(times)}"
                ),
            },
        },
    ]

    if recent_drafts:
        blocks.append({"type": "divider"})
        draft_lines = []
        for d in recent_drafts[:5]:
            title = d.get("title", "Untitled")
            score = d.get("review_score", 0)
            ts = d.get("created_at", 0)
            time_str = time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else "N/A"
            draft_lines.append(f"• {title} (score: {score}) — {time_str}")

        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": "*Recent Drafts:*\n" + "\n".join(draft_lines),
            },
        })

    return blocks


def build_generating_message(merchant_name: str, count: int) -> list[dict]:
    """构建 "正在生成" 的初始消息"""
    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f":rocket: *Starting blog generation for {merchant_name}*\n"
                    f"Generating {count} post{'s' if count > 1 else ''}. "
                    f"I'll update progress in real-time below.\n\n"
                    f":hourglass_flowing_sand: Preparing..."
                ),
            },
        },
    ]


# ── Chat 模式进度阶段 ──────────────────────────────────────
CHAT_PROGRESS_STAGES = [
    {"key": "template",  "emoji": ":art:",                   "label": "Selecting template & layout"},
    {"key": "web",       "emoji": ":globe_with_meridians:",  "label": "Researching competitor articles"},
    {"key": "write",     "emoji": ":pencil:",                "label": "Writing blog content"},
    {"key": "review",    "emoji": ":eyes:",                  "label": "Reviewing content quality"},
    {"key": "rewrite",   "emoji": ":memo:",                  "label": "Revising based on feedback"},
    {"key": "image",     "emoji": ":camera:",                "label": "Processing images"},
    {"key": "render",    "emoji": ":package:",               "label": "Assembling final preview"},
    {"key": "done",      "emoji": ":white_check_mark:",      "label": "Complete!"},
]

_CHAT_STAGE_INDEX = {s["key"]: i for i, s in enumerate(CHAT_PROGRESS_STAGES)}


def build_chat_progress_blocks(
    store_name: str,
    current_stage: str,
    extra_info: str = "",
) -> list[dict]:
    """构建 Chat 模式的实时进度消息（单条消息动态更新）"""
    current_idx = _CHAT_STAGE_INDEX.get(current_stage, 0)

    lines = []
    for i, stage in enumerate(CHAT_PROGRESS_STAGES):
        if stage["key"] == "rewrite" and current_stage != "rewrite":
            continue

        if i < current_idx:
            lines.append(f":white_check_mark:  {stage['label']}")
        elif i == current_idx:
            lines.append(f"{stage['emoji']}  *{stage['label']}...*")
        else:
            lines.append(f":white_circle:  {stage['label']}")

    progress_text = "\n".join(lines)
    extra_line = f"\n\n:bulb: _{extra_info}_" if extra_info else ""

    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f":rocket: *Generating blog for {store_name}*\n\n{progress_text}{extra_line}",
            },
        },
    ]


# ── Auto 模式进度阶段 ─────────────────────────────────────
PROGRESS_STAGES = [
    {"key": "scrape",    "emoji": ":mag:",                   "label": "Scraping trending keywords"},
    {"key": "research",  "emoji": ":brain:",                 "label": "Analyzing SEO opportunities"},
    {"key": "template",  "emoji": ":art:",                   "label": "Selecting template & layout"},
    {"key": "web",       "emoji": ":globe_with_meridians:",  "label": "Researching competitor articles"},
    {"key": "write",     "emoji": ":pencil:",                "label": "Writing blog content"},
    {"key": "review",    "emoji": ":eyes:",                  "label": "Reviewing content quality"},
    {"key": "rewrite",   "emoji": ":memo:",                  "label": "Revising based on feedback"},
    {"key": "artist",    "emoji": ":lower_left_paintbrush:", "label": "Enhancing image prompts"},
    {"key": "image",     "emoji": ":camera:",                "label": "Generating images (Seedream)"},
    {"key": "render",    "emoji": ":package:",               "label": "Assembling final HTML"},
    {"key": "publish",   "emoji": ":outbox_tray:",           "label": "Publishing to WordPress"},
    {"key": "done",      "emoji": ":white_check_mark:",      "label": "Complete!"},
]

_STAGE_INDEX = {s["key"]: i for i, s in enumerate(PROGRESS_STAGES)}


def build_progress_blocks(
    merchant_name: str,
    current_stage: str,
    post_index: int = 1,
    post_total: int = 1,
    extra_info: str = "",
    auto_publish: bool = True,
) -> list[dict]:
    """构建实时进度更新消息

    Args:
        merchant_name: 商家名称
        current_stage: 当前阶段 key（如 "scrape", "write", "review"）
        post_index: 当前第几篇（1-based）
        post_total: 总篇数
        extra_info: 额外信息（如选中的主题名、审核轮次等）
        auto_publish: 是否自动发布到 WordPress（False 时隐藏 publish 阶段）
    """
    current_idx = _STAGE_INDEX.get(current_stage, 0)

    lines = []
    for i, stage in enumerate(PROGRESS_STAGES):
        if stage["key"] == "rewrite" and current_stage != "rewrite":
            continue  # 没有重写时跳过显示
        if stage["key"] == "publish" and not auto_publish:
            continue  # 手动模式下不显示 publish 阶段

        if i < current_idx:
            lines.append(f":white_check_mark:  {stage['label']}")
        elif i == current_idx:
            lines.append(f"{stage['emoji']}  *{stage['label']}...*")
        else:
            lines.append(f":white_circle:  {stage['label']}")

    progress_text = "\n".join(lines)

    header = (
        f":rocket: *Generating blog for {merchant_name}*"
        if post_total == 1
        else f":rocket: *Generating blog {post_index}/{post_total} for {merchant_name}*"
    )

    extra_line = f"\n\n:bulb: _{extra_info}_" if extra_info else ""

    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"{header}\n\n{progress_text}{extra_line}",
            },
        },
    ]


# ── Chat 对话模式专用消息构建 ────────────────────────────────


def build_chat_result_blocks(result: dict) -> list[dict]:
    """构建 Chat 对话模式的博客生成结果消息

    与 auto 模式的 build_blog_result_blocks 类似，但增加了:
    - Publish + Regenerate 按钮（在 thread 内交互）
    - 不显示批量编号（chat 一次只生成一篇）
    - 用户也可以直接在 thread 里打字提修改意见

    Args:
        result: pipeline 返回的结果字典
                示例: {
                    "success": True,
                    "title": "Ultimate Guide to Tesla PPF",
                    "preview_url": "http://localhost:8900/thouseirvine/xxx.html",
                    "blog_data": {"title": "...", "excerpt": "...", "tags": [...], ...},
                    "review_score": 88,
                    "review_rounds": 2,
                    "session_id": "chat_thouseirvine_1712100000_abc123",
                    "usage_report": "...",
                    "generation_time": "45s",
                    "template_name": "Classic White",
                    "layout_label": "How-To Guide",
                }

    Returns:
        Slack Block Kit blocks 列表，包含:
        - 标题 + 摘要
        - SEO 信息
        - 审核评分 + 模板/布局
        - 预览链接
        - Publish + Regenerate 按钮

    输出示例（渲染效果）:
        ┌─────────────────────────────────────────┐
        │ 📓 Blog Generated via Chat               │
        │                                          │
        │ **Ultimate Guide to Tesla PPF**          │
        │ _Protect your Tesla with XPEL PPF..._    │
        │                                          │
        │ ✅ Review: 88/100 (round 2)              │
        │ 🏷️ Tags: PPF  Tesla  PaintProtection     │
        │ 🎨 Template: Classic White               │
        │ ⏱️ Time: 45s                             │
        │                                          │
        │ 👀 [Open Preview in Browser]             │
        │                                          │
        │ [📤 Publish to WordPress] [🔄 Regenerate] │
        └─────────────────────────────────────────┘
    """
    if not result.get("success"):
        return [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f":x: *Blog Generation Failed*\n\n"
                            f"Error: {result.get('error', 'Unknown error')}",
                },
            },
        ]

    title = result.get("title", "Untitled")
    preview_url = result.get("preview_url", "")
    score = result.get("review_score", 0)
    rounds = result.get("review_rounds", 0)
    usage = result.get("usage_report", "")
    blog_data = result.get("blog_data", {})
    excerpt = blog_data.get("excerpt", "")
    tags = blog_data.get("tags", [])
    template_name = result.get("template_name", "N/A")
    layout_label = result.get("layout_label", "N/A")
    gen_time = result.get("generation_time", "N/A")
    session_id = result.get("session_id", "")

    tags_text = "  ".join(f"`{t}`" for t in tags[:5]) if tags else "N/A"

    if score >= 90:
        score_emoji = ":star2:"
    elif score >= 80:
        score_emoji = ":white_check_mark:"
    else:
        score_emoji = ":warning:"

    # 图片数量
    image_count = len(result.get("image_paths", {}))

    # 评分条（视觉化）
    filled = round(score / 10)
    bar = ":large_green_square:" * filled + ":white_large_square:" * (10 - filled)

    blocks = [
        # ── 标题 ──
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": ":sparkles: Blog Ready for Review",
            },
        },
        # ── 博客标题 + 摘要 ──
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f":memo: *{title}*\n\n_{excerpt}_",
            },
        },
        {"type": "divider"},
        # ── 审核评分 ──
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"{score_emoji} *Review Score:*  *{score}/100*  ({rounds} round{'s' if rounds > 1 else ''})\n"
                    f"{bar}"
                ),
            },
        },
        # ── 内容详情 ──
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f":label: *Tags:*  {tags_text}\n"
                    f":frame_with_picture: *Images:*  {image_count} image{'s' if image_count > 1 else ''}\n"
                    f":art: *Template:*  {template_name}  |  :page_facing_up: *Layout:*  {layout_label}\n"
                    f":stopwatch: *Time:*  {gen_time}"
                ),
            },
        },
        {"type": "divider"},
        # ── 预览链接 ──
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f":eyes:  *<{preview_url}|Open Preview in Browser>*",
            },
        },
        # ── 提示 ──
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": ":speech_balloon: _Reply in this thread to request changes, or use the buttons below._",
                },
            ],
        },
    ]

    # ── 操作按钮 ──
    blocks.append({
        "type": "actions",
        "elements": [
            {
                "type": "button",
                "text": {"type": "plain_text", "text": ":outbox_tray: Publish to WordPress"},
                "style": "primary",
                "action_id": f"wp_publish_{session_id}",
            },
            {
                "type": "button",
                "text": {"type": "plain_text", "text": ":arrows_counterclockwise: Regenerate"},
                "action_id": f"chat_regenerate_{session_id}",
            },
        ],
    })

    # ── 用量信息 ──
    if usage:
        blocks.append({"type": "divider"})
        blocks.append({
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": f":bar_chart: *Cost:*  {usage}",
                },
            ],
        })

    return blocks
