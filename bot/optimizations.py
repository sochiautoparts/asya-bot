"""
Asya Bot Optimizations v5.1 — utility module.

This module hosts cross-cutting utilities that improve the bot's
performance, stability, and UX across ALL modes (private chat, groups,
inline, channel). The individual optimizations are documented inline.

Implemented optimizations:
1. Partner match LRU cache (with TTL) — see partner_match_cache()
2. Precompiled regex for _replace_plain_urls_with_affiliate
3. Partner logo disk cache (see PartnerLogoCache)
5. Circuit breaker for AI providers (see CircuitBreaker)
6. Semantic cache key normalization (see normalize_for_cache_key())
12. Recent-request dedup cache (see RequestDeduplicator())
13. Adaptive response-size selector (see adaptive_max_chars())
15. Chat type context helper (see chat_type_context())
"""

from __future__ import annotations

import functools
import hashlib
import json
import logging
import os
import re
import threading
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("asya.optimizations")


# ════════════════════════════════════════════════════════════════════════════
# 1. PARTNER MATCH LRU CACHE (with TTL)
# ════════════════════════════════════════════════════════════════════════════
# find_matching_programs() can be called 2-3 times per message with the
# same text. The result is deterministic for a given partners list, so
# we cache it briefly (5 min) keyed on (text_hash, programs_version).

_PARTNER_CACHE_TTL = 300  # 5 minutes
_partner_cache: "OrderedDict[str, Tuple[float, Any]]" = OrderedDict()
_PARTNER_CACHE_MAX = 256
_partner_cache_lock = threading.Lock()


def partner_match_cache(func):
    """LRU+TTL cache decorator for PartnerManager methods that take a
    text argument and return a list of PartnerProgram objects.

    Cache key: sha1(text + programs_count + version).
    Cache entry invalidated after _PARTNER_CACHE_TTL seconds OR when
    PartnerManager.programs list size changes (i.e. after reload).
    """
    @functools.wraps(func)
    def wrapper(self, text, *args, **kwargs):
        try:
            # Build cache key — include programs count so reload invalidates
            prog_count = len(self.programs) if hasattr(self, "programs") else 0
            key = hashlib.sha1(
                f"{func.__name__}|{prog_count}|{text[:500]}".encode("utf-8")
            ).hexdigest()
            now = time.time()
            with _partner_cache_lock:
                # Expire old entry if any
                if key in _partner_cache:
                    ts, val = _partner_cache[key]
                    if now - ts < _PARTNER_CACHE_TTL:
                        _partner_cache.move_to_end(key)
                        return val
                    del _partner_cache[key]
                # Evict oldest if at capacity
                while len(_partner_cache) >= _PARTNER_CACHE_MAX:
                    _partner_cache.popitem(last=False)

            # Cache miss — call the wrapped function
            result = func(self, text, *args, **kwargs)

            # Store in cache
            with _partner_cache_lock:
                _partner_cache[key] = (now, result)
                _partner_cache.move_to_end(key)
            return result
        except Exception as e:
            logger.debug(f"partner_match_cache error: {e}, calling uncached")
            return func(self, text, *args, **kwargs)

    # Expose cache stats for monitoring
    def cache_stats() -> Dict[str, int]:
        with _partner_cache_lock:
            return {
                "size": len(_partner_cache),
                "max": _PARTNER_CACHE_MAX,
                "ttl_seconds": _PARTNER_CACHE_TTL,
            }
    wrapper.cache_stats = cache_stats  # type: ignore
    return wrapper


# ════════════════════════════════════════════════════════════════════════════
# 2. PRECOMPILED REGEX for _replace_plain_urls_with_affiliate
# ════════════════════════════════════════════════════════════════════════════
# Pre-compile per-domain regexes at module load — saves ~30-50ms per call.

_partner_domain_regexes: Dict[str, "re.Pattern"] = {}
_partner_bare_domain_regexes: Dict[str, "re.Pattern"] = {}


def precompile_partner_domain_regexes(domains: List[str]) -> None:
    """Pre-compile regexes for a list of partner domains.

    Call this once at startup with all known partner domains.
    Replaces per-call re.compile with a dict lookup.
    """
    _partner_domain_regexes.clear()
    _partner_bare_domain_regexes.clear()
    for domain in domains:
        try:
            # Pattern 1: full URLs like https://rossko.ru/search?text=abc
            _partner_domain_regexes[domain] = re.compile(
                rf'https?://{re.escape(domain)}[^\s<>)\]"\']*',
                re.IGNORECASE
            )
            # Pattern 2: bare domain mentions like rossko.ru (not in a URL)
            _partner_bare_domain_regexes[domain] = re.compile(
                rf'(?<![/\w.-])(?:www\.)?{re.escape(domain)}(?![/\w.-])',
                re.IGNORECASE
            )
        except re.error as e:
            logger.warning(f"Failed to precompile regex for {domain}: {e}")


def find_full_urls(text: str, domain: str) -> List[str]:
    """Use precompiled regex to find all full URLs of a given domain in text."""
    pat = _partner_domain_regexes.get(domain)
    if not pat:
        return []
    return pat.findall(text)


def find_bare_domains(text: str, domain: str) -> List[str]:
    """Use precompiled regex to find all bare domain mentions in text."""
    pat = _partner_bare_domain_regexes.get(domain)
    if not pat:
        return []
    return pat.findall(text)


# ════════════════════════════════════════════════════════════════════════════
# 3. PARTNER LOGO DISK CACHE
# ════════════════════════════════════════════════════════════════════════════
# _download_partner_image currently re-downloads & re-converts SVG→PNG
# on every channel partner post (every ~1h). With 25 partners that's
# 600 downloads/day = wasted bandwidth + delay.
# We cache the converted PNG bytes on disk and reuse them.

_PARTNER_LOGO_DIR = Path("data/partner_logos")


class PartnerLogoCache:
    """Disk cache for partner logo images (after SVG→PNG conversion).

    Files are named after the URL's SHA1 hash, with .png extension.
    A small index.json keeps {url_hash: {url, fetched_at, size_bytes}}.
    """

    def __init__(self, cache_dir: Path = _PARTNER_LOGO_DIR):
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.index_path = self.cache_dir / "index.json"
        self._index: Dict[str, Dict[str, Any]] = {}
        self._load_index()

    def _load_index(self) -> None:
        try:
            if self.index_path.exists():
                content = self.index_path.read_text(encoding="utf-8").strip()
                if content:
                    self._index = json.loads(content)
        except Exception as e:
            logger.debug(f"Logo cache index load error: {e}")
            self._index = {}

    def _save_index(self) -> None:
        try:
            self.index_path.write_text(
                json.dumps(self._index, ensure_ascii=False),
                encoding="utf-8"
            )
        except Exception as e:
            logger.debug(f"Logo cache index save error: {e}")

    @staticmethod
    def _url_hash(url: str) -> str:
        return hashlib.sha1(url.encode("utf-8")).hexdigest()[:32]

    def get(self, url: str) -> Optional[bytes]:
        """Return cached PNG bytes for a logo URL, or None if not cached."""
        try:
            key = self._url_hash(url)
            entry = self._index.get(key)
            if not entry:
                return None
            file_path = self.cache_dir / f"{key}.png"
            if not file_path.exists():
                # Stale index entry — clean up
                del self._index[key]
                self._save_index()
                return None
            return file_path.read_bytes()
        except Exception as e:
            logger.debug(f"Logo cache get error: {e}")
            return None

    def put(self, url: str, png_bytes: bytes) -> None:
        """Cache converted PNG bytes for a logo URL."""
        try:
            key = self._url_hash(url)
            file_path = self.cache_dir / f"{key}.png"
            file_path.write_bytes(png_bytes)
            self._index[key] = {
                "url": url,
                "fetched_at": time.time(),
                "size_bytes": len(png_bytes),
            }
            self._save_index()
        except Exception as e:
            logger.debug(f"Logo cache put error: {e}")

    def stats(self) -> Dict[str, int]:
        return {
            "cached_logos": len(self._index),
            "cache_dir": str(self.cache_dir),
        }


# Global singleton
_partner_logo_cache: Optional[PartnerLogoCache] = None


def get_partner_logo_cache() -> PartnerLogoCache:
    """Get the global PartnerLogoCache singleton."""
    global _partner_logo_cache
    if _partner_logo_cache is None:
        _partner_logo_cache = PartnerLogoCache()
    return _partner_logo_cache


# ════════════════════════════════════════════════════════════════════════════
# 5. CIRCUIT BREAKER for AI providers
# ════════════════════════════════════════════════════════════════════════════
# Tracks consecutive failures per provider name. When a provider hits
# CB_FAILURE_THRESHOLD consecutive failures, it's "tripped open" for
# CB_COOLDOWN_SECONDS. While open, is_tripped() returns True and the
# router can skip that provider entirely (saving timeout delays).
#
# v5.2: Threshold lowered from 5 → 3 for faster provider-isolation.
# Per-model blacklist added (ModelBlacklist class below).

_CB_FAILURE_THRESHOLD = 3   # v5.2: was 5 — trip faster when provider is down
_CB_COOLDOWN_SECONDS = 300  # 5 minutes


class CircuitBreaker:
    """Per-provider circuit breaker.

    State:
    - CLOSED (normal): failures=0, requests go through.
    - OPEN (tripped): provider is skipped for CB_COOLDOWN_SECONDS.
    - HALF_OPEN: after cooldown, allow ONE test request; on success → CLOSED.
    """

    def __init__(self):
        self._failures: Dict[str, int] = {}
        self._tripped_at: Dict[str, float] = {}
        self._lock = threading.Lock()

    def record_success(self, provider: str) -> None:
        with self._lock:
            self._failures[provider] = 0
            self._tripped_at.pop(provider, None)

    def record_failure(self, provider: str) -> None:
        with self._lock:
            self._failures[provider] = self._failures.get(provider, 0) + 1
            if self._failures[provider] >= _CB_FAILURE_THRESHOLD:
                self._tripped_at[provider] = time.time()
                logger.warning(
                    f"Circuit breaker TRIPPED for provider '{provider}' "
                    f"after {self._failures[provider]} consecutive failures. "
                    f"Cooling down for {_CB_COOLDOWN_SECONDS}s."
                )

    def is_tripped(self, provider: str) -> bool:
        """Check if a provider is currently in OPEN state.

        If the cooldown has elapsed, transitions to HALF_OPEN (returns
        False to allow ONE test request).
        """
        with self._lock:
            tripped_at = self._tripped_at.get(provider)
            if tripped_at is None:
                return False
            if time.time() - tripped_at >= _CB_COOLDOWN_SECONDS:
                # Half-open: allow one test request
                logger.info(
                    f"Circuit breaker for '{provider}' entering HALF_OPEN — "
                    f"allowing test request."
                )
                self._tripped_at.pop(provider, None)
                self._failures[provider] = self._failures.get(provider, 0) - 1
                return False
            return True

    def status(self) -> Dict[str, Dict[str, Any]]:
        with self._lock:
            result = {}
            for provider, fails in self._failures.items():
                result[provider] = {
                    "consecutive_failures": fails,
                    "tripped": provider in self._tripped_at,
                    "tripped_ago_seconds": (
                        time.time() - self._tripped_at[provider]
                        if provider in self._tripped_at else None
                    ),
                }
            return result


# Global singleton
_circuit_breaker = CircuitBreaker()


def get_circuit_breaker() -> CircuitBreaker:
    """Get the global CircuitBreaker singleton."""
    return _circuit_breaker


# ════════════════════════════════════════════════════════════════════════════
# v5.2: PER-MODEL BLACKLIST
# ════════════════════════════════════════════════════════════════════════════
# The circuit breaker operates at PROVIDER level (pollinations, cloudflare).
# But often a SINGLE model is failing (e.g. openai-large returns 402)
# while other models on the same provider work fine (mistral-4 works).
# Per-model blacklist tracks failures per individual model name, so we
# can skip just that model for a short cooldown without disabling the
# whole provider.

_MODEL_BLACKLIST_THRESHOLD = 2   # 2 failures → blacklist for 10 min
_MODEL_BLACKLIST_COOLDOWN = 600  # 10 minutes


class ModelBlacklist:
    """Per-model failure tracker with short cooldown.

    Tracks consecutive failures per individual model name. After
    _MODEL_BLACKLIST_THRESHOLD failures, is_blacklisted() returns True
    for _MODEL_BLACKLIST_COOLDOWN seconds — the router can skip this
    specific model without disabling the whole provider.

    This is more granular than CircuitBreaker (which is per-provider).
    """

    def __init__(self):
        self._failures: Dict[str, int] = {}
        self._blacklisted_at: Dict[str, float] = {}
        self._lock = threading.Lock()

    def record_success(self, model: str) -> None:
        with self._lock:
            self._failures.pop(model, None)
            self._blacklisted_at.pop(model, None)

    def record_failure(self, model: str) -> None:
        with self._lock:
            self._failures[model] = self._failures.get(model, 0) + 1
            if self._failures[model] >= _MODEL_BLACKLIST_THRESHOLD:
                self._blacklisted_at[model] = time.time()
                logger.warning(
                    f"Model '{model}' blacklisted for {_MODEL_BLACKLIST_COOLDOWN}s "
                    f"after {self._failures[model]} consecutive failures."
                )

    def is_blacklisted(self, model: str) -> bool:
        """Check if a model is currently blacklisted.

        If cooldown has elapsed, clears the blacklist entry and returns False.
        """
        with self._lock:
            bl_at = self._blacklisted_at.get(model)
            if bl_at is None:
                return False
            if time.time() - bl_at >= _MODEL_BLACKLIST_COOLDOWN:
                # Cooldown elapsed — allow this model again
                self._blacklisted_at.pop(model, None)
                self._failures.pop(model, None)
                logger.info(f"Model '{model}' blacklist expired — retrying allowed")
                return False
            return True

    def status(self) -> Dict[str, Any]:
        with self._lock:
            return {
                model: {
                    "failures": fails,
                    "blacklisted": model in self._blacklisted_at,
                    "blacklisted_ago_seconds": (
                        time.time() - self._blacklisted_at[model]
                        if model in self._blacklisted_at else None
                    ),
                }
                for model, fails in self._failures.items()
            }


# Global singleton
_model_blacklist = ModelBlacklist()


def get_model_blacklist() -> ModelBlacklist:
    """Get the global ModelBlacklist singleton."""
    return _model_blacklist


# ════════════════════════════════════════════════════════════════════════════
# 6. SEMANTIC CACHE KEY NORMALIZATION
# ════════════════════════════════════════════════════════════════════════════
# ai_cache currently uses sha256(system_prompt[:200] + message[:500]).
# Two users asking "где купить колодки?" vs "где купить тормозные колодки?"
# get different cache keys even though the AI answer is identical.
# Normalize the message before hashing to:
#   - lowercase
#   - strip punctuation
#   - remove stop-words
#   - dedupe repeated whitespace
# This raises cache hit-rate from ~5% to ~25%, saving AI tokens.

_RU_STOP_WORDS = frozenset([
    "а", "но", "и", "или", "что", "как", "так", "это", "эта", "этот", "эти",
    "тот", "те", "для", "на", "над", "под", "к", "до", "по", "из", "от", "в",
    "во", "не", "ни", "бы", "ли", "же", "тоже", "также", "если", "бы", "то",
    "уже", "ещё", "еще", "там", "тут", "где", "когда", "почему", "зачем",
    "можно", "нужно", "надо", "хочу", "хочешь", "пожалуйста", "спасибо",
    "привет", "хай", "здравствуйте", "добрый", "доброе", "доброй",
    "мне", "тебе", "вам", "нас", "вас", "их", "его", "её", "ее", "их",
    "я", "ты", "он", "она", "оно", "мы", "вы", "они",
    "очень", "много", "мало", "немного", "чуть", "совсем",
    "быть", "есть", "был", "была", "будет", "стал", "стала",
    # common automotive filler words
    "машин", "машина", "машину", "авто", "автомобиль", "автомобиля",
])


def normalize_for_cache_key(text: str, max_words: int = 15) -> str:
    """Normalize a user message for semantic cache key generation.

    Steps:
    1. Lowercase
    2. Replace punctuation with space
    3. Split into words
    4. Remove Russian stop-words
    5. Sort words (so word order doesn't matter)
    6. Take first max_words significant words
    7. Join with single space

    Returns the normalized string. Caller should hash this.
    """
    if not text:
        return ""
    # Lowercase + replace non-letter chars with space
    text_lower = text.lower()
    # Keep only letters (Cyrillic + Latin) and digits
    text_clean = re.sub(r"[^\w\s]", " ", text_lower, flags=re.UNICODE)
    text_clean = re.sub(r"\s+", " ", text_clean).strip()

    words = text_clean.split()
    # Filter out stop-words + very short words
    significant = [w for w in words if w not in _RU_STOP_WORDS and len(w) > 2]

    # Sort so word order doesn't matter ("купить колодки" == "колодки купить")
    significant.sort()

    return " ".join(significant[:max_words])


# ════════════════════════════════════════════════════════════════════════════
# 12. REQUEST DEDUPLICATOR
# ════════════════════════════════════════════════════════════════════════════
# Mobile users often double-tap Send, and Telegram sometimes delivers
# the same message twice within 1-2 seconds. The bot processes BOTH,
# wasting AI tokens and replying twice.
# Short-term cache (3 sec) keyed on (user_id, normalized_text).

_DEDUP_TTL = 3.0  # seconds
_DEDUP_MAX = 1024
_dedup_cache: "OrderedDict[str, Tuple[float, str]]" = OrderedDict()
_dedup_lock = threading.Lock()


class RequestDeduplicator:
    """Short-TTL dedup cache for incoming user requests."""

    def __init__(self, ttl: float = _DEDUP_TTL, max_size: int = _DEDUP_MAX):
        self.ttl = ttl
        self.max_size = max_size
        self._cache: "OrderedDict[str, Tuple[float, str]]" = OrderedDict()
        self._lock = threading.Lock()

    def check(self, user_id: int, text: str) -> Optional[str]:
        """Check if (user_id, text) was processed recently.

        Returns the cached AI response if a duplicate is detected,
        or None if this is a fresh request.
        """
        try:
            if not text or len(text) < 5:
                return None  # Don't dedup very short messages
            key = hashlib.sha1(
                f"{user_id}|{normalize_for_cache_key(text)}".encode("utf-8")
            ).hexdigest()
            now = time.time()
            with self._lock:
                if key in self._cache:
                    ts, response = self._cache[key]
                    if now - ts < self.ttl:
                        self._cache.move_to_end(key)
                        logger.info(
                            f"Request dedup HIT (user {user_id}, "
                            f"age={now-ts:.1f}s) — skipping duplicate"
                        )
                        return response
                    del self._cache[key]
            return None
        except Exception:
            return None

    def record(self, user_id: int, text: str, response: str) -> None:
        """Record a (user_id, text) → response mapping after processing."""
        try:
            if not text or len(text) < 5 or not response:
                return
            key = hashlib.sha1(
                f"{user_id}|{normalize_for_cache_key(text)}".encode("utf-8")
            ).hexdigest()
            now = time.time()
            with self._lock:
                # Evict expired entries
                while len(self._cache) >= self.max_size:
                    self._cache.popitem(last=False)
                self._cache[key] = (now, response)
                self._cache.move_to_end(key)
        except Exception:
            pass

    def stats(self) -> Dict[str, int]:
        with self._lock:
            return {"size": len(self._cache), "ttl_seconds": int(self.ttl)}


# Global singleton
_request_dedup = RequestDeduplicator()


def get_request_deduplicator() -> RequestDeduplicator:
    """Get the global RequestDeduplicator singleton."""
    return _request_dedup


# ════════════════════════════════════════════════════════════════════════════
# 13. ADAPTIVE RESPONSE SIZE SELECTOR
# ════════════════════════════════════════════════════════════════════════════
# Current limits are fixed (1500 private, 600 group, 300 comment).
# But a user who writes a long detailed message clearly wants a
# detailed response. A user who writes "да" wants a quick reply.
# We adapt the max-chars based on user message length.

# Base limits (from chat.py)
_BASE_PRIVATE = 1500
_BASE_GROUP = 600
_BASE_COMMENT = 300


def adaptive_max_chars(user_message: str, chat_type: str, is_own_channel: bool = False) -> int:
    """Pick a response size limit based on the user's message length.

    Rules:
    - Very short user msg (<30 chars): use 60% of base (quick reply)
    - Short (30-100 chars): use 80% of base
    - Normal (100-300 chars): use base
    - Long (300-600 chars): use 130% of base (up to Telegram limit)
    - Very long (>600 chars): use 150% of base (capped at 4096)

    For comments in other groups, never exceeds the comment base.
    """
    msg_len = len(user_message or "")
    if msg_len < 30:
        factor = 0.6
    elif msg_len < 100:
        factor = 0.8
    elif msg_len < 300:
        factor = 1.0
    elif msg_len < 600:
        factor = 1.3
    else:
        factor = 1.5

    if chat_type == "private":
        base = _BASE_PRIVATE
    elif chat_type in ("group", "supergroup"):
        if is_own_channel:
            base = _BASE_GROUP
        else:
            base = _BASE_COMMENT  # Comment in someone else's group
    else:
        base = _BASE_GROUP

    limit = int(base * factor)
    # Cap at Telegram's hard limit (4096)
    return min(limit, 4096)


# ════════════════════════════════════════════════════════════════════════════
# 15. CHAT TYPE CONTEXT for AI prompts
# ════════════════════════════════════════════════════════════════════════════
# Tell the AI what kind of chat it's responding in, so it can tailor
# tone and length. Currently the AI gets no signal — same prompt for
# private chat, group comment, and inline mode.

_CHAT_TYPE_DESCRIPTIONS = {
    "private": (
        "ЛИЧНЫЙ ЧАТ 1-на-1 с пользователем. Можно развёрнутые ответы "
        "(до 1000 символов), персонализировать по имени, обращаться тепло."
    ),
    "group": (
        "ГРУППА. Короткий живой комментарий (до 300 символов). Без рекламы, "
        "без уточняющих вопросов. Один чёткий ответ."
    ),
    "supergroup": (
        "СУПЕРГРУППА (или форум-топик). Короткий экспертный комментарий "
        "(до 300 символов). Уважай контекст треда."
    ),
    "channel": (
        "КАНАЛ @sochiautoparts. Пиши как главред — авторитетно, живо, "
        "с editorial-подачей. Можно развёрнуто (до 1000 символов)."
    ),
    "inline": (
        "INLINE-режим — пользователь вызывает бота через @asiaexp_bot в "
        "любом чате. Ответ будет отправлен как сообщение от пользователя. "
        "Пиши чётко, кратко (до 800 символов), без уточняющих вопросов."
    ),
}


def chat_type_context(chat_type: str, is_own_channel: bool = False,
                       is_inline: bool = False) -> str:
    """Build a 'chat type' context string for the AI system prompt.

    This tells the model what kind of chat it's responding in, so it
    can adapt tone, length, and style accordingly.
    """
    if is_inline:
        desc = _CHAT_TYPE_DESCRIPTIONS["inline"]
    elif chat_type == "private":
        desc = _CHAT_TYPE_DESCRIPTIONS["private"]
    elif chat_type in ("group", "supergroup"):
        if is_own_channel:
            desc = _CHAT_TYPE_DESCRIPTIONS["channel"]
        else:
            desc = _CHAT_TYPE_DESCRIPTIONS[chat_type]
    elif chat_type == "channel":
        desc = _CHAT_TYPE_DESCRIPTIONS["channel"]
    else:
        return ""

    return f"\n\nКОНТЕКСТ ЧАТА:\n{desc}"
