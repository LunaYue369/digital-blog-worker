"""博客预览服务 — 轻量 HTTP 静态文件服务 + HTML 渲染

功能：
1. 将博客数据 + 图片渲染为酷炫的 HTML 文件
2. 启动一个轻量 HTTP 服务供用户在浏览器中预览
3. 返回预览链接到 Slack
"""

import base64
import logging
import math
import os
import re
import threading
from datetime import date
from functools import partial
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path

import config as cfg

log = logging.getLogger(__name__)

# 预览服务是否已启动
_server_started = False
_server_lock = threading.Lock()


def _image_to_data_uri(image_path: Path) -> str:
    """将本地图片转为 data URI，嵌入到 HTML 中（避免跨域问题）"""
    if not image_path or not image_path.exists():
        return ""
    suffix = image_path.suffix.lower()
    mime = {".jpg": "image/jpeg", ".jpeg": "image/jpeg",
            ".png": "image/png", ".webp": "image/webp"}.get(suffix, "image/png")
    with open(image_path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode("utf-8")
    return f"data:{mime};base64,{b64}"


def render_blog_html(
    blog_data: dict,
    image_paths: dict[str, Path],
    merchant_cfg: dict,
    output_path: Path,
    template_file: str = "",
) -> str:
    """将博客数据渲染为完整的 HTML 文件

    Args:
        blog_data: 博客内容 {title, content_html, excerpt, tags, seo_slug, image_prompts}
        image_paths: 图片路径 {"hero": Path, "mid": Path, "end": Path}
        merchant_cfg: 商家配置
        output_path: HTML 输出路径
        template_file: 指定使用的模板文件名（留空则用默认模板）

    Returns:
        预览 URL
    """
    title = blog_data.get("title", "Untitled Blog Post")
    content_html = blog_data.get("content_html", "")
    excerpt = blog_data.get("excerpt", "")
    tags = blog_data.get("tags", [])

    # 商家品牌配色
    primary_color = merchant_cfg.get("brand_color_primary", "#1a1a2e")
    accent_color = merchant_cfg.get("brand_color_accent", "#e94560")
    store_name = merchant_cfg.get("store_name", "Blog")
    website = merchant_cfg.get("website", "#")

    # 图片处理 — 遍历所有槽位（兼容 hero/mid/end 和 img_1/img_2/.../img_N）
    image_alts = blog_data.get("image_alts", {})

    for slot, img_path in image_paths.items():
        placeholder = f"<!-- BLOG_IMAGE:{slot} -->"
        if img_path and Path(img_path).exists():
            data_uri = _image_to_data_uri(Path(img_path))
            alt = image_alts.get(slot, f"{title} - {slot}")
            # 第一张图用 eager loading，其余用 lazy
            loading = "eager" if slot in ("hero", "img_1") else "lazy"
            css_class = f"blog-image {slot}-image"
            replacement = f'<div class="{css_class}"><img src="{data_uri}" alt="{alt}" loading="{loading}"></div>'
            # 只替换第一个占位符（防止 copywriter 重复放同一个占位符导致图片出现两次）
            content_html = content_html.replace(placeholder, replacement, 1)
            # 清掉同一 slot 的多余占位符
            content_html = content_html.replace(placeholder, "")
        else:
            content_html = content_html.replace(placeholder, "")

    # 清理未被替换的占位符（图片缺失时）
    content_html = re.sub(r"<!-- BLOG_IMAGE:\w+ -->", "", content_html)

    # hero 背景改为品牌渐变，不再需要 HERO_IMAGE 变量
    hero_uri = ""

    # 安全网：Copywriter 偶尔编造 <img src="/images/xxx.jpg"> 假标签，直接删掉
    # （真图已通过占位符嵌入，不需要重复）
    fake_img_pattern = re.compile(r'<img\s+[^>]*src="(/images/[^"]+)"[^>]*/?\s*>')
    content_html, fake_count = fake_img_pattern.subn("", content_html)
    if fake_count:
        log.warning("安全网触发：删除了 %d 个 Copywriter 编造的假 <img> 标签", fake_count)

    # 安全措施：移除 HTML 标题中的 emoji/特殊 Unicode 字符（GPT 偶尔仍会生成）
    def _strip_emoji_from_headings(html: str) -> str:
        """移除 <h2>/<h3> 标签内的 emoji 和特殊 Unicode 符号"""
        import re as _re
        # 匹配常见 emoji 范围（包括 supplementary 和组合字符）
        emoji_pattern = _re.compile(
            "[\U0001F300-\U0001F9FF"   # 杂项符号和表情
            "\U00002702-\U000027B0"     # 装饰符号
            "\U0000FE00-\U0000FE0F"     # 变体选择符
            "\U0000200D"                # 零宽连接符
            "\U000024C2-\U0001F251"     # 封闭字母数字补充
            "]+", flags=_re.UNICODE
        )
        def _clean_heading(m):
            tag = m.group(1)
            attrs = m.group(2)
            content = emoji_pattern.sub("", m.group(3)).strip()
            return f"<{tag}{attrs}>{content}</{tag}>"
        html = _re.sub(r"<(h[23])([^>]*)>(.*?)</\1>", _clean_heading, html)
        return html

    content_html = _strip_emoji_from_headings(content_html)

    # 生成标签 HTML
    tags_html = "".join(f'<span class="tag">{tag}</span>' for tag in tags)

    # 读取 HTML 模板 — 从商家 templates/ 目录加载
    merchant_id = merchant_cfg.get("merchant_id", "")
    if template_file:
        from services.template_selector import get_template_path
        template_path = get_template_path(template_file, merchant_id=merchant_id)
    else:
        # 兼容旧逻辑：未指定时用默认模板（优先商家目录）
        merchant_tpl = cfg.MERCHANTS_DIR / merchant_id / "templates" / "blog_template.html"
        template_path = merchant_tpl if merchant_tpl.exists() else cfg.TEMPLATES_DIR / "blog_template.html"

    if template_path.exists():
        template = template_path.read_text(encoding="utf-8")
    else:
        log.warning("HTML 模板不存在: %s，使用内置模板", template_path)
        template = _FALLBACK_TEMPLATE

    # ── SEO 变量计算 ──
    seo_slug = blog_data.get("seo_slug", "blog-post")
    canonical_url = f"{website.rstrip('/')}/blog/{seo_slug}/"
    today = date.today()
    date_iso = today.isoformat()                      # 2026-03-31
    date_display = today.strftime("%B %d, %Y")        # March 31, 2026
    # 阅读时间：按英文 238 wpm 计算（取整，至少 1 分钟）
    word_count = len(re.sub(r"<[^>]+>", "", content_html).split())
    reading_time = str(max(1, math.ceil(word_count / 238)))

    # 填充模板变量
    html = template.replace("{{TITLE}}", title)
    html = html.replace("{{EXCERPT}}", excerpt)
    html = html.replace("{{CONTENT}}", content_html)
    html = html.replace("{{TAGS}}", tags_html)
    html = html.replace("{{HERO_IMAGE}}", hero_uri)
    html = html.replace("{{STORE_NAME}}", store_name)
    html = html.replace("{{WEBSITE}}", website)
    html = html.replace("{{PRIMARY_COLOR}}", primary_color)
    html = html.replace("{{ACCENT_COLOR}}", accent_color)
    # SEO 新增变量
    html = html.replace("{{CANONICAL_URL}}", canonical_url)
    html = html.replace("{{DATE_ISO}}", date_iso)
    html = html.replace("{{DATE_DISPLAY}}", date_display)
    html = html.replace("{{READING_TIME}}", reading_time)

    # 写入文件
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)

    # 构建预览 URL
    base_url = cfg.PREVIEW_BASE_URL.rstrip("/")
    # 路径格式: /merchant_id/filename.html
    merchant_id = merchant_cfg.get("merchant_id", "")
    relative_path = f"/{merchant_id}/{output_path.name}"
    preview_url = f"{base_url}{relative_path}"

    log.info("HTML 已保存: %s → %s", output_path, preview_url)
    return preview_url


def start_preview_server() -> None:
    """启动轻量 HTTP 静态文件预览服务（后台线程）只启动一次，后续调用自动跳过。
    每篇博客生成后就是一个独立的完整 HTML 文件，存在 output/{merchant_id}/ 下。
    打开效果就是一个完整的网页预览服务 http://localhost:8900/thouseirvine/thouseirvine_1711526400_a1b2.html
    """
    global _server_started

    with _server_lock:
        if _server_started:
            return
        _server_started = True

    host = cfg.PREVIEW_HOST
    port = cfg.PREVIEW_PORT
    serve_dir = str(cfg.OUTPUT_DIR)

    handler = partial(SimpleHTTPRequestHandler, directory=serve_dir)

    def _run():
        try:
            server = HTTPServer((host, port), handler)
            log.info("预览服务已启动: http://%s:%d (serving %s)", host, port, serve_dir)
            server.serve_forever()
        except Exception as exc:
            log.error("预览服务启动失败: %s", exc)

    thread = threading.Thread(target=_run, daemon=True, name="preview-server")
    thread.start()


# ── 内置 fallback 模板（仅在 templates/blog_template.html 不存在时使用）──
_FALLBACK_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{{TITLE}} — {{STORE_NAME}}</title>
    <meta name="description" content="{{EXCERPT}}">
    <style>
        body { font-family: system-ui, sans-serif; max-width: 800px; margin: 0 auto; padding: 20px; }
        h1 { font-size: 2em; }
        img { max-width: 100%; border-radius: 8px; }
        .tag { background: #eee; padding: 4px 12px; border-radius: 20px; margin-right: 8px; font-size: 0.85em; }
    </style>
</head>
<body>
    <h1>{{TITLE}}</h1>
    <p><em>{{EXCERPT}}</em></p>
    <div>{{CONTENT}}</div>
    <div style="margin-top:20px">{{TAGS}}</div>
</body>
</html>"""
