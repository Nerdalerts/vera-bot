"""compose(): contexts -> brief -> LLM -> validator -> (informed repair) -> message.

Contract: returns a validated message dict or None. Never raises, never
returns unvalidated model text, never exceeds the caller's deadline.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from . import config, llm
from .brief import build_brief, language_directive, send_as_for, taboo_list
from .facts import GroundingIndex, as_list
from .store import STATE
from .validator import validate

log = logging.getLogger("vera.composer")

SYSTEM_PROMPT = config.SYSTEM_PROMPT_PATH.read_text(encoding="utf-8")
PROMPT_HASH = hashlib.sha256(SYSTEM_PROMPT.encode()).hexdigest()[:10]


@dataclass
class ComposeResult:
    message: Optional[dict] = None
    skipped_reason: Optional[str] = None
    attempts: int = 0
    violations: list[list[str]] = field(default_factory=list)


def _cache_key(payload: dict) -> str:
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256((PROMPT_HASH + config.LLM_MODEL + raw).encode()).hexdigest()


def _normalise(raw: dict, trigger: dict, customer: Optional[dict], brief: dict) -> dict:
    cta = str(raw.get("cta", "none")).strip().lower().replace("-", "_")
    if cta not in ("binary", "open_ended", "none"):
        cta = "open_ended" if "open" in cta else "binary" if cta in ("yes_no", "yes/stop", "yes_stop") else cta
    params = raw.get("template_params")
    if not (isinstance(params, list) and all(isinstance(p, (str, int, float)) for p in params) and params):
        params = [brief.get("address_as", ""), brief.get("merchant_name", "")]
    return {
        "body": str(raw.get("body", "")).strip(),
        "cta": cta,
        # routing facts are decided by code, not by the model
        "send_as": send_as_for(trigger, customer),
        "suppression_key": trigger.get("suppression_key") or f"{trigger.get('kind', 'msg')}:{trigger.get('id', '')}",
        "template_params": [str(p) for p in params][:6],
        "facts_used": as_list(raw.get("facts_used"))[:12],
        "rationale": str(raw.get("rationale", "")).strip()[:600],
    }


_llm_sem: dict[int, asyncio.Semaphore] = {}


def _sem() -> asyncio.Semaphore:
    """One semaphore per event loop (the sync compose() wrapper may run several loops)."""
    loop_id = id(asyncio.get_running_loop())
    if loop_id not in _llm_sem:
        _llm_sem.clear()
        _llm_sem[loop_id] = asyncio.Semaphore(config.LLM_CONCURRENCY)
    return _llm_sem[loop_id]


async def compose(category: Optional[dict], merchant: dict, trigger: dict, customer: Optional[dict], *,
                  now: Optional[str], deadline: float, mode: str = "first_touch",
                  conversation: Optional[dict] = None, prior_bodies: Optional[list[str]] = None,
                  allow_skip: bool = True, max_attempts: int = config.MAX_COMPOSE_ATTEMPTS) -> ComposeResult:
    """Cache -> in-flight dedupe -> fresh composition.

    The fresh composition runs as its own task with its own budget. The caller
    waits only until ITS deadline; if the work outlives the caller (slow LLM),
    it keeps running and lands in the cache, so the next tick is instant
    instead of paying the same latency again."""
    prior_bodies = prior_bodies or []
    try:
        brief = build_brief(category, merchant, trigger, customer, now)
    except Exception as e:  # malformed context must not crash a tick
        log.exception("brief build failed")
        return ComposeResult(skipped_reason=f"brief_error:{e}")

    payload: dict[str, Any] = {
        "mode": mode,
        "brief": brief,
        "contexts": {"category": category, "merchant": merchant, "trigger": trigger, "customer": customer},
        "conversation": conversation,
        "must_not_repeat": prior_bodies[-5:],
    }
    key = _cache_key(payload)
    cached = STATE.compose_cache.get(key)
    if cached:  # determinism: identical inputs -> identical output
        STATE.stats["cache_hits"] += 1
        return ComposeResult(message=dict(cached))

    task = STATE.inflight.get(key)
    if task is None:
        task = asyncio.create_task(_compose_fresh(
            key, payload, brief, category, merchant, trigger, customer, conversation, prior_bodies,
            mode, allow_skip, max(max_attempts, config.MAX_COMPOSE_ATTEMPTS)))
        STATE.inflight[key] = task
        task.add_done_callback(lambda _t, k=key: STATE.inflight.pop(k, None))
    else:
        STATE.stats["inflight_joins"] += 1

    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return ComposeResult(skipped_reason="out_of_time")
    try:
        res = await asyncio.wait_for(asyncio.shield(task), timeout=remaining)
    except asyncio.TimeoutError:
        STATE.stats["compose_deferred"] += 1
        return ComposeResult(skipped_reason="still_composing")
    except Exception as e:  # pragma: no cover - _compose_fresh never raises
        return ComposeResult(skipped_reason=f"compose_error:{e}")
    if res.message:
        res = ComposeResult(message=dict(res.message), attempts=res.attempts, violations=res.violations)
    return res


async def precompose(category: Optional[dict], merchant: dict, trigger: dict, customer: Optional[dict],
                     now: Optional[str]) -> None:
    """Fire-and-forget warm-up when a trigger context is pushed, so the tick
    that follows is served from cache inside a few milliseconds."""
    try:
        await compose(category, merchant, trigger, customer, now=now,
                      deadline=time.monotonic() + config.BACKGROUND_BUDGET_SECONDS)
    except Exception:  # pragma: no cover
        log.exception("precompose failed")


async def _compose_fresh(key: str, payload: dict, brief: dict, category: Optional[dict], merchant: dict,
                         trigger: dict, customer: Optional[dict], conversation: Optional[dict],
                         prior_bodies: list[str], mode: str, allow_skip: bool, max_attempts: int) -> ComposeResult:
    res = ComposeResult()
    deadline = time.monotonic() + config.BACKGROUND_BUDGET_SECONDS
    idx = GroundingIndex(category, merchant, trigger, customer, extra_text=json.dumps(brief, ensure_ascii=False, default=str))
    _, prefers_hindi = language_directive(merchant, customer)
    if conversation and conversation.get("latest_message_language") == "en":
        prefers_hindi = False  # mirror the merchant's own switch to English
    has_prior = bool(prior_bodies) or any(
        isinstance(h, dict) and h.get("from") == "vera" for h in as_list(merchant.get("conversation_history")))

    best_soft: Optional[dict] = None
    repair: Optional[dict] = None
    for _attempt in range(max_attempts):
        remaining = deadline - time.monotonic()
        if remaining < config.MIN_ATTEMPT_SECONDS:
            res.skipped_reason = res.skipped_reason or "out_of_time"
            break
        if best_soft is not None and not repair:
            break
        res.attempts += 1
        call_payload = dict(payload, repair=repair) if repair else payload
        timeout = min(config.LLM_CALL_TIMEOUT, remaining - 0.5)
        try:
            async with _sem():
                text = await asyncio.wait_for(
                    llm.complete(SYSTEM_PROMPT, json.dumps(call_payload, ensure_ascii=False, default=str), timeout),
                    timeout=timeout,
                )
            raw = llm.parse_json_object(text)
        except asyncio.TimeoutError:
            STATE.stats["llm_timeouts"] += 1
            res.skipped_reason = "llm_timeout"
            continue
        except Exception as e:  # provider error / unparsable output
            STATE.stats["llm_errors"] += 1
            res.skipped_reason = f"llm_error:{type(e).__name__}"
            log.warning("llm call failed: %s", e)
            if isinstance(e, llm.LLMError) and "not set" in str(e):
                break  # misconfiguration: retrying cannot help
            repair = {"previous_output": None, "violations": ["output was not a single valid JSON object"]}
            continue

        if raw.get("skip"):
            if allow_skip:
                res.skipped_reason = f"model_skip:{raw.get('reason', '')}"
                return res
            repair = {"previous_output": raw, "violations": [
                "skip is not allowed for this request: write the best message the input honestly supports, "
                "anchored on the strongest real number in the brief"]}
            continue

        msg = _normalise(raw, trigger, customer, brief)
        errors, warnings = validate(
            msg["body"], msg["cta"], idx, prior_bodies=prior_bodies, taboo=taboo_list(category),
            prefers_hindi=prefers_hindi, has_prior_bot_turns=has_prior, facts_used=msg["facts_used"], mode=mode,
        )
        res.violations.append(errors + warnings)
        if not errors and not warnings:
            best_soft = msg
            break
        if not errors:
            best_soft = msg  # already sendable; one polish attempt if time allows
        repair = {"previous_output": {"body": msg["body"], "cta": msg["cta"]}, "violations": errors + warnings}
        if best_soft is not None and deadline - time.monotonic() < config.LLM_CALL_TIMEOUT:
            break  # don't gamble a sendable message on a polish we can't finish

    if best_soft:
        STATE.compose_cache[key] = best_soft
        res.message = dict(best_soft)
        res.skipped_reason = None
        return res
    STATE.stats["compose_rejected"] += 1
    res.skipped_reason = res.skipped_reason or "failed_validation"
    log.info("compose rejected (%s): %s", trigger.get("id"), res.violations[-1:] )
    return res
