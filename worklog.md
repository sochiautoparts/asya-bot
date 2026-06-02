---
Task ID: 1
Agent: main
Task: Overhaul nastya-bot: Pollinations.ai PRIMARY, remove Qwen2.5-3B, add photo caption AI, deploy

Work Log:
- Cloned repo and studied full codebase (router.py, providers, config.py, chat.py, main.py, start.sh, bot.yml)
- Researched Pollinations.ai API extensively: only model is gpt-oss-20b (alias: "openai")
- Tested Pollinations API with user's API key (sk_Bxe1lAQ3oZ5yslfCLHl7jFPRG9r3dJxH) — works!
- Confirmed gpt-oss-20b does NOT support vision (vision=False)
- Tested reasoning_effort parameter: 'none', 'low', 'medium', 'high' — 'low' is best for chat speed
- Added POLLINATIONS_API_KEY as GitHub secret (encrypted with nacl)
- Rewrote pollinations_provider.py v6.0: single model, Bearer auth, reasoning_effort, SSE/JSON parsing
- Rewrote llama_cpp_provider.py v3.0: SINGLE model only (removed dual-model support)
- Rewrote router.py v41.0: Pollinations PRIMARY → LlamaCpp FALLBACK → static fallback
- Updated config.py v41.0: POLLINATIONS_API_KEY, removed MODEL2_PATH/MODEL_PREFERENCE, POLLINATIONS_MAX_TOKENS=512
- Updated chat.py v9.0: photo CAPTION processing via AI, photo rate limiting, extra_context param, truncation=2000
- Updated main.py v41.0: removed MODEL2_PATH/MODEL_PREFERENCE references
- Updated start.sh v41.0: single model download, Pollinations config, cloud-only mode note
- Rewrote bot.yml v41.0: removed Qwen2.5-3B download, added Pollinations API test step, POLLINATIONS_API_KEY env
- Committed and pushed to GitHub
- Triggered workflow dispatch — Run #82 started
- All steps passed: AVX2 install, model download, Pollinations API test, DB cache
- Bot is RUNNING (Run Nastya Bot step in_progress)

Stage Summary:
- Architecture changed from Qwen3=PRIMARY/Qwen2.5-3B=SECONDARY to Pollinations=PRIMARY/Qwen3=FALLBACK
- Qwen2.5-3B completely removed from project
- Photo caption processing added (AI discusses photo captions)
- Pollinations API key added to GitHub secrets
- Response truncation raised from 1200 to 2000 chars
- max_tokens: 512 for Pollinations (cloud), 256 for local Qwen3
- Bot v41.0 deployed and running
