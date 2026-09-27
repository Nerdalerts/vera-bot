"""Phase-4 replay scenarios + the exact probes judge_simulator.py runs."""

import json

from tests.conftest import ScriptedLLM, grounded_writer
from tests.test_endpoints import NOW, push_all

MID = "m_001_drmeera_dentist_delhi"
AUTO = "Thank you for contacting us! Our team will respond shortly."


def reply(c, conv, msg, turn, mid=MID):
    return c.post("/v1/reply", json={"conversation_id": conv, "merchant_id": mid, "customer_id": None,
                                     "from_role": "merchant", "message": msg, "received_at": NOW,
                                     "turn_number": turn}).json()


def test_simulator_auto_reply_probe_new_conversation_each_turn(client_with, dataset):
    # judge_simulator.py uses conv_auto_1..4 — a per-conversation counter would never fire
    c = client_with(ScriptedLLM(grounded_writer))
    push_all(c, dataset)
    actions = [reply(c, f"conv_auto_{i}", AUTO, i + 1)["action"] for i in range(1, 5)]
    assert actions[0] == "wait" and actions[1] == "end"
    assert "send" not in actions


def test_phase4_auto_reply_hell_same_conversation(client_with, dataset):
    fake = ScriptedLLM(grounded_writer)
    c = client_with(fake)
    push_all(c, dataset)
    canned = "Aapki jaankari ke liye bahut-bahut shukriya. Main aapki yeh sabhi baatein hamari team tak pahuncha deti hoon."
    out = [reply(c, "conv_hell", canned, t) for t in range(2, 6)]
    assert [o["action"] for o in out] == ["wait", "end", "end", "end"]
    assert fake.calls == []  # never spent an LLM call on a machine


def test_intent_transition_switches_to_action(client_with, dataset):
    fake = ScriptedLLM(grounded_writer)
    c = client_with(fake)
    push_all(c, dataset)
    r = reply(c, "conv_intent_1", "Ok lets do it. Whats next?", 2)
    assert r["action"] == "send"
    body = r["body"].lower()
    assert not any(q in body for q in ["would you", "do you", "can you tell", "what if", "how about"])
    assert any(a in body for a in ["done", "sending", "draft", "here", "confirm", "proceed", "next"])
    assert fake.calls[-1]["mode"] == "action"


def test_requalifying_draft_is_repaired_into_action(client_with, dataset):
    requalify = lambda p: json.dumps({"body": "Great! Do you want lunch-only or dinner too? How about a trial week first?", "cta": "open_ended", "rationale": "x"})
    fake = ScriptedLLM(requalify, grounded_writer)
    c = client_with(fake)
    push_all(c, dataset)
    r = reply(c, "conv_intent_2", "haan chalo, kar do", 3)
    assert r["action"] == "send" and "do you want" not in r["body"].lower()
    assert any("requalifying" in v for v in fake.calls[-1]["repair"]["violations"])


def test_bare_yes_after_binary_cta_is_intent(client_with, dataset):
    fake = ScriptedLLM(grounded_writer)
    c = client_with(fake)
    push_all(c, dataset, ["trg_004_perf_dip_bharat"])
    acts = c.post("/v1/tick", json={"now": NOW, "available_triggers": ["trg_004_perf_dip_bharat"]}).json()["actions"]
    conv = acts[0]["conversation_id"]
    r = reply(c, conv, "Yes", 2, mid="m_002_bharat_dentist_mumbai")
    assert r["action"] == "send" and fake.calls[-1]["mode"] == "action"
    # the original trigger travels with the conversation, so the reply stays on-topic
    assert fake.calls[-1]["contexts"]["trigger"]["id"] == "trg_004_perf_dip_bharat"


def test_simulator_hostile_probe_ends(client_with, dataset):
    c = client_with(ScriptedLLM(grounded_writer))
    push_all(c, dataset)
    r = reply(c, "conv_hostile", "Stop messaging me. This is useless spam.", 2)
    assert r["action"] == "end"
    # and no further proactive outreach to that merchant
    c.post("/v1/context", json={"scope": "trigger", "context_id": "trg_001_research_digest_dentists", "version": 1,
                                "payload": dataset[3]["trg_001_research_digest_dentists"], "delivered_at": NOW})
    assert c.post("/v1/tick", json={"now": NOW, "available_triggers": ["trg_001_research_digest_dentists"]}).json() == {"actions": []}


def test_phase4_abuse_then_offtopic_stays_polite_and_on_mission(client_with, dataset):
    fake = ScriptedLLM(grounded_writer)
    c = client_with(fake)
    push_all(c, dataset)
    r1 = reply(c, "conv_h2", "You people are useless, bakwas service", 2)
    assert r1["action"] == "send" and "sorry" in r1["body"].lower()
    assert fake.calls[-1]["mode"] == "deescalate"
    r2 = reply(c, "conv_h2", "can you also help me file my GST?", 3)
    assert r2["action"] in ("send", "end")
    assert fake.calls[-1]["mode"] == "deescalate"
    r3 = reply(c, "conv_h2", "idiot bot", 4)
    assert r3["action"] == "end"  # second abuse: exit


def test_decline_and_later(client_with, dataset):
    c = client_with(ScriptedLLM(grounded_writer))
    push_all(c, dataset)
    assert reply(c, "conv_d", "Not interested", 2)["action"] == "end"
    r = reply(c, "conv_l", "busy abhi, kal baat karte hain", 2)
    assert r["action"] == "wait" and r["wait_seconds"] == 86400


def test_llm_down_at_intent_still_confirms_action(client_with, dataset):
    def boom(_):
        raise RuntimeError("provider down")
    c = client_with(ScriptedLLM(boom))
    push_all(c, dataset)
    r = reply(c, "conv_i3", "Let's do it", 2)
    assert r["action"] == "send" and r["body"].lower().startswith("done")


def test_reply_never_repeats_itself(client_with, dataset):
    c = client_with(ScriptedLLM(grounded_writer))
    push_all(c, dataset)
    bodies = []
    for t, m in enumerate(["What does this cost?", "What does this cost again?", "And the timing?"], start=2):
        r = reply(c, "conv_rep", m, t)
        if r["action"] == "send":
            bodies.append(r["body"])
    assert len(bodies) == len(set(bodies))
