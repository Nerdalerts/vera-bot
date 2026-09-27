import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("LLM_PROVIDER", "anthropic")

DATA = ROOT / "dataset"


def load_dataset():
    cats = {json.loads(p.read_text())["slug"]: json.loads(p.read_text()) for p in (DATA / "categories").glob("*.json")}
    merchants = {m["merchant_id"]: m for m in json.loads((DATA / "merchants_seed.json").read_text())["merchants"]}
    customers = {c["customer_id"]: c for c in json.loads((DATA / "customers_seed.json").read_text())["customers"]}
    triggers = {t["id"]: t for t in json.loads((DATA / "triggers_seed.json").read_text())["triggers"]}
    return cats, merchants, customers, triggers


@pytest.fixture(scope="session")
def dataset():
    return load_dataset()


class ScriptedLLM:
    """Stands in for the provider. `script` is a list of callables
    (payload_dict) -> str, consumed in order; the last one repeats."""

    def __init__(self, *script, delay: float = 0.0):
        self.script = list(script)
        self.calls: list[dict] = []
        self.delay = delay

    async def __call__(self, system: str, user: str, timeout: float) -> str:
        import asyncio
        payload = json.loads(user)
        self.calls.append(payload)
        if self.delay:
            await asyncio.sleep(self.delay)
        fn = self.script[min(len(self.calls) - 1, len(self.script) - 1)]
        return fn(payload)


def grounded_writer(payload: dict) -> str:
    """A well-behaved fake model: builds a message only from brief values."""
    b = payload["brief"]
    name = b["address_as"]
    mode = payload["mode"]
    if mode == "action":
        body = f"Done {name} — shuru kar rahi hoon. Pehla draft yahin bhej rahi hoon, aap bas final OK kar dena. Reply YES to publish."
    elif mode == "deescalate":
        body = f"Sorry for the extra messages, {name}. GST filing is outside what I can help with here. Reply STOP and I won't message again."
    else:
        offer = (b.get("merchant_offers_active") or b.get("catalog_offers") or ["your listing"])[0]
        body = f"{name}, aapke liye ek quick update hai — {offer} abhi bhi active hai aur is hafte kaam aa sakta hai. Reply YES aur main draft bhej dungi."
    return json.dumps({"body": body, "cta": "binary", "send_as": "vera", "suppression_key": "x",
                       "template_params": [name], "facts_used": [name], "rationale": f"used brief.address_as={name} and offer"})


@pytest.fixture
def client_with(monkeypatch):
    """Returns a factory: client_with(fake_llm) -> TestClient with fresh state."""
    from fastapi.testclient import TestClient
    from vera import llm
    from vera.store import STATE
    import bot

    opened = []

    def make(fake):
        STATE.reset()
        llm.set_override(fake)
        c = TestClient(bot.app)
        c.__enter__()  # one persistent event loop, like a real server (background tasks survive)
        opened.append(c)
        return c

    yield make
    for c in opened:
        c.__exit__(None, None, None)
    llm.set_override(None)
    STATE.reset()
