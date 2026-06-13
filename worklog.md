---
Task ID: 1
Agent: main
Task: Fix photo extraction + article fetcher + Google News resolution + full article text for Asya VK bot

Work Log:
- Analyzed the entire news pipeline: news.py → content_engine.py → channel.py → image_fetcher.py
- Discovered _extract_entry_images() in news.py ALREADY works correctly — it extracts images from RSS entries
- Root cause of "no photos in posts": Google News RSS items dominate the pipeline (229 items) but have ZERO images
- RSS sources like CAR Magazine, Autocar, Jalopnik, CarExpert, etc. DO extract images correctly
- Created bot/article_fetcher.py — new module that:
  - Resolves Google News redirect URLs to real article URLs
  - Fetches full article pages and extracts:
    - Full article text (from <p> tags) for AI fact-gathering
    - Quality images (og:image, twitter:image, JSON-LD, <picture>, <img>)
  - Provides enrich_news_item() for single item enrichment
  - Provides enrich_news_batch() for batch processing (concurrent)
- Modified news.py:
  - Extract full_text from RSS content field (not just summary)
  - Enrich Google News RSS items with article_fetcher after fetching
  - All 4 add_news_item() calls now pass full_text and resolved_url
- Modified bot/database.py:
  - Added full_text and resolved_url columns to news_items
  - Schema migration (ALTER TABLE) for existing databases
  - add_news_item() accepts and stores full_text and resolved_url
- Modified bot/content_engine.py:
  - After selecting best news item, enrich with article_fetcher if <3 images or <200 char summary
- Modified channel.py:
  - Use full_text for AI post generation (instead of just summary)
  - Use resolved_url for image scraping (Google News redirect)
  - Fix: replaced raw aiosqlite.connect() with _connect_db() (fixes database is locked)
- Modified .github/workflows/bot.yml: added push trigger on main branch
- Pushed all changes to GitHub, workflow should auto-trigger

Stage Summary:
- Article fetcher module created and integrated at 3 points in pipeline
- Google News items now get enriched with images + full text
- AI now gets full article text for fact-gathering (not just RSS summary)
- Database locked errors fixed (WAL mode + busy_timeout everywhere)
- Comments already use local model only (verified)
- PAT token expired — can't manually trigger, but push to main now auto-triggers

---
Task ID: 1
Agent: Main Agent
Task: Audit and fix Asya VK bot — photos, LocalProvider, scoring, GitHub Actions

Work Log:
- Tested _extract_entry_images() — CONFIRMED WORKING, extracts photos from all RSS feeds
- Root cause of missing photos: CDN hotlink protection returning 403 without Referer header
- Fixed LocalProvider singleton — was creating new instance each time (3-7s model reload)
- Added photo priority scoring in content_engine.py (+0.15 to +0.4 bonus for items with photos)
- Added Referer header to image_fetcher for CDN hotlink protection
- Updated Google News RSS to extract images via _extract_entry_images()
- Added image_urls field to SearchResult class
- Verified all 4 Russian RSS sources work (ТАСС Авто, Авто Mail.ru, Коммерсант, 5Колесо)
- 6 unwanted RSS sources already removed (РБК Авто, За Рулем, Колёса.ру, Дром — from previous session)
- Pushed fix commit to GitHub, triggered GitHub Actions workflow
- Bot is running on GitHub Actions (run 27467012818, status: in_progress)

Stage Summary:
- Key fix: image_fetcher now sends Referer header → CAR Magazine, CarExpert, etc. work
- Key fix: LocalProvider uses ai_router._local singleton → no more 3-7s model reloads
- Key fix: Photo bonus scoring → RSS items with photos beat web search items without photos
- All syntax checks passed
- Commit: fb1ea11 pushed to main
- GitHub Actions: workflow triggered and running
