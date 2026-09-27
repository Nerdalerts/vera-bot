# Vera, rebuilt: approach, trade-offs, wish-list

**Approach.** The bot is split into two layers. The first is a deterministic layer that owns every *decision*. Replies are classified before any model call: opt-out, auto-reply (English and Hinglish patterns, plus the same text repeated across a merchant's threads), explicit intent, hostility, off-topic, decline, "later" and question. Auto-replies, "no" and "stop" exit or back off without spending an LLM call. Proactive sends are filtered by suppression key, opt-out, consent and a cadence of one message per recipient per tick.

The second is a writing layer. Before the LLM sees anything, code builds an **evidence brief**. It resolves the digest item a trigger points to, turns dates into day-counts against the judge's clock, turns rates into percentages vs. the category average, picks the salutation ("Dr. Meera") and the language (Hinglish vs. English), and attaches a per-trigger-kind playbook. The playbook sets the goal, the levers (weighted toward social proof and asking-the-merchant, the two the brief says are under-used) and the CTA shape. The model, at temperature 0, writes from that brief. A **deterministic validator** then checks the draft:
- every number must exist in context, typed as %, ₹ or a plain count, so a count of 45 can't become "45%"
- no acronym or citation that isn't in context (so no invented "JIDA paper")
- no generic copy, taboo words, internal field names, multiple CTAs, repeats, re-introductions, or re-qualifying after a "yes"

If the draft fails, the model is told exactly which rule it broke and gets up to two informed repairs; after that the bot sends nothing. Triggers are pre-composed in the background when their context is pushed, so the tick itself is usually served from cache in milliseconds.

**Evidence.** On the 30 canonical pairs, 30 of 30 hand-written grounded drafts pass the validator. When the same drafts are altered to include an invented citation or inflated figures, 48 of 48 are rejected. That is a regression test (`tests/test_reference_set.py`).

**Trade-offs.**
- *Restraint over coverage.* If nothing grounded survives validation, the bot stays silent rather than sending filler. That can cost a message; it never costs a hallucination.
- *Placeholder triggers.* 13 of the 30 pairs carry no event details. The bot names the event only as generically as its kind allows and anchors the message on the merchant's real numbers. This means lower specificity on the "why now" in exchange for zero invention.
- *Number grounding is lexical, not semantic.* It stops invented figures, but it can't prove that a real figure is used in the right sentence; the prompt and the model carry that part.
- *The auto-reply exit is fast.* First detection: wait 4h. Second: end. A human owner who happens to paste canned text twice gets treated as an auto-responder.
- *In-memory state.* Run a single worker. There is no persistence, which suits the contract and the teardown rule.

**What additional context would help most.**
1. Real appointment and slot data for `appointment_tomorrow` / `recall_due` on the generated triggers.
2. Payloads for the placeholder triggers: metric and delta for `perf_dip`/`perf_spike`, competitor details, milestone values, festival name and date.
3. A merchant-level "last message sent by Vera" timestamp across channels, for true cadence control.
4. Per-merchant review counts and ratings, for social proof against `peer_stats`.
5. A small set of labelled real merchant replies (auto-reply vs. human) to calibrate the classifier.
