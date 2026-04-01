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
    layout_prompt: str = "",
    research_brief: str = "",
) -> tuple[dict, dict]:
    """根据选题撰写一篇完整的 SEO 博客

    Args:
        merchant_id: 商家标识
        topic: Researcher 选出的主题
            {title, primary_keyword, secondary_keywords, angle, why, estimated_word_count}
        layout_prompt: 布局风格指令（由 template_selector 选出，注入到 prompt 中引导不同结构）
        research_brief: Web 调研摘要（搜索竞品文章后 LLM 提炼的要点，提升内容深度）

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

    # 布局风格指令 — 如果有的话，会引导 GPT 生成不同结构的文章
    layout_section = f"\n{layout_prompt}\n" if layout_prompt else ""

    # Web 调研摘要 — 如果有的话，注入竞品要点帮助 Copywriter 写出更有深度的内容
    research_section = ""
    if research_brief:
        research_section = f"""
                    ## Competitor Research & Market Intelligence
                    The following insights were gathered from top-ranking articles on this topic.
                    Use these data points and perspectives to write a SUPERIOR article — do NOT copy, but incorporate the facts and fill the gaps.

                    {research_brief}
                    """

    user_prompt = f"""Write a complete SEO blog post based on the following brief.

                    ## Topic Brief
                    - **Title:** {title}
                    - **Primary Keyword:** {primary_kw}
                    - **Secondary Keywords:** {', '.join(secondary_kws)}
                    - **Angle:** {angle}
                    - **Target Word Count:** {word_count} words (range: {word_count - 200} to {word_count + 300})
                    {layout_section}{research_section}
                    ## SEO Requirements (CRITICAL — follow ALL rules)
                    1. **H1 = title only** — The H1 is in the template. Do NOT include any <h1> in your content.
                    2. **Primary keyword in first 100 words** — MUST appear naturally in the opening paragraph.
                    3. **H2 headings with keywords** — Every <h2> MUST contain a secondary keyword or long-tail phrase. Aim for 4-7 H2 sections.
                    4. **H3 for sub-sections** — Nest under H2. Never skip heading levels.
                    5. **Secondary keywords** — Each secondary keyword MUST appear at least once naturally in the article body.
                    6. **Meta description (excerpt)** — MUST be 150-160 characters, include the primary keyword, and end with a CTA.
                    7. **Internal links** — Include 3-5 links to the business website (services, about, contact pages). Use keyword-rich anchor text.
                    8. **External links** — Include 1-2 links to authoritative external sources (industry organizations, manufacturers, government sites).
                    9. **Image alt text** — Write descriptive alt text that naturally includes a keyword for each image placeholder.
                    10. **No emoji** — Do NOT use any emoji or Unicode icons (e.g. 🛡️ 💰 ✅ 🔧) anywhere in the content. They break rendering. Plain text only.

                    ## FAQ Section (REQUIRED)
                    Every article MUST end with a FAQ section BEFORE the CTA. Use this exact HTML:
                    ```
                    <div class="faq-section">
                      <h2>Frequently Asked Questions</h2>
                      <div class="faq-item"><h3>Question?</h3><p>Answer.</p></div>
                      <!-- 3-5 Q&A pairs total -->
                    </div>
                    ```
                    - Questions should match "People Also Ask" style queries related to the topic
                    - Answers should be concise (2-3 sentences) and directly answer the question

                    ## Content Quality Requirements
                    1. **Expert Depth** — Write as an industry authority with specific details, numbers, and actionable advice
                    2. **Scannable Structure** — Short paragraphs (2-3 sentences max), bullet lists, numbered steps
                    3. **Engaging Opening** — Hook the reader in the first 2 sentences with a relatable scenario or surprising fact
                    4. **Strong CTA** — End with a clear call-to-action that drives readers to contact the business (AFTER the FAQ)
                    5. **No Fluff** — Every paragraph must provide value; cut generic filler

                    ## HTML Format
                    - Use semantic HTML: <h2>, <h3>, <p>, <ul>, <ol>, <blockquote>
                    - Add CSS classes for styling: "blog-section", "blog-highlight", "blog-cta", "faq-section", "faq-item"
                    - Include exactly 3 image placeholders as HTML comments: <!-- BLOG_IMAGE:hero -->, <!-- BLOG_IMAGE:mid -->, <!-- BLOG_IMAGE:end -->
                    - CRITICAL: Image placeholders MUST be the exact HTML comments above. Do NOT write <img> tags, do NOT invent image URLs like /images/xxx.jpg. The image system replaces these comment placeholders with real images later. Any <img> tag you write will show as a broken image.
                    - Do NOT include <html>, <head>, <body>, or <h1> tags — only the article body content
                    - ABSOLUTELY NO emoji, icons, or special Unicode characters anywhere in the HTML — not in headings, not in paragraphs, not in lists. Use plain ASCII text only. Emoji will break rendering.
                    - Use <hr> between major sections
                    - Article structure order: Opening → Sections → FAQ → CTA

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
                        }},
                        "image_alts": {{
                            "hero": "SEO-optimized alt text for hero image — describe the scene, include primary keyword naturally",
                            "mid": "SEO-optimized alt text for mid image — describe what is shown, include a relevant keyword",
                            "end": "SEO-optimized alt text for end image — describe the scene, include brand or location keyword"
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
