#!/usr/bin/env bash
# Sets up the Geppetto workflows in n8n via the REST API.
# Run after the stack is started: docker compose up -d
# Usage: ./n8n/setup.sh [--start]
#   --start  also runs `docker compose up -d` before setup

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
N8N_URL="${N8N_URL:-http://localhost:5678}"

cd "$PROJECT_DIR"

# ── Optional: start the stack ─────────────────────────────────────────────────
if [[ "${1:-}" == "--start" ]]; then
  echo "Starting stack..."
  docker compose up -d
  echo ""
fi

# ── 1. Wait for n8n ───────────────────────────────────────────────────────────
printf "Waiting for n8n"
until curl -sf "$N8N_URL/healthz" >/dev/null 2>&1; do
  printf '.'; sleep 2
done
echo " ready."

# ── 2. Resolve API key ────────────────────────────────────────────────────────
if [[ -z "${N8N_API_KEY:-}" ]]; then
  echo ""
  echo "  N8N_API_KEY is not set."
  echo "  To get one: open $N8N_URL → Settings → API → Create API key"
  echo "  Then re-run:  N8N_API_KEY=<key> ./n8n/setup.sh"
  echo ""
  exit 1
fi

# ── 3. Create + activate workflows via REST API ───────────────────────────────
create_and_activate() {
  local file="$1" label="$2"
  echo "Creating $label..."

  # Strip id/active fields so n8n assigns a fresh ID
  local payload
  payload=$(python3 -c "
import json, sys
with open('$file') as f:
    wf = json.load(f)
for k in ['id', 'active', 'versionId', 'meta', 'tags']:
    wf.pop(k, None)
print(json.dumps(wf))
")

  local wid
  wid=$(curl -sf -X POST "$N8N_URL/api/v1/workflows" \
    -H "X-N8N-API-KEY: $N8N_API_KEY" \
    -H "Content-Type: application/json" \
    -d "$payload" | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])" 2>/dev/null || true)

  if [[ -z "$wid" ]]; then
    echo "  ✗ $label — failed to create (check API key or workflow JSON)"
    return
  fi

  curl -sf -X POST "$N8N_URL/api/v1/workflows/$wid/activate" \
    -H "X-N8N-API-KEY: $N8N_API_KEY" >/dev/null

  echo "  ✓ $label (ID: $wid)"
}

create_and_activate "n8n/geppetto-workflow.json" "Webhook workflow"
create_and_activate "n8n/slack-workflow.json"    "Slack slash command workflow"

# ── Done ──────────────────────────────────────────────────────────────────────
echo ""
echo "  n8n:        $N8N_URL"
echo "  Geppetto:   http://localhost:8000"
echo ""
echo "  Webhook trigger test:"
echo "    curl -X POST $N8N_URL/webhook/geppetto \\"
echo "      -H 'Content-Type: application/json' \\"
echo "      -d '{\"title\":\"Add dark-mode toggle\",\"description\":\"Add a theme toggle\",\"jira_id\":\"SCRUM-1\"}'"
echo ""
echo "  Slack slash command endpoint:"
echo "    POST $N8N_URL/webhook/geppetto-slack"
echo "  → Set this URL in your Slack app's slash command config"
echo ""
