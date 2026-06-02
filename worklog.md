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

---
Task ID: 2
Agent: main
Task: v38.0 — Benchmark models, select reserve model, fix text truncation, smart message splitting

Work Log:
- Benchmarked Phi-4-mini vs Qwen3-4B on Russian text generation:
  - Phi-4-mini: first request extremely slow (0.5 t/s), formal robotic style, ignores persona
  - Qwen3-4B: consistently fast (10+ t/s), live conversational style, uses emoji, follows persona
- DECISION: Qwen3-4B = PRIMARY (winner by quality and speed)
- Downloaded Qwen2.5-3B-Instruct Q4_K_M (~2.0GB) as SECONDARY reserve model
- Benchmarked Qwen2.5-3B: fast (12-14 t/s), decent Russian, good as reserve
- Removed Phi-4-mini model (underperformed on all metrics)
- Fixed text truncation bug:
  - OLD: _clean_response() truncated at 800 chars — recipes got cut off!
  - NEW: only truncates at 3900 chars (near Telegram limit), keeps full text
- Implemented _smart_split_message() — splits long messages at sentence/paragraph boundaries
  - Priority: paragraph break > line break > sentence end > word boundary > hard cut
  - Each message part is meaningful and complete
- Expanded limits:
  - max_tokens: 256 → 512 (longer, more detailed responses)
  - history_limit: 10 → 20 (deeper conversation memory)
  - n_ctx: stays 4096
- Updated /no_think: now applies to BOTH Qwen3 and Qwen2.5 (both support it)
- Updated system prompt: recipes/instructions must be complete, don't truncate
- Updated start.sh: downloads both models (Qwen3 + Qwen2.5-3B)
- Updated .env.example with all new defaults
- Pushed v38.0 to GitHub

Stage Summary:
- v38.0 pushed and deployed
- PRIMARY: Qwen3-4B-Instruct Q4_K_M (best Russian, live conversational)
- SECONDARY: Qwen2.5-3B-Instruct Q4_K_M (lightweight fast reserve, 12-14 t/s)
- Text truncation FIXED — no more cut-off recipes
- Smart message splitting at sentence boundaries
- max_tokens=512, history=20 messages, n_ctx=4096
