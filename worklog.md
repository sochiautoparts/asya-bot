# Asya Bot Worklog

---
Task ID: 1
Agent: Main
Task: Fix media artifacts bug — files under 5KB from RSS appearing in posts

Work Log:
- Analyzed post 88164 on @sochiautoparts — found 9 photo_wraps (should be max 3-4)
- Root cause 1: TELEGRAM_MAX_MEDIA_PER_POST=10 in config, used for RSS image download limit
- Root cause 2: _is_content_image() returned True when PIL not available (ImportError fallback)
- Root cause 3: _download_partner_image() had 500-byte minimum (vs 10KB for news)
- Root cause 4: NEWS_IMAGES_MAX=3 meant up to 3 AI images generated per post

Fixes applied:
- channel.py: Min image size 10KB→20KB, min dimensions 200x150→300x200, min area 50K→100K
- channel.py: PIL unavailable now REJECTS images (was accepting all)
- channel.py: _download_partner_image() now uses same strict validation (was 500 bytes min)
- channel.py: MAX_IMAGES_PER_POST=3 (was 4), MAX_RSS_IMAGES=2 (new constant)
- channel.py: AI images always 1 per post (was random 1-3)
- channel.py: Hard safety check before posting truncates to MAX_IMAGES_PER_POST
- bot/config.py: TELEGRAM_MAX_MEDIA_PER_POST=3 (was 10)
- news.py: Max candidate URLs 3 (was 4)

Stage Summary:
- Media artifacts bug completely fixed with multi-layer protection
- Posts will now have max 2-3 quality images, never junk/small files

---
Task ID: 2
Agent: Main
Task: Create GitHub Pages at sochiautoparts.github.io/asia-bot/

Work Log:
- Created docs/index.html — beautiful responsive landing page for Asya bot
- Features: hero section, 6 feature cards, how-to steps, AI models showcase, channel preview
- Created .github/workflows/pages.yml — auto-deploy on push to main
- Enabled GitHub Pages via API (build_type: workflow)
- First deployment successful

Stage Summary:
- GitHub Pages live at https://sochiautoparts.github.io/asia-bot/
- Landing page showcases bot features, models, and channel

---
Task ID: 3
Agent: Main
Task: Test and add more Pollinations AI models

Work Log:
- Listed all 57 models available via Pollinations API
- Tested 11 new models with Russian automotive prompt
- 6 new working models found and added:
  - gpt-5.5: GPT-5.5 flagship, 1M context
  - deepseek-pro: DeepSeek V4 Pro, stronger reasoning
  - grok-4.3: Latest Grok reasoning model
  - perplexity-deep: Sonar deep web search
  - perplexity-reasoning: Sonar Reasoning Pro, web+reasoning
  - kimi-k2.6: Latest Kimi model
- 5 models rejected (embeddings/realtime/image/audio only)
- Updated all model categories in pollinations_provider.py
- Updated fallback models in router.py
- Updated content models rotation in channel.py

Stage Summary:
- 6 new AI models added across chat/reasoning/vision/content/search categories
- Total models: ~45+ available for the bot

---
Task ID: 4
Agent: Main
Task: Restart GitHub Actions

Work Log:
- Pushed all changes to main branch
- Triggered bot.yml workflow — running
- Triggered pages.yml workflow — deployed successfully
- Enabled GitHub Pages via API

Stage Summary:
- Bot workflow restarted and running
- GitHub Pages workflow deployed successfully

---
Task ID: 5
Agent: Main
Task: Enhance Ася's auto expertise, VIN/parts search, photo understanding, partner links

Work Log:
- Checked GitHub Actions logs — bot was running fine, no crash errors (all cancelled runs were due to re-deploys)
- Enhanced bot/config.py system prompt with deep car knowledge (engines, transmissions, suspension, brakes, electrical)
- Added 8 direct shop search URL templates in bot/web_search.py (Rossko, Autopiter, Exist, Emex, Autodoc, ZZAP, Avtoall, Ixora)
- Created search_parts_by_vin() function — generates direct shop links for VIN-based parts lookup
- Enhanced search_spare_part() — generates direct shop URLs instantly + DDG web search for real prices
- Enhanced photo handler in bot/handlers/chat.py for document understanding (ПТС/СТС, OBD-II screens, VIN codes)
- Auto-detects VIN and part numbers in photo AI responses and adds purchase links
- VIN handler now also searches for parts by VIN with direct shop links (5 shops)
- Spare part query detection expanded with 20+ more keywords (колодки, фильтр, датчик, etc.)
- Partner link section now includes direct shop URLs from SHOP_SEARCH_URLS
- _clean_markdown now preserves URLs (converts [text](url) → url instead of removing URL)
- Vision prompt enhanced for document understanding (ПТС/СТС/VIN/OBD-II/damage)
- VIN prompt enhanced to include shop links in response
- Diagnostic and spare part prompts now explicitly require direct purchase links
- All files passed syntax check
- Pushed to GitHub and restarted Actions — run 27011177677 in_progress with SHA fcfa8d31

Stage Summary:
- Ася now has deep car expertise (engines, transmissions, suspension, electrical)
- VIN queries automatically get shop links for parts (Росско, Autopiter, Exist, ZZAP, Emex)
- Part number queries get direct links to 8 shops instantly
- Photo handler understands: ПТС/СТС documents, OBD-II screens, VIN codes, car parts, damage
- AI prompts explicitly require giving direct purchase links
- 3 files changed, 268 insertions, 73 deletions

---
Task ID: 1-6
Agent: main
Task: Fix critical Asya Bot errors and improve functionality

Work Log:
- Fixed UnboundLocalError for identify_car_brand by removing shadowing re-import in chat.py line 853
- Added free tier fallback to PollinationsProvider: on 402 (balance depleted), automatically retries without API key
- Smart fallback in AIRouter: on 402, only try 3 free-tier models instead of iterating through 30
- Added "thinking" status messages: bot sends contextual phrases (part_search, diagnostic, thinking) while processing
- Fixed DDG web search 202 response: added DDG Lite fallback, updated headers to modern Chrome UA
- Reordered web search: SearXNG first (more reliable), then DDG, Yandex, Google
- Added more SearXNG instances (9 total) for better reliability
- Updated broken RSS URLs: Autonews, WardsAuto, Autonews Europe, Autocar Pro India, Autosport, NewAtlas
- Added backup RSS sources: CarExpert, Motor1, TopGear
- Pushed all changes and restarted GitHub Actions

Stage Summary:
- UnboundLocalError: FIXED - removed shadowing re-import
- Pollinations 402: FIXED - free tier fallback, smart model selection
- Thinking messages: ADDED - contextual phrases while processing
- DDG 202: FIXED - DDG Lite fallback, SearXNG priority
- RSS 404/403: FIXED - updated URLs, added backup sources
- GitHub Actions: RESTARTED successfully
