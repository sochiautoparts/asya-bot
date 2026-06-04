---
Task ID: 1
Agent: Main Agent
Task: Full audit and fix of Asya bot (@asiaexp_bot) — all user-requested items

Work Log:
- Cloned repo and read all 19 source files
- Checked GitHub Actions logs — found critical "chat not found" error due to wrong CHANNEL_ID format
- Tested all 14 new Pollinations models — none suitable (all invalid or non-chat models)
- Fixed CHANNEL_ID from "1479468835" to "-1001479468835" (Telegram requires -100 prefix for channels)
- Updated GitHub secret CHANNEL_ID with correct value
- Verified channel posting works via Telegram API (sent test message, poll)
- Fixed morning greeting: natural living-person style (coffee, waking up), no "searching news"
- Fixed /start: greet as a friend, not a service menu listing functions
- Fixed /help: casual conversational style, no "Я Ася — автоэксперт" intro
- Fixed system prompt: explicitly prohibits saying "я живая девушка", listing functions, mentioning news search
- Fixed .gitignore to allow asya_bot.db persistence
- Fixed workflow: stable cache key for DB, git pull --rebase before push, CHANNEL_ID hardcoded
- Replaced 9 broken RSS feeds (404/403 errors) with working alternatives
- Added more RSS sources (Autoblog, Carscoops, Motor1 DE, 5koleso, etc.)
- Improved partner image extraction from admitad data (6+ image field checks)
- Improved partner image download (better content-type handling, skip SVGs)
- Improved channel comments (removed explicit @asiaexp_bot mentions)
- Improved ASYA_PHRASES — more natural, casual language
- Improved mode command messages (/diagnostic, /parts, /normal) — casual style
- All 8 files passed Python syntax check
- Pushed commit to GitHub
- Triggered workflow dispatch — bot running successfully

Stage Summary:
- Critical fix: CHANNEL_ID format (-100 prefix) — channel posting now works
- Communication completely overhauled: natural, living-person style
- System prompt explicitly prevents robotic/service-style language
- Database persistence fixed between workflow runs
- 9 broken RSS feeds replaced with working ones
- Partner media integration improved with better image extraction
- Poll creation verified working in channel
- No new Pollinations models to add (current list is comprehensive)
