"""Deterministic read of an inbound reply, BEFORE any LLM call.

Why deterministic: the four failure modes the brief calls out in today's
Vera (auto-reply pollution, missed intent hand-offs, hostile/off-topic
derailment, over-messaging) are all decisions about *whether and how* to
answer. Those must not depend on a model's mood — they should be the same
every run, testable, and cheap. The LLM is only asked to write words once
this layer has decided a send is the right move, and it is told which mode
it is writing in.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

OPT_OUT_RE = re.compile(
    r"^\s*stop\s*[.!]*\s*$|\b(unsubscribe|opt[\s-]?out|band karo|mat bhejo|message mat|msg mat|"
    r"don'?t (message|text|msg|contact) me|do not (message|text|contact)|remove me|no more (messages|msgs)|"
    r"stop (messaging|sending|texting|this))\b",
    re.I,
)

AUTO_REPLY_RE = re.compile(
    r"(thank(s| you) for (contacting|reaching out|your message|messaging)|we (will|'ll) get back|"
    r"will (revert|respond|get back) (to you )?(shortly|soon|asap)|currently (unavailable|closed|away|busy|out)|"
    r"out of (the )?office|business hours|working hours are|this is an auto(mated)?[- ]?(reply|response|message)|"
    r"auto[- ]?reply|our (team|executive) will (contact|call|reach)|aapka (sandesh|message) (mil gaya|prapt)|"
    r"hum (jald|jaldi) (hi )?(sampark|contact)|we are closed|shop (is )?closed (now|today)|"
    r"automated assistant|virtual assistant|jaankari ke liye .{0,20}shukriya|team tak pahuncha|"
    r"sandesh ke liye dhanyavaad|message ke liye (dhanyavaad|shukriya)|respond (shortly|soon)|"
    r"away from (the )?(phone|desk)|reply (you )?(shortly|as soon as possible))",
    re.I,
)

INTENT_RE = re.compile(
    r"(let'?s do (it|this)|lets do|go ahead|please proceed|\bproceed\b|sign me up|i'?m in\b|count me in|book it|"
    r"\bconfirmed\b|confirm (it|karo|kar do)|ok(ay)? do it|do it\b|start (it|karo|kar do|now)|chalo (start|shuru|karte)|shuru karo|"
    r"kar do\b|haan (chalo|karo|kar do|theek|ji)|i want to (join|start|sign up|try|activate)|"
    r"mujhe join karna|activate (it|karo|kar do)|set it up|send (me )?(the )?(link|details)|how do i start|"
    r"sounds good,? (let'?s|go)|yes,? (please|start|go|do it|let'?s))",
    re.I,
)
BARE_YES_RE = re.compile(r"^\s*(yes|y|yep|yeah|haan|haa|ha|ji|ji haan|ok|okay|sure|done|theek hai|thik hai|👍)\s*[.!]*\s*$", re.I)

DECLINE_RE = re.compile(r"\b(not interested|no thanks|no thank you|nahi chahiye|interest nahi|zarurat nahi|no need)\b|^\s*(no|nahi|nope)\s*[.!]*\s*$", re.I)
LATER_RE = re.compile(r"\b(later|baad mein|abhi (nahi|busy)|busy (now|hu|hoon|hun)|call (you )?later|kal baat|tomorrow)\b", re.I)

HOSTILE_RE = re.compile(
    r"\b(stupid|idiot|shut up|spam(ming)?|scam|fraud|bakwas|pagal|chup|bewakoof|nonsense|harass(ing|ment)?|"
    r"useless|worst|irritat(e|ing)|pestering|f+u+c+k+|wtf|bloody|block(ing)? you|report(ing)? you)\b",
    re.I,
)
OFF_MISSION_RE = re.compile(
    r"\b(gst|income tax|itr|tax return|file my|loan|lawyer|legal notice|court|visa|passport|"
    r"electricity bill|laptop|computer repair|cricket score|horoscope|recipe|homework|stock tips?|crypto|bitcoin)\b",
    re.I,
)
QUESTION_RE = re.compile(r"\?|^\s*(what|how|why|when|where|which|who|kya|kaise|kitna|kitne|kab|kyun|kaun|kahan)\b", re.I)


@dataclass
class ReplyRead:
    label: str                  # opt_out|auto_reply|intent_transition|hostile|decline|later|off_topic|question|general|empty
    confidence: str             # high|medium
    notes: list[str] = field(default_factory=list)


def classify(message: str, *, repeat_count: int, last_bot_cta: str | None) -> ReplyRead:
    msg = (message or "").strip()
    if not msg:
        return ReplyRead("empty", "high")
    if OPT_OUT_RE.search(msg):
        return ReplyRead("opt_out", "high", ["explicit opt-out phrase"])
    if AUTO_REPLY_RE.search(msg):
        return ReplyRead("auto_reply", "high", ["canned auto-reply phrasing"])
    if repeat_count >= 3:
        return ReplyRead("auto_reply", "high", [f"identical message received {repeat_count}x"])
    if HOSTILE_RE.search(msg):
        return ReplyRead("hostile", "high", ["abusive / spam-accusation language"])
    if INTENT_RE.search(msg):
        return ReplyRead("intent_transition", "high", ["explicit go-ahead phrase"])
    if BARE_YES_RE.match(msg) and last_bot_cta == "binary":
        return ReplyRead("intent_transition", "high", ["bare 'yes' to a binary CTA"])
    if DECLINE_RE.search(msg):
        return ReplyRead("decline", "high", ["polite decline"])
    if LATER_RE.search(msg):
        return ReplyRead("later", "medium", ["merchant is busy / asks for later"])
    if OFF_MISSION_RE.search(msg):
        return ReplyRead("off_topic", "medium", ["request outside Vera's remit"])
    if repeat_count == 2:
        return ReplyRead("auto_reply", "medium", ["same message received twice"])
    if QUESTION_RE.search(msg):
        return ReplyRead("question", "medium")
    return ReplyRead("general", "medium")
