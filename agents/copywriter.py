"""SEO 文案撰写 Agent — 根据选题生成完整的 SEO 博客文章

职责：
1. 根据 Researcher 选出的主题撰写 SEO 博客
2. 输出完整的 HTML 格式文章 + 图片占位符
3. 包含 SEO meta 信息（slug, tags, excerpt, 图片 prompt）
"""

import json
import logging
import os

from openai import OpenAI

from agents.soul_loader import build_system_prompt
from services.usage_tracker import record_usage

log = logging.getLogger(__name__)

MODEL = os.getenv("BLOG_MODEL", "gpt-4.1")
_client: OpenAI | None = None


def _get_client() -> OpenAI:
    global _client
    if _client is None:
        _client = OpenAI(max_retries=3)
    return _client


def write_blog(
    merchant_id: str,
    topic: dict,
) -> tuple[dict, dict]:
    """根据选题撰写一篇完整的 SEO 博客

    Args:
        merchant_id: 商家标识
        topic: Researcher 选出的主题
            {title, primary_keyword, secondary_keywords, angle, why, estimated_word_count}

    Returns:
        (blog_data, token_usage)
        blog_data: {title, content_html, excerpt, tags, seo_slug, image_prompt, inline_images}
    """
    # 加载完整的copywriter人格
    system_prompt = build_system_prompt(merchant_id, "copywriter")
    client = _get_client()

    title = topic.get("title", "Untitled")
    primary_kw = topic.get("primary_keyword", "")
    secondary_kws = topic.get("secondary_keywords", [])
    angle = topic.get("angle", "")
    word_count = topic.get("estimated_word_count", 1200)

    user_prompt = f"""Write a complete SEO blog post based on the following brief.

                    ## Topic Brief
                    - **Title:** {title}
                    - **Primary Keyword:** {primary_kw}
                    - **Secondary Keywords:** {', '.join(secondary_kws)}
                    - **Angle:** {angle}
                    - **Target Word Count:** {word_count} words (range: {word_count - 200} to {word_count + 300})

                    ## SEO Requirements
                    1. Include the primary keyword in the title, first paragraph, at least 2 subheadings, and conclusion
                    2. Use secondary keywords naturally throughout the article (each at least once)
                    3. Write a compelling meta description (excerpt) under 160 characters
                    4. Use H2 and H3 headings with keyword-rich text
                    5. Include internal linking opportunities (mark with <!-- INTERNAL_LINK: topic suggestion -->)
                    6. Write alt text for all image placeholders

                    ## Content Quality Requirements
                    1. **Expert Depth** — Write as an industry authority with specific details, numbers, and actionable advice
                    2. **Scannable Structure** — Short paragraphs (2-3 sentences max), bullet lists, numbered steps
                    3. **Engaging Opening** — Hook the reader in the first 2 sentences with a relatable scenario or surprising fact
                    4. **Strong CTA** — End with a clear call-to-action that drives readers to contact the business
                    5. **No Fluff** — Every paragraph must provide value; cut generic filler

                    ## HTML Format
                    - Use semantic HTML: <h2>, <h3>, <p>, <ul>, <ol>, <blockquote>
                    - Add CSS classes for styling: "blog-section", "blog-highlight", "blog-cta"
                    - Include exactly 3 image placeholders: <!-- BLOG_IMAGE:hero -->, <!-- BLOG_IMAGE:mid -->, <!-- BLOG_IMAGE:end -->
                    - Do NOT include <html>, <head>, <body> tags — only the article body content
                    - Use <hr> between major sections

                    ## Output Format (JSON)
                    Return ONLY valid JSON:
                    {{
                        "title": "Final blog title (may refine from brief)",
                        "content_html": "Full article HTML body content",
                        "excerpt": "Meta description under 160 chars — compelling, keyword-rich",
                        "tags": ["tag1", "tag2", "tag3", "tag4", "tag5"],
                        "seo_slug": "url-friendly-slug-with-primary-keyword",
                        "image_prompts": {{
                            "hero": "Detailed prompt for the hero/banner image — professional, high-end, relevant to the topic. No text or logos in the image.",
                            "mid": "Prompt for mid-article image — illustrates a key point. No text or logos.",
                            "end": "Prompt for end-of-article image — reinforces the CTA or conclusion. No text or logos."
                        }}
                    }}"""

    # 调用gpt
    resp = client.chat.completions.create(
        model=MODEL,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0.7,
        max_tokens=8000,
        response_format={"type": "json_object"},
    )

    pt = resp.usage.prompt_tokens
    ct = resp.usage.completion_tokens
    usage = record_usage(merchant_id, "copywriter", MODEL, pt, ct)

    try:
        blog_data = json.loads(resp.choices[0].message.content)
    except json.JSONDecodeError:
        log.error("[%s] Copywriter JSON 解析失败: %s",
                  merchant_id, resp.choices[0].message.content[:200])
        blog_data = {
            "title": title,
            "content_html": "<p>Generation failed. Please retry.</p>",
            "excerpt": "",
            "tags": [],
            "seo_slug": "error",
            "image_prompts": {"hero": "", "mid": "", "end": ""},
        }

    word_count_actual = len(blog_data.get("content_html", "").split())
    log.info("[%s] Copywriter 完成: '%s' (~%d words)",
             merchant_id, blog_data.get("title", ""), word_count_actual)

    return blog_data, usage


def rewrite_blog(
    merchant_id: str,
    blog_data: dict,
    feedback: dict,
    round_num: int,
) -> tuple[dict, dict]:
    """根据 Reviewer 反馈重写博客

    Args:
        merchant_id: 商家标识
        blog_data: 当前草稿数据
        feedback: Reviewer 的反馈 {score, issues, suggestions}
        round_num: 当前修改轮次 (1-based)

    Returns:
        (revised_blog_data, token_usage)
    """
    system_prompt = build_system_prompt(merchant_id, "copywriter")
    client = _get_client()

    issues_text = "\n".join(f"- {i}" for i in feedback.get("issues", []))
    suggestions_text = "\n".join(f"- {s}" for s in feedback.get("suggestions", []))

    urgency = {
        1: "First revision. Fix all listed issues carefully.",
        2: "SECOND revision — previous fixes were insufficient. Be more aggressive.",
        3: "FINAL attempt. Make radical changes to address every single issue.",
    }.get(round_num, "Fix all issues.")

    user_prompt = f"""## REWRITE REQUEST (Round {round_num})

{urgency}

## Current Draft Title
{blog_data.get('title', '')}

## Current Content
{blog_data.get('content_html', '')}

## Reviewer Score: {feedback.get('score', 0)}/100

## Issues Found
{issues_text}

## Suggestions
{suggestions_text}

## Rewrite Rules
1. Keep the same TOPIC — fix the execution
2. Maintain all SEO keywords from the original
3. Keep image placeholders (<!-- BLOG_IMAGE:hero/mid/end -->)
4. Improve the specific areas flagged by the reviewer
5. Maintain or improve word count

## Output Format (JSON)
Return the same JSON structure as the original:
{{
    "title": "...",
    "content_html": "...",
    "excerpt": "...",
    "tags": [...],
    "seo_slug": "...",
    "image_prompts": {{"hero": "...", "mid": "...", "end": "..."}}
}}"""

    resp = client.chat.completions.create(
        model=MODEL,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0.75,
        max_tokens=8000,
        response_format={"type": "json_object"},
    )

    pt = resp.usage.prompt_tokens
    ct = resp.usage.completion_tokens
    usage = record_usage(merchant_id, f"copywriter_rewrite_r{round_num}", MODEL, pt, ct)

    try:
        revised = json.loads(resp.choices[0].message.content)
    except json.JSONDecodeError:
        log.error("[%s] Copywriter rewrite JSON 解析失败 (round %d)", merchant_id, round_num)
        revised = blog_data  # 解析失败时保留原稿

    log.info("[%s] Copywriter 重写完成 (round %d): '%s'",
             merchant_id, round_num, revised.get("title", ""))

    return revised, usage
