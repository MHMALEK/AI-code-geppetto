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


# ── Telegram bot webhook ──────────────────────────────────────────────────────

TELEGRAM_HELP_TEXT = "\n".join([
    "codeGeppetto commands:",
    "/task <description> - create and run a task",
    "/jira <summary> - create a Jira issue and run it",
    "/status - show the last 5 tasks",
    "/help - show this command list",
])


def _telegram_send_message(chat_id: int | str, text: str) -> None:
    from config import TELEGRAM_BOT_TOKEN

    if not TELEGRAM_BOT_TOKEN:
        return

    try:
        httpx.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": text},
            timeout=10,
        )
    except Exception:
        pass


def _telegram_command(text: str) -> tuple[str, str]:
    command, _, rest = text.partition(" ")
    return command.split("@", 1)[0].lower(), rest.strip()


def _status_emoji(status: str) -> str:
    return {
        "pending": "⏳",
        "running": "🏃",
        "completed": "✅",
        "failed": "❌",
    }.get(status, "•")


def _telegram_status_text() -> str:
    tasks = list_tasks()[:5]
    if not tasks:
        return "No tasks yet."

    lines = ["Last 5 tasks:"]
    for task in tasks:
        jira = f" [{task.jira_id}]" if task.jira_id else ""
        lines.append(f"{_status_emoji(task.status)}{jira} {task.title} — {task.status}")
    return "\n".join(lines)


def _telegram_task_result(task_id: str, title: str) -> str:
    task = get_task(task_id)
    if not task:
        return f"❌ Task disappeared: {title}"

    if task.status != "completed":
        return f"❌ Geppetto hit an error on: {title}\nCheck the dashboard for details."

    pr_url = None
    cost_usd = duration_s = tool_calls = 0
    for ev in task.events:
        if ev.get("type") == "tool_result" and ev.get("tool") == "push_and_create_pr":
            m = re.search(r"https?://\S+", ev.get("result", ""))
            if m:
                pr_url = m.group(0).rstrip(".")
        if ev.get("type") == "stats":
            cost_usd = ev.get("cost_usd", 0.0)
            duration_s = int(ev.get("duration_s", 0))
            tool_calls = ev.get("tool_calls", 0)

    lines = [
        f"✅ Done: {title}",
        f"⏱ {duration_s}s · 💰 ${cost_usd} · 🔧 {tool_calls} tool calls",
    ]
    if task.jira_id:
        lines.append(f"Jira: {task.jira_id}")
    if pr_url:
        lines.append(f"PR: {pr_url}")
    return "\n".join(lines)


def _run_and_notify_telegram(
    task_id: str,
    description: str,
    jira_key: str | None,
    chat_id: int | str,
    title: str,
) -> None:
    _run_task(task_id, description, jira_key)
    _telegram_send_message(chat_id, _telegram_task_result(task_id, title))


def process_telegram_message(message: dict) -> dict:
    """Handle one Telegram message (webhook or long-poll). Returns JSON-safe dict."""
    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    text = str(message.get("text") or "").strip()

    if not chat_id or not text:
        return {"ok": True}

    command, args = _telegram_command(text)
    user = message.get("from") or {}
    username = user.get("username") or user.get("first_name") or "unknown"

    if command == "/help":
        _telegram_send_message(chat_id, TELEGRAM_HELP_TEXT)
        return {"ok": True}

    if command == "/status":
        _telegram_send_message(chat_id, _telegram_status_text())
        return {"ok": True}

    if command == "/task":
        if not args:
            _telegram_send_message(chat_id, "Usage: /task <description>")
            return {"ok": True}

        data = TaskCreate(
            title=args,
            description=f"Telegram task requested by @{username}",
        )
        task = create_task(data)
        full_desc = f"Task: {data.title}\n\n{data.description}"
        threading.Thread(
            target=_run_and_notify_telegram,
            args=(task.id, full_desc, None, chat_id, data.title),
            daemon=True,
        ).start()
        _telegram_send_message(chat_id, f"⏳ On it! Running Geppetto on: {data.title}")
        return {"ok": True, "task_id": task.id}

    if command == "/jira":
        if not args:
            _telegram_send_message(chat_id, "Usage: /jira <summary>")
            return {"ok": True}

        try:
            from api.jira import create_issue

            issue = create_issue(
                args,
                description=f"Created from Telegram by @{username}. Geppetto will run this task.",
            )
        except Exception as e:
            _telegram_send_message(chat_id, f"❌ Could not create Jira issue: {e}")
            return {"ok": False}

        data = TaskCreate(
            title=args,
            description=f"Telegram task for Jira issue {issue['key']} requested by @{username}",
            jira_id=issue["key"],
        )
        task = create_task(data)
        full_desc = f"[{data.jira_id}] Task: {data.title}\n\n{data.description}"
        threading.Thread(
            target=_run_and_notify_telegram,
            args=(task.id, full_desc, data.jira_id, chat_id, data.title),
            daemon=True,
        ).start()
        _telegram_send_message(chat_id, f"⏳ Created {issue['key']} and started Geppetto: {data.title}")
        return {"ok": True, "task_id": task.id, "jira_id": issue["key"]}

    _telegram_send_message(chat_id, TELEGRAM_HELP_TEXT)
    return {"ok": True}


def _telegram_poll_loop() -> None:
    """Long polling for local/dev — no HTTPS or ngrok required."""
    from config import TELEGRAM_BOT_TOKEN, TELEGRAM_POLLING

    if not TELEGRAM_POLLING or not TELEGRAM_BOT_TOKEN:
        return

    base = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
    try:
        httpx.post(f"{base}/deleteWebhook", json={"drop_pending_updates": False}, timeout=15)
    except Exception:
        pass

    offset = 0
    while True:
        try:
            r = httpx.get(
                f"{base}/getUpdates",
                params={"timeout": 45, "offset": offset, "allowed_updates": ["message"]},
                timeout=50,
            )
            body = r.json()
            if not body.get("ok"):
                time.sleep(2)
                continue
            for upd in body.get("result", []):
                offset = upd["update_id"] + 1
                msg = upd.get("message") or upd.get("edited_message")
                if isinstance(msg, dict):
                    process_telegram_message(msg)
        except Exception:
            time.sleep(3)


@app.on_event("startup")
def _start_telegram_long_poll() -> None:
    from config import TELEGRAM_POLLING, TELEGRAM_BOT_TOKEN

    if TELEGRAM_POLLING and TELEGRAM_BOT_TOKEN:
        threading.Thread(target=_telegram_poll_loop, name="telegram-poll", daemon=True).start()


@app.post("/telegram")
async def telegram_webhook(request: Request):
    payload = await request.json()
    message = payload.get("message") or payload.get("edited_message") or {}
    return process_telegram_message(message)


# ── Code Q&A ──────────────────────────────────────────────────────────────────

@app.post("/ask")
async def ask_code(request: Request):
    """Answer a question about the codebase via RAG + LLM, streamed as SSE."""
    data = await request.json()
    question = (data.get("question") or "").strip()
    if not question:
        raise HTTPException(400, "question required")

    from agent.tools import retrieve_for_ask
    from config import LLM_MODEL, VERTEXAI_PROJECT, VERTEXAI_LOCATION
    import litellm

    context, sources = retrieve_for_ask(question, n_results=6)

    messages = [
        {
            "role": "system",
            "content": (
                "You are a senior engineer who knows this codebase deeply. "
                "Answer questions concisely and precisely. "
                "When referencing code, quote the relevant lines. "
                "If the context doesn't contain the answer, say so honestly."
            ),
        },
        {
            "role": "user",
            "content": f"Codebase context:\n{context}\n\nQuestion: {question}",
        },
    ]

    async def stream():
        try:
            yield f"data: {json.dumps({'type': 'meta', 'model': LLM_MODEL, 'sources': sources})}\n\n"
            response = litellm.completion(
                model=LLM_MODEL,
                messages=messages,
                max_tokens=2048,
                stream=True,
                vertex_project=VERTEXAI_PROJECT or None,
                vertex_location=VERTEXAI_LOCATION or None,
            )
            for chunk in response:
                delta = chunk.choices[0].delta.content
                if delta:
                    yield f"data: {json.dumps({'type': 'chunk', 'text': delta})}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'type': 'error', 'error': str(e)})}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── Dashboard ─────────────────────────────────────────────────────────────────

_dashboard = Path(__file__).parent.parent / "dashboard"


@app.get("/", response_class=HTMLResponse)
def root():
    return (_dashboard / "index.html").read_text()
