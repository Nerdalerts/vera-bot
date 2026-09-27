"""In-memory state. The challenge contract guarantees no restarts during a
test run, so process memory is the fastest and simplest correct store.
IMPORTANT: run a single worker process (see README) — multiple uvicorn
workers would each hold a different copy of this state.
"""

from __future__ import annotations

import asyncio
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Optional

VALID_SCOPES = ("category", "merchant", "customer", "trigger")


class ContextStore:
    def __init__(self) -> None:
        self._data: dict[tuple[str, str], dict] = {}
        self._alias: dict[tuple[str, str], str] = {}  # (scope, alt_id) -> context_id

    def put(self, scope: str, context_id: str, version: int, payload: dict) -> tuple[str, Optional[int]]:
        """Returns ("stored"|"duplicate"|"stale", current_version)."""
        key = (scope, context_id)
        cur = self._data.get(key)
        if cur and cur["version"] == version:
            return "duplicate", version  # idempotent re-post: accepted, no side effects
        if cur and cur["version"] > version:
            return "stale", cur["version"]
        self._data[key] = {"version": version, "payload": payload, "stored_at": time.time()}
        # the payload's own id / slug may differ from context_id; index both so a
        # trigger that says merchant_id="m_42" finds context_id="merchant:m_42"
        for f in ("id", f"{scope}_id", "slug", "category_slug" if scope == "category" else None):
            if f and isinstance(payload.get(f), str) and payload[f] != context_id:
                self._alias[(scope, payload[f])] = context_id
        return "stored", version

    def get(self, scope: str, ident: Optional[str]) -> Optional[dict]:
        if not ident:
            return None
        e = self._data.get((scope, ident))
        if e is None and (scope, ident) in self._alias:
            e = self._data.get((scope, self._alias[(scope, ident)]))
        return e["payload"] if e else None

    def counts(self) -> dict[str, int]:
        c = {s: 0 for s in VALID_SCOPES}
        for s, _ in self._data:
            c[s] = c.get(s, 0) + 1
        return c

    def clear(self) -> None:
        self._data.clear()
        self._alias.clear()


@dataclass
class Conversation:
    conversation_id: str
    merchant_id: Optional[str] = None
    customer_id: Optional[str] = None
    trigger_id: Optional[str] = None
    turns: list[dict] = field(default_factory=list)       # {"from", "body", "ts"}
    inbound_counts: Counter = field(default_factory=Counter)
    hostile_count: int = 0
    auto_reply_count: int = 0
    last_bot_cta: Optional[str] = None
    ended: bool = False

    def bot_bodies(self) -> list[str]:
        return [t["body"] for t in self.turns if t["from"] == "bot"]

    def add(self, who: str, body: str) -> None:
        self.turns.append({"from": who, "body": body, "ts": time.time()})


class State:
    def __init__(self) -> None:
        self.contexts = ContextStore()
        self.conversations: dict[str, Conversation] = {}
        self.sent_suppression_keys: set[str] = set()
        self.opted_out: set[str] = set()                   # merchant_id (or merchant_id:customer_id)
        self.compose_cache: dict[str, dict] = {}
        self.inflight: dict[str, "asyncio.Task"] = {}
        self.merchant_inbound: Counter = Counter()          # (merchant_id, normalized msg) across conversations
        self.merchant_auto_replies: Counter = Counter()     # merchant_id -> auto-replies seen
        self.stats: Counter = Counter()

    def conv(self, conversation_id: str, **meta: Any) -> Conversation:
        c = self.conversations.get(conversation_id)
        if c is None:
            c = Conversation(conversation_id)
            self.conversations[conversation_id] = c
        for k, v in meta.items():
            if v and not getattr(c, k, None):
                setattr(c, k, v)
        return c

    def reset(self) -> None:
        for t in list(self.inflight.values()):
            t.cancel()
        self.__init__()


STATE = State()
