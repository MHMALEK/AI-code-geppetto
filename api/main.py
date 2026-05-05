import asyncio
import json
import threading
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from api.models import (
    TaskCreate, init_db,
    create_task, get_task, list_tasks,
    update_status, append_event,
)
from agent.runner import run_agent

app = FastAPI(title="Geppetto")
_screenshots = Path("data/screenshots")
_screenshots.mkdir(parents=True, exist_ok=True)
app.mount("/screenshots", StaticFiles(directory=str(_screenshots)), name="screenshots")
init_db()


# ── Background agent runner ───────────────────────────────────────────────────

def _run_task(task_id: str, description: str, jira_key: str | None):
    from config import JIRA_TRANSITION_IN_PROGRESS, JIRA_TRANSITION_IN_REVIEW
    from api.jira import transition_issue, add_comment

    def emit(event: dict):
        append_event(task_id, event)
        if event["type"] in ("complete", "error"):
            status = "completed" if event["type"] == "complete" else "failed"
            update_status(task_id, status)
            if jira_key and status == "completed":
                try:
                    transition_issue(jira_key, JIRA_TRANSITION_IN_REVIEW)
                    add_comment(jira_key, f"🤖 Geppetto completed this task.\n{event.get('message', '')}")
                except Exception:
                    pass

    update_status(task_id, "running")

    if jira_key:
        try:
            transition_issue(jira_key, JIRA_TRANSITION_IN_PROGRESS)
        except Exception:
            pass

    try:
        run_agent(task_id, description, emit)
    except Exception as e:
        emit({"type": "error", "message": str(e)})


# ── Task routes ───────────────────────────────────────────────────────────────

@app.post("/tasks", status_code=201)
def create_new_task(data: TaskCreate):
    task = create_task(data)
    description = f"Task: {data.title}\n\n{data.description}"
    if data.jira_id:
        description = f"[{data.jira_id}] {description}"
    threading.Thread(
        target=_run_task, args=(task.id, description, data.jira_id), daemon=True
    ).start()
    return task


@app.get("/tasks")
def get_all_tasks():
    return list_tasks()


@app.get("/tasks/{task_id}")
def get_single_task(task_id: str):
    task = get_task(task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    return task


@app.get("/tasks/{task_id}/stream")
async def stream_task(task_id: str):
    task = get_task(task_id)
    if not task:
        raise HTTPException(404, "Task not found")

    async def event_stream():
        last = 0
        while True:
            t = get_task(task_id)
            if not t:
                break
            for ev in t.events[last:]:
                yield f"data: {json.dumps(ev)}\n\n"
                last += 1
            if t.status in ("completed", "failed") and last >= len(t.events):
                break
            await asyncio.sleep(0.4)

    return StreamingResponse(event_stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ── Jira routes ───────────────────────────────────────────────────────────────

@app.get("/jira/issues")
def jira_issues():
    from api.jira import list_issues
    try:
        return list_issues()
    except Exception as e:
        raise HTTPException(502, f"Jira error: {e}")


@app.get("/jira/issues/{key}")
def jira_issue(key: str):
    from api.jira import get_issue
    try:
        return get_issue(key)
    except Exception as e:
        raise HTTPException(502, f"Jira error: {e}")


# ── Stats ─────────────────────────────────────────────────────────────────────

@app.get("/stats")
def get_stats() -> dict[str, Any]:
    """Aggregate metrics across all task runs."""
    tasks = list_tasks()
    total = len(tasks)

    by_status: dict[str, int] = {}
    total_tokens = 0
    total_cost = 0.0
    total_duration = 0.0
    total_tool_calls = 0
    tool_freq: dict[str, int] = {}

    for task in tasks:
        by_status[task.status] = by_status.get(task.status, 0) + 1
        for ev in task.events:
            if ev.get("type") == "stats":
                total_tokens    += ev.get("total_tokens", 0)
                total_cost      += ev.get("cost_usd", 0.0)
                total_duration  += ev.get("duration_s", 0.0)
                total_tool_calls += ev.get("tool_calls", 0)
            if ev.get("type") == "tool_call":
                t = ev.get("tool", "unknown")
                tool_freq[t] = tool_freq.get(t, 0) + 1

    completed = by_status.get("completed", 0)
    return {
        "total_tasks":     total,
        "by_status":       by_status,
        "success_rate":    round(completed / total * 100, 1) if total else 0,
        "total_tokens":    total_tokens,
        "total_cost_usd":  round(total_cost, 4),
        "total_duration_s": round(total_duration, 1),
        "avg_duration_s":  round(total_duration / completed, 1) if completed else 0,
        "total_tool_calls": total_tool_calls,
        "tool_frequency":  dict(sorted(tool_freq.items(), key=lambda x: -x[1])),
        "recent_tasks": [
            {
                "id":         t.id,
                "title":      t.title,
                "status":     t.status,
                "jira_id":    t.jira_id,
                "created_at": t.created_at,
            }
            for t in tasks[:20]
        ],
    }


# ── Generic webhook (n8n / Jira / any HTTP caller) ────────────────────────────

@app.post("/webhook", status_code=201)
def webhook(payload: dict) -> dict:
    """
    Accepts task triggers from n8n, Jira webhooks, or any HTTP caller.

    Supported payload shapes
    ────────────────────────
    Generic:   {"title": "...", "description": "...", "jira_id": "PROJ-123"}
    Jira:      {"issue": {"key": "PROJ-123", "fields": {"summary": "...", "description": "..."}}}
    n8n pass:  any of the above wrapped by an n8n HTTP Request node
    """
    if "issue" in payload:
        # Jira webhook format
        issue  = payload["issue"]
        fields = issue.get("fields") or {}
        desc   = fields.get("description") or ""
        if isinstance(desc, dict):
            # Jira description is sometimes Atlassian Document Format (ADF)
            desc = desc.get("text") or str(desc)
        title   = fields.get("summary") or "Jira Task"
        jira_id = issue.get("key") or None
    else:
        title   = payload.get("title") or "Webhook Task"
        desc    = payload.get("description") or ""
        jira_id = payload.get("jira_id") or None

    data = TaskCreate(title=title, description=desc, jira_id=jira_id)
    task = create_task(data)
    full_desc = f"Task: {data.title}\n\n{data.description}"
    if data.jira_id:
        full_desc = f"[{data.jira_id}] {full_desc}"

    threading.Thread(
        target=_run_task, args=(task.id, full_desc, data.jira_id), daemon=True
    ).start()

    return {
        "task_id":    task.id,
        "status":     "queued",
        "stream_url": f"/tasks/{task.id}/stream",
        "poll_url":   f"/tasks/{task.id}",
    }


# ── Dashboard ─────────────────────────────────────────────────────────────────

_dashboard = Path(__file__).parent.parent / "dashboard"


@app.get("/", response_class=HTMLResponse)
def root():
    return (_dashboard / "index.html").read_text()
