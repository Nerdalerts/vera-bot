#!/usr/bin/env bash
# Hits every endpoint once against a running bot. Usage: BOT_URL=http://localhost:8080 scripts/smoke.sh
set -euo pipefail
U=${BOT_URL:-http://localhost:8080}
DS=$(cd "$(dirname "$0")/.." && pwd)/dataset
j() { python3 -c "import json,sys; print(json.dumps(json.load(open(sys.argv[1]))$2))" "$1"; }
echo "healthz:  $(curl -fsS $U/v1/healthz)"
echo "metadata: $(curl -fsS $U/v1/metadata | head -c 120)..."
curl -fsS -X POST $U/v1/context -H 'content-type: application/json' \
  -d "{\"scope\":\"category\",\"context_id\":\"dentists\",\"version\":1,\"payload\":$(j $DS/categories/dentists.json ''),\"delivered_at\":\"2026-04-26T10:00:00Z\"}" >/dev/null
curl -fsS -X POST $U/v1/context -H 'content-type: application/json' \
  -d "{\"scope\":\"merchant\",\"context_id\":\"m_001_drmeera_dentist_delhi\",\"version\":1,\"payload\":$(j $DS/merchants_seed.json "['merchants'][0]"),\"delivered_at\":\"2026-04-26T10:00:00Z\"}" >/dev/null
curl -fsS -X POST $U/v1/context -H 'content-type: application/json' \
  -d "{\"scope\":\"trigger\",\"context_id\":\"trg_001_research_digest_dentists\",\"version\":1,\"payload\":$(j $DS/triggers_seed.json "['triggers'][0]"),\"delivered_at\":\"2026-04-26T10:00:00Z\"}" >/dev/null
echo "contexts pushed; ticking..."
time curl -fsS -X POST $U/v1/tick -H 'content-type: application/json' \
  -d '{"now":"2026-04-26T10:30:00Z","available_triggers":["trg_001_research_digest_dentists"]}' | python3 -m json.tool
echo "reply (auto-reply probe):"
curl -fsS -X POST $U/v1/reply -H 'content-type: application/json' \
  -d '{"conversation_id":"smoke_1","merchant_id":"m_001_drmeera_dentist_delhi","from_role":"merchant","message":"Thank you for contacting us! Our team will respond shortly.","received_at":"2026-04-26T10:35:00Z","turn_number":2}'
echo
