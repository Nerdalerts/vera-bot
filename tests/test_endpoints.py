"""HTTP contract + operational-penalty table (testing brief §2, §10)."""

import json
import time

from tests.conftest import ScriptedLLM, grounded_writer

NOW = "2026-04-26T10:30:00Z"


def push_all(client, dataset, triggers=()):
    cats, merchants, customers, trigs = dataset
    for slug, c in cats.items():
        assert client.post("/v1/context", json={"scope": "category", "context_id": slug, "version": 1, "payload": c, "delivered_at": NOW}).status_code == 200
    for mid, m in merchants.items():
        client.post("/v1/context", json={"scope": "merchant", "context_id": mid, "version": 1, "payload": m, "delivered_at": NOW})
    for cid, c in customers.items():
        client.post("/v1/context", json={"scope": "customer", "context_id": cid, "version": 1, "payload": c, "delivered_at": NOW})
    for tid in triggers:
        client.post("/v1/context", json={"scope": "trigger", "context_id": tid, "version": 1, "payload": trigs[tid], "delivered_at": NOW})


def test_context_idempotency_and_errors(client_with, dataset):
    c = client_with(ScriptedLLM(grounded_writer))
    cats = dataset[0]
    body = {"scope": "category", "context_id": "dentists", "version": 2, "payload": cats["dentists"], "delivered_at": NOW}
    assert c.post("/v1/context", json=body).json()["accepted"] is True
    r = c.post("/v1/context", json=body)  # same version again: idempotent no-op
    assert r.status_code == 200 and r.json()["accepted"] is True and r.json()["duplicate"] is True
    r = c.post("/v1/context", json={**body, "version": 1})  # older
    assert r.status_code == 409 and r.json()["current_version"] == 2
    r = c.post("/v1/context", json={**body, "version": 3})  # newer replaces
    assert r.status_code == 200 and r.json()["accepted"]
    r = c.post("/v1/context", json={**body, "scope": "planet"})
    assert r.status_code == 400 and r.json()["reason"] == "invalid_scope"
    r = c.post("/v1/context", json={"scope": "merchant"})  # malformed -> JSON 400, not an HTML 422
    assert r.status_code == 400 and r.json()["accepted"] is False


def test_healthz_counts_and_metadata(client_with, dataset):
    c = client_with(ScriptedLLM(grounded_writer))
    push_all(c, dataset)
    h = c.get("/v1/healthz").json()
    assert h["status"] == "ok" and h["contexts_loaded"] == {"category": 5, "merchant": 10, "customer": 15, "trigger": 0}
    md = c.get("/v1/metadata").json()
    assert {"team_name", "team_members", "model", "approach", "contact_email", "version", "submitted_at"} <= md.keys()


def test_tick_returns_well_formed_actions_and_respects_cadence(client_with, dataset):
    fake = ScriptedLLM(grounded_writer)
    c = client_with(fake)
    tids = ["trg_001_research_digest_dentists", "trg_002_compliance_dci_radiograph",
            "trg_003_recall_due_priya", "trg_004_perf_dip_bharat"]
    push_all(c, dataset, tids)
    acts = c.post("/v1/tick", json={"now": NOW, "available_triggers": tids + ["trg_does_not_exist"]}).json()["actions"]
    by_trigger = {a["trigger_id"]: a for a in acts}
    # Meera has two merchant-facing triggers: only the more urgent (DCI, urgency 4) goes this tick
    assert "trg_002_compliance_dci_radiograph" in by_trigger and "trg_001_research_digest_dentists" not in by_trigger
    # customer-facing recall goes to a different recipient, so it is allowed alongside
    assert by_trigger["trg_003_recall_due_priya"]["send_as"] == "merchant_on_behalf"
    assert by_trigger["trg_004_perf_dip_bharat"]["send_as"] == "vera"
    keys = {"conversation_id", "merchant_id", "customer_id", "send_as", "trigger_id", "template_name",
            "template_params", "body", "cta", "suppression_key", "rationale"}
    for a in acts:
        assert keys <= a.keys() and a["body"].strip()
    assert len({a["conversation_id"] for a in acts}) == len(acts)

    # next tick: already-sent triggers are suppressed, the deferred one now goes
    acts2 = c.post("/v1/tick", json={"now": NOW, "available_triggers": tids}).json()["actions"]
    assert [a["trigger_id"] for a in acts2] == ["trg_001_research_digest_dentists"]


def test_tick_never_times_out_when_llm_hangs(client_with, dataset, monkeypatch):
    from vera import config
    monkeypatch.setattr(config, "TICK_BUDGET_SECONDS", 1.5)
    c = client_with(ScriptedLLM(grounded_writer, delay=30))
    push_all(c, dataset)
    c.post("/v1/context", json={"scope": "trigger", "context_id": "trg_004_perf_dip_bharat", "version": 1,
                                "payload": dataset[3]["trg_004_perf_dip_bharat"], "delivered_at": NOW})
    t0 = time.monotonic()
    r = c.post("/v1/tick", json={"now": NOW, "available_triggers": ["trg_004_perf_dip_bharat"]})
    assert time.monotonic() - t0 < 3 and r.json() == {"actions": []}


def test_tick_survives_llm_exceptions(client_with, dataset):
    def boom(_):
        raise RuntimeError("provider 500")
    c = client_with(ScriptedLLM(boom))
    push_all(c, dataset, ["trg_004_perf_dip_bharat"])
    r = c.post("/v1/tick", json={"now": NOW, "available_triggers": ["trg_004_perf_dip_bharat"]})
    assert r.status_code == 200 and r.json() == {"actions": []}


def test_informed_repair_fixes_an_ungrounded_draft(client_with, dataset):
    def invents(p):
        return json.dumps({"body": "Dr. Bharat, calls fell 73% — a new clinic 0.4km away is taking them. Want a fix?",
                           "cta": "binary", "rationale": "x"})
    fake = ScriptedLLM(invents, grounded_writer)
    c = client_with(fake)
    push_all(c, dataset, ["trg_004_perf_dip_bharat"])
    acts = c.post("/v1/tick", json={"now": NOW, "available_triggers": ["trg_004_perf_dip_bharat"]}).json()["actions"]
    assert len(acts) == 1 and "73%" not in acts[0]["body"]
    repair = fake.calls[-1]["repair"]
    assert any("73%" in v for v in repair["violations"])  # the model was told exactly what was wrong


def test_persistently_ungrounded_model_sends_nothing(client_with, dataset):
    bad = lambda p: json.dumps({"body": "Flat 30% off for all your patients! Hurry!", "cta": "binary", "rationale": "x"})
    c = client_with(ScriptedLLM(bad))
    push_all(c, dataset, ["trg_004_perf_dip_bharat"])
    assert c.post("/v1/tick", json={"now": NOW, "available_triggers": ["trg_004_perf_dip_bharat"]}).json() == {"actions": []}


def test_same_inputs_same_output(client_with, dataset):
    fake = ScriptedLLM(grounded_writer)
    c = client_with(fake)
    from bot import _compose  # noqa
    from vera.composer import compose
    import asyncio
    cats, merchants, customers, trigs = dataset
    t = trigs["trg_001_research_digest_dentists"]; m = merchants[t["merchant_id"]]
    async def twice():
        a = await compose(cats["dentists"], m, t, None, now=NOW, deadline=time.monotonic() + 5)
        b = await compose(cats["dentists"], m, t, None, now=NOW, deadline=time.monotonic() + 5)
        return a, b
    a, b = asyncio.run(twice())
    assert a.message == b.message and len(fake.calls) == 1


def test_precompose_on_context_push_makes_tick_instant(client_with, dataset):
    fake = ScriptedLLM(grounded_writer, delay=0.3)
    c = client_with(fake)
    push_all(c, dataset, ["trg_004_perf_dip_bharat"])
    time.sleep(0.8)  # background warm-up finishes
    t0 = time.monotonic()
    acts = c.post("/v1/tick", json={"now": NOW, "available_triggers": ["trg_004_perf_dip_bharat"]}).json()["actions"]
    assert len(acts) == 1 and time.monotonic() - t0 < 0.25 and len(fake.calls) == 1
