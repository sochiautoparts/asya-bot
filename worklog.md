---
Task ID: 1
Agent: Main
Task: Fix nastya-bot — diagnose and fix garbage responses, model not found errors, and broken AI pipeline

Work Log:
- Cloned repository https://github.com/sochiautoparts/nastya-bot
- Examined all key files: ollama_cluster_provider.py, router.py, config.py, chat.py, main.py, news.py, start.sh, pollinations_provider.py
- Identified ROOT CAUSES of garbage responses:
  1. qwen2.5:1.5b NOT installed but configured as primary → 404 errors → falls back to slow reserve
  2. _detect_models() just logs warning when primary model missing, doesn't auto-select
  3. num_predict=100 too low for Qwen3 (thinking mode uses tokens before visible response)
  4. Qwen3 thinking mode not properly handled (think=False not always effective)
  5. vikhr-1b still installed and could be selected despite generating garbage
  6. News links too aggressively appended (2-word match threshold too low)
- Implemented v34 SMART MODEL AUTO-DETECTION:
  1. OllamaClusterProvider now auto-detects best model from installed models
  2. MODEL_PRIORITY list: qwen2.5:1.5b > qwen3:4b-instruct > others
  3. BANNED_MODELS: vikhr completely ignored
  4. MODEL_CONFIGS: individual num_predict, timeout, think per model
  5. Qwen3: num_predict=250, think=False, timeout=60s
  6. qwen2.5:1.5b: num_predict=150, timeout=25s
- Updated start.sh with retry logic for model pulling
- Optimized system prompt with examples for small models
- Stricter news link matching (3+ words instead of 2)
- Minimal web search injection (1 result instead of 2)
- Increased news commentary delay to 15s
- Pushed all changes to repository (commit 8fdeb7b)

Stage Summary:
- 7 files modified, 338 insertions, 211 deletions
- Key fix: model auto-detection prevents "model not found" errors
- Key fix: Qwen3 thinking mode handled correctly with larger num_predict
- Key fix: banned models (vikhr) completely ignored
- Commit pushed: v34 SMART MODEL AUTO-DETECTION
