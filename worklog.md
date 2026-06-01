---
Task ID: 1
Agent: Super Z (main)
Task: Diagnose and fix nastya-bot Telegram bot AI response failures

Work Log:
- Cloned repository https://github.com/sochiautoparts/nastya-bot
- Performed deep analysis of all 21 Python files
- Identified root causes: Ollama models timing out (15-90s on 2 CPU cores), system prompt too large (~800-1100 tokens), Semaphore(1) blocking all requests, Pollinations rate-limited from overload
- Decided on HYBRID architecture: Pollinations PRIMARY for chat, Ollama PRIMARY for background
- Rewrote ai/router.py: separate _route_chat() and _route_background() methods
- Rewrote ai/providers/ollama_cluster_provider.py: separate semaphores, reduced timeouts, Pollinations fallback outside semaphore
- Drastically reduced system prompt from ~800-1100 to ~200-250 tokens in bot/config.py
- Removed dead code from chat.py: knowledge injection, memory extraction, zodiac scanning, verbose instructions
- Reduced news.py prompts from ~300-400 tokens to ~80-100 tokens each
- Updated bot/main.py: version 32.0, health watchdog no longer kills process on Ollama failure
- Committed and pushed v32.0 to repository

Stage Summary:
- All 6 modified files pass Python syntax check
- Commit: v32.0: HYBRID EDITION — Pollinations for chat + Ollama for background
- Pushed to origin/main successfully
- Key improvement: Chat responses should now take 2-5 seconds (via Pollinations/GPT-4o-mini) instead of 15-90 seconds (via Ollama)
