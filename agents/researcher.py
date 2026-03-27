"""热词研究员 Agent — 爬取热词 + LLM 分析选题

职责：
1. 从 Google Search Suggest / Google News RSS 爬取与商家产品相关的热词
2. 用 LLM 分析热词的 SEO 价值，排名筛选
3. 输出 TOP N 候选主题（含标题、关键词、角度建议）
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
    """懒加载 OpenAI 客户端"""
    global _client
    if _client is None:
        _client = OpenAI(max_retries=3)
    return _client


def analyze_and_pick_topics(
    merchant_id: str,
    trending_data: list[dict],
    recent_titles: list[str],
    count: int = 3,
) -> tuple[list[dict], dict]:
    """用 LLM 分析热词数据，选出最佳 SEO 博客主题

    Args:
        merchant_id: 商家标识
        trending_data: 爬取到的热词列表 [{keyword, source, context}, ...]
        recent_titles: 最近已发布的博客标题（用于去重）
        count: 需要选出的主题数量

    Returns:
        (topics, token_usage)
        topics: [{title, keywords, angle, why}, ...]
        token_usage: {"prompt_tokens": N, "completion_tokens": N}
    """
    # 拼接出researcher人格的完整Prompt
    system_prompt = build_system_prompt(merchant_id, "researcher")
    client = _get_client()

    # 构建热词摘要
    trending_lines = []
    for i, item in enumerate(trending_data[:50], 1):
        source = item.get("source", "unknown")
        keyword = item.get("keyword", "")
        context = item.get("context", "")[:200]
        line = f"{i}. [{source}] {keyword}"
        if context:
            line += f" — {context}"
        trending_lines.append(line)
    
    # trending_summary的例子：
    # """
    # 1. [google_suggest] paint protection film tesla model 3 — Autocomplete for: paint protection film Tesla
    # 2. [google_suggest] paint protection film tesla model y — Autocomplete for: paint protection film Tesla
    # ...
    # """
    trending_summary = "\n".join(trending_lines)

    # 最近已发布的标题（避免重复）
    dedup_section = ""
    if recent_titles:
        # """
        # - Why Self-Healing PPF Is the Best Investment for Your Tesla
        # - Ceramic Coating vs PPF: Which One Does Your Car Need?
        # - 5 Window Tinting Myths Every Irvine Car Owner Should Know
        # """
        titles_list = "\n".join(f"  - {t}" for t in recent_titles[-10:])

        # 给gpt的舍弃的title list
        dedup_section = (
            f"\n## Recently Published (DO NOT REPEAT)\n"
            f"These topics have been covered recently. Pick DIFFERENT topics.\n"
            f"{titles_list}\n"
        )

    user_prompt = f"""Analyze the following trending keywords and search data, then select the TOP {count} best topics for an SEO blog post.

                    ## Trending Keywords & Search Data
                    {trending_summary}

                    {dedup_section}

                    ## Selection Criteria
                    1. **Search Volume Signal** — Keywords from Google Suggest indicate real user searches
                    2. **Business Relevance** — Must directly relate to the company's products/services
                    3. **Timeliness** — News-driven topics get bonus points for urgency
                    4. **Low Competition Opportunity** — Long-tail keywords with specific intent
                    5. **Conversion Potential** — Topics that naturally lead readers to the company's services

                    ## Output Format (JSON)
                    Return ONLY valid JSON:
                    {{
                        "topics": [
                            {{
                                "title": "Compelling, SEO-friendly blog title (under 70 chars)",
                                "primary_keyword": "main target keyword phrase",
                                "secondary_keywords": ["keyword2", "keyword3", "keyword4"],
                                "angle": "The unique angle/perspective for this article",
                                "why": "Why this topic is worth writing now (1-2 sentences)",
                                "estimated_word_count": 1200
                            }}
                        ]
                    }}"""
    
    # 调用research人格
    resp = client.chat.completions.create(
        model=MODEL,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0.4,
        max_tokens=2000,
        response_format={"type": "json_object"},
    )

    # 记录 token 用量
    pt = resp.usage.prompt_tokens
    ct = resp.usage.completion_tokens
    usage = record_usage(merchant_id, "researcher", MODEL, pt, ct)

    try:
        result = json.loads(resp.choices[0].message.content)
        topics = result.get("topics", [])[:count]
    except json.JSONDecodeError:
        log.error("[%s] Researcher JSON 解析失败: %s",
                  merchant_id, resp.choices[0].message.content[:200])
        topics = []

    log.info("[%s] Researcher 选出 %d 个候选主题", merchant_id, len(topics))
    return topics, usage
