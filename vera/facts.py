"""Grounding substrate.

Builds, from the raw context JSON, the two things the validator needs:
  1. a lowercase text blob of every groundable string, and
  2. an index of every groundable *number* (as floats, with the common
     re-expressions a copywriter legitimately uses: 0.021 -> 2.1%,
     1499 -> "1.5k" is NOT added on purpose, see below).

Derived numbers (day counts, % gaps vs peers) are computed in brief.py and
indexed here too, so the model never has to do arithmetic to be specific.
"""

from __future__ import annotations

import json
import re
from typing import Any, Iterable, Optional

NUM_RE = re.compile(r"-?\d[\d,]*(?:\.\d+)?")


# ---------------------------------------------------------------------------
# Tolerant accessors — the judge's schema may differ in small ways from our
# fixtures; never KeyError on a missing branch.
# ---------------------------------------------------------------------------

def dig(obj: Any, *paths: str, default: Any = None) -> Any:
    """dig(m, "identity.name", "name") -> first path that resolves."""
    for path in paths:
        cur = obj
        ok = True
        for part in path.split("."):
            if isinstance(cur, dict) and part in cur:
                cur = cur[part]
            else:
                ok = False
                break
        if ok and cur not in (None, "", [], {}):
            return cur
    return default


def as_list(x: Any) -> list:
    if x is None:
        return []
    return x if isinstance(x, list) else [x]


def iter_leaves(obj: Any, prefix: str = "") -> Iterable[tuple[str, Any]]:
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from iter_leaves(v, f"{prefix}.{k}" if prefix else str(k))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from iter_leaves(v, f"{prefix}[{i}]")
    else:
        yield prefix, obj


def parse_number(tok: str) -> Optional[float]:
    try:
        return float(tok.replace(",", ""))
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Grounding index
# ---------------------------------------------------------------------------

ISO_TIME_RE = re.compile(r"T(\d{2}):(\d{2})")


class GroundingIndex:
    """Everything a message may state as a number or name. Built from the raw
    contexts PLUS the evidence brief (which only holds values copied from, or
    computed deterministically out of, those contexts)."""

    def __init__(self, *payloads: Optional[dict], extra_text: str = ""):
        parts = [json.dumps(p, ensure_ascii=False) for p in payloads if p]
        parts.append(extra_text)
        self.blob = " ".join(parts).lower()

        nums: set[float] = set()
        for tok in NUM_RE.findall(" ".join(parts)):
            v = parse_number(tok)
            if v is None:
                continue
            nums.add(abs(v))
            if 0 < abs(v) < 1:  # rates stored as fractions -> copy says "2.1%"
                nums.add(round(abs(v) * 100, 4))
        # Typed sub-indexes: a "45%" in the copy must match something that is a
        # percentage in context (not just any 45, e.g. 45 direction requests),
        # and "₹299" must match a money value, not a count.
        text = " ".join(parts)
        self.pct_numbers: set[float] = set()
        for tok in re.findall(r"([+-]?\d[\d,]*(?:\.\d+)?)\s*%", text):
            v = parse_number(tok); v is not None and self.pct_numbers.add(abs(v))
        for tok in re.findall(r"[_\s(]([+-]\d+(?:\.\d+)?)\b", text):  # "ORS_demand_+40", "(+18%)"
            v = parse_number(tok); v is not None and self.pct_numbers.add(abs(v))
        for v in nums:
            if 0 < v < 1:
                self.pct_numbers.add(round(v * 100, 4))
        self.money_numbers: set[float] = set()
        for tok in re.findall(r"(?:₹|rs\.?\s?|inr\s?)\s?(\d[\d,]*(?:\.\d+)?)", text, re.I):
            v = parse_number(tok); v is not None and self.money_numbers.add(v)
        for p in payloads:
            for path, val in iter_leaves(p or {}):
                if isinstance(val, (int, float, str)) and re.search(r"(value|price|amount|fee|mrp|cost|lifetime_value|aov)$", path.split(".")[-1].split("[")[0], re.I):
                    v = parse_number(str(val)) if isinstance(val, str) else float(val)
                    if v is not None:
                        self.money_numbers.add(abs(v))
        # "2026-04-26T19:30" may be written as "7:30 PM"
        for h, m in ISO_TIME_RE.findall(" ".join(parts)):
            nums.add(float(int(h) % 12 or 12))
            nums.add(float(int(m)))
        self.numbers = nums

    def has_number(self, value: float, decimals: int, kind: str = "plain") -> bool:
        pool = self.pct_numbers if kind == "pct" else self.money_numbers if kind == "money" else self.numbers
        for n in pool:
            if abs(n - value) < 1e-9:
                return True
            # a copywriter may round 4.63 -> 4.6; allowed only when the body
            # itself shows decimals (so "5" can never "match" 4.63)
            if decimals > 0 and abs(round(n, decimals) - value) < 1e-9:
                return True
        return False

    def has_text(self, s: str) -> bool:
        return s.lower().strip() in self.blob
