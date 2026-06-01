---
Task ID: 1
Agent: Main Agent
Task: Complete rewrite of nastya-bot from Ollama to llama-cpp-python

Work Log:
- Cloned and thoroughly analyzed the entire nastya-bot codebase (20+ files)
- Identified root causes: Ollama HTTP overhead, 1B model garbage, qwen2.5:1.5b not installed, Pollinations 429
- Installed llama-cpp-python v0.3.24 with AVX2 acceleration
- Downloaded Qwen3-4B-Instruct Q4_K_M GGUF model (~2.4GB) from unsloth/HuggingFace
- Tested model: 3.2s load, 2-5s generation, excellent Russian quality
- Created new LlamaCppProvider (ai/providers/llama_cpp_provider.py):
  - Direct GGUF loading via llama-cpp-python — no HTTP server!
  - asyncio.to_thread() for non-blocking generation
  - Semaphore(1) for serialized model access
  - /no_think prefix for Qwen3 — disables thinking mode
  - Auto warm-up on init
- Rewrote AI Router (ai/router.py) v36.0:
  - LlamaCppProvider as PRIMARY for chat
  - PollinationsProvider as FALLBACK
  - Static fallback if both fail
- Updated bot/config.py:
  - Removed OLLAMA_BASE_URL
  - Added MODEL_PATH, MODEL_N_CTX, MODEL_N_THREADS, MODEL_MAX_TOKENS
- Updated bot/main.py:
  - Health watchdog: checks model health instead of Ollama
  - Auto model reload on failure
  - Removed Ollama restart logic
- Updated start.sh:
  - Downloads GGUF model from HuggingFace
  - Installs llama-cpp-python with AVX2
  - No more Ollama server
- Updated .github/workflows/bot.yml:
  - Removed all Ollama steps (install, start, pull models)
  - Added GGUF model caching and download
  - Added llama-cpp-python installation with AVX2
  - Removed OLLAMA_BASE_URL, OLLAMA_KEEP_ALIVE env vars
  - Added MODEL_PATH, MODEL_N_CTX, MODEL_N_THREADS, MODEL_MAX_TOKENS
- Updated requirements.txt: added llama-cpp-python, huggingface_hub
- Updated .env.example: new model settings
- Updated .gitignore: GGUF models excluded (too large for git)
- Updated admin handler: /providers shows llama-cpp stats, /reset reloads model
- Cleaned up all Ollama references across codebase
- Tested end-to-end: model loads, generates quality Russian text
- Committed and pushed to GitHub: v36.0

Stage Summary:
- Complete architecture change: Ollama → llama-cpp-python
- Model: Qwen3-4B-Instruct Q4_K_M (2.4GB, Q4 quantization)
- Performance: 2-5s generation (vs 7-47s with Ollama)
- Quality: Excellent Russian language, natural conversation
- Deploy: GitHub Actions workflow updated for GGUF model caching
---
Task ID: 1
Agent: main
Task: Complete overhaul of nastya-bot v37.0 — Dual-model system with Phi-4-mini + Qwen3-4B

Work Log:
- Examined entire codebase: config.py, llama_cpp_provider.py, router.py, chat.py, main.py, channel.py, news.py, database.py, web_search.py, bot.yml
- Found Phi-4-mini-instruct GGUF model: unsloth/Phi-4-mini-instruct-GGUF, Q4_K_M ~2.32GB, officially supports Russian
- Implemented LlamaCppProvider v2.0 with dual-model support (primary + secondary with auto-failover)
- Updated AIRouter v37.0 with auto-test on startup for Russian quality
- Updated config.py: MODEL2_PATH, MODEL_PREFERENCE, n_ctx=4096, max_tokens=256, history_limit=10
- Updated chat.py: expanded user context, news with links, web search with snippets, improved discussion mode
- Updated GitHub Actions workflow: dual model download, new env vars (MODEL2_PATH, MODEL_PREFERENCE, MODEL_HISTORY_LIMIT)
- Updated main.py: v37.0 version strings, new config imports
- Committed and pushed v37.0 to GitHub
- Triggered GitHub Actions workflow dispatch (HTTP 204 success)

Stage Summary:
- v37.0 pushed and deployed with DUAL-MODEL system
- PRIMARY: Phi-4-mini-instruct-Q4_K_M (officially supports Russian, 200K vocab)
- SECONDARY: Qwen3-4B-Instruct-Q4_K_M (proven fallback)
- Context expanded: 2048→4096 tokens, max_output: 80→256 tokens, history: 4→10 messages
- Auto-test on startup selects best model for Russian
- Auto-failover when primary model fails
- Enhanced "Обсудить с Настей" with detailed discussion prompt and link inclusion
- News context now includes URLs so model can reference them
- System prompt encourages 2-4 sentence substantive responses with links for events
