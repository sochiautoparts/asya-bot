---
Task ID: 1
Agent: main
Task: Analyze Nastya Bot v39 production logs and fix critical issues

Work Log:
- Cloned repo and analyzed all source files
- Identified ROOT CAUSE: bot.yml env vars override config.py defaults
  - MODEL_N_CTX=4096, MODEL_MAX_TOKENS=384, MODEL_HISTORY_LIMIT=15 (from bot.yml)
  - These overrode the v39 config.py defaults (2048/200/10)
  - Result: 65-89s response times because 384 tokens + thinking tokens = huge generation
- Identified CRITICAL dedup bug: timestamp-based dedup was set on EVERY message
  - Quick reactions (donate, age, etc.) set _user_processing timestamp but never cleared it
  - Result: user sends donate → next 45s ALL messages blocked!
  - Fixed: changed to asyncio.Task-based tracking — only blocks when AI is actually processing
- Updated all version strings from v39 → v40
- Updated bot.yml env vars: 2048/200/10
- Updated cache keys for fresh deployment
- Pushed and triggered GitHub Actions deployment

Stage Summary:
- v40.0 deployed to GitHub Actions (Run #78)
- Critical fixes: config override, dedup logic, version consistency
- Bot is running with n_ctx=2048, max_tokens=200, history=10
- Expected response time: ~20s (down from 65-89s!)
