import asyncio
import json
import threading
from pathlib import Path

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
init_db()


# ── Background task runner ────────────────────────────────────────────────────

def _run_task(task_id: str, description: str):
    def emit(event: dict):
        append_event(task_id, event)
        if event["type"] in ("complete", "error"):
            update_status(task_id, "completed" if event["type"] == "complete" else "failed")

    update_status(task_id, "running")
    try:
        run_agent(task_id, description, emit)
    except Exception as e:
        emit({"type": "error", "message": str(e)})


# ── Routes ────────────────────────────────────────────────────────────────────

@app.post("/tasks", status_code=201)
def create_new_task(data: TaskCreate):
    task = create_task(data)

    description = f"Task: {data.title}\n\n{data.description}"
    if data.jira_id:
        description = f"[{data.jira_id}] {description}"

    t = threading.Thread(target=_run_task, args=(task.id, description), daemon=True)
    t.start()

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
    """
    SSE endpoint. Replays stored events then polls SQLite every 500ms for new ones.
    Simple but sufficient for a demo — no in-memory queue complexity.
    """
    task = get_task(task_id)
    if not task:
        raise HTTPException(404, "Task not found")

    async def event_stream():
        last = 0
        while True:
            t = get_task(task_id)
            if not t:
                break

            new_events = t.events[last:]
            for ev in new_events:
                yield f"data: {json.dumps(ev)}\n\n"
                last += 1

            if t.status in ("completed", "failed") and last >= len(t.events):
                break

            await asyncio.sleep(0.5)

    return StreamingResponse(event_stream(), media_type="text/event-stream")


# ── Static dashboard ──────────────────────────────────────────────────────────

_dashboard = Path(__file__).parent.parent / "dashboard"


@app.get("/", response_class=HTMLResponse)
def root():
    return (_dashboard / "index.html").read_text()
