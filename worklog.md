---
Task ID: 1
Agent: Main Agent
Task: Fix Nastya Bot - diagnose and fix timeout issues with Ollama models on CPU

Work Log:
- Cloned repository https://github.com/sochiautoparts/nastya-bot
- Analyzed full codebase: ollama_cluster_provider.py, router.py, chat.py, config.py, main.py, news.py, channel.py
- Identified 5 root causes of bot not responding to users
- Implemented v31.0 CPU-OPTIMIZED fixes across 8 files
- Committed and pushed to GitHub

Stage Summary:
- ROOT CAUSE: Qwen3-4B (4B params) was primary model — too slow on 2 CPU cores (45+ seconds timeout)
- FIX: Swapped models — Vikhr-1B is now primary (5-15s on CPU), Qwen3-4B is reserve
- Reduced num_ctx from 4096 to 2048, max_tokens from 400 to 100, history from 12 to 6
- Increased timeouts: 90s primary, 120s reserve (realistic for CPU)
- Shortened system prompt from ~200 tokens to ~80 tokens
- Increased background task delays to prevent blocking user chat
- Always disable thinking mode for CPU speed
- Commit: a52b5d0 pushed to main branch
