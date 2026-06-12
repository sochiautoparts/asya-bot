"""Local LLM Provider — Qwen3-4B via llama-cpp-python (GGUF).

Provides local inference for Asya Bot using llama-cpp-python with:
  - Qwen3-4B-Instruct Q4_K_M quantization (~2.5GB)
  - CPU-only inference (GitHub Actions compatible)
  - Thread-configurable for performance
  - Chat template formatting for instruction models
  - Automatic /no_think prefix for fast non-reasoning responses
  - Memory management with context window sizing
  - Fallback to cloud providers on failure

USAGE STRATEGY:
  Level 0 (LOCAL): Simple chat, comments, short responses
  Level 1-3 (CLOUD): Function routes, channel posts, VIN, diagnostics, vision

  Local model excels at:
    - Quick chat responses (saves cloud balance)
    - Group comments (short, fast, cheap)
    - Simple Q&A about cars
    - Fallback when all cloud providers are down

  Cloud models are better for:
    - Channel post generation (needs creativity + quality)
    - VIN decoding (needs accuracy)
    - Diagnostics (needs expert knowledge)
    - Vision tasks (local model can't do vision)
"""

import logging
import os
import time
from typing import Optional, List, Dict

from ai.providers.base import BaseAIProvider, AIResponse
from bot.config import config

logger = logging.getLogger("asya.ai.local")

# ── Qwen3 chat template ──
# Qwen3 uses ChatML format with special tokens
QWEN3_SYSTEM_START = "<|im_start|>system\n"
QWEN3_USER_START = "<|im_start|>user\n"
QWEN3_ASSISTANT_START = "<|im_start|>assistant\n"
QWEN3_END = "<|im_end|>\n"


class LocalProvider(BaseAIProvider):
    """Local LLM provider using llama-cpp-python for GGUF models.

    Supports Qwen3-4B-Instruct with ChatML template formatting.
    CPU-only, designed for GitHub Actions runners (ubuntu-latest).
    """

    def __init__(self):
        super().__init__(
            name="local",
            api_key="",
            base_url="",
        )
        self._llm = None
        self._model_loaded = False
        self._model_path = config.MODEL_PATH
        self._n_ctx = config.MODEL_N_CTX
        self._n_threads = config.MODEL_N_THREADS
        self._max_tokens = config.MODEL_MAX_TOKENS
        self._history_limit = config.MODEL_HISTORY_LIMIT
        self._total_requests = 0
        self._total_errors = 0
        self._last_error_time = 0.0
        self._consecutive_errors = 0
        self._available = False

    def _download_model(self) -> bool:
        """Download the GGUF model from HuggingFace if auto-download is enabled.

        Uses MODEL_DOWNLOAD_URL from config. Supports authenticated downloads
        via HF_TOKEN environment variable (required for gated models).
        Returns True if download succeeded or file already exists.
        """
        if not self._model_path:
            logger.warning("Local model: MODEL_PATH not set, cannot download")
            return False

        # Already exists
        if os.path.exists(self._model_path):
            size_mb = os.path.getsize(self._model_path) / (1024 * 1024)
            logger.info(f"Model file already exists: {self._model_path} ({size_mb:.1f} MB)")
            return True

        if not config.MODEL_AUTO_DOWNLOAD:
            logger.info("Auto-download disabled (MODEL_AUTO_DOWNLOAD=false)")
            return False

        download_url = config.MODEL_DOWNLOAD_URL
        hf_token = os.getenv("HF_TOKEN", "")

        try:
            # Create models directory
            model_dir = os.path.dirname(self._model_path)
            if model_dir:
                os.makedirs(model_dir, exist_ok=True)

            # Method 1: Use huggingface_hub if available and HF_TOKEN is set
            if hf_token:
                try:
                    from huggingface_hub import hf_hub_download
                    logger.info("Downloading model via huggingface_hub (authenticated)...")

                    # Parse repo and filename from URL
                    # URL format: https://huggingface.co/{repo_id}/resolve/main/{filename}
                    if "huggingface.co/" in download_url:
                        parts = download_url.split("huggingface.co/")[1]
                        # parts: Qwen/Qwen3-4B-GGUF/resolve/main/Qwen3-4B-Q4_K_M.gguf
                        path_parts = parts.split("/resolve/")
                        if len(path_parts) >= 2:
                            repo_id = path_parts[0]  # Qwen/Qwen3-4B-GGUF
                            filename = path_parts[1].split("/", 1)[-1]  # Qwen3-4B-Q4_K_M.gguf

                            start_time = time.time()
                            downloaded_path = hf_hub_download(
                                repo_id=repo_id,
                                filename=filename,
                                token=hf_token,
                                local_dir=model_dir or ".",
                            )
                            elapsed = time.time() - start_time

                            # hf_hub_download may save to a different path — move if needed
                            if downloaded_path != self._model_path and os.path.exists(downloaded_path):
                                import shutil
                                shutil.move(downloaded_path, self._model_path)

                            if os.path.exists(self._model_path):
                                size_mb = os.path.getsize(self._model_path) / (1024 * 1024)
                                if size_mb > 100:
                                    logger.info(f"Model downloaded via HF hub: {size_mb:.1f} MB in {elapsed:.1f}s")
                                    return True

                    logger.warning("Could not parse HuggingFace URL, falling back to direct download")
                except ImportError:
                    logger.info("huggingface_hub not installed, falling back to direct download")
                except Exception as e:
                    logger.warning(f"HF hub download failed: {e}, falling back to direct download")

            # Method 2: Direct download via urllib
            if not download_url:
                logger.warning("MODEL_DOWNLOAD_URL not set, cannot download")
                return False

            import urllib.request

            logger.info(f"Downloading model from {download_url}")
            logger.info(f"Target: {self._model_path}")

            # Add HF token as authorization header if available
            if hf_token:
                logger.info("Using HF_TOKEN for authenticated download")
                opener = urllib.request.build_opener()
                request = urllib.request.Request(download_url)
                request.add_header("Authorization", f"Bearer {hf_token}")
                response = opener.open(request)
                with open(self._model_path, 'wb') as f:
                    # Download with progress
                    total_size = int(response.headers.get('content-length', 0))
                    downloaded = 0
                    block_size = 8192
                    last_report = 0
                    while True:
                        chunk = response.read(block_size)
                        if not chunk:
                            break
                        f.write(chunk)
                        downloaded += len(chunk)
                        # Report every 10% or every 256MB
                        now = time.time()
                        if total_size > 0 and (downloaded * 100 // total_size > last_report + 10
                                               or downloaded - last_report * total_size // 100 > 256 * 1024 * 1024):
                            pct = downloaded * 100 // total_size
                            logger.info(f"  Download: {pct}% ({downloaded // 1048576}/{total_size // 1048576} MB)")
                            last_report = pct
            else:
                # No token — try unauthenticated download
                def report_progress(block_num, block_size, total_size):
                    downloaded = block_num * block_size
                    if total_size > 0:
                        percent = min(100, downloaded * 100 / total_size)
                        if block_num % 50 == 0:
                            logger.info(f"  Download: {percent:.0f}% ({downloaded // 1048576}/{total_size // 1048576} MB)")

                start_time = time.time()
                urllib.request.urlretrieve(download_url, self._model_path, reporthook=report_progress)

            # Verify download
            if not os.path.exists(self._model_path):
                logger.error("Download completed but file not found!")
                return False

            size_mb = os.path.getsize(self._model_path) / (1024 * 1024)
            if size_mb < 100:  # Sanity check — model should be ~2.5GB
                logger.error(f"Downloaded file too small ({size_mb:.1f} MB), likely corrupted. Removing.")
                os.remove(self._model_path)
                return False

            logger.info(f"Model downloaded: {size_mb:.1f} MB")
            return True

        except Exception as e:
            logger.error(f"Failed to download model: {e}")
            # Clean up partial download
            if os.path.exists(self._model_path):
                try:
                    os.remove(self._model_path)
                except Exception:
                    pass
            return False

    def _load_model(self) -> bool:
        """Load the GGUF model using llama-cpp-python.

        Automatically downloads model if file not found and MODEL_AUTO_DOWNLOAD=true.
        """
        if self._model_loaded and self._llm is not None:
            return True

        if not config.ENABLE_LOCAL_MODEL:
            logger.info("Local model DISABLED by config (ENABLE_LOCAL_MODEL=false)")
            return False

        if not self._model_path:
            logger.warning("Local model: MODEL_PATH not set")
            return False

        # Auto-download model if not found
        if not os.path.exists(self._model_path):
            logger.info(f"Model file not found at {self._model_path}, attempting auto-download...")
            if not self._download_model():
                logger.warning(f"Local model unavailable: file not found and download failed")
                return False

        try:
            from llama_cpp import Llama

            logger.info(
                f"Loading local model: {self._model_path} "
                f"(n_ctx={self._n_ctx}, n_threads={self._n_threads})"
            )

            start_time = time.time()

            self._llm = Llama(
                model_path=self._model_path,
                n_ctx=self._n_ctx,
                n_threads=self._n_threads,
                n_gpu_layers=0,  # CPU only — GitHub Actions has no GPU
                verbose=False,
                use_mlock=False,  # Don't lock memory — saves RAM
                use_mmap=True,    # Memory-mapped file — faster loading
                seed=42,          # Deterministic by default, temperature handles randomness
            )

            elapsed = time.time() - start_time
            self._model_loaded = True
            self._available = True

            logger.info(
                f"Local model loaded in {elapsed:.1f}s "
                f"(Qwen3-4B Q4_K_M, ctx={self._n_ctx}, threads={self._n_threads})"
            )
            return True

        except ImportError:
            logger.error(
                "llama-cpp-python not installed! "
                "Install with: CMAKE_ARGS='-DGGML_BLAS=ON -DGGML_BLAS_VENDOR=OpenBLAS' pip install llama-cpp-python"
            )
            return False
        except Exception as e:
            logger.error(f"Failed to load local model: {e}")
            self._llm = None
            self._model_loaded = False
            self._available = False
            return False

    def _format_messages_chatml(self, messages: List[Dict[str, str]]) -> str:
        """Format messages using ChatML template (Qwen3 format).

        Applies /no_think prefix for fast non-reasoning responses.
        Limits conversation history to MODEL_HISTORY_LIMIT exchanges.
        """
        # Limit history to reduce context length
        if len(messages) > self._history_limit + 1:  # +1 for system prompt
            # Keep system prompt + last N messages
            system_msgs = [m for m in messages if m.get("role") == "system"]
            non_system = [m for m in messages if m.get("role") != "system"]
            limited_non_system = non_system[-self._history_limit:]
            messages = system_msgs + limited_non_system

        prompt = ""
        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")

            if role == "system":
                prompt += f"{QWEN3_SYSTEM_START}{content}{QWEN3_END}"
            elif role == "user":
                prompt += f"{QWEN3_USER_START}{content}{QWEN3_END}"
            elif role == "assistant":
                prompt += f"{QWEN3_ASSISTANT_START}{content}{QWEN3_END}"

        # Add assistant prefix for generation
        # /no_think tells Qwen3 to skip reasoning and answer directly
        prompt += f"{QWEN3_ASSISTANT_START}/no_think\n"

        return prompt

    async def chat(
        self,
        messages: List[Dict[str, str]],
        model: str = "",
        temperature: float = 0.7,
        max_tokens: int = 0,
        **kwargs,
    ) -> AIResponse:
        """Generate a chat completion using the local model.

        Uses ChatML formatting with /no_think for fast responses.
        Handles think tag cleanup automatically.
        """
        if not self._load_model():
            return AIResponse(
                text="",
                model="local-qwen3-4b",
                provider=self.name,
                error=True,
                error_message="Local model not available (not loaded or not enabled)",
            )

        # Circuit breaker: if too many consecutive errors, pause briefly
        if self._consecutive_errors >= 5:
            elapsed_since_error = time.time() - self._last_error_time
            if elapsed_since_error < 120:  # 2-minute cooldown
                return AIResponse(
                    text="",
                    model="local-qwen3-4b",
                    provider=self.name,
                    error=True,
                    error_message=f"Local model in cooldown ({self._consecutive_errors} consecutive errors)",
                )
            else:
                self._consecutive_errors = 0  # Reset after cooldown

        max_tokens = max_tokens or self._max_tokens

        try:
            # Format prompt using ChatML
            prompt = self._format_messages_chatml(messages)

            # Check prompt length vs context window
            # FIX: Russian text in Qwen3 BPE tokenizes at ~1.2-1.5 chars/token,
            # NOT 2-4 as for English. Using // 2 was too optimistic and caused
            # "Requested tokens (9894) exceed context window of 4096" errors.
            # Use a CONSERVATIVE ratio of 1.3 chars/token for Russian text.
            CHARS_PER_TOKEN = 1.3
            estimated_tokens = int(len(prompt) / CHARS_PER_TOKEN)
            # Reserve tokens for the model's response + safety margin
            max_input_tokens = self._n_ctx - max_tokens - 64  # 64 token safety margin
            
            if estimated_tokens > max_input_tokens:
                logger.warning(
                    f"Prompt too long ({estimated_tokens} est. tokens, "
                    f"max_input={max_input_tokens}, ctx={self._n_ctx}), "
                    f"truncating history"
                )
                # Progressive truncation: try keeping fewer messages each time
                # Start with system + last 2, then system + last 1, then system only
                for keep_count in [2, 1, 0]:
                    if keep_count == 0:
                        truncated_messages = [messages[0]]  # System only
                    else:
                        truncated_messages = [messages[0]] + messages[-keep_count:]
                    prompt = self._format_messages_chatml(truncated_messages)
                    new_est = int(len(prompt) / CHARS_PER_TOKEN)
                    if new_est <= max_input_tokens:
                        logger.info(f"Truncated to {keep_count} history messages ({new_est} est. tokens)")
                        break
                
                # Final safety check — if even system-only prompt is too long,
                # truncate the system message itself to fit
                final_est = int(len(prompt) / CHARS_PER_TOKEN)
                if final_est > max_input_tokens:
                    # System message alone is too long — hard truncate it
                    max_system_chars = int(max_input_tokens * CHARS_PER_TOKEN)
                    system_content = messages[0].get("content", "")[:max_system_chars]
                    truncated_messages = [{"role": "system", "content": system_content}]
                    prompt = self._format_messages_chatml(truncated_messages)
                    logger.warning(
                        f"System prompt truncated to {max_system_chars} chars "
                        f"({int(len(prompt) / CHARS_PER_TOKEN)} est. tokens) to fit context"
                    )
                
                # ABSOLUTE SAFETY: After all truncation, if estimated tokens still exceed
                # context window, hard-truncate the raw prompt string to guaranteed max chars.
                # This is the last line of defense against llama.cpp context overflow.
                max_prompt_chars = int(max_input_tokens * CHARS_PER_TOKEN)
                if len(prompt) > max_prompt_chars:
                    # Keep the ChatML structure — find last complete message
                    prompt = prompt[:max_prompt_chars]
                    # Ensure we don't cut in the middle of a ChatML tag
                    last_end = prompt.rfind(QWEN3_END)
                    if last_end > len(prompt) // 2:
                        prompt = prompt[:last_end + len(QWEN3_END)]
                    # Add assistant prefix for generation
                    prompt += f"{QWEN3_ASSISTANT_START}/no_think\n"
                    logger.warning(
                        f"HARD TRUNCATION: prompt cut to {len(prompt)} chars "
                        f"({int(len(prompt) / CHARS_PER_TOKEN)} est. tokens)"
                    )

            start_time = time.time()

            # Run inference in thread pool to avoid blocking event loop
            import asyncio
            loop = asyncio.get_event_loop()

            result = await loop.run_in_executor(
                None,
                self._generate,
                prompt,
                max_tokens,
                temperature,
            )

            elapsed = time.time() - start_time

            text = result

            if not text or len(text.strip()) < 3:
                self._consecutive_errors += 1
                self._last_error_time = time.time()
                return AIResponse(
                    text="",
                    model="local-qwen3-4b",
                    provider=self.name,
                    error=True,
                    error_message="Empty or too short response from local model",
                )

            # Clean response
            text = self._clean_response(text)

            # Reset error tracking on success
            self._consecutive_errors = 0
            self._total_requests += 1

            logger.info(
                f"Local model response: {len(text)} chars, "
                f"{elapsed:.1f}s, tokens={max_tokens}"
            )

            return AIResponse(
                text=text,
                model="local-qwen3-4b",
                provider=self.name,
            )

        except Exception as e:
            self._consecutive_errors += 1
            self._last_error_time = time.time()
            self._total_errors += 1
            logger.error(f"Local model error: {e}")
            return AIResponse(
                text="",
                model="local-qwen3-4b",
                provider=self.name,
                error=True,
                error_message=str(e),
            )

    def _generate(self, prompt: str, max_tokens: int, temperature: float) -> str:
        """Synchronous generation call (runs in thread pool)."""
        result = self._llm(
            prompt,
            max_tokens=max_tokens,
            temperature=temperature,
            top_p=0.9,
            top_k=40,
            repeat_penalty=1.1,
            stop=["<|im_end|>", "<|endoftext|>", "<|im_start|>"],
        )

        # Extract text from result
        if isinstance(result, dict):
            choices = result.get("choices", [])
            if choices:
                text = choices[0].get("text", "")
                return text
        elif isinstance(result, str):
            return result

        return ""

    def _clean_response(self, text: str) -> str:
        """Clean local model response artifacts."""
        if not text:
            return ""

        # Remove think tags (Qwen3 reasoning)
        import re
        text = re.sub(r'<think\b[^>]*>.*?</think\s*>', '', text, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r'<thinking\b[^>]*>.*?</thinking\s*>', '', text, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r'</?think[^>]*>', '', text, flags=re.IGNORECASE)
        text = re.sub(r'</?thinking[^>]*>', '', text, flags=re.IGNORECASE)

        # Remove /no_think and /think prefixes
        text = re.sub(r'^/no_think\s*', '', text)
        text = re.sub(r'^/think\s*', '', text)

        # Remove ChatML artifacts
        text = text.replace("<|im_end|>", "")
        text = text.replace("<|endoftext|>", "")
        text = text.replace("<|im_start|>", "")

        # Remove common AI prefixes
        for prefix in ["Ася:", "Asya:", "АСЯ:", "Assistant:", "Ответ Аси:"]:
            if text.startswith(prefix):
                text = text[len(prefix):].strip()

        # Strip quotes
        if text.startswith('"') and text.endswith('"'):
            text = text[1:-1]
        if text.startswith("'") and text.endswith("'"):
            text = text[1:-1]

        # Strip markdown bold/italic
        text = re.sub(r'\*\*([^*]+)\*\*', r'\1', text)
        text = re.sub(r'\*([^*]+)\*', r'\1', text)

        # Clean up whitespace
        text = re.sub(r'\n{3,}', '\n\n', text)
        text = text.strip()

        return text

    async def generate_channel_post(
        self,
        topic: str,
        source_text: str = "",
        extra_instructions: str = "",
    ) -> AIResponse:
        """Generate a channel post using local model.

        NOTE: Local model is not ideal for creative channel posts.
        Used as fallback when cloud providers are unavailable.
        """
        system_prompt = (
            "Ты Ася — главред автоканала @sochiautoparts. "
            "Пиши живой автоновостной пост на русском. "
            "Без markdown. Без буллетов. С эмоцией и мнением. "
            "В конце: Автор @asiaexp_bot\\n@sochiautoparts\\n#sochiautoparts + хештеги. "
            "До 1024 символов."
        )

        # CRITICAL: Truncate source text to prevent context overflow.
        # With 4096 ctx, we can afford ~2000 chars for user content
        # (system prompt ~200 chars, max_tokens=800 ~1040 chars output, safety margin).
        user_content = f"Тема: {topic[:200]}"
        if source_text:
            user_content += f"\n\nИсходный текст:\n{source_text[:1500]}"
        if extra_instructions:
            user_content += f"\n\nИнструкции: {extra_instructions[:300]}"

        # Total user content must not exceed ~2000 chars for local model
        if len(user_content) > 2000:
            user_content = user_content[:2000]

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ]

        return await self.chat(
            messages=messages,
            temperature=0.8,
            max_tokens=800,  # Shorter for local model
        )

    async def is_available(self) -> bool:
        """Check if local model is available."""
        if not config.ENABLE_LOCAL_MODEL:
            return False

        if self._consecutive_errors >= 5:
            elapsed = time.time() - self._last_error_time
            if elapsed < 120:
                return False

        # Try to load if not loaded
        if not self._model_loaded:
            return self._load_model()

        return self._model_loaded and self._llm is not None

    def get_status(self) -> str:
        """Get status summary."""
        if not config.ENABLE_LOCAL_MODEL:
            return "DISABLED"

        if self._model_loaded:
            return (
                f"LOADED (Qwen3-4B, ctx={self._n_ctx}, "
                f"threads={self._n_threads}, "
                f"reqs={self._total_requests}, "
                f"errors={self._total_errors})"
            )
        elif self._model_path and not os.path.exists(self._model_path):
            return f"MODEL_NOT_FOUND ({self._model_path})"
        else:
            return "NOT_LOADED"

    def unload(self) -> None:
        """Unload model to free memory."""
        if self._llm is not None:
            del self._llm
            self._llm = None
            self._model_loaded = False
            logger.info("Local model unloaded (memory freed)")
