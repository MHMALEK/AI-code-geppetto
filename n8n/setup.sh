#!/usr/bin/env bash
# Sets up the Geppetto workflow in n8n.
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

# ── 2. Import workflows via n8n CLI ──────────────────────────────────────────
import_workflow() {
  local file="$1" label="$2"
  echo "Importing $label..."
  docker compose exec -T n8n \
    n8n import:workflow --input="/home/node/.n8n/workflows/$file" 2>/dev/null \
    && echo "  ✓ $label" || echo "  ✗ $label (may already exist — continuing)"
}

import_workflow "geppetto-workflow.json"    "Webhook workflow"
import_workflow "slack-workflow.json"       "Slack slash command workflow"

# ── 3. Activate workflows via REST API ───────────────────────────────────────
echo "Activating workflows..."
WORKFLOW_IDS=$(curl -sf "$N8N_URL/api/v1/workflows" \
  -H "Accept: application/json" | jq -r '.data[].id // empty' 2>/dev/null || true)

if [[ -z "$WORKFLOW_IDS" ]]; then
  echo ""
  echo "  Could not read workflow IDs from REST API."
  echo "  Open $N8N_URL and activate each workflow manually."
else
  while IFS= read -r wid; do
    [[ -z "$wid" ]] && continue
    curl -sf -X PATCH "$N8N_URL/api/v1/workflows/$wid" \
      -H "Content-Type: application/json" \
      -d '{"active":true}' >/dev/null
    echo "  Activated workflow: $wid"
  done <<< "$WORKFLOW_IDS"
fi

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
