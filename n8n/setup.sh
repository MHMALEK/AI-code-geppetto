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

# ── 2. Import workflow via n8n CLI ────────────────────────────────────────────
echo "Importing workflow..."
docker compose exec -T n8n \
  n8n import:workflow --input=/home/node/.n8n/workflows/geppetto-workflow.json

# ── 3. Activate via REST API ──────────────────────────────────────────────────
echo "Activating workflow..."
WORKFLOW_ID=$(curl -sf "$N8N_URL/api/v1/workflows" \
  -H "Accept: application/json" | jq -r '.data[0].id // empty' 2>/dev/null || true)

if [[ -z "$WORKFLOW_ID" ]]; then
  echo ""
  echo "  Could not read workflow ID from REST API."
  echo "  Open $N8N_URL, find the workflow, and activate it manually."
else
  curl -sf -X PATCH "$N8N_URL/api/v1/workflows/$WORKFLOW_ID" \
    -H "Content-Type: application/json" \
    -d '{"active":true}' >/dev/null
  echo "  Activated (id: $WORKFLOW_ID)"
fi

# ── Done ──────────────────────────────────────────────────────────────────────
echo ""
echo "  n8n:        $N8N_URL"
echo "  Geppetto:   http://localhost:8000"
echo ""
echo "  Test trigger:"
echo "    curl -X POST $N8N_URL/webhook/geppetto \\"
echo "      -H 'Content-Type: application/json' \\"
echo "      -d '{\"title\":\"Add dark-mode toggle\",\"description\":\"Add a theme toggle to the nav bar\",\"jira_id\":\"SCRUM-1\"}'"
echo ""
