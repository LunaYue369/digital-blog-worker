"""图片 Prompt 美化师 Agent — 将简单描述转化为高质量 Seedream 生图 prompt

参考 digital-media-worker 的 media_engineer 人格，为博客配图生成
专业的、高大上的 AI 图片 prompt。
"""

import json
import logging
import os

from openai import OpenAI

from agents.soul_loader import build_system_prompt
from services.usage_tracker import record_usage

log = logging.getLogger(__name__)

MODEL = os.getenv("RESEARCH_MODEL", "gpt-4.1-mini")
_client: OpenAI | None = None


def _get_client() -> OpenAI:
    global _client
    if _client is None:
        _client = OpenAI(max_retries=3)
    return _client


def enhance_image_prompts(
    merchant_id: str,
    image_prompts: dict[str, str],
    blog_title: str,
    blog_excerpt: str,
) -> tuple[dict[str, str], dict]:
    """美化博客配图的 Seedream prompt

    将 Copywriter 生成的简单图片描述优化为高质量的 Seedream 生图 prompt，
    确保生成的图片专业、高大上、适合企业级博客。

    Args:
        merchant_id: 商家标识
        image_prompts: Copywriter 输出的原始 prompt {"hero": "...", "mid": "...", "end": "..."}
        blog_title: 博客标题（提供上下文）
        blog_excerpt: 博客摘要（提供上下文）

    Returns:
        (enhanced_prompts, token_usage)
        enhanced_prompts: {"hero": "optimized prompt", "mid": "...", "end": "..."}
    """
    system_prompt = build_system_prompt(merchant_id, "artist")
    client = _get_client()

    user_prompt = f"""Enhance the following image prompts for a professional business blog.

                    ## Blog Context
                    - **Title:** {blog_title}
                    - **Summary:** {blog_excerpt}

                    ## Original Image Prompts (from copywriter)
                    - **Hero (banner):** {image_prompts.get('hero', 'No prompt provided')}
                    - **Mid-article:** {image_prompts.get('mid', 'No prompt provided')}
                    - **End-article (CTA):** {image_prompts.get('end', 'No prompt provided')}

                    ## Enhancement Requirements
                    1. Each prompt should be 80-150 words, highly detailed
                    2. Style: Professional, high-end, corporate-quality photography or 3D rendering
                    3. Include specific details: lighting, composition, color palette, perspective, texture
                    4. NO text, logos, watermarks, or brand names in the image
                    5. Hero image should be the most visually striking — wide composition (16:9 friendly)
                    6. Mid image should illustrate a specific concept from the article
                    7. End image should evoke trust and professionalism (supports CTA)

                    ## Output Format (JSON)
                    Return ONLY valid JSON:
                    {{
                        "hero": "Enhanced Seedream prompt for hero banner image...",
                        "mid": "Enhanced Seedream prompt for mid-article image...",
                        "end": "Enhanced Seedream prompt for end CTA image..."
                    }}"""

    resp = client.chat.completions.create(
        model=MODEL,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0.6,
        max_tokens=1200,
        response_format={"type": "json_object"},
    )

    pt = resp.usage.prompt_tokens
    ct = resp.usage.completion_tokens
    usage = record_usage(merchant_id, "artist", MODEL, pt, ct)

    try:
        enhanced = json.loads(resp.choices[0].message.content)
    except json.JSONDecodeError:
        log.error("[%s] Artist JSON 解析失败", merchant_id)
        enhanced = image_prompts  # 失败时回退到原始 prompt

    log.info("[%s] Artist 美化完成: hero=%d字, mid=%d字, end=%d字",
             merchant_id,
             len(enhanced.get("hero", "")),
             len(enhanced.get("mid", "")),
             len(enhanced.get("end", "")))

    return enhanced, usage
