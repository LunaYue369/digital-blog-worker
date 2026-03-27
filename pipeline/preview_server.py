"""博客预览服务 — 轻量 HTTP 静态文件服务 + HTML 渲染

功能：
1. 将博客数据 + 图片渲染为酷炫的 HTML 文件
2. 启动一个轻量 HTTP 服务供用户在浏览器中预览
3. 返回预览链接到 Slack
"""

import base64
import logging
import os
import threading
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
) -> str:
    """将博客数据渲染为完整的 HTML 文件

    Args:
        blog_data: 博客内容 {title, content_html, excerpt, tags, seo_slug, image_prompts}
        image_paths: 图片路径 {"hero": Path, "mid": Path, "end": Path}
        merchant_cfg: 商家配置
        output_path: HTML 输出路径

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

    # 图片处理 — 嵌入 data URI 或留空
    hero_uri = _image_to_data_uri(image_paths.get("hero")) if image_paths.get("hero") else ""
    mid_uri = _image_to_data_uri(image_paths.get("mid")) if image_paths.get("mid") else ""
    end_uri = _image_to_data_uri(image_paths.get("end")) if image_paths.get("end") else ""

    # 替换内容中的图片占位符
    if hero_uri:
        content_html = content_html.replace(
            "<!-- BLOG_IMAGE:hero -->",
            f'<div class="blog-image hero-inline"><img src="{hero_uri}" alt="{title}" loading="lazy"></div>'
        )
    if mid_uri:
        content_html = content_html.replace(
            "<!-- BLOG_IMAGE:mid -->",
            f'<div class="blog-image mid-image"><img src="{mid_uri}" alt="Article illustration" loading="lazy"></div>'
        )
    if end_uri:
        content_html = content_html.replace(
            "<!-- BLOG_IMAGE:end -->",
            f'<div class="blog-image end-image"><img src="{end_uri}" alt="Professional service" loading="lazy"></div>'
        )

    # 生成标签 HTML
    tags_html = "".join(f'<span class="tag">{tag}</span>' for tag in tags)

    # 读取 HTML 模板
    template_path = cfg.TEMPLATES_DIR / "blog_template.html"
    if template_path.exists():
        template = template_path.read_text(encoding="utf-8")
    else:
        log.warning("HTML 模板不存在，使用内置模板")
        template = _FALLBACK_TEMPLATE

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
