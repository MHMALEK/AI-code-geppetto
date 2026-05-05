import asyncio
import json
import threading
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse

from api.models import (
    TaskCreate, init_db,
    create_task, get_task, list_tasks,
    update_status, append_event,
)
from agent.runner import run_agent

app = FastAPI(title="Geppetto")
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


# ── Dashboard ─────────────────────────────────────────────────────────────────

_dashboard = Path(__file__).parent.parent / "dashboard"


@app.get("/", response_class=HTMLResponse)
def root():
    return (_dashboard / "index.html").read_text()
