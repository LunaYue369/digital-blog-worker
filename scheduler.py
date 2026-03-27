"""定时调度器 — 管理每个商家的自动发帖计划

功能：
    用户在 Slack 频道发 "auto on 09:00 14:00 18:00"
    → 本模块为该商家注册 3 个定时任务（闹钟）
    → 每天到了 09:00 / 14:00 / 18:00（美西时间）自动触发博客生成流水线
    → 生成结果发回 Slack 频道

支持的操作：
    schedule_on()  — 开启定时（注册闹钟）
    schedule_off() — 关闭定时（移除闹钟）
    get_schedule_status() — 查看当前状态

注意：调度状态不持久化，Bot 重启后所有闹钟清空，需要重新发 auto on 指令。
"""

import logging

# APScheduler 第三方库：Python 定时任务调度器
# BackgroundScheduler — 在后台线程运行，不阻塞主线程
# CronTrigger — 类似 Linux cron 的触发器，支持 "每天几点几分" 这种定时
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

import config as cfg

log = logging.getLogger(__name__)

# ── 模块级变量（全局唯一，相当于单例）──────────────────────

# APScheduler 实例 — 相当于一个按美西时间走的闹钟
# 启动后在后台线程里每秒检查"现在几点了，有没有到点的任务"
_scheduler: BackgroundScheduler | None = None

# Slack WebClient 引用 — 存下来是因为定时任务触发时（可能是凌晨无人时）
# 需要用它往 Slack 频道发消息。在 init() 时由 main.py 传入。
_slack_client = None

# 内存中的调度状态（仅运行期间有效，重启后清空）
# 用于 "auto status" 查询当前哪些商家开了定时、时间点是什么
# 例: {"thouseirvine": {"active": True, "times": ["09:00", "14:00"], "channel_id": "C0ANTP265MK"}}
_schedule_state: dict[str, dict] = {}


# ── 初始化 ────────────────────────────────────────────────

def init(slack_client) -> None:
    """初始化调度器 — 由 main.py 启动时调用一次

    做两件事：
    1. 存下 Slack client 引用（后续定时任务用它发消息）
    2. 创建并启动 APScheduler 后台调度器

    Args:
        slack_client: Slack WebClient 实例（main.py 的 app.client）
    """
    global _scheduler, _slack_client
    # 存下 Slack client，定时任务到点时用它发消息到频道
    _slack_client = slack_client

    # 创建 APScheduler 实例，设定时区为美西时间
    # 这样用户说 "09:00" 就是美西时间早上 9 点
    _scheduler = BackgroundScheduler(timezone=cfg.TIMEZONE)
    # 启动后台线程，开始计时（从此刻起 APScheduler 开始工作）
    _scheduler.start()
    log.info("调度器已启动 (timezone=%s)", cfg.TIMEZONE)


# ── 定时任务回调（闹钟响了执行什么）──────────────────────

def _job_callback(merchant_id: str, channel_id: str) -> None:
    """定时任务回调 — APScheduler 到点时自动调用这个函数

    这个函数就是"闹钟响了之后要做的事"：
    1. 往 Slack 发一条"开始生成"的提示
    2. 执行完整的博客生成流水线（爬热词→选题→写作→审核→生图→预览）
    3. 把结果发回 Slack 频道

    generate_single_blog 内部已有商家级锁保护：
    - 如果此时用户正在手动生成（auto 3），定时任务不会阻塞等待
    - 而是直接返回 "already running" 错误，发一条 skip 消息到 Slack

    Args:
        merchant_id: 商家标识（如 "thouseirvine"）
        channel_id: 目标 Slack 频道 ID（如 "C0ANTP265MK"）
    """
    # 延迟导入：避免模块之间循环依赖
    # （scheduler.py 和 blog_generator.py 互相不能在文件顶部 import 对方）
    from core.merchant_config import get_merchant
    from pipeline.blog_generator import generate_single_blog
    from slack_ui.blocks import build_blog_result_blocks

    log.info("[%s] 定时任务触发 — 开始生成博客", merchant_id)

    # 从内存中取出该商家的完整配置（merchant.json 里的内容）
    merchant_cfg = get_merchant(merchant_id)
    if not merchant_cfg:
        log.error("[%s] 商家配置不存在，跳过", merchant_id)
        return

    try:
        # 先往 Slack 频道发一条提示消息，告诉用户"定时任务开始了"
        store_name = merchant_cfg.get("store_name", merchant_id)
        if _slack_client:
            _slack_client.chat_postMessage(
                channel=channel_id,
                text=f":clock3: Scheduled blog generation started for {store_name}...",
            )

        # 执行完整的博客生成流水线（6 步：爬热词→选题→写作→审核→生图→组装 HTML）
        # 内部有商家锁，如果有人正在手动生成，会返回 success=False
        result = generate_single_blog(merchant_id, merchant_cfg)

        # 把结果发回 Slack 频道
        if _slack_client:
            if result.get("success"):
                # 生成成功 — 发送带预览链接、评分、用量的富消息卡片
                blocks = build_blog_result_blocks(result, index=1, total=1)
                _slack_client.chat_postMessage(
                    channel=channel_id,
                    text=f"Blog generated: {result.get('title', 'N/A')}",
                    blocks=blocks,
                )
            else:
                # 生成失败或被锁跳过 — 发送警告消息
                _slack_client.chat_postMessage(
                    channel=channel_id,
                    text=f":warning: Scheduled generation skipped: {result.get('error', 'Unknown')}",
                )
    except Exception as exc:
        # 未预期的异常 — 记录日志并通知 Slack
        log.exception("[%s] 定时生成失败: %s", merchant_id, exc)
        if _slack_client:
            _slack_client.chat_postMessage(
                channel=channel_id,
                text=f":x: Scheduled blog generation failed: {exc}",
            )


# ── 注册/移除定时任务（管理闹钟）──────────────────────────

def _register_jobs(merchant_id: str, channel_id: str, times: list[str]) -> None:
    """为商家注册定时任务 — 往 APScheduler 的闹钟里添加响铃时间

    比如 times=["09:00", "14:00", "18:00"] 会注册 3 个 job：
        blog_thouseirvine_0900 → 每天 09:00 触发 _job_callback
        blog_thouseirvine_1400 → 每天 14:00 触发 _job_callback
        blog_thouseirvine_1800 → 每天 18:00 触发 _job_callback

    每次调用前会先清除该商家的旧任务，防止用户改时间后闹钟叠加。

    Args:
        merchant_id: 商家标识
        channel_id: 目标 Slack 频道
        times: 时间点列表（HH:MM 格式，如 ["09:00", "14:00"]）
    """
    if not _scheduler:
        return

    # 先清除该商家的所有旧闹钟（防止改时间后新旧叠加）
    _remove_jobs(merchant_id)

    # 逐个时间点注册新闹钟
    for t in times:
        try:
            # 拆分 "09:00" → hour=9, minute=0
            hour, minute = t.split(":")
            # 生成唯一 job ID，如 "blog_thouseirvine_0900"
            job_id = f"blog_{merchant_id}_{t.replace(':', '')}"

            # 调用 APScheduler 的 add_job 注册定时任务
            _scheduler.add_job(
                _job_callback,       # 到点执行什么函数
                trigger=CronTrigger( # 每天几点几分触发（cron 风格）
                    hour=int(hour),
                    minute=int(minute),
                    timezone=cfg.TIMEZONE,
                ),
                id=job_id,           # 任务唯一标识（用于后续查找和删除）
                args=[merchant_id, channel_id],  # 传给 _job_callback 的参数
                replace_existing=True,    # 同 ID 已存在就覆盖（安全措施）
                misfire_grace_time=300,   # 如果错过触发时间，5 分钟内还能补执行
            )
            log.info("[%s] 已注册定时任务: %s", merchant_id, t)
        except Exception as exc:
            log.error("[%s] 注册定时任务失败 (%s): %s", merchant_id, t, exc)


def _remove_jobs(merchant_id: str) -> None:
    """移除指定商家的所有定时任务 — 清除该商家的所有闹钟

    通过 job ID 前缀匹配来找到该商家的所有任务。
    比如 merchant_id="thouseirvine"，会匹配并删除：
        blog_thouseirvine_0900
        blog_thouseirvine_1400
        blog_thouseirvine_1800
    """
    if not _scheduler:
        return

    # 所有该商家的 job ID 都以 "blog_{merchant_id}_" 开头
    prefix = f"blog_{merchant_id}_"
    # 遍历 APScheduler 中所有已注册的 job，按前缀匹配删除
    for job in _scheduler.get_jobs():
        if job.id.startswith(prefix):
            job.remove()  # APScheduler 提供的删除方法
            log.info("[%s] 已移除定时任务: %s", merchant_id, job.id)


# ── 公共 API（供 main.py 调用）────────────────────────────

def schedule_on(merchant_id: str, channel_id: str, times: list[str] | None = None) -> list[str]:
    """开启商家的自动调度 — 用户发 "auto on 09:00 14:00" 时调用

    流程：
    1. 如果用户没指定时间，用商家默认时间或全局默认时间
    2. 调用 _register_jobs 注册闹钟
    3. 更新内存中的调度状态

    Args:
        merchant_id: 商家标识
        channel_id: 目标 Slack 频道
        times: 用户指定的时间点列表；为空则使用默认值

    Returns:
        实际使用的时间点列表（返回给 Slack 展示）
    """
    if not times:
        # 用户没指定时间 → 尝试用商家 merchant.json 里的 default_post_times
        from core.merchant_config import get_merchant
        merchant_cfg = get_merchant(merchant_id) or {}
        times = merchant_cfg.get("default_post_times")
        if not times:
            # 商家也没配 → 用全局默认（.env 里的 POST_TIMES，如 "09:00,13:00,17:00"）
            times = [t.strip() for t in cfg.DEFAULT_POST_TIMES.split(",") if t.strip()]

    # 往 APScheduler 注册闹钟
    _register_jobs(merchant_id, channel_id, times)

    # 记录状态到内存（用于 "auto status" 查询）
    _schedule_state[merchant_id] = {
        "active": True,
        "times": times,
        "channel_id": channel_id,
    }

    log.info("[%s] 调度已开启: %s", merchant_id, times)
    return times


def schedule_off(merchant_id: str) -> None:
    """关闭商家的自动调度 — 用户发 "auto off" 时调用

    1. 移除该商家在 APScheduler 中的所有闹钟
    2. 更新内存状态为 inactive
    """
    # 删除所有该商家的定时任务
    _remove_jobs(merchant_id)

    # 标记为非活跃
    if merchant_id in _schedule_state:
        _schedule_state[merchant_id]["active"] = False

    log.info("[%s] 调度已关闭", merchant_id)


def get_schedule_status(merchant_id: str) -> dict:
    """获取商家的调度状态 — 用户发 "auto status" 时调用

    Returns:
        {"active": True/False, "times": ["09:00", "14:00"], "channel_id": "C0..."}
        如果该商家从未开启过调度，返回默认的空状态
    """
    return _schedule_state.get(merchant_id, {"active": False, "times": [], "channel_id": ""})
