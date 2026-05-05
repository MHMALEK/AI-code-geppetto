import asyncio
import json
import re
import threading
import time
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
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
_assets = Path(__file__).parent.parent / "dashboard" / "assets"
_assets.mkdir(parents=True, exist_ok=True)
app.mount("/assets", StaticFiles(directory=str(_assets)), name="assets")
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


# ── Task summary (for n8n / external pollers) ─────────────────────────────────

@app.get("/tasks/{task_id}/summary")
def task_summary(task_id: str) -> dict:
    """
    Lightweight summary of a task — designed for n8n polling nodes.
    Extracts PR URL, branch, cost, and duration from the event log so the
    caller doesn't have to parse raw events.
    """
    task = get_task(task_id)
    if not task:
        raise HTTPException(404, "Task not found")

    pr_url = None
    branch = None
    cost_usd = 0.0
    duration_s = 0
    total_tokens = 0
    tool_calls = 0
    complete_message = ""

    for ev in task.events:
        t = ev.get("type")
        if t == "tool_result" and ev.get("tool") == "push_and_create_pr":
            m = re.search(r"https?://\S+", ev.get("result", ""))
            if m:
                pr_url = m.group(0).rstrip(".")
        if t == "tool_call" and ev.get("tool") == "create_branch":
            branch = ev.get("input", {}).get("branch_name")
        if t == "stats":
            cost_usd      = ev.get("cost_usd", 0.0)
            duration_s    = int(ev.get("duration_s", 0))
            total_tokens  = ev.get("total_tokens", 0)
            tool_calls    = ev.get("tool_calls", 0)
        if t == "complete":
            complete_message = ev.get("message", "")

    return {
        "id":            task.id,
        "title":         task.title,
        "status":        task.status,
        "jira_id":       task.jira_id,
        "pr_url":        pr_url,
        "branch":        branch,
        "cost_usd":      cost_usd,
        "duration_s":    duration_s,
        "total_tokens":  total_tokens,
        "tool_calls":    tool_calls,
        "message":       complete_message,
        "dashboard_url": "http://localhost:8000",
        "created_at":    task.created_at,
        "updated_at":    task.updated_at,
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


# ── Slack slash command (/geppetto <task>) ────────────────────────────────────

def _run_and_notify_slack(task_id: str, description: str, response_url: str, title: str):
    """Run the agent then POST the result back to Slack via response_url."""
    _run_task(task_id, description, None)

    # Poll until terminal (task updated in-place by _run_task)
    for _ in range(60):
        task = get_task(task_id)
        if task and task.status in ("completed", "failed"):
            break
        time.sleep(3)

    task = get_task(task_id)
    if not task:
        return

    if task.status == "completed":
        pr_url = None
        cost_usd = duration_s = tool_calls = 0
        for ev in task.events:
            if ev.get("type") == "tool_result" and ev.get("tool") == "push_and_create_pr":
                m = re.search(r"https?://\S+", ev.get("result", ""))
                if m:
                    pr_url = m.group(0).rstrip(".")
            if ev.get("type") == "stats":
                cost_usd   = ev.get("cost_usd", 0.0)
                duration_s = int(ev.get("duration_s", 0))
                tool_calls = ev.get("tool_calls", 0)

        lines = [f"✅ *Done!* — {title}",
                 f"> ⏱ {duration_s}s  ·  💰 ${cost_usd}  ·  🔧 {tool_calls} tool calls"]
        if pr_url:
            lines.append(f"> 🔗 <{pr_url}|View Pull Request>")
        text = "\n".join(lines)
    else:
        text = f"❌ *Geppetto hit an error* on: {title}\n> Check the dashboard for details."

    try:
        httpx.post(response_url, json={"response_type": "in_channel", "text": text}, timeout=10)
    except Exception:
        pass


@app.post("/slack")
async def slack_slash_command(request: Request):
    """
    Slack slash command endpoint for /geppetto <task>.
    Slack sends a form-encoded POST; we ACK immediately then run the agent
    in the background and post the result back via response_url.
    """
    form = await request.form()
    text         = str(form.get("text", "")).strip()
    response_url = str(form.get("response_url", ""))
    user_name    = str(form.get("user_name", "unknown"))
    channel_name = str(form.get("channel_name", "unknown"))

    if not text:
        return JSONResponse({"response_type": "ephemeral",
                             "text": "Usage: `/geppetto <task description>`"})

    data = TaskCreate(
        title=text,
        description=f"Slack task requested by @{user_name} in #{channel_name}",
    )
    task = create_task(data)
    full_desc = f"Task: {data.title}\n\n{data.description}"

    threading.Thread(
        target=_run_and_notify_slack,
        args=(task.id, full_desc, response_url, text),
        daemon=True,
    ).start()

    return JSONResponse({
        "response_type": "in_channel",
        "text": f"⏳ On it! Running Geppetto on: *{text}*",
    })


# ── Dashboard ─────────────────────────────────────────────────────────────────

_dashboard = Path(__file__).parent.parent / "dashboard"


@app.get("/", response_class=HTMLResponse)
def root():
    return (_dashboard / "index.html").read_text()
