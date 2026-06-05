"""
Inter-Bot Communication — Ася ↔ Настя collaboration interface.

This module provides a simple mechanism for bots to communicate with each other
through a shared JSON file in the PR (private repository). This allows Ася and
Настя (nastya-bot) to coordinate their work, avoid posting duplicate content,
and share relevant information.

STUB: This is the interface definition for future inter-bot collaboration.
The actual implementation will be completed when the shared repository
infrastructure is set up between the two bots.

Future capabilities:
- Coordinate channel posting schedules (avoid both bots posting at same time)
- Share news items that the other bot might find useful
- Alert each other about trending topics
- De-duplicate content across both bots' channels
"""

import json
import logging
import os
from typing import List, Optional
from datetime import datetime
from zoneinfo import ZoneInfo

logger = logging.getLogger("asya.interbot")

# Moscow timezone
_MOSCOW_TZ = ZoneInfo("Europe/Moscow")

# Path to shared messages file in the PR repo
# This will be set via environment variable when the infrastructure is ready
_INTERBOT_MESSAGES_FILE = os.getenv(
    "INTERBOT_MESSAGES_FILE",
    "data/interbot_messages.json"
)


async def send_to_nastya(message: str, msg_type: str = "info") -> bool:
    """Post a message to the shared inter-bot channel for Настя to read.

    Args:
        message: The message content to send.
        msg_type: Type of message — "info", "alert", "news_tip", "schedule".

    Returns:
        True if the message was successfully posted, False otherwise.

    STUB: Currently just logs the message. Actual implementation will write
    to a shared JSON file that nastya-bot reads periodically.
    """
    now = datetime.now(_MOSCOW_TZ)

    msg_data = {
        "from": "asya",
        "to": "nastya",
        "type": msg_type,
        "message": message,
        "timestamp": now.isoformat(),
    }

    logger.info(f"[INTERBOT→Nastya] ({msg_type}): {message[:80]}...")

    # STUB: Write to shared file
    # Future implementation:
    # try:
    #     messages = []
    #     if os.path.exists(_INTERBOT_MESSAGES_FILE):
    #         with open(_INTERBOT_MESSAGES_FILE, "r", encoding="utf-8") as f:
    #             messages = json.load(f)
    #     messages.append(msg_data)
    #     # Keep only last 100 messages
    #     messages = messages[-100:]
    #     with open(_INTERBOT_MESSAGES_FILE, "w", encoding="utf-8") as f:
    #         json.dump(messages, f, ensure_ascii=False, indent=2)
    #     return True
    # except Exception as e:
    #     logger.error(f"Failed to send interbot message: {e}")
    #     return False

    return True


async def check_messages() -> List[str]:
    """Check for messages from Настя in the shared inter-bot channel.

    Returns:
        List of message strings from Настя. Empty list if no messages.

    STUB: Currently returns empty list. Actual implementation will read
    from the shared JSON file and return pending messages.
    """
    logger.debug("[INTERBOT] Checking for messages from Nastya...")

    # STUB: Read from shared file
    # Future implementation:
    # try:
    #     if not os.path.exists(_INTERBOT_MESSAGES_FILE):
    #         return []
    #     with open(_INTERBOT_MESSAGES_FILE, "r", encoding="utf-8") as f:
    #         messages = json.load(f)
    #     # Filter messages addressed to Asya
    #     nastya_msgs = [
    #         m["message"] for m in messages
    #         if m.get("to") == "asya" and m.get("from") == "nastya"
    #     ]
    #     return nastya_msgs
    # except Exception as e:
    #     logger.error(f"Failed to check interbot messages: {e}")
    #     return []

    return []
