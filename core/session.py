"""Chat 会话状态管理 — 每个 Slack thread 对应一个独立会话

状态机（与 auto 流程完全隔离，仅用于 @Bot chat / 自由对话）：

  GATHERING  →  CONFIRMING  →  GENERATING  →  REVIEWING  →  DONE
       ^            |                                        |
       │            └── (用户继续修改 → 回到 GATHERING)        │
       └─────────────────────────────────────────────────────┘  (用户要求修改时回到 GATHERING)

每个会话存储：
  - 对话历史 messages[]            给 GPT 做上下文理解
  - 提取参数 params{}              主题、关键词、图片模式等
  - 创意简报 creative_brief{}      多轮对话综合生成的详细写作指令（内容结构、用户文本、语气等）
  - 图片要求 user_image_requests{} 用户对每张图的自然语言描述（"img_2": "夕阳下Tesla侧面"）
  - 用户图片 user_images[]         用户从 Slack 上传的图片本地路径
  - 当前草稿 draft{}               生成完毕后存放预览结果
  - token 用量 usage{}             本次会话的 API 调用统计

使用示例：
  # 用户在 Slack 发送 "@Bot 帮我写一篇 Tesla Model Y 贴膜的文章"
  # main.py 路由到 chat 流程：
  sess = get_or_create("1234567890.123456", "C0ANTP265MK")
  # → 返回新会话 {"thread_ts": "1234567890.123456", "stage": "gathering_info", ...}

  # 用户继续补充信息后，conversation agent 判断 ready=True
  update_stage("1234567890.123456", GENERATING)
  # → 会话进入生成状态

  # 生成完毕，等待用户审核
  update_stage("1234567890.123456", REVIEWING)
  # → 用户可以点按钮或打字修改

线程安全：所有操作都通过 _lock 保护，可在多个 Slack 事件线程中并发调用。
"""

import threading
import time

# ── 全局线程锁 — 保护 _sessions 字典的读写 ──
_lock = threading.Lock()

# ── 所有活跃会话 — key 是 Slack thread_ts（消息时间戳） ──
# 示例: {"1234567890.123456": {会话数据...}, ...}
_sessions: dict[str, dict] = {}

# ── 会话状态常量 ──────────────────────────────────────────
GATHERING = "gathering_info"   # 正在收集信息（对话中）
CONFIRMING = "confirming"      # 参数确认中，等待用户点 Confirm 或继续修改
GENERATING = "generating"      # 正在调用 pipeline 生成博客
REVIEWING = "reviewing"        # 生成完毕，等待用户审核/修改
DONE = "done"                  # 本轮完成（用户满意或已发布）


def get_or_create(thread_ts: str, channel: str) -> dict:
    """获取已有会话，或创建一个新会话

    Args:
        thread_ts: Slack 消息的 thread timestamp（线程唯一标识）
                   示例: "1234567890.123456"
        channel:   Slack 频道 ID
                   示例: "C0ANTP265MK"

    Returns:
        会话字典，包含完整的会话状态。示例:
        {
            "thread_ts": "1234567890.123456",
            "channel": "C0ANTP265MK",
            "stage": "gathering_info",
            "messages": [],
            "params": {},
            "creative_brief": {},
            "user_image_requests": {},
            "user_images": [],
            "draft": {},
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "api_calls": 0, "estimated_cost": 0.0},
            "created_at": 1712100000.0
        }
    """
    with _lock:
        if thread_ts not in _sessions:
            _sessions[thread_ts] = {
                "thread_ts": thread_ts,
                "channel": channel,
                "stage": GATHERING,
                "messages": [],           # 完整对话历史，格式: [{"role": "user"/"assistant", "content": "..."}]
                "params": {},             # GPT 提取的生成参数（主题、关键词、图片模式等）
                "creative_brief": {},     # 多轮对话综合生成的创意简报（内容结构、用户文本、语气等）
                                          # 示例: {
                                          #   "content_structure": [{"section": "开头", "requirement": "..."}],
                                          #   "user_provided_text": {"price_data": "Model Y 全车 $6500"},
                                          #   "tone": "professional but approachable",
                                          #   "special_requests": "强调XPEL认证"
                                          # }
                "user_image_requests": {}, # 用户对每张图的自然语言描述
                                          # 示例: {"img_2": "夕阳下Tesla侧面施工特写，暖色调",
                                          #         "img_4": "价格对比图，简洁风格"}
                "user_images": [],        # 用户上传的图片本地路径列表
                "language": "en",         # 用户语言（"zh" 或 "en"），根据首条消息自动检测
                "draft": {},              # 当前草稿: {"result": pipeline 返回的 dict, "session_id": "..."}
                "usage": {                # 本次会话的 token 用量统计
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "api_calls": 0,
                    "estimated_cost": 0.0,
                },
                "created_at": time.time(),
            }
        return _sessions[thread_ts]


def get(thread_ts: str) -> dict | None:
    """获取已有会话（不创建）

    Args:
        thread_ts: Slack thread timestamp

    Returns:
        会话字典，不存在时返回 None
    """
    with _lock:
        return _sessions.get(thread_ts)


def update_stage(thread_ts: str, stage: str):
    """更新会话状态

    Args:
        thread_ts: Slack thread timestamp
        stage:     新状态（GATHERING / GENERATING / REVIEWING / DONE）

    示例:
        update_stage("1234567890.123456", GENERATING)
        # → 会话从 gathering_info 变为 generating
    """
    with _lock:
        if thread_ts in _sessions:
            _sessions[thread_ts]["stage"] = stage


MAX_MESSAGES_PER_THREAD = 20  # 保留最近 N 条（约 10 轮对话）


def add_message(thread_ts: str, role: str, content: str):
    """添加一条对话记录到历史（超过上限时丢弃最早的消息）

    Args:
        thread_ts: Slack thread timestamp
        role:      "user" 或 "assistant"
        content:   消息文本

    示例:
        add_message("1234567890.123456", "user", "帮我写一篇关于 PPF 贴膜的文章")
        add_message("1234567890.123456", "assistant", "好的！请问想侧重哪个方面？")
    """
    with _lock:
        if thread_ts in _sessions:
            msgs = _sessions[thread_ts]["messages"]
            msgs.append({
                "role": role,
                "content": content,
            })
            if len(msgs) > MAX_MESSAGES_PER_THREAD:
                _sessions[thread_ts]["messages"] = msgs[-MAX_MESSAGES_PER_THREAD:]


def add_user_image(thread_ts: str, path: str):
    """记录用户上传的图片路径（自动去重）

    Args:
        thread_ts: Slack thread timestamp
        path:      图片本地路径，如 "C:/Users/Luna/repos/digital-blog-worker/output/uploads/20260403_120000_photo.jpg"

    示例:
        add_user_image("1234567890.123456", "/path/to/20260403_car.jpg")
        add_user_image("1234567890.123456", "/path/to/20260403_car.jpg")  # 重复，不会再次添加
        # → user_images = ["/path/to/20260403_car.jpg"]
    """
    with _lock:
        if thread_ts in _sessions:
            if path not in _sessions[thread_ts]["user_images"]:
                _sessions[thread_ts]["user_images"].append(path)


def add_usage(thread_ts: str, prompt_tokens: int, completion_tokens: int, cost: float):
    """累加本次会话的 token 用量

    Args:
        thread_ts:         Slack thread timestamp
        prompt_tokens:     输入 token 数
        completion_tokens: 输出 token 数
        cost:             本次调用的预估费用（美元）

    示例:
        add_usage("1234567890.123456", 500, 200, 0.003)
        # → usage = {"prompt_tokens": 500, "completion_tokens": 200, "api_calls": 1, "estimated_cost": 0.003}
        add_usage("1234567890.123456", 300, 100, 0.002)
        # → usage = {"prompt_tokens": 800, "completion_tokens": 300, "api_calls": 2, "estimated_cost": 0.005}
    """
    with _lock:
        if thread_ts in _sessions:
            u = _sessions[thread_ts]["usage"]
            u["prompt_tokens"] += prompt_tokens
            u["completion_tokens"] += completion_tokens
            u["api_calls"] += 1
            u["estimated_cost"] = round(u["estimated_cost"] + cost, 6)


def cleanup_old(max_age_hours: int = 24) -> int:
    """清理超过指定时间的过期会话，释放内存

    Args:
        max_age_hours: 最大存活时间（小时），默认 24 小时

    Returns:
        被清理的会话数量

    示例:
        cleaned = cleanup_old(12)  # 清理超过 12 小时的会话
        # → 返回 3（清理了 3 个过期会话）
    """
    cutoff = time.time() - max_age_hours * 3600
    with _lock:
        expired = [ts for ts, s in _sessions.items() if s["created_at"] < cutoff]
        for ts in expired:
            del _sessions[ts]
    return len(expired)
