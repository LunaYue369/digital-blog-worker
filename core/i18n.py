"""多语言支持 — 根据用户语言返回对应的 UI 文本

所有面向用户的 Slack 消息文本集中在此管理。
语言检测基于 Unicode 字符范围判断（中文字符 → zh，其他 → en）。

使用：
    from core.i18n import t, detect_language

    lang = detect_language("帮我写一篇文章")  # → "zh"
    t("generating", lang)                     # → "正在生成博客..."
"""

import re

# ── 翻译表 ─────────────────────────────────────────────────
_STRINGS = {
    # ── 进度阶段（Chat 模式） ──
    "stage_template":       {"en": "Selecting template & layout",       "zh": "选择模板和布局"},
    "stage_web":            {"en": "Researching competitor articles",    "zh": "调研竞品文章"},
    "stage_write":          {"en": "Writing blog content",              "zh": "撰写博客内容"},
    "stage_review":         {"en": "Reviewing content quality",         "zh": "审核内容质量"},
    "stage_rewrite":        {"en": "Revising based on feedback",        "zh": "根据反馈修改中"},
    "stage_image":          {"en": "Processing images",                 "zh": "处理图片"},
    "stage_render":         {"en": "Assembling final preview",          "zh": "组装最终预览"},
    "stage_done":           {"en": "Complete!",                         "zh": "完成！"},

    # ── 进度阶段（Auto 模式） ──
    "stage_scrape":         {"en": "Scraping trending keywords",        "zh": "抓取热门关键词"},
    "stage_research":       {"en": "Analyzing SEO opportunities",       "zh": "分析 SEO 机会"},
    "stage_artist":         {"en": "Enhancing image prompts",           "zh": "优化图片提示词"},
    "stage_image_gen":      {"en": "Generating images (Seedream)",      "zh": "生成图片 (Seedream)"},
    "stage_render_html":    {"en": "Assembling final HTML",             "zh": "组装最终 HTML"},
    "stage_publish":        {"en": "Publishing to WordPress",           "zh": "发布到 WordPress"},

    # ── 进度消息 ──
    "generating_for":       {"en": "Generating blog for {name}",        "zh": "正在为 {name} 生成博客"},

    # ── 结果卡片 ──
    "blog_ready":           {"en": "Blog Ready for Review",             "zh": "博客已生成，请审核"},
    "blog_failed":          {"en": "Blog Generation Failed",            "zh": "博客生成失败"},
    "review_score":         {"en": "Review Score",                      "zh": "审核评分"},
    "tags":                 {"en": "Tags",                              "zh": "标签"},
    "images":               {"en": "image",                             "zh": "张图片"},
    "images_plural":        {"en": "images",                            "zh": "张图片"},
    "template":             {"en": "Template",                          "zh": "模板"},
    "layout":               {"en": "Layout",                            "zh": "布局"},
    "time":                 {"en": "Time",                              "zh": "耗时"},
    "cost":                 {"en": "Cost",                              "zh": "费用"},
    "open_preview":         {"en": "Open Preview in Browser",           "zh": "在浏览器中预览"},
    "reply_hint":           {"en": "Reply in this thread to request changes, or use the buttons below.", "zh": "在此对话中回复修改意见，或使用下方按钮操作。"},
    "publish_wp":           {"en": "Publish to WordPress",              "zh": "发布到 WordPress"},
    "regenerate":           {"en": "Regenerate",                        "zh": "重新生成"},

    # ── 确认按钮 ──
    "confirm_generate":     {"en": "Confirm & Generate",                "zh": "确认并生成"},
    "confirm_edit":         {"en": "Let me adjust...",                  "zh": "我再调整一下..."},

    # ── 状态提示 ──
    "starting_generation":  {"en": "Starting blog generation, please wait...",        "zh": "开始生成博客，请稍候..."},
    "still_generating":     {"en": "Still generating, please wait...",                "zh": "还在生成中，请稍候..."},
    "confirm_or_adjust":    {"en": "Click *Confirm & Generate* to start, or tell me what to adjust.", "zh": "点击 *确认并生成* 开始，或告诉我需要调整的地方。"},
    "click_or_change":      {"en": "Click a button above, or tell me what you'd like to change.",     "zh": "点击上方按钮，或告诉我你想修改什么。"},
    "no_problem_adjust":    {"en": "No problem! Tell me what you'd like to change.",  "zh": "没问题！告诉我你想调整什么。"},
    "regenerating":         {"en": "Regenerating blog from scratch...",               "zh": "正在从头重新生成博客..."},
    "session_expired":      {"en": "Session expired. Please start a new conversation.", "zh": "会话已过期，请开始新的对话。"},
    "working_on_mods":      {"en": "Working on your modifications...",                "zh": "正在处理你的修改..."},
    "no_draft_to_modify":   {"en": "No existing draft to modify — generating a new blog instead.", "zh": "没有可修改的草稿，将生成新博客。"},
    "gen_failed":           {"en": "Blog generation failed: {error}\n\nPlease try again or adjust your request.", "zh": "博客生成失败：{error}\n\n请重试或调整你的要求。"},

    # ── 图片上传 ──
    "received_images":      {"en": "Received {count} image(s)",         "zh": "已收到 {count} 张图片"},
    "total_images":         {"en": "total: {total}",                    "zh": "共 {total} 张"},

    # ── 发布 ──
    "publishing":           {"en": "Publishing *{title}* to WordPress...",  "zh": "正在将 *{title}* 发布到 WordPress..."},
    "publish_failed":       {"en": "Publish failed:",                       "zh": "发布失败："},
    "publish_error":        {"en": "Publish error:",                        "zh": "发布出错："},

    # ── 其他 ──
    "round":                {"en": "round",                             "zh": "轮"},
    "rounds":               {"en": "rounds",                            "zh": "轮"},
}


def t(key: str, lang: str = "en", **kwargs) -> str:
    """获取翻译文本

    Args:
        key:    翻译键名
        lang:   语言代码 ("en" 或 "zh")
        kwargs: 格式化参数

    Returns:
        翻译后的字符串
    """
    entry = _STRINGS.get(key, {})
    text = entry.get(lang, entry.get("en", key))
    if kwargs:
        text = text.format(**kwargs)
    return text


# ── 中文字符范围 ──
_CJK_RE = re.compile(
    r'[\u4e00-\u9fff\u3400-\u4dbf\uf900-\ufaff]'
)


def detect_language(text: str) -> str:
    """根据文本内容检测语言

    简单策略：包含中文字符 → "zh"，否则 → "en"

    Args:
        text: 用户消息文本

    Returns:
        "zh" 或 "en"
    """
    if _CJK_RE.search(text or ""):
        return "zh"
    return "en"
