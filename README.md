# Digital Blog Worker

AI-powered SEO blog generation and publishing system. Operates as a Slack bot that serves multiple businesses simultaneously -- each with its own brand voice, WordPress site, and content strategy. Supports fully automated scheduled posting and interactive conversational content creation.

## Features

- **Dual Operating Modes**
  - **Auto Mode** -- Fully automated: scrapes trending keywords, researches competitors, writes SEO-optimized articles, generates images, and publishes to WordPress on a cron schedule
  - **Chat Mode** -- Conversational: users collaborate with the bot to define topics, tone, structure, and image strategy through a multi-turn dialogue
- **Multi-Merchant Architecture** -- Single bot instance serves multiple businesses, each with isolated config, brand personality, and WordPress credentials
- **5-Agent Pipeline** -- Researcher, Copywriter, Reviewer, Artist, and Publisher work in sequence with an iterative review loop (up to 3 revision rounds)
- **AI Image Generation** -- Seedream 4.5 generates 3 contextual images per article (hero, mid-article, closing)
- **WordPress Publishing** -- Uploads images to WP media library, sets featured image, SEO meta tags, and publishes via REST API
- **Bilingual UI** -- Slack interface supports English and Chinese with automatic language detection

## Architecture

```
main.py                     # Slack bot entry point (Bolt, Socket Mode)
pipeline/
  blog_generator.py         # Auto mode: full end-to-end generation pipeline
  chat_generator.py         # Chat mode: stateful conversational pipeline
  trend_scraper.py          # Google Search trending keyword scraper
  web_researcher.py         # Competitor article analysis
agents/
  researcher.py             # Topic selection + duplicate avoidance
  copywriter.py             # SEO blog writing (GPT-4.1)
  reviewer.py               # Quality scoring + revision feedback
  artist.py                 # Image prompt enhancement
  conversation.py           # Chat intent parsing + state management
services/
  seedream_client.py         # Volcengine Seedream 4.5 image generation
  wordpress_publisher.py     # WP REST API: image upload + post creation
  template_selector.py       # HTML template + layout diversity
  usage_tracker.py           # Token & cost tracking per session
core/
  session.py                 # Chat session state machine
  merchant_config.py         # Per-merchant configuration loader
  i18n.py                    # Bilingual translation system
slack_ui/
  blocks.py                  # Slack Block Kit rich message builder
```

## Pipeline Flow (Auto Mode)

```
Trending Keywords ──> Topic Research ──> Competitor Analysis
        |
        v
   Copywriting (GPT-4.1) ──> Review Loop (score >= 80/100)
        |
        v
   Image Prompts ──> Seedream Generation (3 images)
        |
        v
   WordPress Upload ──> Publish (SEO tags, featured image)
        |
        v
   Slack Notification (preview card + action buttons)
```

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Bot Framework | Slack Bolt (Socket Mode) |
| LLM | OpenAI GPT-4.1 |
| Image Generation | Volcengine Seedream 4.5 |
| CMS | WordPress REST API |
| Scheduling | APScheduler |
| Preview Server | Flask |
| Templates | Jinja2 HTML |
