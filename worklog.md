---
Task ID: 1
Agent: main
Task: Refactor asya-bot to use single JSON news source

Work Log:
- Studied full asya-bot architecture: news.py (629 lines), content_engine.py (1875 lines), channel.py (2143 lines), image_fetcher.py, article_fetcher.py, partners.py
- Identified key flow: news.py parses RSS → DB → content_engine selects best → channel.py generates post → Telegram
- Rewrote news.py: Now fetches news from https://raw.githubusercontent.com/creastudioai-beep/news/refs/heads/main/data/news.json instead of 21+ RSS feeds. 195 lines vs 629 lines.
- Rewrote content_engine.py: Removed web search, AI discovery, RSS fallback, image search phases. Kept: interest scoring, topic registry dedup, tone analysis, editorial team personality, translation hints. 530 lines vs 1875 lines.
- Updated channel.py: Changed _get_post_images() to download images directly from JSON URLs instead of using ImageFetcher (article page scraping). Removed web_search import.
- Partner posts: UNCHANGED — works exactly as before
- Pushed all changes to GitHub (commit 8799c87)
- Updated GH_PAT_TOKEN secret with new token
- Triggered Actions workflow (run #27475797548) — bot is running successfully

Stage Summary:
- Bot refactored to single-source JSON pipeline (v3.0)
- 2330 lines removed, 503 lines added — much simpler architecture
- News JSON verified available (104KB, HTTP 200)
- Actions workflow running — bot step is in_progress
- Key files changed: news.py, bot/content_engine.py, channel.py
- Key files UNCHANGED: bot/partners.py, bot/web_search.py, bot/media_handler.py, ai/router.py, all handlers
