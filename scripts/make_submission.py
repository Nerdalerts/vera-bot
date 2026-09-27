#!/usr/bin/env python3
"""Generate submission.jsonl for the 30 canonical test pairs (brief §6, §7.2).

    python scripts/make_submission.py                       # uses LLM_PROVIDER / key from .env
    python scripts/make_submission.py --dump-briefs out/    # also writes each pair's brief (debugging)
    python scripts/make_submission.py --replay drafts.json  # validate pre-written drafts instead of calling an LLM

Every line goes through the SAME brief -> composer -> validator pipeline the
live /v1/tick uses, so what you submit is what the judge will see live.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from vera import llm  # noqa: E402
from vera.brief import build_brief  # noqa: E402
from vera.composer import compose  # noqa: E402

DATASET = ROOT / "dataset"
EXPANDED = ROOT / "dataset" / "expanded"
# The dataset is authored around 26 Apr 2026 (see brief §3/§4); anchoring
# "today" there keeps day-counts consistent with the payloads.
REFERENCE_NOW = "2026-04-26T10:30:00+05:30"


def ensure_expanded() -> None:
    if (EXPANDED / "test_pairs.json").exists():
        return
    subprocess.run([sys.executable, str(DATASET / "generate_dataset.py"), "--seed-dir", str(DATASET),
                    "--out", str(EXPANDED)], check=True)


def load(kind: str, ident: str) -> dict:
    return json.loads((EXPANDED / kind / f"{ident}.json").read_text())


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "submission.jsonl"))
    ap.add_argument("--now", default=REFERENCE_NOW)
    ap.add_argument("--dump-briefs")
    ap.add_argument("--replay", help="JSON file {test_id: raw model output object} to validate instead of calling an LLM")
    args = ap.parse_args()

    ensure_expanded()
    pairs = json.loads((EXPANDED / "test_pairs.json").read_text())["pairs"]

    if args.replay:
        drafts = json.loads(Path(args.replay).read_text())
        current = {"tid": None}

        async def replay(system: str, user: str, timeout: float) -> str:
            payload = json.loads(user)
            if payload.get("repair"):
                print(f"  [{current['tid']}] REJECTED: {payload['repair']['violations']}", file=sys.stderr)
            return json.dumps(drafts[current["tid"]])
        llm.set_override(replay)
    elif not llm.is_configured():
        sys.exit("No LLM configured: set LLM_PROVIDER and its API key in .env (or use --replay).")

    if args.dump_briefs:
        Path(args.dump_briefs).mkdir(parents=True, exist_ok=True)

    lines, failures = [], []
    for p in pairs:
        trig = load("triggers", p["trigger_id"])
        merchant = load("merchants", p["merchant_id"])
        category = json.loads((EXPANDED / "categories" / f"{merchant['category_slug']}.json").read_text())
        customer = load("customers", p["customer_id"]) if p.get("customer_id") else None
        if args.dump_briefs:
            brief = build_brief(category, merchant, trig, customer, args.now)
            Path(args.dump_briefs, f"{p['test_id']}.json").write_text(json.dumps(brief, indent=1, ensure_ascii=False))
        if args.replay:
            current["tid"] = p["test_id"]
            if p["test_id"] not in drafts:
                failures.append(p["test_id"]); continue
        r = await compose(category, merchant, trig, customer, now=args.now,
                          deadline=time.monotonic() + 60, allow_skip=False, max_attempts=3)
        m = r.message
        if not m:
            failures.append(p["test_id"])
            print(f"  [{p['test_id']}] no validated message ({r.skipped_reason}); last violations: {r.violations[-1:]}", file=sys.stderr)
            m = {"body": "", "cta": "none", "send_as": "merchant_on_behalf" if customer else "vera",
                 "suppression_key": trig.get("suppression_key", ""), "rationale": f"no validated message: {r.skipped_reason}"}
        lines.append({"test_id": p["test_id"], "body": m["body"], "cta": m["cta"], "send_as": m["send_as"],
                      "suppression_key": m["suppression_key"], "rationale": m["rationale"]})
        print(f"{p['test_id']} {trig['kind']:<24} {'OK ' if r.message else 'FAIL'} {m['body'][:90]}")

    Path(args.out).write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in lines) + "\n", encoding="utf-8")
    print(f"\nwrote {len(lines)} lines to {args.out}; {len(failures)} without a validated message: {failures}")


if __name__ == "__main__":
    asyncio.run(main())
