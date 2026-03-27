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
    """构建单篇博客生成结果的 Slack Block Kit 消息

    Args:
        result: generate_single_blog 的返回值
        index: 当前篇数（1-based）
        total: 总篇数

    Returns:
        Slack Block Kit blocks 列表
    """
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
        ]

    title = result.get("title", "Untitled")
    preview_url = result.get("preview_url", "")
    score = result.get("review_score", 0)
    rounds = result.get("review_rounds", 0)
    usage = result.get("usage_report", "")
    blog_data = result.get("blog_data", {})
    excerpt = blog_data.get("excerpt", "")
    tags = blog_data.get("tags", [])
    keywords = ", ".join(tags[:5]) if tags else "N/A"

    # 评分颜色 emoji
    if score >= 90:
        score_emoji = ":star:"
    elif score >= 80:
        score_emoji = ":white_check_mark:"
    else:
        score_emoji = ":warning:"

    blocks = [
        # 标题
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": f"Blog Draft #{index} of {total}",
            },
        },
        # 博客信息
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"*{title}*\n\n"
                    f"_{excerpt}_\n\n"
                    f":label: *Keywords:* {keywords}\n"
                    f"{score_emoji} *Review Score:* {score}/100 (round {rounds})\n"
                ),
            },
        },
        {"type": "divider"},
        # 预览链接
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f":link: *Preview:* <{preview_url}|Open in Browser>",
            },
        },
    ]

    # Token 用量
    if usage:
        blocks.append({
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": usage,
                },
            ],
        })

    blocks.append({"type": "divider"})

    return blocks


def build_batch_summary_blocks(results: list[dict], merchant_name: str) -> list[dict]:
    """构建批量生成结果的汇总消息

    Args:
        results: generate_multiple_blogs 的返回值
        merchant_name: 商家名称

    Returns:
        Slack Block Kit blocks 列表
    """
    success_count = sum(1 for r in results if r.get("success"))
    total_count = len(results)

    blocks = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": f":memo: Blog Generation Complete — {merchant_name}",
            },
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"Generated *{success_count}/{total_count}* blog posts successfully.",
            },
        },
        {"type": "divider"},
    ]

    # 每篇博客的结果卡片
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
    """构建 "正在生成" 的过渡消息"""
    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f":hourglass_flowing_sand: *Generating {count} blog post{'s' if count > 1 else ''} "
                    f"for {merchant_name}...*\n\n"
                    f"This takes 2-5 minutes per post. I'll update you when done.\n\n"
                    f":mag: Scraping trending keywords...\n"
                    f":brain: Analyzing SEO opportunities...\n"
                    f":pencil: Writing content...\n"
                    f":eyes: Reviewing quality...\n"
                    f":art: Generating images...\n"
                    f":globe_with_meridians: Building preview..."
                ),
            },
        },
    ]
