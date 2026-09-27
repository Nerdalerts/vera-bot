"""Guards the validator against drifting too strict OR too loose, on the 30
canonical test pairs: 30 human-quality grounded drafts must all pass, and the
same drafts with one injected fabrication must all fail."""

import asyncio
import json
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

from vera import llm
from vera.composer import compose

ROOT = Path(__file__).resolve().parent.parent
EXP = ROOT / "dataset" / "expanded"
DRAFTS = json.loads((ROOT / "tests" / "fixtures" / "reference_drafts.json").read_text())
NOW = "2026-04-26T10:30:00+05:30"


@pytest.fixture(scope="module")
def pairs():
    if not (EXP / "test_pairs.json").exists():
        subprocess.run([sys.executable, str(ROOT / "dataset" / "generate_dataset.py"), "--seed-dir",
                        str(ROOT / "dataset"), "--out", str(EXP)], check=True, capture_output=True)
    return json.loads((EXP / "test_pairs.json").read_text())["pairs"]


def _ctx(p):
    L = lambda k, i: json.loads((EXP / k / f"{i}.json").read_text())
    t = L("triggers", p["trigger_id"]); m = L("merchants", p["merchant_id"])
    c = json.loads((EXP / "categories" / f"{m['category_slug']}.json").read_text())
    cu = L("customers", p["customer_id"]) if p.get("customer_id") else None
    return c, m, t, cu


def _run(pairs, drafts):
    from vera.store import STATE
    STATE.reset()
    ok = {}
    for p in pairs:
        if p["test_id"] not in drafts:
            continue
        draft = drafts[p["test_id"]]
        async def fake(system, user, timeout, draft=draft):
            return json.dumps(draft)
        llm.set_override(fake)
        c, m, t, cu = _ctx(p)
        r = asyncio.run(compose(c, m, t, cu, now=NOW, deadline=time.monotonic() + 10, allow_skip=False, max_attempts=1))
        ok[p["test_id"]] = r.message is not None
    llm.set_override(None)
    return ok


def test_all_grounded_reference_drafts_pass(pairs):
    ok = _run(pairs, DRAFTS)
    assert len(ok) == 30 and all(ok.values()), [k for k, v in ok.items() if not v]


def test_injected_citation_is_always_caught(pairs):
    bad = {k: dict(v, body=v["body"] + " A recent AIIMS survey found 64% of customers prefer this.") for k, v in DRAFTS.items()}
    ok = _run(pairs, bad)
    assert not any(ok.values()), [k for k, v in ok.items() if v]


def test_inflated_numbers_are_always_caught(pairs):
    def bump(b):
        m = re.search(r"₹\s?\d[\d,]*|\d+(?:\.\d+)?%", b)
        if not m:
            return None
        new = re.sub(r"\d[\d,]*(?:\.\d+)?", lambda x: str(int(float(x.group(0).replace(",", "")) * 1.37) + 3), m.group(0), count=1)
        return b[:m.start()] + new + b[m.end():]
    bad = {k: dict(v, body=bump(v["body"])) for k, v in DRAFTS.items() if bump(v["body"])}
    ok = _run(pairs, bad)
    assert len(ok) >= 15 and not any(ok.values()), [k for k, v in ok.items() if v]
