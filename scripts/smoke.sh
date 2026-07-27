#!/usr/bin/env bash
# M0 smoke test: liveness, models, missing-model 400, and (if a key is set) a live call.
# Usage: ./scripts/smoke.sh [BASE_URL]
set -euo pipefail

BASE="${1:-http://localhost:8000}"

echo "==> /healthz"
curl -fsS "$BASE/healthz"
echo

echo "==> /v1/models"
curl -fsS "$BASE/v1/models" | head -c 240
echo

echo "==> chat without model (expect 400 + OpenAI error)"
HTTP=$(curl -s -o /tmp/m0_resp.json -w '%{http_code}' -X POST "$BASE/v1/chat/completions" \
  -H 'content-type: application/json' \
  -d '{"messages":[{"role":"user","content":"hi"}]}')
if [ "$HTTP" = "400" ]; then
  echo "missing-model 400 OK"
else
  echo "UNEXPECTED status $HTTP"; cat /tmp/m0_resp.json; exit 1
fi

if [ -z "${OPENAI_API_KEY:-}" ] && [ -z "${DEEPSEEK_API_KEY:-}" ]; then
  echo "==> no provider key set; skipping live chat (DoD live-call requires a key)"
else
  echo "==> live chat (requires network + valid key)"
  curl -fsS -X POST "$BASE/v1/chat/completions" \
    -H 'content-type: application/json' \
    -d '{"model":"gpt-4o-mini","messages":[{"role":"user","content":"say hi in one word"}]}' | head -c 300
  echo
fi

echo "SMOKE OK"
