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


def _build_image_instructions(image_count: int, mode: str, image_plan: dict | None = None) -> str:
    """根据模式和图片数量生成图片占位符指令

    auto 模式: 固定 3 个命名槽位 hero/mid/end
    chat 模式（有 plan）: 按 plan 逐槽位说明处理方式，让 GPT 知道哪些是用户原图
    chat 模式（无 plan）: fallback，N 个编号槽位
    """
    if mode == "chat" and image_plan:
        lines = [f"- This article has {image_count} image slot(s). Each slot's handling:"]
        for slot, info in image_plan.items():
            ph = f"<!-- BLOG_IMAGE:{slot} -->"
            user_req = info.get("user_request", "")
            req_hint = f'\n      USER REQUEST: "{user_req}" — incorporate this into your image_prompt.' if user_req else ""

            if info["action"] == "raw":
                lines.append(
                    f"  - {ph} — USER PHOTO ({info['original_name']}): "
                    f"User's original photo, used as-is. "
                    f"Write article text that naturally references this real photo. "
                    f"Do NOT write an image_prompt for this slot."
                )
            elif info["action"] == "reference":
                lines.append(
                    f"  - {ph} — REFERENCE ({info['original_name']}): "
                    f"AI will generate a NEW image inspired by user's photo. "
                    f"Write an image_prompt describing the desired new image."
                    f"{req_hint}"
                )
            elif info["action"] == "enhance":
                lines.append(
                    f"  - {ph} — ENHANCE ({info['original_name']}): "
                    f"User's photo will be AI-restyled (same content, new style). "
                    f"Write an image_prompt describing the desired style changes."
                    f"{req_hint}"
                )
            else:  # generate
                lines.append(
                    f"  - {ph} — AI GENERATE: "
                    f"Fully AI-generated. Write a detailed image_prompt."
                    f"{req_hint}"
                )
        lines.append(
            "- Distribute placeholders evenly: first near top (after opening), "
            "last near end (before FAQ/CTA), others spaced between sections."
        )
        first_slot = next(iter(image_plan))
        lines.append(f"- The first image (<!-- BLOG_IMAGE:{first_slot} -->) will be the hero/banner, place it prominently.")
        return "\n                    ".join(lines)
    elif mode == "chat":
        # fallback: 无 plan 时的通用指令
        placeholders = ", ".join(f"<!-- BLOG_IMAGE:img_{i} -->" for i in range(1, image_count + 1))
        return (
            f"- Include exactly {image_count} image placeholders as HTML comments: {placeholders}\n"
            f"                    - Distribute them evenly throughout the article: first image near the top (after opening), "
            f"last image near the end (before FAQ/CTA), others spaced between sections.\n"
            f"                    - The first image (<!-- BLOG_IMAGE:img_1 -->) will be used as the hero/banner, so place it prominently."
        )
    else:
        return (
            "- Include exactly 3 image placeholders as HTML comments: "
            "<!-- BLOG_IMAGE:hero -->, <!-- BLOG_IMAGE:mid -->, <!-- BLOG_IMAGE:end -->"
        )


def _build_output_format(image_count: int, mode: str, image_plan: dict | None = None) -> str:
    """根据模式和图片数量生成 JSON 输出格式指令

    auto 模式: image_prompts/image_alts 用 hero/mid/end 键
    chat 模式（有 plan）: image_prompts 只含需要生图的槽位，image_alts 包含所有槽位
    chat 模式（无 plan）: fallback，全部槽位都要 prompt
    """
    if mode == "chat" and image_plan:
        # 需要写 prompt 的槽位（非 raw）
        prompt_slots = [slot for slot, info in image_plan.items() if info["action"] != "raw"]
        all_slots = list(image_plan.keys())

        if prompt_slots:
            prompt_entries = ",\n                            ".join(
                f'"{slot}": "Detailed image prompt — professional, relevant. No text or logos."'
                for slot in prompt_slots
            )
        else:
            # 所有槽位都是用户原图，不需要任何 prompt
            prompt_entries = ""

        # 所有槽位都需要 SEO alt text（包括用户原图）
        alt_entries = ",\n                            ".join(
            f'"{slot}": "SEO-optimized alt text — describe the scene, include a keyword"'
            for slot in all_slots
        )

        # 提示 GPT 哪些不需要写 prompt
        raw_slots = [slot for slot, info in image_plan.items() if info["action"] == "raw"]
        prompt_note = ""
        if raw_slots and prompt_slots:
            prompt_note = f"\n                    IMPORTANT: Do NOT include image_prompts for {', '.join(raw_slots)} — those are user photos used as-is. Only write prompts for: {', '.join(prompt_slots)}."
        elif raw_slots and not prompt_slots:
            prompt_note = "\n                    IMPORTANT: image_prompts should be an empty object {{}} — all images are user photos."

        return f"""## Output Format (JSON)
                    Return ONLY valid JSON:
                    {{
                        "title": "Final blog title (may refine from brief)",
                        "content_html": "Full article HTML body content",
                        "excerpt": "Meta description under 160 chars — compelling, keyword-rich",
                        "tags": ["tag1", "tag2", "tag3", "tag4", "tag5"],
                        "seo_slug": "url-friendly-slug-with-primary-keyword",
                        "image_prompts": {{
                            {prompt_entries}
                        }},
                        "image_alts": {{
                            {alt_entries}
                        }}
                    }}{prompt_note}"""
    elif mode == "chat":
        # fallback: 无 plan 时全部槽位都要 prompt
        prompt_entries = ",\n                            ".join(
            f'"img_{i}": "Detailed prompt for image {i} — professional, relevant. No text or logos."'
            for i in range(1, image_count + 1)
        )
        alt_entries = ",\n                            ".join(
            f'"img_{i}": "SEO-optimized alt text for image {i} — describe the scene, include a keyword"'
            for i in range(1, image_count + 1)
        )
        return f"""## Output Format (JSON)
                    Return ONLY valid JSON:
                    {{
                        "title": "Final blog title (may refine from brief)",
                        "content_html": "Full article HTML body content",
                        "excerpt": "Meta description under 160 chars — compelling, keyword-rich",
                        "tags": ["tag1", "tag2", "tag3", "tag4", "tag5"],
                        "seo_slug": "url-friendly-slug-with-primary-keyword",
                        "image_prompts": {{
                            {prompt_entries}
                        }},
                        "image_alts": {{
                            {alt_entries}
                        }}
                    }}"""
    else:
        # auto 模式: 普通字符串，花括号原样保留
        return """## Output Format (JSON)
                    Return ONLY valid JSON:
                    {
                        "title": "Final blog title (may refine from brief)",
                        "content_html": "Full article HTML body content",
                        "excerpt": "Meta description under 160 chars — compelling, keyword-rich",
                        "tags": ["tag1", "tag2", "tag3", "tag4", "tag5"],
                        "seo_slug": "url-friendly-slug-with-primary-keyword",
                        "image_prompts": {
                            "hero": "Detailed prompt for the hero/banner image — professional, high-end, relevant to the topic. No text or logos in the image.",
                            "mid": "Prompt for mid-article image — illustrates a key point. No text or logos.",
                            "end": "Prompt for end-of-article image — reinforces the CTA or conclusion. No text or logos."
                        },
                        "image_alts": {
                            "hero": "SEO-optimized alt text for hero image — describe the scene, include primary keyword naturally",
                            "mid": "SEO-optimized alt text for mid image — describe what is shown, include a relevant keyword",
                            "end": "SEO-optimized alt text for end image — describe the scene, include brand or location keyword"
                        }
                    }"""


def write_blog(
    merchant_id: str,
    topic: dict,
    layout_prompt: str = "",
    research_brief: str = "",
    image_count: int = 3,
    mode: str = "auto",
    image_plan: dict | None = None,
) -> tuple[dict, dict]:
    """根据选题撰写一篇完整的 SEO 博客

    Args:
        merchant_id: 商家标识
        topic: Researcher 选出的主题
            {title, primary_keyword, secondary_keywords, angle, why, estimated_word_count}
        layout_prompt: 布局风格指令（由 template_selector 选出，注入到 prompt 中引导不同结构）
        research_brief: Web 调研摘要（搜索竞品文章后 LLM 提炼的要点，提升内容深度）
        image_count: 图片数量（auto 模式固定 3，chat 模式由用户决定）
        mode: "auto" — 使用 hero/mid/end 三个固定槽位
              "chat" — 使用编号槽位 img_1, img_2, ..., img_N
        image_plan: Chat 模式的图片处理计划（每个槽位的 source/action/original_name）
                    传入后 copywriter 会知道哪些是用户原图、哪些需要写生图 prompt

    Returns:
        (blog_data, token_usage)
        blog_data: {title, content_html, excerpt, tags, seo_slug, image_prompts, image_alts}
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
                    ## Language Requirement
                    The entire blog post MUST be written in English. All content — title, body, excerpt, tags, slug — must be in English regardless of the input language.

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
                    {_build_image_instructions(image_count, mode, image_plan)}
                    - CRITICAL: Image placeholders MUST be the exact HTML comments specified above. Do NOT write <img> tags, do NOT invent image URLs like /images/xxx.jpg. The image system replaces these comment placeholders with real images later. Any <img> tag you write will show as a broken image.
                    - Do NOT include <html>, <head>, <body>, or <h1> tags — only the article body content
                    - ABSOLUTELY NO emoji, icons, or special Unicode characters anywhere in the HTML — not in headings, not in paragraphs, not in lists. Use plain ASCII text only. Emoji will break rendering.
                    - Use <hr> between major sections
                    - Article structure order: Opening → Sections → FAQ → CTA

                    {_build_output_format(image_count, mode, image_plan)}"""

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


def write_chat_blog(
    merchant_id: str,
    creative_brief: dict,
    image_plan: dict,
    topic: dict,
    layout_prompt: str = "",
    research_brief: str = "",
) -> tuple[dict, dict]:
    """Chat 模式专用 — 根据创意简报撰写高度定制化的 SEO 博客

    与 write_blog (auto 模式) 的核心区别：
    - auto: copywriter 自由发挥，只给主题和 SEO 规则
    - chat: copywriter 按用户的"导演脚本"执行，严格遵循内容结构、用户文本、图文配对

    Args:
        merchant_id:    商家标识
        creative_brief: 对话层综合生成的创意简报，包含：
            - content_structure: 用户要求的文章结构（段落顺序、每段要求、配图）
              示例: [
                {"section": "Opening Hook", "requirement": "Start with a surprising stat"},
                {"section": "Price Table", "requirement": "Compare 3 brands", "user_text": "Full body $6500"},
                {"section": "Installation", "requirement": "Step by step", "image": "img_2"},
              ]
            - user_provided_text: 用户提供的原始文本/数据（必须融入文章）
              示例: {"price_data": "Model Y full body $6500", "testimonial": "We are XPEL certified"}
            - tone: 写作语气
              示例: "professional but approachable"
            - target_audience: 目标读者
              示例: "Tesla owners in Irvine, 30-50 age"
            - special_requests: 其他特殊要求
              示例: "mention XPEL warranty is transferable"
        image_plan:     图片处理计划（来自 _build_image_plan）
            示例: {
                "img_1": {"source": "user", "action": "raw", "original_name": "front.jpg"},
                "img_2": {"source": "user", "action": "reference", "original_name": "side.jpg",
                          "user_request": "Sunset Tesla side shot, warm tones"},
                "img_3": {"source": "ai", "action": "generate",
                          "user_request": "Price comparison infographic"},
            }
        topic:          SEO 参数字典（来自 _build_topic_from_params）
            示例: {"title": "Tesla PPF Cost Guide", "primary_keyword": "Tesla PPF Irvine", ...}
        layout_prompt:  布局风格指令
        research_brief: 竞品调研摘要

    Returns:
        (blog_data, usage)
        blog_data 示例: {
            "title": "How Much Does Tesla PPF Cost in Irvine?",
            "content_html": "<p>...</p><!-- BLOG_IMAGE:img_1 --><p>...</p>...",
            "excerpt": "Tesla PPF costs $1,500-$7,000...",
            "tags": ["Tesla PPF", "paint protection film cost"],
            "seo_slug": "tesla-ppf-cost-irvine",
            "image_prompts": {"img_2": "...", "img_3": "..."},   ← 只含非 raw 槽位
            "image_alts": {"img_1": "...", "img_2": "...", "img_3": "..."},  ← 全部槽位
        }
    """
    system_prompt = build_system_prompt(merchant_id, "copywriter")
    client = _get_client()

    # ── 从 topic dict 提取 SEO 参数 ──
    title = topic.get("title", "Untitled")
    primary_kw = topic.get("primary_keyword", "")
    secondary_kws = topic.get("secondary_keywords", [])
    angle = topic.get("angle", "")
    word_count = topic.get("estimated_word_count", 1200)
    image_count = len(image_plan)

    # ── 构建创意简报部分 ──
    brief_sections = []

    # 内容结构（用户的导演脚本）
    content_structure = creative_brief.get("content_structure", [])
    if content_structure:
        struct_lines = ["## Content Structure (FOLLOW THIS ORDER — user's specific requirements)"]
        for i, sec in enumerate(content_structure, 1):
            section_name = sec.get("section", f"Section {i}")
            requirement = sec.get("requirement", "")
            image_slot = sec.get("image", "")
            user_text = sec.get("user_text", "")
            line = f"{i}. **{section_name}**"
            if requirement:
                line += f": {requirement}"
            if image_slot:
                line += f" [Place <!-- BLOG_IMAGE:{image_slot} --> in this section]"
            if user_text:
                line += f'\n   USER-PROVIDED TEXT (must include): "{user_text}"'
            struct_lines.append(line)
        brief_sections.append("\n".join(struct_lines))

    # 用户提供的原始文本
    user_texts = creative_brief.get("user_provided_text", {})
    if user_texts:
        text_lines = ["## User-Provided Content (MUST incorporate into the article — do not discard)"]
        for label, text in user_texts.items():
            text_lines.append(f'- **{label}**: "{text}"')
        brief_sections.append("\n".join(text_lines))

    # 语气和风格
    tone = creative_brief.get("tone", "")
    target_audience = creative_brief.get("target_audience", "")
    special = creative_brief.get("special_requests", "")

    style_lines = []
    if tone:
        style_lines.append(f"- **Tone**: {tone}")
    if target_audience:
        style_lines.append(f"- **Target Audience**: {target_audience}")
    if special:
        style_lines.append(f"- **Special Requests**: {special}")
    if style_lines:
        brief_sections.append("## Writing Style & Requirements\n" + "\n".join(style_lines))

    creative_brief_text = "\n\n".join(brief_sections) if brief_sections else ""

    # 布局风格
    layout_section = f"\n{layout_prompt}\n" if layout_prompt else ""

    # 竞品调研
    research_section = ""
    if research_brief:
        research_section = f"""
                    ## Competitor Research & Market Intelligence
                    Use these data points to write a SUPERIOR article — do NOT copy, but incorporate the facts.

                    {research_brief}
                    """

    # ── 图片指令 + 输出格式（复用现有函数，已支持 image_plan）──
    image_instructions = _build_image_instructions(image_count, "chat", image_plan)
    output_format = _build_output_format(image_count, "chat", image_plan)

    # ── 组装完整 prompt ──
    user_prompt = f"""Write a highly customized SEO blog post following the user's creative brief below.
The user has provided specific requirements through a multi-turn conversation — follow them precisely.

                    ## Topic & SEO Brief
                    - **Topic:** {title}
                    - **Primary Keyword:** {primary_kw}
                    - **Secondary Keywords:** {', '.join(secondary_kws)}
                    - **Angle:** {angle}
                    - **Target Word Count:** {word_count} words (range: {word_count - 200} to {word_count + 300})

                    {creative_brief_text}
                    {layout_section}{research_section}
                    ## Language Requirement
                    The entire blog post MUST be written in English. All content — title, body, excerpt, tags, slug — must be in English regardless of the input language.

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
                    10. **No emoji** — Do NOT use any emoji or Unicode icons anywhere. Plain text only.

                    ## FAQ Section (REQUIRED)
                    Every article MUST end with a FAQ section BEFORE the CTA. Use this exact HTML:
                    ```
                    <div class="faq-section">
                      <h2>Frequently Asked Questions</h2>
                      <div class="faq-item"><h3>Question?</h3><p>Answer.</p></div>
                    </div>
                    ```

                    ## Content Quality Requirements
                    1. **Follow the creative brief** — If the user specified a content structure, follow it. If not, create a logical structure.
                    2. **Include user's text** — Any text the user provided MUST appear naturally in the article (may be lightly edited for flow).
                    3. **Expert Depth** — Write as an industry authority with specific details, numbers, and actionable advice.
                    4. **Scannable Structure** — Short paragraphs (2-3 sentences max), bullet lists, numbered steps.
                    5. **Strong CTA** — End with a clear call-to-action.
                    6. **No Fluff** — Every paragraph must provide value.

                    ## HTML Format
                    - Use semantic HTML: <h2>, <h3>, <p>, <ul>, <ol>, <blockquote>
                    - Add CSS classes: "blog-section", "blog-highlight", "blog-cta", "faq-section", "faq-item"
                    {image_instructions}
                    - CRITICAL: Image placeholders MUST be the exact HTML comments specified above. Do NOT write <img> tags.
                    - Do NOT include <html>, <head>, <body>, or <h1> tags — only the article body content
                    - ABSOLUTELY NO emoji or special Unicode characters anywhere in the HTML.
                    - Use <hr> between major sections
                    - Article structure order: Opening → Sections → FAQ → CTA

                    {output_format}"""

    # ── 调用 GPT ──
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
    usage = record_usage(merchant_id, "copywriter_chat", MODEL, pt, ct)

    try:
        blog_data = json.loads(resp.choices[0].message.content)
    except json.JSONDecodeError:
        log.error("[%s] Chat Copywriter JSON 解析失败: %s",
                  merchant_id, resp.choices[0].message.content[:200])
        blog_data = {
            "title": title,
            "content_html": "<p>Generation failed. Please retry.</p>",
            "excerpt": "",
            "tags": [],
            "seo_slug": "error",
            "image_prompts": {},
            "image_alts": {},
        }

    word_count_actual = len(blog_data.get("content_html", "").split())
    log.info("[%s] Chat Copywriter 完成: '%s' (~%d words, %d images)",
             merchant_id, blog_data.get("title", ""), word_count_actual, image_count)

    return blog_data, usage


def rewrite_blog(
    merchant_id: str,
    blog_data: dict,
    feedback: dict,
    round_num: int,
    mode: str = "auto",
    image_count: int = 3,
) -> tuple[dict, dict]:
    """根据 Reviewer 反馈重写博客

    Args:
        merchant_id: 商家标识
        blog_data: 当前草稿数据
        feedback: Reviewer 的反馈 {score, issues, suggestions}
        round_num: 当前修改轮次 (1-based)
        mode: "auto" — hero/mid/end 占位符; "chat" — img_1/img_2/.../img_N 占位符
        image_count: 图片数量（chat 模式下使用）

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

    # 根据模式生成正确的占位符指令和输出格式
    if mode == "chat":
        placeholder_rule = f"3. Keep image placeholders (<!-- BLOG_IMAGE:img_1 --> through <!-- BLOG_IMAGE:img_{image_count} -->)"
        # 只为原本有 prompt 的槽位要求重写（raw 槽位没有 prompt）
        existing_prompt_slots = list(blog_data.get("image_prompts", {}).keys())
        if existing_prompt_slots:
            prompt_entries = ", ".join(f'"{s}": "..."' for s in existing_prompt_slots)
        else:
            prompt_entries = ", ".join(f'"img_{i}": "..."' for i in range(1, image_count + 1))
        output_format = f'{{{prompt_entries}}}'
    else:
        placeholder_rule = "3. Keep image placeholders (<!-- BLOG_IMAGE:hero -->, <!-- BLOG_IMAGE:mid -->, <!-- BLOG_IMAGE:end -->)"
        output_format = '{"hero": "...", "mid": "...", "end": "..."}'

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
{placeholder_rule}
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
    "image_prompts": {output_format}
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
