"""Inter-Bot Communication — DISABLED (legacy Настя integration removed).

Each bot now works independently. The interbot system was used for
Ася ↔ Настя editorial review workflow, but Настя no longer participates
in Ася's post pipeline. Ася handles all content filtering and publishing
on her own.

This module is kept as a stub for backward compatibility — any imports
of interbot_manager will get a no-op instance.
"""
import logging
from typing import Dict, List

logger = logging.getLogger("asya.interbot")


class InterbotManager:
    """Stub — inter-bot communication is disabled. Each bot works independently."""

    def __init__(self):
        pass

    def configure(self, gh_pat: str = "", channel_manager=None):
        """No-op — interbot is disabled."""
        logger.info("InterbotManager: configure called (NO-OP, interbot disabled)")

    async def init(self):
        """No-op — interbot is disabled."""
        logger.info("InterbotManager: init called (NO-OP, interbot disabled)")

    async def submit_news_candidate(self, title: str, summary: str = "", category: str = "auto") -> str:
        """No-op — returns empty ID."""
        return ""

    async def check_reviews(self) -> List[Dict]:
        """No-op — returns empty list."""
        return []

    async def get_reviewed_candidates(self) -> List[Dict]:
        """No-op — returns empty list."""
        return []

    async def mark_candidate_processed(self, candidate_id: str):
        """No-op."""
        pass

    def should_publish_without_review(self, candidate: Dict) -> bool:
        """Always True — no review needed, Ася publishes independently."""
        return True

    async def cleanup_stale_candidates(self, max_age_seconds: int = 600):
        """No-op."""
        pass

    async def send_message(self, text: str, to: str = "nastya") -> bool:
        """No-op — returns True to not break callers."""
        return True

    async def check_messages(self) -> List[Dict]:
        """No-op — returns empty list."""
        return []

    def register_shared_chat(self, chat_id: int, chat_title: str = ""):
        """No-op."""
        pass

    def is_shared_chat(self, chat_id: int) -> bool:
        """Always False — no shared chats."""
        return False

    def get_status(self) -> Dict:
        """Returns empty status."""
        return {
            "pending_candidates": 0,
            "processed_candidates": 0,
            "unread_messages": 0,
            "shared_chats": 0,
        }


# ── Global instance (stub) ──
interbot_manager = InterbotManager()


# ── Backward compatible convenience functions ──

async def send_to_nastya(message: str, msg_type: str = "info") -> bool:
    """No-op — interbot is disabled."""
    return True


async def check_messages() -> List[str]:
    """No-op — returns empty list."""
    return []
