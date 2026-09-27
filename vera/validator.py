"""Deterministic guardrail layer. No LLM in here — that is the point.

validate() returns (errors, warnings):
  errors   -> the message may NOT be sent. The composer feeds them back to
              the model for one informed repair, then falls back to a no-op.
  warnings -> quality issues worth one repair attempt, but acceptable on the
              final attempt (e.g. English to a Hindi-preferring merchant is
              still valid WhatsApp copy, just weaker).

Each rule maps to a specific penalty/anti-pattern in the challenge brief, so
every rejection is explainable to a reviewer.
"""

from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher
from typing import Optional

from . import config
from .facts import GroundingIndex, parse_number

# ---- generic / spammy copy the judge's Specificity rubric docks ----------------
GENERIC_PATTERNS = [
    r"\bflat\s*\d+\s*%\s*off\b",
    r"\bup\s*to\s*\d+\s*%\s*off\b",
    r"\bincrease your (sales|revenue|footfall|business)\b",
    r"\bgrow your business\b",
    r"\bboost your (sales|business|revenue|growth)\b",
    r"\btake your business to the next level\b",
    r"\bamazing (deal|offer|discount)s?\b",
    r"\bhope (you('| a)re|this (message )?finds you) (doing )?well\b",
    r"\bdear (sir|madam|customer|valued|merchant|partner)\b",
    r"\bvalued (customer|partner|merchant)\b",
    r"\blimited[- ]time offer\b",
    r"\bdon'?t miss (out|this)\b",
    r"\bhurry\b",
    r"\bact now\b",
    r"\bunlock (your|the) (full )?potential\b",
    r"\bwe are (excited|thrilled|delighted) to\b",
    r"\bbest in (town|class|the city)\b",
    r"\bgreetings of the day\b",
]
_GENERIC_RE = [re.compile(p, re.IGNORECASE) for p in GENERIC_PATTERNS]

# ---- citation words: a "study" is only allowed if context carries a source ----
CITATION_RE = re.compile(r"\b(study|studies|research(ers)?|paper|journal|survey|published|according to|report(ed)? by)\b", re.I)
SOURCE_MARKERS = ("digest", "source", "study", "research", "paper", "journal", "survey", "report")

# ---- acronyms: "JIDA paper", "IMA says" -> must exist in context ---------------
ACRONYM_RE = re.compile(r"\b[A-Z][A-Z0-9]{1,}\b")
ACRONYM_ALLOW = {
    "YES", "NO", "STOP", "OK", "OKAY", "UPI", "AM", "PM", "QR", "ID", "GST", "EMI", "SMS",
    "FAQ", "DM", "FYI", "ASAP", "BTW", "HI", "PS", "NA", "N/A", "TV", "CTA", "WA", "OTP",
    "Y", "N", "INR", "RS",
}

# internal field names / system words leaking into merchant-facing copy
JARGON_RE = re.compile(r"\b[a-z]+(?:_[a-z0-9]+)+\b|\b(placeholder|payload|suppression[ _]key|trigger|json|null)\b", re.I)
# phrases that mean "still qualifying" — banned once the merchant has said go
QUALIFYING_RE = re.compile(r"\b(would you|do you|can you tell|what if|how about|are you sure|kya aap chahenge|should we)\b", re.I)

# a reply *instruction* ("Reply YES", "YES reply kijiye"), not the noun in "review-reply drafts"
CTA_INSTR_RE = re.compile(r"(?<![-\w])reply\s+(?:with\s+)?[\"']?(?:yes|no|stop|y|n|\d)\b|\b(?:yes|stop|\d)\s+reply\b", re.I)

REINTRO_RE = re.compile(r"\b(i('| a)?m vera|this is vera|main vera|vera (here|this side)|meet vera)\b", re.I)

HINGLISH_MARKERS = re.compile(
    r"\b(hai|hain|aap|aapka|aapke|aapki|kya|ji|karein|kariye|karo|nahi|nahin|haan|chahiye|abhi|"
    r"bhi|mein|se|ka|ki|ke|ko|hoga|raha|rahe|wala|wale|kaise|kitna|thoda|bas|chalo|dekhiye)\b",
    re.I,
)
DEVANAGARI_RE = re.compile(r"[ऀ-ॿ]")

# number with optional currency prefix and scale suffix
BODY_NUM_RE = re.compile(
    r"(?P<cur>₹|rs\.?\s?|inr\s?)?(?P<num>\d[\d,]*(?:\.\d+)?)\s?(?P<suf>k\b|lakh\b|lac\b|l\b|cr\b|crore\b|%)?",
    re.I,
)
SCALE = {"k": 1e3, "lakh": 1e5, "lac": 1e5, "l": 1e5, "cr": 1e7, "crore": 1e7}
# digits that are CTA option labels ("Reply 1 for Wed, 2 for Thu"), not facts
CTA_DIGIT_RE = re.compile(r"(?:reply|type|send|press|option)\s+(\d)\b|\b(\d)\s*(?:for|=|->|→|:)\s", re.I)


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "").lower()
    text = re.sub(r"[^\w\s₹%]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _cta_digit_spans(body: str) -> set[tuple[int, int]]:
    spans = set()
    for m in CTA_DIGIT_RE.finditer(body):
        g = 1 if m.group(1) else 2
        spans.add(m.span(g))
    return spans


def ungrounded_numbers(body: str, idx: GroundingIndex) -> list[str]:
    bad = []
    cta_spans = _cta_digit_spans(body)
    for m in BODY_NUM_RE.finditer(body):
        if m.span("num") in cta_spans:
            continue
        raw = m.group("num")
        # skip digits glued to letters (e.g. "4pm" handled below, "B2B", "24x7")
        start, end = m.span("num")
        if start > 0 and body[start - 1].isalpha():
            continue
        val = parse_number(raw)
        if val is None:
            continue
        decimals = len(raw.split(".")[1]) if "." in raw else 0
        suf = (m.group("suf") or "").lower()
        if suf in SCALE:
            target = val * SCALE[suf]
            # "1.5k" legitimately rounds 1,480; allow 5% for scaled shorthand only
            if not any(abs(n - target) <= 0.05 * max(n, 1) for n in idx.numbers):
                bad.append(m.group(0).strip())
            continue
        kind = "pct" if suf == "%" else "money" if m.group("cur") else "plain"
        if not idx.has_number(val, decimals, kind):
            bad.append(m.group(0).strip())
    return bad


def validate(body: str, cta: str, idx: GroundingIndex, *, prior_bodies: list[str],
             taboo: list[str], prefers_hindi: bool, has_prior_bot_turns: bool,
             facts_used: Optional[list] = None, mode: str = "first_touch") -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []

    if not isinstance(body, str) or not body.strip():
        return ["empty_body: body is empty"], []
    b = body.strip()

    # --- shape ---
    if len(b) > config.MAX_BODY_CHARS:
        errors.append(f"too_long: {len(b)} chars, limit {config.MAX_BODY_CHARS}; cut to the single strongest fact + CTA")
    if len(b) < config.MIN_BODY_CHARS:
        errors.append("too_short: body carries no usable content")
    if cta not in ("binary", "open_ended", "none"):
        errors.append(f"bad_cta_field: cta must be binary|open_ended|none, got {cta!r}")
    if len(CTA_INSTR_RE.findall(b)) > 1:
        errors.append("multiple_cta: more than one 'Reply ...' instruction; keep exactly one")
    if len(set(re.findall(r"\b(\d)\s*(?:for|=|->|→)\s", b, re.I))) >= 3:
        errors.append("too_many_options: 3+ numbered options; offer at most two")
    if b.count("?") > 2:
        errors.append("question_stack: more than two questions; ask one thing")
    if re.search(r"https?://|www\.", b, re.I) and not idx.has_text(re.search(r"(https?://\S+|www\.\S+)", b).group(0).rstrip(".,)")):
        errors.append("ungrounded_link: URL not present in context")

    # --- generic copy ---
    for rx in _GENERIC_RE:
        m = rx.search(b)
        if m:
            errors.append(f"generic_copy: '{m.group(0)}' is a generic framing; replace with a concrete fact from context")

    # --- taboo vocabulary from category.voice ---
    for word in taboo:
        if word and re.search(rf"\b{re.escape(word)}\b", b, re.I):
            errors.append(f"taboo_word: '{word}' is in category.voice.vocab_taboo")

    # --- grounding: numbers ---
    for n in ungrounded_numbers(b, idx):
        errors.append(f"ungrounded_number: '{n}' does not appear in context or derived_facts; remove it or use an exact value from context")

    # --- grounding: named bodies / acronyms ---
    for ac in sorted(set(ACRONYM_RE.findall(b))):
        if ac not in ACRONYM_ALLOW and ac.lower() not in idx.blob:
            errors.append(f"ungrounded_entity: '{ac}' is not in context; do not name sources/organisations that are not given")

    # --- grounding: citations ---
    if CITATION_RE.search(b) and not any(mk in idx.blob for mk in SOURCE_MARKERS):
        errors.append("ungrounded_citation: message cites a study/report but context has no source")

    # --- internal jargon (judge docks "exposing internal jargon") ---
    for m in JARGON_RE.finditer(b):
        errors.append(f"internal_jargon: '{m.group(0)}' is a system/field name; say it in plain words")
        break

    # --- intent transition: act, don't re-qualify ---
    if mode == "action":
        m = QUALIFYING_RE.search(b)
        if m:
            errors.append(f"requalifying: merchant already said go; remove '{m.group(0)}' and state what you are doing now")

    # --- conversation hygiene ---
    nb = normalize(b)
    for prev in prior_bodies:
        np_ = normalize(prev)
        if nb == np_:
            errors.append("verbatim_repeat: identical to an earlier message in this conversation")
            break
        if np_ and SequenceMatcher(None, nb, np_).ratio() >= 0.9:
            errors.append("near_repeat: >=90% similar to an earlier message; say something new")
            break
    if has_prior_bot_turns and REINTRO_RE.search(b):
        errors.append("reintroduction: conversation already started; do not re-introduce Vera")

    # --- soft quality signals ---
    if prefers_hindi and not (DEVANAGARI_RE.search(b) or len(HINGLISH_MARKERS.findall(b)) >= 2):
        warnings.append("language: merchant prefers Hindi; use natural Hindi-English code-mix")
    for f in facts_used or []:
        if isinstance(f, (str, int, float)) and str(f).strip() and not idx.has_text(str(f)):
            warnings.append(f"facts_used_mismatch: '{f}' is not a literal value in context; list exact values only")
            break

    return errors, warnings
