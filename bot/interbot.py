"""Inter-Bot Communication System v2.0 — GitHub-based state sync.

Ася ↔ Настя — межботовое взаимодействие через GitHub-hosted interbot_state.json.

Architecture:
  - Each bot has interbot_state.json in its own repo
  - Read other bot's state via raw.githubusercontent.com
  - Write own state via GitHub Contents API (PAT auth)
  - Настя as AI-Filter: reviews Ася's news candidates before publishing

Flow:
  1. Ася collects news candidates
  2. Ася writes candidates to its interbot_state.json (pending_reviews)
  3. Настя reads Ася's candidates, reviews them, writes reviews to her state
  4. Ася reads Настя's reviews, publishes only approved/reimproved posts
  5. If Настя unavailable, Ася publishes independently after timeout
"""
import json
import logging
import time
import asyncio
import uuid
import httpx
import base64
from typing import List, Dict, Optional
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo

logger = logging.getLogger("asya.interbot")

_MOSCOW_TZ = ZoneInfo("Europe/Moscow")

# GitHub repo details
ASYA_REPO = "sochiautoparts/asya-bot"
NASTYA_REPO = "sochiautoparts/nastya-bot"
INTERBOT_FILE = "interbot_state.json"

# URLs
ASYA_RAW_URL = f"https://raw.githubusercontent.com/{ASYA_REPO}/main/{INTERBOT_FILE}"
NASTYA_RAW_URL = f"https://raw.githubusercontent.com/{NASTYA_REPO}/main/{INTERBOT_FILE}"
ASYA_API_URL = f"https://api.github.com/repos/{ASYA_REPO}/contents/{INTERBOT_FILE}"

# Settings
LOCAL_CACHE = Path("data/interbot_state.json")
REFRESH_INTERVAL = 30  # seconds — check more frequently for reviews
CANDIDATE_TIMEOUT = 180  # 3 min — Настя reviews every 2 min, so 3 min gives buffer
MAX_PENDING_CANDIDATES = 20


class InterbotManager:
    """Manages inter-bot communication between Ася and Настя."""

    def __init__(self):
        self._gh_pat: str = ""
        self._own_state: Dict = {}
        self._other_state: Dict = {}
        self._last_own_sha: str = ""
        self._last_refresh: float = 0
        self._channel_manager = None  # Set later for publishing

    def configure(self, gh_pat: str = "", channel_manager=None):
        """Configure with GitHub PAT and channel manager."""
        self._gh_pat = gh_pat
        self._channel_manager = channel_manager
        logger.info(f"InterbotManager configured (PAT={'set' if gh_pat else 'missing'}, channel={'set' if channel_manager else 'missing'})")

    async def init(self):
        """Initialize: load own state, fetch other bot's state."""
        self._own_state = await self._fetch_state(ASYA_RAW_URL)
        if not self._own_state:
            self._own_state = self._empty_state("asya")
            await self._push_state()

        self._other_state = await self._fetch_state(NASTYA_RAW_URL)
        if not self._other_state:
            self._other_state = self._empty_state("nastya")

        logger.info(f"Interbot initialized: {len(self._own_state.get('pending_reviews', []))} candidates, "
                     f"Настя has {len(self._other_state.get('reviews', []))} reviews")

    def _empty_state(self, bot_name: str) -> Dict:
        return {
            "bot": bot_name,
            "version": "1.0",
            "last_updated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "pending_reviews": [],
            "reviews": [],
            "shared_chats": {},
            "messages": [],
            "read_receipts": [],
        }

    async def _fetch_state(self, url: str) -> Dict:
        """Fetch interbot state from URL."""
        try:
            async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
                response = await client.get(url, headers={"Cache-Control": "no-cache"})
                if response.status_code == 200:
                    return response.json()
        except Exception as e:
            logger.warning(f"Failed to fetch interbot state from {url[:60]}: {e}")
        return {}

    async def _push_state(self) -> bool:
        """Push own state to GitHub via Contents API."""
        if not self._gh_pat:
            logger.warning("No GitHub PAT — cannot push interbot state")
            return False

        self._own_state["last_updated"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                headers = {
                    "Authorization": f"token {self._gh_pat}",
                    "Accept": "application/vnd.github.v3+json",
                }

                # Get current SHA
                response = await client.get(ASYA_API_URL, headers=headers)
                if response.status_code == 200:
                    self._last_own_sha = response.json().get("sha", "")
                elif response.status_code == 404:
                    self._last_own_sha = ""

                # Push content
                content = json.dumps(self._own_state, ensure_ascii=False, indent=2)
                encoded = base64.b64encode(content.encode("utf-8")).decode("utf-8")

                data = {
                    "message": f"interbot: update state ({time.strftime('%Y-%m-%d %H:%M')})",
                    "content": encoded,
                    "committer": {"name": "asya-bot", "email": "bot@asya.local"},
                }
                if self._last_own_sha:
                    data["sha"] = self._last_own_sha

                response = await client.put(ASYA_API_URL, headers=headers, json=data)
                if response.status_code in (200, 201):
                    result = response.json()
                    self._last_own_sha = result.get("content", {}).get("sha", self._last_own_sha)
                    logger.info(f"Pushed interbot state to GitHub")
                    return True
                else:
                    logger.error(f"Failed to push interbot state: {response.status_code}")
                    return False
        except Exception as e:
            logger.error(f"Error pushing interbot state: {e}")
            return False

    async def maybe_refresh(self):
        """Refresh state from GitHub if enough time has passed."""
        now = time.time()
        if now - self._last_refresh < REFRESH_INTERVAL:
            return
        self._last_refresh = now

        other = await self._fetch_state(NASTYA_RAW_URL)
        if other:
            self._other_state = other

        own = await self._fetch_state(ASYA_RAW_URL)
        if own:
            self._own_state = own

    # ── AI-Filter: Submit candidates, check reviews ──

    async def submit_news_candidate(self, title: str, summary: str = "", category: str = "auto") -> str:
        """Submit a news candidate for Настя's review.

        Returns candidate ID.
        """
        candidate_id = f"c_{int(time.time())}_{uuid.uuid4().hex[:6]}"
        candidate = {
            "id": candidate_id,
            "from": "asya",
            "type": "news_candidate",
            "title": title,
            "summary": summary[:500],
            "category": category,
            "timestamp": time.time(),
            "status": "pending",
        }

        self._own_state.setdefault("pending_reviews", []).append(candidate)

        # Keep only last MAX_PENDING_CANDIDATES
        self._own_state["pending_reviews"] = self._own_state["pending_reviews"][-MAX_PENDING_CANDIDATES:]

        await self._push_state()
        logger.info(f"Submitted news candidate '{title[:40]}...' (id={candidate_id})")
        return candidate_id

    async def check_reviews(self) -> List[Dict]:
        """Check for reviews from Настя.

        Returns list of reviews for our candidates.
        """
        await self.maybe_refresh()

        reviews = self._other_state.get("reviews", [])
        our_candidate_ids = {c["id"] for c in self._own_state.get("pending_reviews", [])}

        # Find reviews for our candidates
        relevant = [r for r in reviews if r.get("candidate_id") in our_candidate_ids]

        if relevant:
            logger.info(f"Found {len(relevant)} reviews from Настя")

        return relevant

    async def get_reviewed_candidates(self) -> List[Dict]:
        """Get candidates that have been reviewed by Настя.

        Returns list of (candidate, review) tuples.
        """
        reviews = await self.check_reviews()

        candidates_by_id = {c["id"]: c for c in self._own_state.get("pending_reviews", [])}

        reviewed = []
        seen_ids = set()
        for review in reviews:
            cid = review.get("candidate_id", "")
            if cid in seen_ids:
                continue
            seen_ids.add(cid)
            if cid in candidates_by_id:
                reviewed.append({
                    "candidate": candidates_by_id[cid],
                    "review": review,
                })

        return reviewed

    async def mark_candidate_processed(self, candidate_id: str):
        """Mark a candidate as processed (published or skipped)."""
        for c in self._own_state.get("pending_reviews", []):
            if c.get("id") == candidate_id:
                c["status"] = "processed"
                break
        await self._push_state()

    def should_publish_without_review(self, candidate: Dict) -> bool:
        """Check if enough time has passed to publish without Настя's review.

        This is the fallback — if Настя is unavailable, Ася publishes independently
        after CANDIDATE_TIMEOUT seconds.
        """
        submitted_at = candidate.get("timestamp", 0)
        return (time.time() - submitted_at) > CANDIDATE_TIMEOUT

    # ── Inter-bot messaging ──

    async def send_message(self, text: str, to: str = "nastya") -> bool:
        """Send a message to the other bot."""
        msg = {
            "from": "asya",
            "to": to,
            "text": text,
            "timestamp": time.time(),
            "read": False,
        }
        self._own_state.setdefault("messages", []).append(msg)
        self._own_state["messages"] = self._own_state["messages"][-100:]
        return await self._push_state()

    async def check_messages(self) -> List[Dict]:
        """Check for unread messages from Настя."""
        await self.maybe_refresh()
        messages = self._other_state.get("messages", [])
        unread = [m for m in messages if m.get("to") == "asya" and not m.get("read", False)]
        return unread

    # ── Shared chat coordination ──

    def register_shared_chat(self, chat_id: int, chat_title: str = ""):
        """Register a chat where both bots are present."""
        chat_key = str(chat_id)
        if chat_key not in self._own_state.get("shared_chats", {}):
            self._own_state.setdefault("shared_chats", {})[chat_key] = {
                "title": chat_title,
                "topics": [],
                "last_discussion": 0,
                "nastya_active": True,
                "asya_active": True,
            }

    def is_shared_chat(self, chat_id: int) -> bool:
        """Check if a chat is a shared chat with Настя."""
        return str(chat_id) in self._own_state.get("shared_chats", {})

    # ── Status ──

    def get_status(self) -> Dict:
        pending = self._own_state.get("pending_reviews", [])
        return {
            "pending_candidates": len([p for p in pending if p.get("status") == "pending"]),
            "processed_candidates": len([p for p in pending if p.get("status") == "processed"]),
            "unread_messages": len([m for m in self._other_state.get("messages", []) if m.get("to") == "asya" and not m.get("read", False)]),
            "shared_chats": len(self._own_state.get("shared_chats", {})),
        }


# ── Global instance ──
interbot_manager = InterbotManager()


# ── Backward compatible convenience functions ──

async def send_to_nastya(message: str, msg_type: str = "info") -> bool:
    """Post a message to Настя."""
    return await interbot_manager.send_message(f"[{msg_type}] {message}", to="nastya")


async def check_messages() -> List[str]:
    """Check for messages from Настя."""
    msgs = await interbot_manager.check_messages()
    return [m.get("text", "") for m in msgs]
