# Task: Fix Web Search and Content Engine for asya-bot

## Summary of Changes

### Fix 1: web_search.py
- Removed `f"{query} новости авто"` from `search_news()` — query is now passed as-is
- Added 10 more reliable SearXNG instances for better resilience
- Added `search_google_news_rss()` function using feedparser
- Improved DDG 202 handling: now waits 2s and retries before falling back to lite
- Added Google News RSS as Strategy 5 in `web_search()` fallback chain (before DDG API)
- Added `search_google_news_rss()` as additional fallback in `search_news()`
- Added `import asyncio` for sleep calls in DDG retry logic

### Fix 2: content_engine.py
- Imported `search_google_news_rss` from web_search
- `search_auto_news()` now uses `web_search()` directly instead of `search_news()`
- Added `published_time` field to all news items from search results
- Added Google News RSS as additional source in `search_auto_news()` pipeline
- `search_russian_auto_news()` now uses `web_search()` directly instead of `search_news()`
- Added Google News RSS for Russian-specific queries
- Added today-specific queries with month/date context (e.g., "автомобильные новости сегодня {month} {year}")
- Added `_score_freshness()` function: +0.3 for <3h, +0.2 for <12h, +0.1 for <24h, -0.2 for >48h
- Applied freshness scoring in `get_best_news_item()` scoring loop
- Fixed `search_news_images()`: `num_results` → `max_results`, `result.get()` → `result.url`/`result.snippet`
- `_get_search_query()` now includes `{month}` template variable with month name resolution

### Fix 3: channel.py — Partner posts with images
- `_download_partner_image()` now handles SVG images by converting to PNG using cairosvg
- SVG detection via content-type, magic bytes (`<svg`), or URL extension
- SVG→PNG conversion at 512px width for Telegram compatibility
- Relaxed minimum size from 5000→2000 bytes for post-conversion images
- Removed `hasattr` check for `program.image` (always exists on PartnerProgram)
- Added logging when partner image from data file is successfully used

### Installed dependencies
- `feedparser` (for Google News RSS parsing)
- `cairosvg` was already available (for SVG→PNG conversion)
