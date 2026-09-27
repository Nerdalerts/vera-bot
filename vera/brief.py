"""The evidence brief: deterministic pre-digestion of the 4 contexts.

The raw contexts are big and uneven: a trigger may point at a digest item
by id (`top_item_id`), may carry rich payload (slots, batch numbers), or may
be a bare placeholder. A model handed raw JSON has to do retrieval,
arithmetic, salutation rules and language resolution *while* writing copy,
and that is where it slips (wrong item, miscomputed %, "Hi Bharat Dental
Care", English to a Hinglish merchant).

So before the LLM is called, this module resolves all of it in code and
hands the model a short, authoritative brief:
  - who we are talking to, how to address them, in which language
  - the resolved "why now" (digest item looked up, dates turned into
    day-counts against the judge's clock, fractions turned into %)
  - the merchant's strongest verifiable numbers and offers
  - an honest list of what is NOT known for thin/placeholder triggers
  - a per-trigger-kind playbook (goal, levers, CTA shape)

The brief contains only values copied or computed from the contexts, so it
is itself part of the grounding index.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Optional

from .facts import as_list, dig

# ---------------------------------------------------------------------------
# Trigger-kind playbooks — the "routing layer" the brief suggests (§13), kept
# as data so a PM can tune it without touching code.
# ---------------------------------------------------------------------------

PLAYBOOK: dict[str, dict[str, str]] = {
    "research_digest": {
        "goal": "Share the one digest finding that matters to THIS practice and offer to do the follow-up work.",
        "levers": "specificity (trial size, effect, source+page) · reciprocity · effort externalization",
        "cta": "open_ended or binary offer to pull the abstract / draft a patient message",
    },
    "regulation_change": {
        "goal": "Flag the rule change, the exact deadline and what it means for their setup; offer a checklist.",
        "levers": "loss aversion (deadline) · specificity (old vs new limit) · effort externalization",
        "cta": "binary: want the audit checklist?",
    },
    "cde_opportunity": {
        "goal": "Surface the CDE event with date, credits and fee; make registering effortless.",
        "levers": "specificity (date/time, credits) · curiosity (topic) · single binary",
        "cta": "binary: want me to share the registration details?",
    },
    "competitor_opened": {
        "goal": "Tell them a named competitor opened nearby and how their offer compares; propose a counter-move built on the merchant's own strengths.",
        "levers": "loss aversion · specificity (distance, their price) · effort externalization",
        "cta": "binary: want me to draft the counter-offer post?",
    },
    "perf_dip": {
        "goal": "Name the exact metric drop and window, one likely lever from context, and a concrete fix Vera can start.",
        "levers": "loss aversion · specificity · effort externalization",
        "cta": "binary",
    },
    "seasonal_perf_dip": {
        "goal": "Reassure: the dip is seasonal (say so, with the season note); suggest using the quiet window productively.",
        "levers": "reciprocity (reassurance) · asking the merchant · effort externalization",
        "cta": "open_ended or binary",
    },
    "perf_spike": {
        "goal": "Celebrate the specific spike and its likely driver, then compound it with one next step.",
        "levers": "specificity · curiosity · effort externalization",
        "cta": "binary: want me to repeat what worked?",
    },
    "milestone_reached": {
        "goal": "Mark the milestone (or how close it is) and give a low-effort push to cross it.",
        "levers": "specificity · social proof (vs peer average if given) · effort externalization",
        "cta": "binary",
    },
    "review_theme_emerged": {
        "goal": "Quote the emerging review theme with its count, and offer a concrete fix + a reply draft.",
        "levers": "specificity (count, quote) · loss aversion · effort externalization",
        "cta": "binary: want me to draft the review replies?",
    },
    "renewal_due": {
        "goal": "State days left and plan/amount plainly; tie renewal to one number they would lose.",
        "levers": "loss aversion · specificity · single binary",
        "cta": "binary: Reply YES to renew",
    },
    "winback_eligible": {
        "goal": "Show what changed since the subscription lapsed (their own numbers) without guilt-tripping; offer an easy restart.",
        "levers": "loss aversion · reciprocity · effort externalization",
        "cta": "binary",
    },
    "dormant_with_vera": {
        "goal": "Re-open the thread with something genuinely new and useful for them, not 'just checking in'.",
        "levers": "curiosity · reciprocity · asking the merchant",
        "cta": "open_ended, one easy question",
    },
    "curious_ask_due": {
        "goal": "Ask the merchant one sharp, easy question about their business this week, and say what you'll do with the answer.",
        "levers": "asking the merchant · reciprocity · effort externalization",
        "cta": "open_ended",
    },
    "festival_upcoming": {
        "goal": "Name the festival, the exact days left, and one festival-specific service@price idea from the catalog.",
        "levers": "loss aversion (window) · specificity · effort externalization",
        "cta": "binary: want me to draft the festival post/offer?",
    },
    "category_seasonal": {
        "goal": "Translate the seasonal demand shifts into one shelf/stock action for this store.",
        "levers": "specificity (the % shifts) · effort externalization",
        "cta": "binary",
    },
    "ipl_match_today": {
        "goal": "Tie tonight's match (teams, time) to one concrete order/footfall move using their own offer.",
        "levers": "timeliness · specificity · effort externalization",
        "cta": "binary",
    },
    "active_planning_intent": {
        "goal": "The merchant already asked for this. Skip pitching: give a first concrete draft of the plan right away, with any number you cannot ground left as a blank for them to fill.",
        "levers": "effort externalization · asking the merchant (for missing numbers)",
        "cta": "binary: approve / tweak",
    },
    "gbp_unverified": {
        "goal": "Explain the verification gap in one line, the path, and the upside number given; offer to walk them through it.",
        "levers": "loss aversion · specificity · effort externalization",
        "cta": "binary",
    },
    "supply_alert": {
        "goal": "Urgent, precise: molecule, batch numbers, manufacturer, what to do; offer the affected-customer list.",
        "levers": "specificity · loss aversion (compliance) · effort externalization",
        "cta": "binary",
    },
    # customer-facing
    "recall_due": {
        "goal": "Friendly recall reminder from the merchant: months since last visit, the due service, real open slots, real price.",
        "levers": "specificity (slots, price) · low-friction choice",
        "cta": "slot choice (max 2 numbered options) or binary",
    },
    "customer_lapsed_soft": {
        "goal": "Warm nudge back from the merchant, referencing their history; one catalog service@price; easy booking.",
        "levers": "reciprocity · specificity · single binary",
        "cta": "binary",
    },
    "customer_lapsed_hard": {
        "goal": "Win-back from the merchant: acknowledge the gap without guilt, reference their previous goal, offer an easy restart.",
        "levers": "reciprocity · specificity · single binary",
        "cta": "binary",
    },
    "appointment_tomorrow": {
        "goal": "Clear reminder of tomorrow's appointment at this merchant with an easy confirm/reschedule.",
        "levers": "clarity · single binary",
        "cta": "binary: Reply YES to confirm",
    },
    "chronic_refill_due": {
        "goal": "Refill reminder: which medicines, when stock runs out, delivery option if saved; one-tap confirm.",
        "levers": "specificity · effort externalization",
        "cta": "binary: Reply YES to repeat the order",
    },
    "trial_followup": {
        "goal": "Follow up the trial with the next real session option and an easy yes.",
        "levers": "specificity (session slot) · single binary",
        "cta": "binary",
    },
    "wedding_package_followup": {
        "goal": "Bridal follow-up: days to the wedding, the next program window, one clear next step.",
        "levers": "timeliness · specificity · single binary",
        "cta": "binary",
    },
}
DEFAULT_PLAY = {
    "goal": "Explain in one line why this is worth their attention now, anchored on one verifiable number, and offer one concrete next step.",
    "levers": "specificity · effort externalization · single binary",
    "cta": "binary or open_ended",
}

# which digest item kind to fall back to when a trigger has no explicit item id
DIGEST_KIND_FOR = {
    "research_digest": ("research",), "regulation_change": ("compliance",),
    "cde_opportunity": ("cde",), "supply_alert": ("alert", "supply"),
    "category_trend_movement": ("trend",), "category_seasonal": ("trend", "supply"),
}
ITEM_ID_KEYS = ("top_item_id", "digest_item_id", "alert_id", "item_id")

HUMAN_METRIC = {"views": "profile views", "calls": "calls", "ctr": "CTR", "directions": "direction requests", "leads": "leads"}


def _parse_dt(s: Any) -> Optional[datetime]:
    if not isinstance(s, str) or not re.match(r"^\d{4}-\d{2}-\d{2}", s):
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _pct(x: Any) -> Optional[str]:
    """x is a fraction (0.18 -> '+18%'), as every *_pct field in the dataset is."""
    if isinstance(x, (int, float)) and not isinstance(x, bool):
        v = x * 100
        s = f"{abs(v):.1f}".rstrip("0").rstrip(".")
        return ("+" if v > 0 else "-" if v < 0 else "") + s + "%"
    return None


def language_directive(merchant: dict, customer: Optional[dict]) -> tuple[str, bool]:
    """Returns (directive, prefers_hindi)."""
    if customer:
        pref = str(dig(customer, "identity.language_pref", default="en")).lower()
        if "hi" in pref and ("en" in pref or "mix" in pref):
            return "Hindi-English code-mix in Roman script (customer pref: hi-en mix)", True
        if pref in ("hi", "hindi"):
            return "Hindi in Roman script, simple words (customer pref: hi)", True
        return "English (customer pref: English)", False
    langs = [str(l).lower() for l in as_list(dig(merchant, "identity.languages", default=["en"]))]
    if "hi" in langs or "hindi" in langs:
        return "natural Hindi-English code-mix in Roman script (merchant languages include hi)", True
    return "English", False


def salutation(category_slug: str, merchant: dict, customer: Optional[dict]) -> str:
    if customer:
        parts = str(dig(customer, "identity.name", default="")).split()
        if len(parts) >= 2 and parts[0].rstrip(".").lower() in ("mr", "mrs", "ms", "dr", "shri", "smt"):
            return " ".join(parts[:2])  # "Mr. Sharma", not "Mr."
        return parts[0] if parts else "there"
    first = dig(merchant, "identity.owner_first_name")
    if first:
        first = re.sub(r"^dr\.?\s*", "", str(first), flags=re.I).strip()
        return f"Dr. {first}" if category_slug == "dentists" else first
    return str(dig(merchant, "identity.name", default="there"))


def resolve_digest_item(category: Optional[dict], trigger: dict) -> Optional[dict]:
    items = as_list(dig(category or {}, "digest", default=[]))
    payload = trigger.get("payload") or {}
    for k in ITEM_ID_KEYS:
        want = payload.get(k)
        if want:
            for it in items:
                if isinstance(it, dict) and it.get("id") == want:
                    return it
    # thin trigger: pick the first digest item of the matching kind
    kinds = DIGEST_KIND_FOR.get(trigger.get("kind", ""), ())
    for it in items:
        if isinstance(it, dict) and it.get("kind") in kinds:
            return it
    return None


def active_offers(merchant: dict) -> list[str]:
    return [o.get("title") for o in as_list(merchant.get("offers")) if isinstance(o, dict)
            and o.get("title") and str(o.get("status", "active")).lower() == "active"]


def catalog_offers(category: Optional[dict], audience: Optional[str] = None, n: int = 4) -> list[str]:
    out = []
    for o in as_list(dig(category or {}, "offer_catalog", default=[])):
        if not isinstance(o, dict) or not o.get("title"):
            continue
        if audience and o.get("audience") and o["audience"] != audience:
            continue
        out.append(o["title"])
    return out[:n]


def perf_lines(merchant: dict, category: Optional[dict]) -> list[str]:
    perf = merchant.get("performance") or {}
    peer = dig(category or {}, "peer_stats", default={}) or {}
    lines = []
    win = perf.get("window_days", 30)
    for k in ("views", "calls", "directions", "leads"):
        if isinstance(perf.get(k), (int, float)):
            peer_v = peer.get(f"avg_{k}_{win}d") or peer.get(f"avg_{k}")
            s = f"{HUMAN_METRIC[k]} last {win}d: {perf[k]}"
            if isinstance(peer_v, (int, float)) and peer_v:
                s += f" (category avg {peer_v}; {_pct((perf[k] - peer_v) / peer_v)} vs avg)"
            lines.append(s)
    if isinstance(perf.get("ctr"), (int, float)):
        s = f"CTR: {_pct(perf['ctr']).lstrip('+')}"
        if isinstance(peer.get("avg_ctr"), (int, float)):
            s += f" (category avg {_pct(peer['avg_ctr']).lstrip('+')})"
        lines.append(s)
    for k, v in (perf.get("delta_7d") or {}).items():
        p = _pct(v)
        if p:
            lines.append(f"7-day change in {HUMAN_METRIC.get(k.replace('_pct', ''), k.replace('_pct', ''))}: {p}")
    return lines


def time_lines(trigger: dict, customer: Optional[dict], now: Optional[str]) -> list[str]:
    nd = _parse_dt(now)
    if not nd:
        return []
    lines = [f"today: {nd.strftime('%a %d %b %Y')}"]
    payload = trigger.get("payload") or {}
    for k, v in list(payload.items()) + [("expires_at", trigger.get("expires_at"))]:
        d = _parse_dt(v)
        if not d:
            continue
        days = (d.date() - nd.date()).days
        label = k.replace("_iso", "").replace("_", " ")
        if k == "expires_at" and days < 0:
            continue  # the judge listed it as active; an expiry in the past is noise, not a fact to message
        if days > 0:
            lines.append(f"{label} ({d.strftime('%d %b %Y')}) is {days} days from today")
        elif days == 0:
            lines.append(f"{label} is today ({d.strftime('%d %b')}, {d.strftime('%I:%M %p').lstrip('0')})")
        else:
            lines.append(f"{label} ({d.strftime('%d %b %Y')}) was {-days} days ago")
    if customer:
        lv = _parse_dt(dig(customer, "relationship.last_visit"))
        if lv:
            days = (nd.date() - lv.date()).days
            if days >= 0:
                lines.append(f"customer's last visit was {days} days ago (~{round(days / 30.4)} months)")
    return lines


def seasonal_now(category: Optional[dict], now: Optional[str]) -> list[str]:
    nd = _parse_dt(now)
    if not nd:
        return []
    mon = nd.strftime("%b")
    order = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    out = []
    for b in as_list(dig(category or {}, "seasonal_beats", default=[])):
        rng = str(b.get("month_range", ""))
        parts = [p.strip()[:3] for p in rng.split("-")]
        try:
            a = order.index(parts[0]); z = order.index(parts[-1])
        except ValueError:
            continue
        i = order.index(mon)
        inside = a <= i <= z if a <= z else (i >= a or i <= z)
        if inside:
            out.append(f"{rng}: {b.get('note')}")
    return out


def is_thin(trigger: dict) -> bool:
    p = trigger.get("payload") or {}
    return bool(p.get("placeholder")) or not [k for k in p if k not in ("placeholder", "metric_or_topic", "category")]


def build_brief(category: Optional[dict], merchant: dict, trigger: dict,
                customer: Optional[dict], now: Optional[str]) -> dict:
    slug = merchant.get("category_slug") or dig(category or {}, "slug", default="")
    kind = trigger.get("kind", "unknown")
    play = PLAYBOOK.get(kind, DEFAULT_PLAY)
    lang, _ = language_directive(merchant, customer)
    customer_facing = bool(customer) or trigger.get("scope") == "customer"
    thin = is_thin(trigger)
    item = resolve_digest_item(category, trigger)

    b: dict[str, Any] = {
        "audience": "customer (you write AS the merchant, send_as=merchant_on_behalf)" if customer_facing
                    else "merchant owner (you are Vera, send_as=vera)",
        "address_as": salutation(slug, merchant, customer if customer_facing else None),
        "merchant_name": dig(merchant, "identity.name"),
        "locality": ", ".join(x for x in [dig(merchant, "identity.locality"), dig(merchant, "identity.city")] if x),
        "category": slug,
        "voice": {
            "tone": dig(category or {}, "voice.tone"),
            "register": dig(category or {}, "voice.register"),
            "allowed_vocab": as_list(dig(category or {}, "voice.vocab_allowed"))[:10],
            "never_say": taboo_list(category),
            "tone_examples": as_list(dig(category or {}, "voice.tone_examples"))[:3],
        },
        "language": lang,
        "why_now": {
            "trigger_kind": kind,
            "urgency_1_to_5": trigger.get("urgency"),
            "facts": {k: v for k, v in (trigger.get("payload") or {}).items() if k not in ("placeholder", "metric_or_topic")},
            "resolved_digest_item": item,
            "dates": time_lines(trigger, customer, now),
        },
        "playbook": play,
        "merchant_numbers": perf_lines(merchant, category),
        "merchant_offers_active": active_offers(merchant),
        "catalog_offers": catalog_offers(category, "repeat_user" if customer and dig(customer, "relationship.visits_total", default=0) > 1 else None),
        "seasonal_now": seasonal_now(category, now),
        "subscription": merchant.get("subscription"),
        "customer_aggregate": merchant.get("customer_aggregate"),
        "review_themes": merchant.get("review_themes") or [],
        "recent_history": [
            {"from": h.get("from"), "body": h.get("body"), "engagement": h.get("engagement")}
            for h in as_list(merchant.get("conversation_history"))[-4:] if isinstance(h, dict)
        ],
    }
    if customer_facing:
        # never leak the merchant's analytics/billing into a customer message
        for k in ("merchant_numbers", "subscription", "customer_aggregate", "review_themes", "recent_history"):
            b.pop(k, None)
    if customer_facing and customer:
        b["customer"] = {
            "name": dig(customer, "identity.name"),
            "state": customer.get("state"),
            "relationship": customer.get("relationship"),
            "preferences": customer.get("preferences"),
            "consent_scope": dig(customer, "consent.scope"),
        }
    if thin:
        b["why_now"]["thin_trigger_warning"] = (
            f"This '{kind}' event arrived with NO specifics (no counts, names, dates, times or amounts). "
            "You may say the event happened only in the generic sense its kind implies. Do not invent any of its details. "
            "Anchor the message on the strongest verifiable number from merchant_numbers / offers / catalog instead."
        )
    return b


def taboo_list(category: Optional[dict]) -> list[str]:
    raw = as_list(dig(category or {}, "voice.vocab_taboo", "voice.taboos", default=[]))
    out = []
    for t in raw:
        t = re.sub(r"\(.*?\)", "", str(t)).strip()
        if t:
            out.append(t)
    return out


def send_as_for(trigger: dict, customer: Optional[dict]) -> str:
    return "merchant_on_behalf" if (customer or trigger.get("scope") == "customer") else "vera"
