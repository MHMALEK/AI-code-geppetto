# n8n ↔ Geppetto Integration

## Quick start

```bash
# 1. Run n8n locally (persists data in ~/n8n-data)
docker run -it --rm \
  -p 5678:5678 \
  -v ~/n8n-data:/home/node/.n8n \
  n8nio/n8n

# 2. Open http://localhost:5678 → Settings → Import Workflow → geppetto-workflow.json
# 3. Activate the workflow
# 4. Start Geppetto: uvicorn api.main:app --reload
```

## Trigger manually (test)

```bash
curl -X POST http://localhost:5678/webhook/geppetto \
  -H "Content-Type: application/json" \
  -d '{
    "title": "Add dark-mode toggle",
    "description": "Add a dark/light theme toggle to the top navigation bar",
    "jira_id": "SCRUM-42"
  }'
```

## How the workflow works

```
[Webhook POST] → Create Task (POST /webhook) → Store task_id
       ↓
   Wait 8s → Poll GET /tasks/{id} → Still running? ──yes──→ Wait 8s (loop)
                                           │
                                          no
                                           ↓
                                    Completed? ──yes──→ ✅ Success node
                                              ──no───→ ❌ Failed node
```

The Success/Failed nodes are intentionally left as Set nodes — connect them
to whatever fits your stack: Slack, email, Jira comment, PagerDuty, etc.

## Jira webhook variant

Set the webhook URL in your Jira project:
`Project settings → Webhooks → Create → URL: http://<your-ip>:5678/webhook/geppetto`

Geppetto's `/webhook` endpoint automatically parses Jira's payload format:
```json
{
  "issue": {
    "key": "PROJ-123",
    "fields": {
      "summary": "Add pagination to DataTable",
      "description": "..."
    }
  }
}
```

## Swap to LangGraph runner

In `api/main.py`, change the import:
```python
# from agent.runner       import run_agent          # for-loop version
from agent.runner_graph import run_agent_graph as run_agent   # LangGraph version
```

Both have identical signatures — the only difference is internal structure.
