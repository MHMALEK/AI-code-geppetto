# n8n ↔ Geppetto Integration

Two workflows are included:

| Workflow | File | Trigger |
|---|---|---|
| Webhook / Jira | `geppetto-workflow.json` | `POST /webhook/geppetto` |
| **Slack slash command** | `slack-workflow.json` | `/geppetto <task>` in Slack |

---

## Quick start

```bash
# 1. Start the full stack
docker compose up -d

# 2. Import + activate both workflows automatically
./n8n/setup.sh

# 3. Open n8n
open http://localhost:5678
```

---

## Slack slash command setup

### What it does

```
/geppetto Add a dark-mode toggle to the navbar
```

↓ immediate reply in Slack:

```
⏳ On it! Running Geppetto on: *Add a dark-mode toggle to the navbar*
```

↓ after the agent finishes (~1–2 min):

```
✅ Done! — Add a dark-mode toggle to the navbar
> ⏱ 82s  ·  💰 $0.08  ·  🔧 14 tool calls
> 🔗 View Pull Request
> 📊 Dashboard
```

### Create the Slack app (one-time)

1. Go to **https://api.slack.com/apps** → **Create New App** → **From scratch**
2. Name it `Geppetto`, pick your workspace → **Create App**
3. In the left sidebar: **Slash Commands** → **Create New Command**
   - Command: `/geppetto`
   - Request URL: `http://<your-server-ip>:5678/webhook/geppetto-slack`
   - Short Description: `Run the Geppetto coding agent`
   - Usage Hint: `[task description]`
   - → **Save**
4. **Install App** → **Install to Workspace** → Allow
5. Done — type `/geppetto` in any channel

> **Local dev tip**: use [ngrok](https://ngrok.com) to expose n8n publicly:
> ```bash
> ngrok http 5678
> # Use the https ngrok URL as the slash command Request URL
> ```

---

## Webhook trigger (generic / Jira)

```bash
# Generic task
curl -X POST http://localhost:5678/webhook/geppetto \
  -H "Content-Type: application/json" \
  -d '{"title": "Add dark-mode toggle", "description": "...", "jira_id": "SCRUM-42"}'

# Jira webhook → point your Jira project webhook at:
# POST http://<your-ip>:5678/webhook/geppetto
# Geppetto auto-parses the Jira payload format.
```

---

## Task summary endpoint

Both workflows poll `GET /tasks/{id}/summary` — a clean endpoint that extracts
key fields from the event log without parsing raw events:

```json
{
  "id": "abc-123",
  "title": "Add dark-mode toggle",
  "status": "completed",
  "pr_url": "https://github.com/org/repo/pull/42",
  "branch": "feat/SCRUM-1-dark-mode-toggle",
  "cost_usd": 0.08,
  "duration_s": 82,
  "total_tokens": 12400,
  "tool_calls": 14,
  "dashboard_url": "http://localhost:8000"
}
```

---

## Workflow diagram

```
/geppetto <text>  (Slack slash command)
  ↓
n8n Webhook  →  ACK to Slack ("⏳ On it!")
  ↓
POST /webhook  →  task created
  ↓
Store task_id + response_url
  ↓
Wait 12s → GET /tasks/{id}/summary
  ↓
status = running/pending? ──yes──→ Wait 12s (loop)
  ↓ no
status = completed? ──yes──→ POST success to response_url  ✅
             ──no───→ POST failure to response_url  ❌
```
