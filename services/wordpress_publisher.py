"""WordPress 发布服务 — 通过 WP REST API 发布博客文章

功能：
1. 上传图片到 WordPress 媒体库（返回媒体 ID 和 URL）
2. 创建/查找 WordPress 标签（tags）
3. 替换文章内容中的 base64 图片为 WP 媒体库 URL
4. 发布文章（支持 private/draft/publish 状态）
5. 设置 featured image（特色图片）

认证方式：WordPress Application Passwords
凭据从 .env 读取，格式：WP_{MERCHANT_ID}_USER / WP_{MERCHANT_ID}_PASSWORD
WP 站点 URL 从 merchant.json 的 wordpress_url 字段读取

使用方式：
    publisher = WordPressPublisher(merchant_id, merchant_cfg)
    result = publisher.publish_blog(blog_data, image_paths)
"""

import logging
import mimetypes
import re
from pathlib import Path

import requests

log = logging.getLogger(__name__)

# ── 请求超时（秒）──────────────────────────────────────────
_TIMEOUT = 30
_UPLOAD_TIMEOUT = 60  # 图片上传可能较慢


class WordPressPublisher:
    """WordPress REST API 客户端 — 负责图片上传和文章发布"""

    def __init__(self, merchant_id: str, merchant_cfg: dict):
        """初始化 WordPress 发布器

        从 merchant_cfg 获取 WP 站点 URL，从环境变量获取认证凭据。

        Args:
            merchant_id: 商家标识（如 "thouseirvine"）
            merchant_cfg: 商家配置字典（必须包含 wordpress_url 和 wordpress_env_prefix）
        """
        self.merchant_id = merchant_id

        # WordPress 站点 URL（从 merchant.json 读取）
        self.wp_url = merchant_cfg.get("wordpress_url", "").rstrip("/")
        if not self.wp_url:
            raise ValueError(f"[{merchant_id}] merchant.json 缺少 wordpress_url 配置")

        # REST API 基础路径
        self.api_base = f"{self.wp_url}/wp-json/wp/v2"

        # 从 merchant.json 读取认证凭据
        self.wp_user = merchant_cfg.get("wordpress_user", "")
        self.wp_password = merchant_cfg.get("wordpress_password", "")

        if not self.wp_user or not self.wp_password:
            raise ValueError(
                f"[{merchant_id}] 缺少 WordPress 凭据，请在 merchant.json 中配置 "
                f"wordpress_user 和 wordpress_password"
            )

        # requests 的 HTTP Basic Auth（WP Application Password 用这种方式认证）
        self._auth = (self.wp_user, self.wp_password)

        log.info("[%s] WordPress 发布器初始化完成: %s", merchant_id, self.wp_url)

    # ── 图片上传 ────────────────────────────────────────────

    def upload_image(self, image_path: Path, alt_text: str = "") -> dict:
        """上传一张图片到 WordPress 媒体库

        Args:
            image_path: 本地图片文件路径
            alt_text: 图片的 alt 文本（SEO 用）

        Returns:
            {"id": 媒体ID, "url": 图片URL, "success": True/False}
        """
        if not image_path or not image_path.exists():
            log.warning("[%s] 图片文件不存在: %s", self.merchant_id, image_path)
            return {"id": 0, "url": "", "success": False}

        # 读取图片文件
        filename = image_path.name
        mime_type = mimetypes.guess_type(filename)[0] or "image/png"

        try:
            with open(image_path, "rb") as f:
                image_data = f.read()

            # 通过 WP REST API 上传
            # 请求头里的 Content-Disposition 告诉 WP 文件名
            headers = {
                "Content-Type": mime_type,
                "Content-Disposition": f'attachment; filename="{filename}"',
            }

            resp = requests.post(
                f"{self.api_base}/media",
                auth=self._auth,
                headers=headers,
                data=image_data,
                timeout=_UPLOAD_TIMEOUT,
            )
            resp.raise_for_status()

            media = resp.json()
            media_id = media.get("id", 0)
            media_url = media.get("source_url", "")

            # 如果提供了 alt_text，更新图片的 alt 属性（需要额外一次请求）
            if alt_text and media_id:
                try:
                    requests.post(
                        f"{self.api_base}/media/{media_id}",
                        auth=self._auth,
                        json={"alt_text": alt_text},
                        timeout=_TIMEOUT,
                    )
                except Exception:
                    pass  # alt_text 更新失败不影响主流程

            log.info("[%s] 图片上传成功: %s → ID=%d URL=%s",
                     self.merchant_id, filename, media_id, media_url)

            return {"id": media_id, "url": media_url, "success": True}

        except requests.RequestException as exc:
            log.error("[%s] 图片上传失败 (%s): %s", self.merchant_id, filename, exc)
            return {"id": 0, "url": "", "success": False}

    # ── 标签处理 ────────────────────────────────────────────

    def _get_or_create_tag(self, tag_name: str) -> int:
        """获取已有的 WordPress 标签 ID，如果不存在则创建

        WordPress 标签是全局的，同名标签只会有一个。
        先搜索，找到就返回 ID；找不到就创建新的。

        Args:
            tag_name: 标签名称（如 "ceramic coating"）

        Returns:
            标签 ID（失败返回 0）
        """
        try:
            # 先搜索已有标签
            resp = requests.get(
                f"{self.api_base}/tags",
                auth=self._auth,
                params={"search": tag_name, "per_page": 5},
                timeout=_TIMEOUT,
            )
            resp.raise_for_status()
            tags = resp.json()

            # 精确匹配（搜索是模糊的，需要二次过滤）
            for tag in tags:
                if tag.get("name", "").lower() == tag_name.lower():
                    return tag["id"]

            # 没找到 → 创建新标签
            resp = requests.post(
                f"{self.api_base}/tags",
                auth=self._auth,
                json={"name": tag_name},
                timeout=_TIMEOUT,
            )
            resp.raise_for_status()
            new_tag = resp.json()
            tag_id = new_tag.get("id", 0)
            log.info("[%s] 创建新标签: %s → ID=%d", self.merchant_id, tag_name, tag_id)
            return tag_id

        except requests.RequestException as exc:
            log.error("[%s] 标签处理失败 (%s): %s", self.merchant_id, tag_name, exc)
            return 0

    def _resolve_tags(self, tag_names: list[str]) -> list[int]:
        """将标签名称列表转换为 WordPress 标签 ID 列表

        Args:
            tag_names: 标签名称列表（如 ["ppf", "ceramic coating", "tesla"]）

        Returns:
            标签 ID 列表
        """
        tag_ids = []
        for name in tag_names:
            name = name.strip()
            if not name:
                continue
            tag_id = self._get_or_create_tag(name)
            if tag_id:
                tag_ids.append(tag_id)
        return tag_ids

    # ── 内容处理 ────────────────────────────────────────────

    @staticmethod
    def _replace_base64_images(content_html: str, image_url_map: dict[str, str]) -> str:
        """替换文章内容中的 base64 data URI 为 WordPress 媒体库 URL

        blog_generator 渲染 HTML 时会把图片嵌入为 base64 data URI。
        发布到 WordPress 前需要替换为媒体库的真实 URL。

        Args:
            content_html: 包含 base64 图片的 HTML 内容
            image_url_map: {slot: wp_url}，如 {"hero": "https://xxx.com/wp-content/...jpg"}

        Returns:
            替换后的 HTML 内容
        """
        # 找到所有 data:image/...;base64,... 格式的 URI
        # 按照 CSS class 来匹配 slot（hero-inline, mid-image, end-image）
        slot_class_map = {
            "hero": "hero-inline",
            "mid": "mid-image",
            "end": "end-image",
        }

        for slot, wp_url in image_url_map.items():
            if not wp_url:
                continue

            css_class = slot_class_map.get(slot, "")
            if css_class:
                # 匹配包含特定 class 的 <img> 标签中的 src="data:..."
                # 将整个 data URI 替换为 WP URL
                pattern = (
                    rf'(<div\s+class="blog-image\s+{re.escape(css_class)}">'
                    rf'\s*<img\s+src=")data:image/[^"]*(")'
                )
                replacement = rf'\g<1>{wp_url}\g<2>'
                content_html = re.sub(pattern, replacement, content_html, count=1)

        return content_html

    # ── 发布文章 ────────────────────────────────────────────

    def publish_blog(
        self,
        blog_data: dict,
        image_paths: dict[str, Path],
        status: str = "private",
    ) -> dict:
        """发布一篇博客文章到 WordPress

        完整流程：
        1. 上传 3 张图片（hero/mid/end）到媒体库
        2. 替换 content_html 中的 base64 为 WP 图片 URL
        3. 创建/查找 WordPress 标签
        4. 创建文章（设置 featured image + tags + status）

        Args:
            blog_data: 博客数据字典
                {title, content_html, excerpt, tags, seo_slug, image_prompts}
            image_paths: 本地图片路径
                {"hero": Path, "mid": Path, "end": Path}
            status: 文章状态
                "private"  — 仅登录用户可见（默认）
                "draft"    — 草稿
                "publish"  — 公开发布

        Returns:
            {
                "success": True/False,
                "post_id": WordPress 文章 ID,
                "post_url": 文章 URL,
                "edit_url": 后台编辑 URL,
                "error": 错误信息（失败时）,
            }
        """
        title = blog_data.get("title", "Untitled")
        content_html = blog_data.get("content_html", "")
        excerpt = blog_data.get("excerpt", "")
        tags = blog_data.get("tags", [])
        seo_slug = blog_data.get("seo_slug", "")

        log.info("[%s] 开始发布到 WordPress: %s", self.merchant_id, title)

        # ── Step 1: 上传图片到媒体库 ──────────────────────
        log.info("[%s] Step 1: 上传图片到媒体库...", self.merchant_id)
        uploaded_images = {}   # {slot: {"id": int, "url": str}}
        image_url_map = {}     # {slot: url} — 用于替换 content 中的 base64

        for slot in ["hero", "mid", "end"]:
            img_path = image_paths.get(slot)
            if not img_path:
                continue

            # 用标题作为 alt text 的一部分（SEO 友好）
            alt_text = f"{title} - {slot} image"
            result = self.upload_image(img_path, alt_text=alt_text)

            if result["success"]:
                uploaded_images[slot] = result
                image_url_map[slot] = result["url"]
            else:
                log.warning("[%s] %s 图片上传失败，跳过", self.merchant_id, slot)

        log.info("[%s] 图片上传完成: %d/3 成功", self.merchant_id, len(uploaded_images))

        # ── Step 2: 替换 content 中的 base64 图片 ─────────
        if image_url_map:
            content_html = self._replace_base64_images(content_html, image_url_map)
            log.info("[%s] Step 2: 已替换 %d 张图片的 base64 → WP URL", self.merchant_id, len(image_url_map))

        # ── Step 3: 处理标签 ──────────────────────────────
        log.info("[%s] Step 3: 处理标签...", self.merchant_id)
        tag_ids = self._resolve_tags(tags)
        log.info("[%s] 标签处理完成: %d 个标签", self.merchant_id, len(tag_ids))

        # ── Step 4: 创建文章 ──────────────────────────────
        log.info("[%s] Step 4: 创建文章 (status=%s)...", self.merchant_id, status)

        post_data = {
            "title": title,
            "content": content_html,
            "excerpt": excerpt,
            "slug": seo_slug,
            "status": status,
            "tags": tag_ids,
            "comment_status": "open",   # 允许评论（SEO 互动信号）
            "ping_status": "open",      # 允许 pingback
        }

        # 设置 featured image（特色图片）— 用 hero 图
        hero_media = uploaded_images.get("hero")
        if hero_media:
            post_data["featured_media"] = hero_media["id"]

        try:
            resp = requests.post(
                f"{self.api_base}/posts",
                auth=self._auth,
                json=post_data,
                timeout=_TIMEOUT,
            )
            resp.raise_for_status()

            post = resp.json()
            post_id = post.get("id", 0)
            post_url = post.get("link", "")
            edit_url = f"{self.wp_url}/wp-admin/post.php?post={post_id}&action=edit"

            log.info("[%s] 文章发布成功: ID=%d URL=%s", self.merchant_id, post_id, post_url)

            return {
                "success": True,
                "post_id": post_id,
                "post_url": post_url,
                "edit_url": edit_url,
                "status": status,
                "images_uploaded": len(uploaded_images),
                "tags_count": len(tag_ids),
            }

        except requests.RequestException as exc:
            error_msg = str(exc)
            # 尝试从响应体获取更详细的错误信息
            try:
                error_detail = exc.response.json()
                error_msg = error_detail.get("message", error_msg)
            except Exception:
                pass

            log.error("[%s] 文章发布失败: %s", self.merchant_id, error_msg)
            return {
                "success": False,
                "post_id": 0,
                "post_url": "",
                "edit_url": "",
                "error": error_msg,
            }

    # ── 连接测试 ────────────────────────────────────────────

    def test_connection(self) -> dict:
        """测试 WordPress 连接是否正常

        验证凭据有效性和 REST API 可访问性。

        Returns:
            {"success": True/False, "user": 用户名, "error": 错误信息}
        """
        try:
            resp = requests.get(
                f"{self.api_base}/users/me",
                auth=self._auth,
                timeout=_TIMEOUT,
            )
            resp.raise_for_status()
            user = resp.json()
            name = user.get("name", "Unknown")
            log.info("[%s] WordPress 连接测试成功: %s", self.merchant_id, name)
            return {"success": True, "user": name}

        except requests.RequestException as exc:
            log.error("[%s] WordPress 连接测试失败: %s", self.merchant_id, exc)
            return {"success": False, "user": "", "error": str(exc)}
