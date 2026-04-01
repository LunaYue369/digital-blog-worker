"""Digital Blog Worker — 多商家 SEO 博客自动生成 Slack Bot

入口文件：
1. 加载所有商家配置和 Agent 人格
2. 启动预览服务器（HTTP 静态文件服务）
3. 启动 APScheduler 定时调度器
4. 启动 Slack Bot（Socket Mode）监听各频道的 auto 指令

一个 Bot 实例服务所有商家，通过频道 ID 区分。

指令：
    auto 3         → 立即生成 3 篇博客供审核
    auto on        → 开启定时调度（使用默认时间点）
    auto on 09:00 14:00 18:00 → 开启定时调度（自定义时间点）
    auto off       → 关闭定时调度
    auto status    → 查看当前调度状态
"""

import logging
import re
import sys
import threading
from pathlib import Path

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

import config as cfg
import scheduler
from core.merchant_config import load_all_merchants, get_merchant_by_channel
from core.channel_router import parse_auto_command
from pipeline.blog_generator import generate_multiple_blogs
from pipeline.preview_server import start_preview_server
from slack_ui.blocks import (
    build_batch_summary_blocks,
    build_schedule_status_blocks,
    build_generating_message,
    build_progress_blocks,
)
from services.wordpress_publisher import WordPressPublisher
from store.blog_store import get_drafts

# ── 日志配置 ──────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(cfg.LOGS_DIR / "bot.log", encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)

# ── Slack App 初始化 ──────────────────────────────────────
app = App(token=cfg.SLACK_BOT_TOKEN)

# Bot 自身的 user_id（用于过滤自己的消息，防止回环）
_bot_user_id: str = ""


# ── 消息事件处理 ──────────────────────────────────────────

@app.event("message")
def handle_message(event: dict, say, client) -> None:
    """处理频道中的消息 — 只响应 @mention 的 auto 指令"""
    # 过滤 Bot 自身消息
    user = event.get("user", "")
    if user == _bot_user_id:
        return

    # 过滤子类型消息（编辑、删除等）
    if event.get("subtype"):
        return

    text = event.get("text", "").strip()
    channel = event.get("channel", "")

    # 必须 @mention Bot 才响应
    if f"<@{_bot_user_id}>" not in text:
        return

    # 去掉 @mention 部分，提取纯指令文本
    text = text.replace(f"<@{_bot_user_id}>", "").strip()

    # ── 检查 publish 指令 ──
    if text.lower().startswith("publish"):
        _handle_publish(text, channel, say, client)
        return

    # 检查是否是 auto 指令
    cmd = parse_auto_command(text)
    if cmd is None:
        # @mention 了但不是 auto 指令 → 显示帮助
        say(
            ":wave: Hi! Here's how to use me:\n\n"
            f"• `@bot auto 3` — Generate 3 blog posts now\n"
            f"• `@bot auto on` — Start daily scheduled generation\n"
            f"• `@bot auto on 09:00 14:00` — Schedule at custom times\n"
            f"• `@bot auto off` — Stop scheduled generation\n"
            f"• `@bot auto status` — Check schedule & recent posts\n"
            f"• `@bot publish` — Publish latest draft to WordPress (private)"
        )
        return

    # 查找频道对应的商家
    merchant_cfg = get_merchant_by_channel(channel)
    if not merchant_cfg:
        say(":warning: This channel is not linked to any merchant. Please check merchant configuration.")
        return

    merchant_id = merchant_cfg["merchant_id"]
    store_name = merchant_cfg.get("store_name", merchant_id)

    log.info("[%s] 收到指令: %s (channel=%s, user=%s)", merchant_id, cmd.action, channel, user)

    # ── 立即生成 ──────────────────────────────────────────
    # “auto n”，立刻generate n篇blog
    if cmd.action == "generate":
        count = cmd.count

        # 发送初始进度消息（后续会 chat_update 更新这条消息）
        init_resp = client.chat_postMessage(
            channel=channel,
            text=f"Generating {count} blog post(s) for {store_name}...",
            blocks=build_generating_message(store_name, count),
        )
        progress_ts = init_resp["ts"]  # 消息 timestamp，用于后续更新

        # 在后台线程中执行（避免阻塞 Slack 事件循环）
        def _do_generate():
            try:
                # 进度回调 — 更新同一条 Slack 消息
                def _on_progress(stage, extra="", post_index=1, post_total=count):
                    try:
                        blocks = build_progress_blocks(
                            store_name, stage,
                            post_index=post_index, post_total=post_total,
                            extra_info=extra,
                            auto_publish=False,
                        )
                        client.chat_update(
                            channel=channel, ts=progress_ts,
                            text=f"Generating blog for {store_name}...",
                            blocks=blocks,
                        )
                    except Exception:
                        pass  # 更新失败不阻断

                # auto N 手动模式：只生成预览，不自动发布到 WordPress
                results = generate_multiple_blogs(
                    merchant_id, merchant_cfg, count,
                    progress_cb=_on_progress,
                    auto_publish=False,
                )

                # 生成完成 — 更新进度消息为 "完成"
                try:
                    done_blocks = build_progress_blocks(
                        store_name, "done",
                        post_index=count, post_total=count,
                    )
                    client.chat_update(
                        channel=channel, ts=progress_ts,
                        text=f"Blog generation complete for {store_name}",
                        blocks=done_blocks,
                    )
                except Exception:
                    pass

                # 发送结果摘要（新消息）
                blocks = build_batch_summary_blocks(results, store_name)
                client.chat_postMessage(
                    channel=channel,
                    text=f"Generated {len(results)} blog posts for {store_name}",
                    blocks=blocks,
                )
            except Exception as exc:
                log.exception("[%s] 生成失败: %s", merchant_id, exc)
                client.chat_postMessage(
                    channel=channel,
                    text=f":x: Blog generation failed: {exc}",
                )

        thread = threading.Thread(target=_do_generate, daemon=True,
                                  name=f"gen-{merchant_id}")
        thread.start()

    # ── 开启定时调度 ──────────────────────────────────────
    # auto on，times是选择的自动post的时间点
    elif cmd.action == "schedule_on":
        times = scheduler.schedule_on(merchant_id, channel, cmd.times or None)
        times_str = ", ".join(times)
        say(f":white_check_mark: *Schedule activated for {store_name}*\n"
            f"Posts will be generated daily at: *{times_str}*\n"
            f"Each time slot will trigger a fresh keyword scrape + blog generation.")

    # ── 关闭定时调度 ──────────────────────────────────────
    elif cmd.action == "schedule_off":
        scheduler.schedule_off(merchant_id)
        say(f":no_entry_sign: *Schedule deactivated for {store_name}*\n"
            f"No more automatic blog generation. Use `auto` to generate manually.")

    # ── 查看状态 ──────────────────────────────────────────
    elif cmd.action == "status":
        status = scheduler.get_schedule_status(merchant_id)
        recent_drafts = get_drafts(merchant_id, limit=5)
        blocks = build_schedule_status_blocks(
            store_name,
            status.get("active", False),
            status.get("times", []),
            recent_drafts,
        )
        say(text=f"Schedule status for {store_name}", blocks=blocks)


def _handle_publish(text: str, channel: str, say, client) -> None:
    """处理 publish 指令 — 手动发布最近一篇草稿到 WordPress

    从 blog_store 获取最近的草稿，读取对应的 HTML 文件，
    然后通过 WordPressPublisher 发布到 WordPress（status=private）。

    用法：
        @Bot publish        → 发布最近一篇草稿
    """
    # 查找频道对应的商家
    merchant_cfg = get_merchant_by_channel(channel)
    if not merchant_cfg:
        say(":warning: This channel is not linked to any merchant.")
        return

    merchant_id = merchant_cfg["merchant_id"]
    store_name = merchant_cfg.get("store_name", merchant_id)
    wp_url = merchant_cfg.get("wordpress_url", "")

    # 检查是否配置了 WordPress
    if not wp_url:
        say(f":warning: *{store_name}* has no WordPress URL configured in merchant.json.")
        return

    # 获取最近的草稿
    drafts = get_drafts(merchant_id, limit=1)
    if not drafts:
        say(f":warning: No drafts found for *{store_name}*. Generate a blog first with `@bot auto 1`.")
        return

    draft = drafts[0]
    draft_title = draft.get("title", "Untitled")

    say(f":outbox_tray: Publishing *{draft_title}* to WordPress (private)...")

    try:
        publisher = WordPressPublisher(merchant_id, merchant_cfg)

        # 从草稿数据构建 blog_data
        blog_data = {
            "title": draft_title,
            "content_html": draft.get("content_html", ""),
            "excerpt": draft.get("excerpt", ""),
            "tags": draft.get("tags", []),
            "seo_slug": draft.get("seo_slug", ""),
            "image_alts": draft.get("image_alts", {}),
        }

        # 从草稿记录读取图片路径
        image_paths = {}
        saved_paths = draft.get("image_paths", {})
        for slot, path_str in saved_paths.items():
            p = Path(path_str)
            if p.exists():
                image_paths[slot] = p

        wp_result = publisher.publish_blog(
            blog_data=blog_data,
            image_paths=image_paths,
            status="private",
        )

        if wp_result.get("success"):
            post_url = wp_result.get("post_url", "")
            edit_url = wp_result.get("edit_url", "")
            img_count = wp_result.get("images_uploaded", 0)
            tag_count = wp_result.get("tags_count", 0)
            image_names = wp_result.get("image_names", {})
            img_lines = "\n".join(
                f"  • `{slot}`: `{name}`" for slot, name in image_names.items()
            ) if image_names else "  (none)"
            say(
                f":white_check_mark: *Published to WordPress!*\n\n"
                f":page_facing_up: *Title:* {draft_title}\n"
                f":framed_picture: *Images:* {img_count}/3 uploaded\n{img_lines}\n"
                f":label: *Tags:* {tag_count}\n"
                f":lock: *Status:* Private\n"
                f":link: *Post:* <{post_url}|View on WordPress>\n"
                f":pencil2: *Edit:* <{edit_url}|Open in WP Admin>"
            )
        else:
            error = wp_result.get("error", "Unknown error")
            say(f":x: *WordPress publish failed:* {error}")

    except Exception as exc:
        log.exception("[%s] Manual publish failed: %s", merchant_id, exc)
        say(f":x: *Publish error:* {exc}")


# ── Publish 按钮点击处理 ─────────────────────────────────

@app.action(re.compile(r"^wp_publish_"))
def handle_publish_button(ack, body, client) -> None:
    """处理 Slack 中 Publish 按钮的点击事件

    按钮的 action_id 格式: wp_publish_{session_id}
    通过 session_id 找到对应的草稿，发布到 WordPress。
    """
    ack()  # 必须在 3 秒内确认收到

    action_id = body["actions"][0]["action_id"]
    session_id = action_id.replace("wp_publish_", "")
    channel = body["channel"]["id"]
    user = body["user"]["id"]

    # 查找频道对应的商家
    merchant_cfg = get_merchant_by_channel(channel)
    if not merchant_cfg:
        client.chat_postMessage(channel=channel, text=":warning: This channel is not linked to any merchant.")
        return

    merchant_id = merchant_cfg["merchant_id"]
    store_name = merchant_cfg.get("store_name", merchant_id)

    # 从草稿中找到对应 session_id 的记录
    all_drafts = get_drafts(merchant_id, limit=50)
    draft = None
    for d in all_drafts:
        if d.get("session_id") == session_id:
            draft = d
            break

    if not draft:
        client.chat_postMessage(channel=channel, text=f":warning: Draft not found (session: `{session_id}`)")
        return

    draft_title = draft.get("title", "Untitled")

    # 发一条新消息表示开始发布（每篇独立，不会互相覆盖）
    progress_resp = client.chat_postMessage(
        channel=channel,
        text=f":hourglass_flowing_sand: Publishing *{draft_title}* to WordPress...",
    )
    progress_ts = progress_resp["ts"]

    log.info("[%s] Publish 按钮点击: session=%s title=%s user=%s",
             merchant_id, session_id, draft_title, user)

    try:
        publisher = WordPressPublisher(merchant_id, merchant_cfg)

        blog_data = {
            "title": draft_title,
            "content_html": draft.get("content_html", ""),
            "excerpt": draft.get("excerpt", ""),
            "tags": draft.get("tags", []),
            "seo_slug": draft.get("seo_slug", ""),
            "image_alts": draft.get("image_alts", {}),
        }

        # 从草稿记录读取图片路径（不再用时间戳猜测）
        image_paths = {}
        saved_paths = draft.get("image_paths", {})
        for slot, path_str in saved_paths.items():
            p = Path(path_str)
            if p.exists():
                image_paths[slot] = p
            else:
                log.warning("[%s] 图片文件不存在: %s", merchant_id, path_str)

        wp_result = publisher.publish_blog(
            blog_data=blog_data,
            image_paths=image_paths,
            status="private",
        )

        if wp_result.get("success"):
            post_url = wp_result.get("post_url", "")
            edit_url = wp_result.get("edit_url", "")
            img_count = wp_result.get("images_uploaded", 0)
            tag_count = wp_result.get("tags_count", 0)
            image_names = wp_result.get("image_names", {})
            img_lines = "\n".join(
                f"  • `{slot}`: `{name}`" for slot, name in image_names.items()
            ) if image_names else "  (none)"
            publish_text = (
                f":white_check_mark: *Published to WordPress!*\n"
                f":page_facing_up: *Title:* {draft_title}\n"
                f":framed_picture: *Images:* {img_count}/3 uploaded\n{img_lines}\n"
                f":label: *Tags:* {tag_count}\n"
                f":lock: *Status:* Private\n"
                f":link: *Post:* <{post_url}|View on WordPress>\n"
                f":pencil2: *Edit:* <{edit_url}|Open in WP Admin>"
            )
            # 更新自己的进度消息为成功信息
            client.chat_update(
                channel=channel, ts=progress_ts,
                text=f"Published: {draft_title}",
                blocks=[{
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": publish_text},
                }],
            )
        else:
            error = wp_result.get("error", "Unknown error")
            client.chat_update(
                channel=channel, ts=progress_ts,
                text=f":x: *Publish failed:* {draft_title}: {error}",
            )

    except Exception as exc:
        log.exception("[%s] Button publish failed: %s", merchant_id, exc)
        client.chat_postMessage(channel=channel, text=f":x: *Publish error:* {exc}")


@app.event("app_mention")
def handle_mention(event: dict, say) -> None:
    """app_mention 事件 — 已在 message 事件中统一处理，此处留空防止 Slack 报错"""
    pass


# ── 启动 ──────────────────────────────────────────────────

def main() -> None:
    """主启动函数"""
    global _bot_user_id

    log.info("=" * 60)
    log.info("Digital Blog Worker — Starting up")
    log.info("=" * 60)

    # 1. 验证必需的配置
    if not cfg.SLACK_BOT_TOKEN or not cfg.SLACK_APP_TOKEN:
        log.error("缺少 SLACK_BOT_TOKEN 或 SLACK_APP_TOKEN")
        sys.exit(1)

    if not cfg.OPENAI_API_KEY:
        log.error("缺少 OPENAI_API_KEY")
        sys.exit(1)

    # 2. 加载所有商家配置和 Agent 人格，会完成一下配置
    # _channel_map 所有频道 ID → 商家配置(merchant.json+scrape_targets.yaml)的映射
    # _merchant_map：所有商家 ID → 商家配置(merchant.json+scrape_targets.yaml)的映射
    # _soul_store：# 所有商家人格存储: {merchant_id: {"_shared": "内容", "researcher": "内容", ...}}
    log.info("Step 1: 加载商家配置...")
    load_all_merchants()

    # 3. 获取 Bot 自身 user_id（用于防回环）
    log.info("Step 2: 获取 Bot 身份...")
    auth_resp = app.client.auth_test()
    _bot_user_id = auth_resp.get("user_id", "")
    log.info("Bot user_id: %s", _bot_user_id)

    # 4. 启动预览服务器
    # 让每篇blog可以有localhost的网页浏览链接
    log.info("Step 3: 启动预览服务器...")
    start_preview_server()

    # 5. 启动定时调度器
    # 把slack bot的webclient传给scheduler初始化
    # 后台开启美西钟表
    log.info("Step 4: 启动定时调度器...")
    scheduler.init(app.client)

    # 6. 启动 Slack Socket Mode
    # 开始监听@app.event("message")和@app.event("app_mention")
    log.info("Step 5: 启动 Slack Socket Mode...")
    log.info("=" * 60)
    log.info("Bot is ready! Listening for 'auto' commands in merchant channels.")
    log.info("=" * 60)

    handler = SocketModeHandler(app, cfg.SLACK_APP_TOKEN)
    handler.start()


if __name__ == "__main__":
    main()
