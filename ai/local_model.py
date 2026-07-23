"""Local LLM provider — Qwen2.5-0.5B-Instruct (GGUF format).

Always available, no network needed. Ensures posting NEVER stops
even when all cloud providers (Pollinations, OpenClaw, Cloudflare) fail.

Model: Qwen/Qwen2.5-0.5B-Instruct-GGUF (Q4_K_M, ~469MB)
- Load time: ~0.5s
- Generation: ~17 tok/s (4 CPU threads)
- Russian quality: good (Qwen trained on multilingual data)
- RAM: ~1GB
"""
import asyncio, logging, os, time
from typing import Optional

logger = logging.getLogger("asya.local")

_llm = None
_init_lock = asyncio.Lock()
_init_failed = False

async def _get_llm():
    """Load local model lazily (first call). Returns Llama instance or None."""
    global _llm, _init_failed
    if _llm is not None:
        return _llm
    if _init_failed:
        return None  # Don't retry if init failed once
    async with _init_lock:
        if _llm is not None:
            return _llm
        try:
            from llama_cpp import Llama
            model_path = os.getenv("LOCAL_MODEL_PATH", "data/qwen2.5-0.5b.gguf")
            if not os.path.exists(model_path):
                logger.warning(f"Local model file not found: {model_path}")
                _init_failed = True
                return None
            logger.info(f"Loading local model: {model_path}")
            t0 = time.time()
            _llm = Llama(
                model_path=model_path,
                n_ctx=4096,
                n_threads=int(os.getenv("LOCAL_MODEL_THREADS", "4")),
                n_gpu_layers=0,
                verbose=False,
            )
            logger.info(f"Local model loaded in {time.time()-t0:.1f}s")
            return _llm
        except ImportError:
            logger.warning("llama-cpp-python not installed — local model unavailable")
            _init_failed = True
            return None
        except Exception as e:
            logger.warning(f"Local model load failed: {e}")
            _init_failed = True
            return None

async def call_local(messages, max_tokens=250, temperature=0.8):
    """Call local Qwen2.5-0.5B model.

    Args:
        messages: List of {"role": "user"/"system"/"assistant", "content": "..."}
        max_tokens: Max tokens to generate (capped at 250 for speed)
        temperature: 0.0-1.0

    Returns:
        Generated text or empty string on failure.
    """
    llm = await _get_llm()
    if llm is None:
        return ""

    try:
        loop = asyncio.get_event_loop()

        # Truncate messages to fit context window (leave room for generation)
        # Context is 4096, reserve 512 for generation, 3584 for input
        truncated = []
        total_chars = 0
        max_input_chars = 12000  # ~3000 tokens (safe margin)
        for msg in messages:
            content = msg.get("content", "")
            if total_chars + len(content) > max_input_chars:
                # Truncate this message
                remaining = max_input_chars - total_chars
                if remaining > 100:
                    content = content[:remaining] + "..."
                    msg = {**msg, "content": content}
                    truncated.append(msg)
                break
            truncated.append(msg)
            total_chars += len(content)

        def _generate():
            return llm.create_chat_completion(
                messages=truncated,
                max_tokens=min(max_tokens, 250),
                temperature=temperature,
                top_p=0.9,
                repeat_penalty=1.15,
            )

        response = await loop.run_in_executor(None, _generate)
        content = response["choices"][0]["message"]["content"].strip()
        if content and len(content) > 10:
            return content
        return ""
    except Exception as e:
        logger.warning(f"Local model generation error: {e}")
        return ""

def is_available():
    """Check if local model is loaded and ready."""
    return _llm is not None
