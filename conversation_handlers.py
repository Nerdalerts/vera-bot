"""Optional multi-turn contract from challenge-brief §7.4.

    respond(state, merchant_message) -> dict

A thin wrapper over the same handler /v1/reply uses, so the Python API and
the HTTP API can never disagree.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from bot import ReplyBody, handle_reply


@dataclass
class ConversationState:
    conversation_id: str
    merchant_id: str
    customer_id: Optional[str] = None
    turn_number: int = 1
    from_role: str = "merchant"


def respond(state: ConversationState, merchant_message: str) -> dict:
    """Returns {"action": "send"|"wait"|"end", ...} exactly as /v1/reply does.
    Contexts must already be loaded (via /v1/context or vera.store.STATE)."""
    state.turn_number += 1
    body = ReplyBody(conversation_id=state.conversation_id, merchant_id=state.merchant_id,
                     customer_id=state.customer_id, from_role=state.from_role, message=merchant_message,
                     received_at=datetime.now(timezone.utc).isoformat(), turn_number=state.turn_number)
    return asyncio.run(handle_reply(body))
