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
import sys
import threading

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
)
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
    """处理频道中的消息 — 识别 auto 指令"""
    # 过滤 Bot 自身消息
    user = event.get("user", "")
    if user == _bot_user_id:
        return

    # 过滤子类型消息（编辑、删除等）
    if event.get("subtype"):
        return

    text = event.get("text", "").strip()
    channel = event.get("channel", "")

    # 检查是否是 auto 指令
    cmd = parse_auto_command(text)
    if cmd is None:
        return  # 不是 auto 指令，忽略

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

        # 发送 "正在生成" 提示
        say(
            text=f"Generating {count} blog post(s) for {store_name}...",
            blocks=build_generating_message(store_name, count),
        )

        # 在后台线程中执行（避免阻塞 Slack 事件循环）
        def _do_generate():
            try:
                # 为某个商家生成count篇blogs
                results = generate_multiple_blogs(merchant_id, merchant_cfg, count)
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


@app.event("app_mention")
def handle_mention(event: dict, say) -> None:
    """处理 @mention 事件 — 提示用户使用 auto 指令"""
    say(
        ":wave: Hi! I'm the Blog Generator Bot. Here's how to use me:\n\n"
        "• `auto 3` — Generate 3 blog posts now\n"
        "• `auto on` — Start daily scheduled generation (default times)\n"
        "• `auto on 09:00 14:00` — Schedule at custom times\n"
        "• `auto off` — Stop scheduled generation\n"
        "• `auto status` — Check schedule & recent posts"
    )


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
