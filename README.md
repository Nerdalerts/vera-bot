# Vera: magicpin merchant-assistant bot

This bot is an HTTP service that meets the magicpin AI Challenge contract (`/v1/context`, `/v1/tick`, `/v1/reply`, `/v1/healthz`, `/v1/metadata`, plus `/v1/teardown`). It writes WhatsApp messages to merchants and their customers that are specific, grounded, and in the right voice. It also handles multi-turn replies: auto-replies, intent hand-offs, hostility, and opt-outs.

**The core idea:** deterministic code decides *whether and how* to act, the LLM only writes the words, and a deterministic validator decides whether those words may leave the process.

```
/v1/context ──► ContextStore (versioned, idempotent) ──► on trigger push: pre-compose in background
/v1/tick ────► candidate filter (suppression, opt-out, consent, 1 msg/recipient/tick, urgency rank)
                 └─► evidence brief (code) ─► LLM @ temp 0 ─► validator ─► informed repair ─► action / restraint
/v1/reply ───► reply classifier (code): opt-out · auto-reply · intent · hostile · off-topic · decline · later · question
                 ├─► deterministic exits: end / wait (no LLM spent on machines or "no")
                 └─► compose in the right mode: action · answer · deescalate · continue
```

## Quick start (local)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env          # add ANTHROPIC_API_KEY (or an OpenAI-compatible key) + team fields
uvicorn bot:app --host 0.0.0.0 --port 8080 --workers 1
```

Check it:

```bash
BOT_URL=http://localhost:8080 scripts/smoke.sh     # hits every endpoint once
python -m pytest -q                                 # 39 tests, no API key needed (LLM is stubbed)
```

Run magicpin's simulator: put `judge_simulator.py` next to a `dataset/` folder, set `BOT_URL`, `LLM_PROVIDER` and `LLM_API_KEY` at the top of the file, then run `python judge_simulator.py`.

Generate the 30-line submission with your configured model:

```bash
python scripts/make_submission.py            # writes submission.jsonl (expands the dataset on first run)
```

`submission.reference.jsonl` is a baseline set written by hand against the same briefs. Every line has passed this bot's validator. Use it to compare against what your model produces, and submit the file your deployed model generates.

## Deploy (any public HTTPS URL works)

Whichever host you use, three rules apply:
1. Run **one worker**. All state lives in process memory.
2. Keep the service **always on**. Free tiers that sleep will fail `/v1/healthz`, and three failures in a row disqualify the slot.
3. Set the API key as a secret, never in the image.

| Target | Steps |
|---|---|
| **Render** | Push this folder to GitHub → New → Blueprint → pick the repo (`render.yaml` is included) → set `ANTHROPIC_API_KEY`, `TEAM_*` → deploy. URL: `https://<name>.onrender.com`. Use a paid instance, not the free one that sleeps. |
| **Railway** | New project → Deploy from GitHub. It detects the `Dockerfile`. Add the env vars, then under Settings → Networking choose "Generate Domain". |
| **Fly.io** | `fly launch --no-deploy` (keep the included `fly.toml`, change `app`) → `fly secrets set ANTHROPIC_API_KEY=...` → `fly deploy`. The machine is set to stay on in the Mumbai region (`bom`). |
| **Docker anywhere** | `docker build -t vera . && docker run -p 8080:8080 --env-file .env vera` |
| **Quick demo** | Run locally, then `ngrok http 8080` and submit the https URL. Fine for a trial run, but risky for the scored window. |

After deploying: `BOT_URL=https://<your-host> scripts/smoke.sh`, then submit the URL.

## Configuration (`.env`)

| Var | Default | Notes |
|---|---|---|
| `LLM_PROVIDER` | `anthropic` | `anthropic` or `openai` (covers any OpenAI-compatible API: OpenAI, Groq, OpenRouter, DeepSeek, Gemini's `/openai` endpoint) |
| `LLM_MODEL` | `claude-sonnet-5` | Any chat model. A frontier model gives the best copy. |
| `TICK_BUDGET_SECONDS` / `REPLY_BUDGET_SECONDS` | `13` | The local simulator times out at 15s. The real judge allows 30s, so you can raise these to ~25 there. |
| `LLM_CALL_TIMEOUT` | `10` | Per attempt |
| `LLM_CONCURRENCY` | `6` | Global cap on simultaneous LLM calls (protects rate limits) |
| `TEAM_NAME`, `TEAM_MEMBERS`, `CONTACT_EMAIL` | — | Returned by `/v1/metadata` |

## Layout

```
bot.py                     HTTP endpoints + compose() from brief §7.1
conversation_handlers.py   respond() from brief §7.4 (same code path as /v1/reply)
vera/brief.py              evidence brief: digest lookup, date math, % vs peers, language, salutation, playbooks
vera/composer.py           cache → in-flight dedupe → LLM → validator → informed repair (never raises, honours deadline)
vera/validator.py          grounding (numbers typed as %, ₹, plain; acronyms; citations; URLs), generic copy, taboo, CTA shape, jargon, repeats, re-intro, re-qualifying
vera/classifier.py         reply reading: opt-out, auto-reply (EN + Hinglish), intent, hostile, off-topic, decline, later, question
vera/llm.py                Anthropic + OpenAI-compatible adapters over httpx (async, no SDK pins)
vera/store.py              versioned context store, conversations, suppression, opt-outs, cache
prompts/composer_system_prompt.md   the single prompt; edit here, not in code
scripts/make_submission.py submission.jsonl for the 30 canonical pairs, through the same pipeline
tests/                     contract, timeout/chaos, replay scenarios, validator rules, 30-pair reference set
```
