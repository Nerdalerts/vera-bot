"""One failing and one passing case per guardrail rule, on real dataset shapes."""

import json

from vera.brief import build_brief, taboo_list
from vera.facts import GroundingIndex
from vera.validator import validate


def _idx(dataset, trg_id, now="2026-04-26T10:30:00Z"):
    cats, merchants, customers, triggers = dataset
    t = triggers[trg_id]
    m = merchants[t["merchant_id"]]
    c = cats[m["category_slug"]]
    cu = customers.get(t.get("customer_id"))
    brief = build_brief(c, m, t, cu, now)
    return GroundingIndex(c, m, t, cu, extra_text=json.dumps(brief, ensure_ascii=False)), c


def run(body, idx, cat, **kw):
    args = dict(prior_bodies=[], taboo=taboo_list(cat), prefers_hindi=False, has_prior_bot_turns=False)
    args.update(kw)
    return validate(body, kw.pop("cta", "binary"), idx, **args)


GOOD_MEERA = ("Dr. Meera, JIDA Oct 2026 p.14 has one for your 124 high-risk adults: 2,100-patient trial, "
              "3-month fluoride recall cut caries recurrence 38% vs 6-month. Want me to draft a recall WhatsApp for them?")


def test_grounded_research_message_passes(dataset):
    idx, cat = _idx(dataset, "trg_001_research_digest_dentists")
    errors, _ = run(GOOD_MEERA, idx, cat)
    assert errors == []


def test_invented_statistic_fails(dataset):
    idx, cat = _idx(dataset, "trg_001_research_digest_dentists")
    errors, _ = run(GOOD_MEERA.replace("38%", "45%"), idx, cat)
    assert any(e.startswith("ungrounded_number") and "45%" in e for e in errors)


def test_invented_source_fails(dataset):
    idx, cat = _idx(dataset, "trg_001_research_digest_dentists")
    errors, _ = run("Dr. Meera, a new ADA review backs 3-month recall for high-risk adults. Want the summary?", idx, cat)
    assert any(e.startswith("ungrounded_entity") and "ADA" in e for e in errors)


def test_rates_can_be_written_as_percent(dataset):
    idx, cat = _idx(dataset, "trg_004_perf_dip_bharat")
    errors, _ = run("Dr. Bharat, calls dropped 50% this week and your CTR is 1.8% vs a 3% category average. "
                    "Want me to push a Dental Cleaning @ ₹299 post today?", idx, cat)
    assert errors == []


def test_iso_time_as_12h_clock_is_grounded(dataset):
    idx, cat = _idx(dataset, "trg_010_ipl_match_delhi")
    errors, _ = run("DC vs MI tonight at 7:30 PM at Arun Jaitley Stadium — want a match-night combo @ ₹777 post live?", idx, cat)
    assert not any("7:30" in e or "'7'" in e for e in errors)
    assert any("₹777" in e for e in errors)  # invented price


def test_count_cannot_masquerade_as_percentage(dataset):
    # Meera has 45 direction requests; "45%" must still be rejected
    idx, cat = _idx(dataset, "trg_001_research_digest_dentists")
    assert 45 in idx.numbers and 45 not in idx.pct_numbers


def test_money_must_be_a_money_value(dataset):
    idx, cat = _idx(dataset, "trg_001_research_digest_dentists")
    ok, _ = run("Dr. Meera, Dental Cleaning @ ₹299 is live. Want a post for it?", idx, cat)
    bad, _ = run("Dr. Meera, Dental Cleaning @ ₹540 is live. Want a post for it?", idx, cat)  # 540 = patient count
    assert ok == [] and any("₹540" in e for e in bad)


def test_generic_copy_fails(dataset):
    idx, cat = _idx(dataset, "trg_006_festival_diwali")
    errors, _ = run("Diwali is coming! Grab this amazing deal and grow your business. Reply YES.", idx, cat)
    assert sum(e.startswith("generic_copy") for e in errors) >= 2


def test_taboo_word_fails(dataset):
    idx, cat = _idx(dataset, "trg_001_research_digest_dentists")
    errors, _ = run("Dr. Meera, whitening results are guaranteed with this. Reply YES to post it.", idx, cat)
    assert any(e.startswith("taboo_word") for e in errors)


def test_multiple_cta_fails(dataset):
    idx, cat = _idx(dataset, "trg_001_research_digest_dentists")
    errors, _ = run("Dr. Meera, JIDA Oct 2026 is out. Reply YES for the abstract, reply NO to skip.", idx, cat)
    assert any(e.startswith("multiple_cta") for e in errors)


def test_two_slot_booking_is_allowed(dataset):
    idx, cat = _idx(dataset, "trg_003_recall_due_priya")
    body = ("Hi Priya, Dr. Meera's clinic here. Aapka 6-month cleaning recall due hai. Slots: Wed 5 Nov, 6pm ya Thu 6 Nov, 5pm "
            "— Dental Cleaning @ ₹299. Reply 1 for Wed, 2 for Thu.")
    errors, _ = run(body, idx, cat)
    assert errors == []


def test_internal_jargon_fails(dataset):
    idx, cat = _idx(dataset, "trg_004_perf_dip_bharat")
    errors, _ = run("Dr. Bharat, your perf_dip_severe flag is on. Want me to fix it?", idx, cat)
    assert any(e.startswith("internal_jargon") for e in errors)


def test_verbatim_and_near_repeat_fail(dataset):
    idx, cat = _idx(dataset, "trg_001_research_digest_dentists")
    errors, _ = run(GOOD_MEERA, idx, cat, prior_bodies=[GOOD_MEERA])
    assert any(e.startswith("verbatim_repeat") for e in errors)
    errors, _ = run(GOOD_MEERA + " Thanks!", idx, cat, prior_bodies=[GOOD_MEERA])
    assert any(e.startswith("near_repeat") for e in errors)


def test_reintroduction_fails_only_after_first_touch(dataset):
    idx, cat = _idx(dataset, "trg_001_research_digest_dentists")
    body = "Hi Dr. Meera, this is Vera from magicpin. JIDA Oct 2026 p.14 is out. Want the abstract?"
    assert not any(e.startswith("reintroduction") for e in run(body, idx, cat)[0])
    assert any(e.startswith("reintroduction") for e in run(body, idx, cat, has_prior_bot_turns=True)[0])


def test_requalifying_in_action_mode_fails(dataset):
    idx, cat = _idx(dataset, "trg_013_corporate_thali_planning")
    errors, _ = run("Great! Do you want 3 tiers or 2? What if we start with lunch only?", idx, cat, mode="action")
    assert any(e.startswith("requalifying") for e in errors)


def test_empty_body_fails(dataset):
    idx, cat = _idx(dataset, "trg_001_research_digest_dentists")
    assert run("   ", idx, cat)[0][0].startswith("empty_body")


def test_hindi_preference_is_a_warning_not_an_error(dataset):
    idx, cat = _idx(dataset, "trg_001_research_digest_dentists")
    errors, warnings = run(GOOD_MEERA, idx, cat, prefers_hindi=True)
    assert errors == [] and any(w.startswith("language") for w in warnings)
    hinglish = GOOD_MEERA.replace("has one for your", "mein aapke").replace("Want me to", "Kya main")
    assert not any(w.startswith("language") for w in run(hinglish, idx, cat, prefers_hindi=True)[1])
