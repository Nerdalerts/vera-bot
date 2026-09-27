"""Vera — magicpin AI Challenge bot (HTTP surface).

    uvicorn bot:app --host 0.0.0.0 --port 8080 --workers 1

Endpoints (challenge-testing-brief §2): /v1/context, /v1/tick, /v1/reply,
/v1/healthz, /v1/metadata, plus optional /v1/teardown.

Also exposes the brief's plain-Python contract, compose() (§7.1), and
respond() (§7.4), both backed by the same pipeline as the HTTP endpoints.

Design in one line: deterministic code decides WHETHER and HOW to act
(routing, suppression, auto-reply/intent/hostility detection, timing); the
LLM only writes words, and a deterministic validator decides whether those
words may leave the process.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import datetime, timezone
from typing import Any, Literal, Optional

from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from vera import config, llm
from vera.classifier import classify
from vera.composer import compose as _compose, precompose
from vera.store import STATE, VALID_SCOPES
from vera.validator import DEVANAGARI_RE, HINGLISH_MARKERS, normalize

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("vera.bot")

APP_START = time.time()
app = FastAPI(title="Vera — magicpin merchant assistant", version=config.BOT_VERSION)
_background: set[asyncio.Task] = set()


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _spawn(coro) -> None:
    t = asyncio.create_task(coro)
    _background.add(t)
    t.add_done_callback(_background.discard)


# ---------------------------------------------------------------------------
# Never emit a non-JSON error: a malformed response costs -2 per call.
# ---------------------------------------------------------------------------

@app.exception_handler(RequestValidationError)
async def _bad_request(request: Request, exc: RequestValidationError):
    path = request.url.path
    if path.endswith("/tick"):
        return JSONResponse(status_code=200, content={"actions": []})
    if path.endswith("/reply"):
        return JSONResponse(status_code=200, content={"action": "wait", "wait_seconds": 300,
                                                      "rationale": "Could not parse the reply payload; backing off."})
    return JSONResponse(status_code=400, content={"accepted": False, "reason": "malformed_request",
                                                  "details": str(exc.errors())[:500]})


@app.exception_handler(Exception)
async def _unhandled(request: Request, exc: Exception):
    log.exception("unhandled error on %s", request.url.path)
    path = request.url.path
    if path.endswith("/tick"):
        return JSONResponse(status_code=200, content={"actions": []})
    if path.endswith("/reply"):
        return JSONResponse(status_code=200, content={"action": "wait", "wait_seconds": 600,
                                                      "rationale": "Internal error while composing; backing off instead of sending something unvalidated."})
    return JSONResponse(status_code=500, content={"accepted": False, "reason": "internal_error"})


# ---------------------------------------------------------------------------
# /v1/context
# ---------------------------------------------------------------------------

class CtxBody(BaseModel):
    scope: str
    context_id: str
    version: int
    payload: dict[str, Any]
    delivered_at: Optional[str] = None


@app.post("/v1/context")
async def push_context(body: CtxBody, response: Response):
    if body.scope not in VALID_SCOPES:
        response.status_code = 400
        return {"accepted": False, "reason": "invalid_scope", "details": f"scope must be one of {list(VALID_SCOPES)}"}
    outcome, current = STATE.contexts.put(body.scope, body.context_id, body.version, body.payload)
    if outcome == "stale":
        response.status_code = 409
        return {"accepted": False, "reason": "stale_version", "current_version": current}
    if outcome == "duplicate":  # brief §2.1: re-posting the same version is a no-op
        return {"accepted": True, "ack_id": f"ack_{body.scope}_{body.context_id}_v{body.version}",
                "stored_at": _utcnow(), "duplicate": True}

    if body.scope == "trigger" and llm.is_configured():
        # warm the composition now, so the tick that lists this trigger is instant
        trig = body.payload
        merchant = STATE.contexts.get("merchant", trig.get("merchant_id"))
        if merchant:
            category = STATE.contexts.get("category", merchant.get("category_slug"))
            customer = STATE.contexts.get("customer", trig.get("customer_id"))
            if not trig.get("customer_id") or customer:
                _spawn(precompose(category, merchant, trig, customer, body.delivered_at or _utcnow()))

    return {"accepted": True, "ack_id": f"ack_{body.scope}_{body.context_id}_v{body.version}", "stored_at": _utcnow()}


# ---------------------------------------------------------------------------
# /v1/tick
# ---------------------------------------------------------------------------

class TickBody(BaseModel):
    now: Optional[str] = None
    available_triggers: list[str] = Field(default_factory=list)


def _conv_id(trigger: dict, trg_id: str) -> str:
    who = trigger.get("customer_id") or trigger.get("merchant_id") or "x"
    return f"conv_{who}_{trg_id}"


def _select_candidates(trigger_ids: list[str]) -> tuple[list[dict], list[dict]]:
    """Cheap, deterministic filtering before any LLM work. Returns (chosen, skipped)."""
    chosen: dict[tuple[str, str], dict] = {}
    skipped: list[dict] = []
    for order, trg_id in enumerate(dict.fromkeys(trigger_ids)):
        trigger = STATE.contexts.get("trigger", trg_id)
        if not trigger:
            skipped.append({"trigger_id": trg_id, "why": "unknown_trigger"}); continue
        mid = trigger.get("merchant_id") or (trigger.get("payload") or {}).get("merchant_id")
        merchant = STATE.contexts.get("merchant", mid)
        if not merchant:
            skipped.append({"trigger_id": trg_id, "why": "no_merchant_context"}); continue
        cid = trigger.get("customer_id")
        customer = STATE.contexts.get("customer", cid) if cid else None
        if cid and not customer:
            skipped.append({"trigger_id": trg_id, "why": "no_customer_context"}); continue
        skey = trigger.get("suppression_key") or trg_id
        if skey in STATE.sent_suppression_keys:
            skipped.append({"trigger_id": trg_id, "why": "suppressed_already_sent"}); continue
        if mid in STATE.opted_out and not cid:
            skipped.append({"trigger_id": trg_id, "why": "merchant_opted_out"}); continue
        if cid and (f"{mid}:{cid}" in STATE.opted_out or (customer.get("preferences") or {}).get("reminder_opt_in") is False):
            skipped.append({"trigger_id": trg_id, "why": "customer_not_opted_in"}); continue
        conv_id = _conv_id(trigger, trg_id)
        if conv_id in STATE.conversations:
            skipped.append({"trigger_id": trg_id, "why": "conversation_already_open"}); continue
        cand = {"order": order, "trg_id": trg_id, "trigger": trigger, "merchant": merchant, "merchant_id": mid,
                "customer": customer, "customer_id": cid, "conv_id": conv_id,
                "category": STATE.contexts.get("category", merchant.get("category_slug"))}
        # cadence: one proactive message per recipient per tick — the most urgent one.
        # The others stay un-suppressed and are reconsidered next tick.
        recipient = (mid, cid or "-")
        cur = chosen.get(recipient)
        rank = (-(trigger.get("urgency") or 0), order)
        if cur is None or rank < (-(cur["trigger"].get("urgency") or 0), cur["order"]):
            if cur:
                skipped.append({"trigger_id": cur["trg_id"], "why": "deferred_same_recipient"})
            chosen[recipient] = cand
        else:
            skipped.append({"trigger_id": trg_id, "why": "deferred_same_recipient"})
    ranked = sorted(chosen.values(), key=lambda c: (-(c["trigger"].get("urgency") or 0), c["order"]))
    return ranked[: config.MAX_ACTIONS_PER_TICK], skipped


@app.post("/v1/tick")
async def tick(body: TickBody):
    deadline = time.monotonic() + config.TICK_BUDGET_SECONDS
    now = body.now or _utcnow()
    candidates, _skipped = _select_candidates(body.available_triggers)
    if not candidates or not llm.is_configured():
        return {"actions": []}

    async def one(c: dict):
        r = await _compose(c["category"], c["merchant"], c["trigger"], c["customer"], now=now, deadline=deadline)
        return c, r

    results = await asyncio.gather(*(one(c) for c in candidates), return_exceptions=True)
    actions = []
    for item in results:
        if isinstance(item, Exception):
            log.warning("compose task failed: %s", item); continue
        c, r = item
        if not r.message:
            continue  # restraint: nothing validated for this trigger this tick
        m = r.message
        if time.monotonic() > deadline + 1:  # belt and braces; compose already honours the deadline
            break
        STATE.sent_suppression_keys.add(m["suppression_key"])
        conv = STATE.conv(c["conv_id"], merchant_id=c["merchant_id"], customer_id=c["customer_id"], trigger_id=c["trg_id"])
        conv.add("bot", m["body"])
        conv.last_bot_cta = m["cta"]
        actions.append({
            "conversation_id": c["conv_id"],
            "merchant_id": c["merchant_id"],
            "customer_id": c["customer_id"],
            "send_as": m["send_as"],
            "trigger_id": c["trg_id"],
            "template_name": f"{'merchant' if m['send_as'] == 'merchant_on_behalf' else 'vera'}_{c['trigger'].get('kind', 'update')}_v1",
            "template_params": m["template_params"],
            "body": m["body"],
            "cta": m["cta"],
            "suppression_key": m["suppression_key"],
            "rationale": m["rationale"] or f"{c['trigger'].get('kind')} for {c['merchant_id']}",
        })
    STATE.stats["actions_sent"] += len(actions)
    return {"actions": actions}


# ---------------------------------------------------------------------------
# /v1/reply
# ---------------------------------------------------------------------------

class ReplyBody(BaseModel):
    conversation_id: str
    merchant_id: Optional[str] = None
    customer_id: Optional[str] = None
    from_role: str = "merchant"
    message: str = ""
    received_at: Optional[str] = None
    turn_number: int = 0


def _message_language(msg: str) -> str:
    return "hi" if DEVANAGARI_RE.search(msg) or len(HINGLISH_MARKERS.findall(msg)) >= 2 else "en"


def _wait(seconds: int, why: str) -> dict:
    return {"action": "wait", "wait_seconds": seconds, "rationale": why}


def _end(why: str) -> dict:
    return {"action": "end", "rationale": why}


def _action_fallback(lang: str) -> str:
    """Only used if the LLM is down at the exact moment a merchant says 'go'.
    Contains no facts, so it can't be wrong; keeps momentum instead of
    going silent, which is the costliest failure at an intent hand-off."""
    if lang == "hi":
        return "Done, main abhi shuru kar rahi hoon. Pehla draft next 10 minute mein yahin bhej dungi — aap bas final OK kar dena."
    return "Done, starting on it now. I'll send the first draft right here in the next 10 minutes; you just give the final OK."


async def handle_reply(body: ReplyBody) -> dict:
    deadline = time.monotonic() + config.REPLY_BUDGET_SECONDS
    conv = STATE.conv(body.conversation_id, merchant_id=body.merchant_id, customer_id=body.customer_id)
    msg = (body.message or "").strip()
    conv.add(body.from_role or "merchant", msg)
    mid = conv.merchant_id or body.merchant_id or ""
    cid = conv.customer_id or body.customer_id

    # repeat counting is per conversation AND per merchant: a WA Business
    # auto-responder fires the same text into every thread we open
    norm = normalize(msg)
    conv.inbound_counts[norm] += 1
    STATE.merchant_inbound[(mid, norm)] += 1
    repeats = max(conv.inbound_counts[norm], STATE.merchant_inbound[(mid, norm)])
    read = classify(msg, repeat_count=repeats, last_bot_cta=conv.last_bot_cta)
    STATE.stats[f"reply_{read.label}"] += 1
    lang = _message_language(msg)

    if conv.ended and read.label in ("auto_reply", "opt_out", "hostile", "decline", "empty"):
        return _end(f"Conversation already closed; inbound read as {read.label}, so staying closed.")

    if read.label == "empty":
        return _wait(900, "Empty inbound message; nothing to respond to yet.")

    if read.label == "opt_out":
        conv.ended = True
        STATE.opted_out.add(f"{mid}:{cid}" if cid else mid)
        return _end("Explicit opt-out ('stop'/unsubscribe). Exiting immediately and suppressing all future proactive sends to this recipient.")

    if read.label == "auto_reply":
        conv.auto_reply_count += 1
        STATE.merchant_auto_replies[mid] += 1
        if conv.auto_reply_count >= 2 or STATE.merchant_auto_replies[mid] >= 2 or repeats >= 2:
            conv.ended = True
            return _end(f"WhatsApp Business auto-reply detected again ({'; '.join(read.notes)}). "
                        "Exiting now instead of burning more turns on a bot; will re-engage on the next real trigger.")
        return _wait(14400, f"Auto-reply detected ({'; '.join(read.notes)}): owner is not on the phone. "
                            "Not replying to a machine; backing off 4h so the next message lands when a human is likely to read it.")

    if read.label == "decline":
        conv.ended = True
        return _end("Merchant declined ('not interested'). Closing politely without a counter-pitch; this thread is not re-opened proactively.")

    if read.label == "later":
        secs = 86400 if re.search(r"\b(tomorrow|kal)\b", msg, re.I) else 10800
        return _wait(secs, f"Merchant asked for time; backing off {secs // 3600}h and resuming the same thread then.")

    if read.label == "hostile":
        conv.hostile_count += 1
        if conv.hostile_count >= 2:
            conv.ended = True
            STATE.opted_out.add(f"{mid}:{cid}" if cid else mid)
            return _end("Second hostile message in this thread. Exiting and suppressing further outreach; persisting would only damage the relationship.")

    mode = {"intent_transition": "action", "question": "answer", "hostile": "deescalate",
            "off_topic": "deescalate"}.get(read.label, "continue")
    conv.ended = False  # the merchant re-engaged on their own

    merchant = STATE.contexts.get("merchant", mid) or {"merchant_id": mid}
    category = STATE.contexts.get("category", merchant.get("category_slug"))
    customer = STATE.contexts.get("customer", cid) if cid else None
    trigger = (STATE.contexts.get("trigger", conv.trigger_id) if conv.trigger_id else None) or {
        "id": f"reply:{body.conversation_id}",
        "scope": "customer" if cid else "merchant",
        "kind": "merchant_reply",
        "source": "internal",
        "merchant_id": mid,
        "customer_id": cid,
        "payload": {"merchant_message": msg},
        "suppression_key": f"reply:{body.conversation_id}:{body.turn_number}",
    }
    conversation = {
        "transcript": [{"from": t["from"], "body": t["body"]} for t in conv.turns[-10:]],
        "latest_message": msg,
        "latest_message_read": {"label": read.label, "notes": read.notes},
        "latest_message_language": lang,
        "turn_number": body.turn_number,
    }
    r = await _compose(category, merchant, trigger, customer, now=body.received_at or _utcnow(),
                       deadline=deadline, mode=mode, conversation=conversation, prior_bodies=conv.bot_bodies())

    if r.message:
        m = r.message
        conv.add("bot", m["body"])
        conv.last_bot_cta = m["cta"]
        return {"action": "send", "body": m["body"], "cta": m["cta"],
                "rationale": f"[{read.label} -> {mode}] " + (m["rationale"] or "")}

    if mode == "action":
        text = _action_fallback(lang)
        if normalize(text) not in {normalize(b) for b in conv.bot_bodies()}:
            conv.add("bot", text)
            conv.last_bot_cta = "none"
            return {"action": "send", "body": text, "cta": "none",
                    "rationale": "Merchant committed; composition unavailable this turn, so confirming action immediately (no facts asserted) rather than going silent."}
    if mode == "deescalate":
        conv.ended = True
        return _end("Merchant upset/off-mission and no validated de-escalation copy this turn; exiting respectfully rather than risk a bad send.")
    return _wait(1800, f"No validated reply this turn ({r.skipped_reason}); backing off 30 min instead of sending filler.")


@app.post("/v1/reply")
async def reply(body: ReplyBody):
    try:
        return await asyncio.wait_for(handle_reply(body), timeout=config.REPLY_BUDGET_SECONDS + 1)
    except asyncio.TimeoutError:
        return _wait(300, "Composition exceeded the time budget; retrying shortly rather than risking a late or bad send.")


# ---------------------------------------------------------------------------
# /v1/healthz, /v1/metadata, /v1/teardown
# ---------------------------------------------------------------------------

@app.get("/v1/healthz")
async def healthz():
    return {"status": "ok", "uptime_seconds": int(time.time() - APP_START),
            "contexts_loaded": STATE.contexts.counts()}


@app.get("/v1/metadata")
async def metadata():
    return {
        "team_name": config.TEAM_NAME,
        "team_members": config.TEAM_MEMBERS,
        "model": config.LLM_MODEL,
        "approach": ("Deterministic router (auto-reply / intent / hostility / opt-out detection, suppression, cadence) "
                     "+ evidence brief built in code (digest lookup, date math, peer deltas, language, salutation) "
                     "+ LLM composer at temperature 0 with per-trigger-kind playbooks "
                     "+ deterministic validator (number/entity/citation grounding, generic-copy, taboo, CTA shape, repeat) "
                     "with informed repair; background pre-composition on context push."),
        "contact_email": config.CONTACT_EMAIL,
        "version": config.BOT_VERSION,
        "submitted_at": _utcnow(),
    }


@app.post("/v1/teardown")
async def teardown():
    STATE.reset()
    return {"status": "wiped"}


@app.get("/")
async def root():
    return {"service": "vera", "docs": "/docs", "health": "/v1/healthz"}


# ---------------------------------------------------------------------------
# Plain-Python contract from challenge-brief §7.1 / §7.4
# ---------------------------------------------------------------------------

def compose(category: dict, merchant: dict, trigger: dict, customer: Optional[dict] = None,
            now: Optional[str] = None) -> dict:
    """Synchronous compose() as specified in the brief. Returns
    {body, cta, send_as, suppression_key, rationale}; body is "" only if no
    grounded message could be validated."""
    async def run():
        r = await _compose(category, merchant, trigger, customer, now=now or _utcnow(),
                           deadline=time.monotonic() + 28, allow_skip=False)
        return r
    r = asyncio.run(run())
    m = r.message or {}
    return {"body": m.get("body", ""), "cta": m.get("cta", "none"),
            "send_as": m.get("send_as", "merchant_on_behalf" if customer else "vera"),
            "suppression_key": m.get("suppression_key", trigger.get("suppression_key", "")),
            "rationale": m.get("rationale", f"no validated message: {r.skipped_reason}")}
