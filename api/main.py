import asyncio
import json
import logging
import re
import sys
import threading
import time
from io import BytesIO
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from api.models import (
    TaskCreate, JiraIssueCreate, init_db,
    create_task, get_task, list_tasks,
    update_status, append_event,
    telegram_try_claim_message,
)
from agent.runner import run_agent
from config import SCREENSHOTS_DIR, SQLITE_PATH

app = FastAPI(title="Geppetto")
log = logging.getLogger("geppetto.telegram")

# Keeps an exclusive flock alive on Unix so only one process runs getUpdates (Telegram allows one poller per bot).
_telegram_poll_lock_file: object | None = None

app.mount("/screenshots", StaticFiles(directory=str(SCREENSHOTS_DIR)), name="screenshots")
_assets = Path(__file__).parent.parent / "dashboard" / "assets"
_assets.mkdir(parents=True, exist_ok=True)
app.mount("/assets", StaticFiles(directory=str(_assets)), name="assets")
init_db()


def dashboard_task_url(task_id: str) -> str:
    """Deep-link to the dashboard trace for a task (running or finished)."""
    from config import PUBLIC_BASE_URL

    base = (PUBLIC_BASE_URL or "http://127.0.0.1:8000").rstrip("/")
    return f"{base}/?task={task_id}"


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


@app.post("/jira/issues", status_code=201)
def jira_create_issue(data: JiraIssueCreate):
    """Create a Jira ticket only. Start the agent with POST /tasks (jira_id + title + description) or the dashboard Run Agent."""
    from api.jira import create_issue

    if not data.summary.strip():
        raise HTTPException(400, "summary is required")
    try:
        return create_issue(
            data.summary.strip(),
            data.description.strip(),
            data.issue_type or "Task",
        )
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


def _strip_trailing_url_punctuation(url: str) -> str:
    return url.rstrip(").,]}\"'")


def _pr_url_from_push_tool_result(result: str | None) -> str | None:
    """
    Human-openable GitHub PR URL from push_and_create_pr output.

    Success is "PR created: https://github.com/org/repo/pull/42". Failures often
    mention https://api.github.com/graphql; a naive first-URL match surfaces a
    bogus link in Telegram/Slack.
    """
    if not result:
        return None
    low = result.lower()
    if "push failed" in low or "pr creation failed" in low:
        return None
    if "no remote configured" in low:
        return None

    m = re.search(r"PR created:\s*(https://\S+)", result)
    if m:
        return _strip_trailing_url_punctuation(m.group(1))

    matches = list(
        re.finditer(
            r"https://github\.com/[^/\s)]+/[^/\s)]+/pull/\d+",
            result,
            re.IGNORECASE,
        )
    )
    if matches:
        return _strip_trailing_url_punctuation(matches[-1].group(0))
    return None


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
            u = _pr_url_from_push_tool_result(ev.get("result"))
            if u:
                pr_url = u
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
        "dashboard_url": dashboard_task_url(task.id),
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
                u = _pr_url_from_push_tool_result(ev.get("result"))
                if u:
                    pr_url = u
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
    "/start — welcome + this list",
    "/task <description> - run without a Jira ticket",
    "/jcreate <summary> - create a Jira issue only; then use /from_jira",
    "/from_jira <KEY> - run the agent on an existing issue (same flow as dashboard)",
    "/jira <summary> - create issue + run in one step",
    "/status - show the last 5 tasks",
    "/help - show command list",
    "",
    "Each run includes a dashboard link so you can watch the trace live (PR optional).",
    "",
    "Voice: send a voice note — same commands as text (needs OPENAI_API_KEY + Whisper). Speech without a leading / is treated as /task ….",
])

TELEGRAM_START_TEXT = "\n".join([
    "Welcome to codeGeppetto.",
    "I run coding tasks against your linked repo.",
    "",
    TELEGRAM_HELP_TEXT,
])


def _telegram_send_message(chat_id: int | str, text: str) -> None:
    from config import TELEGRAM_BOT_TOKEN

    if not TELEGRAM_BOT_TOKEN:
        log.warning("TELEGRAM_BOT_TOKEN missing — cannot send message")
        return

    try:
        r = httpx.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": text},
            timeout=10,
        )
        body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        if not r.is_success or not body.get("ok", False):
            log.warning(
                "Telegram sendMessage failed: status=%s body=%s",
                r.status_code,
                body or r.text[:500],
            )
    except Exception as e:
        log.exception("Telegram sendMessage error: %s", e)


def _telegram_command(text: str) -> tuple[str, str]:
    command, _, rest = text.partition(" ")
    return command.split("@", 1)[0].lower(), rest.strip()


def _telegram_voice_audio_meta(message: dict) -> tuple[str | None, str, int]:
    """Telegram file_id, filename for Whisper, duration in seconds (0 if unknown)."""
    v = message.get("voice")
    if isinstance(v, dict) and v.get("file_id"):
        return str(v["file_id"]), "voice.ogg", int(v.get("duration") or 0)
    a = message.get("audio")
    if isinstance(a, dict) and a.get("file_id"):
        mime = (a.get("mime_type") or "").lower()
        if "ogg" in mime:
            name = "audio.ogg"
        elif "mpeg" in mime or "mp3" in mime:
            name = "audio.mp3"
        elif "m4a" in mime or "mp4" in mime:
            name = "audio.m4a"
        else:
            name = "audio.ogg"
        return str(a["file_id"]), name, int(a.get("duration") or 0)
    return None, "", 0


def _telegram_download_tg_file(file_id: str) -> bytes:
    from config import TELEGRAM_BOT_TOKEN

    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN missing")
    base = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
    r = httpx.get(f"{base}/getFile", params={"file_id": file_id}, timeout=25)
    r.raise_for_status()
    body = r.json()
    if not body.get("ok"):
        raise RuntimeError(str(body.get("description") or body))
    path = body["result"]["file_path"]
    url = f"https://api.telegram.org/file/bot{TELEGRAM_BOT_TOKEN}/{path}"
    r2 = httpx.get(url, timeout=120)
    r2.raise_for_status()
    return r2.content


def _telegram_transcribe_whisper(audio: bytes, filename: str) -> str:
    from config import OPENAI_API_KEY

    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY is not set")
    from openai import OpenAI

    client = OpenAI(api_key=OPENAI_API_KEY)
    buf = BytesIO(audio)
    buf.name = filename
    tr = client.audio.transcriptions.create(model="whisper-1", file=buf)
    return (getattr(tr, "text", None) or "").strip()


def _telegram_try_transcribe_voice(message: dict, chat_id: int | str) -> str | None:
    """
    If the message has voice/audio, return transcript.
    None = nothing to transcribe (ignore silently).
    '' = user was notified of an error / missing config.
    """
    file_id, filename, duration = _telegram_voice_audio_meta(message)
    if not file_id:
        return None
    from config import OPENAI_API_KEY, TELEGRAM_VOICE_MAX_SECONDS

    if not OPENAI_API_KEY:
        _telegram_send_message(
            chat_id,
            "Voice notes need OPENAI_API_KEY in .env (OpenAI Whisper). Text commands still work.",
        )
        return ""
    if duration and duration > TELEGRAM_VOICE_MAX_SECONDS:
        _telegram_send_message(
            chat_id,
            f"Voice too long ({duration}s). Max is {TELEGRAM_VOICE_MAX_SECONDS}s.",
        )
        return ""
    try:
        audio = _telegram_download_tg_file(file_id)
        text = _telegram_transcribe_whisper(audio, filename)
        if not text:
            _telegram_send_message(chat_id, "No speech recognized — try again or type the command.")
            return ""
        return text
    except Exception as e:
        log.warning("Telegram voice transcription failed: %s", e)
        _telegram_send_message(chat_id, f"Voice transcription failed: {e}")
        return ""


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

    dash = dashboard_task_url(task_id)
    if task.status != "completed":
        return f"❌ Geppetto hit an error on: {title}\n{dash}"

    pr_url = None
    cost_usd = duration_s = tool_calls = 0
    for ev in task.events:
        if ev.get("type") == "tool_result" and ev.get("tool") == "push_and_create_pr":
            u = _pr_url_from_push_tool_result(ev.get("result"))
            if u:
                pr_url = u
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
    lines.append(f"Dashboard: {dashboard_task_url(task_id)}")
    return "\n".join(lines)


def _telegram_jira_issue_key_from_args(args: str) -> str | None:
    """First token as Jira key, e.g. PROJ-123 or proj-42 → normalized."""
    if not args:
        return None
    key = args.strip().split()[0].strip().upper()
    if len(key) < 3 or "-" not in key:
        return None
    return key


def _telegram_launch_jira_agent(
    chat_id: int | str,
    username: str,
    *,
    jira_key: str,
    title: str,
    description: str,
) -> dict:
    """Start background agent with Jira transitions (same contract as dashboard Run)."""
    data = TaskCreate(title=title, description=description, jira_id=jira_key)
    task = create_task(data)
    full_desc = f"[{data.jira_id}] Task: {data.title}\n\n{data.description}"
    threading.Thread(
        target=_run_and_notify_telegram,
        args=(task.id, full_desc, data.jira_id, chat_id, data.title),
        daemon=True,
    ).start()
    _telegram_send_message(
        chat_id,
        "\n".join([
            f"⏳ Running Geppetto on {jira_key}: {data.title}",
            f"Watch live: {dashboard_task_url(task.id)}",
        ]),
    )
    return {"ok": True, "task_id": task.id, "jira_id": jira_key}


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
    if not chat_id:
        return {"ok": True}

    mid = message.get("message_id")
    if mid is not None:
        try:
            cid = int(chat_id)
            qmid = int(mid)
        except (TypeError, ValueError):
            log.warning("telegram non-integer chat_id or message_id: %r %r", chat_id, mid)
        else:
            if not telegram_try_claim_message(cid, qmid):
                log.debug("telegram duplicate delivery chat=%s message_id=%s", cid, qmid)
                return {"ok": True}

    text = str(message.get("text") or "").strip()
    if not text:
        transcribed = _telegram_try_transcribe_voice(message, chat_id)
        if transcribed is None:
            return {"ok": True}
        if transcribed == "":
            return {"ok": True}
        text = transcribed.strip()
        if text and not text.lstrip().startswith("/"):
            text = "/task " + text

    command, args = _telegram_command(text)
    user = message.get("from") or {}
    username = user.get("username") or user.get("first_name") or "unknown"

    if command == "/start":
        _telegram_send_message(chat_id, TELEGRAM_START_TEXT)
        return {"ok": True}

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
        _telegram_send_message(
            chat_id,
            "\n".join([
                f"⏳ On it! Running Geppetto on: {data.title}",
                f"Watch live: {dashboard_task_url(task.id)}",
            ]),
        )
        return {"ok": True, "task_id": task.id}

    if command == "/jcreate":
        if not args:
            _telegram_send_message(chat_id, "Usage: /jcreate <summary>")
            return {"ok": True}
        try:
            from api.jira import create_issue

            issue = create_issue(args, description=f"Created from Telegram by @{username}.")
        except Exception as e:
            _telegram_send_message(chat_id, f"❌ Could not create Jira issue: {e}")
            return {"ok": False}

        _telegram_send_message(
            chat_id,
            "\n".join([
                f"✅ Created {issue['key']}: {issue.get('summary') or args}",
                f"When ready: /from_jira {issue['key']}",
            ]),
        )
        return {"ok": True, "jira_id": issue["key"]}

    if command == "/from_jira":
        key = _telegram_jira_issue_key_from_args(args)
        if not key:
            _telegram_send_message(chat_id, "Usage: /from_jira PROJ-123")
            return {"ok": True}
        try:
            from api.jira import get_issue

            issue = get_issue(key)
        except Exception as e:
            _telegram_send_message(chat_id, f"❌ Could not load Jira issue {key}: {e}")
            return {"ok": False}

        body = (issue.get("description") or "").strip()
        desc = f"Jira issue {issue['key']} picked up via Telegram by @{username}."
        if body:
            desc = f"{desc}\n\n{body}"
        return _telegram_launch_jira_agent(
            chat_id,
            username,
            jira_key=issue["key"],
            title=issue.get("summary") or issue["key"],
            description=desc,
        )

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

        return _telegram_launch_jira_agent(
            chat_id,
            username,
            jira_key=issue["key"],
            title=args,
            description=f"Telegram task for Jira issue {issue['key']} requested by @{username}",
        )

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
                params={"timeout": 45, "offset": offset},
                timeout=50,
            )
            body = r.json()
            if not body.get("ok"):
                desc = body.get("description") or body
                if isinstance(desc, str) and "Conflict" in desc and "getUpdates" in desc:
                    log.warning(
                        "getUpdates conflict: another client is already long-polling this bot token. "
                        "Stop the other process (second run.sh/uvicorn, IDE runner, or cloud worker with TELEGRAM_POLLING). "
                        "Description: %s",
                        desc,
                    )
                else:
                    log.warning("getUpdates not ok: %s", desc)
                time.sleep(2)
                continue
            for upd in body.get("result", []):
                offset = upd["update_id"] + 1
                msg = upd.get("message") or upd.get("edited_message")
                if isinstance(msg, dict):
                    process_telegram_message(msg)
        except Exception:
            log.exception("telegram poll loop error")
            time.sleep(3)


def _try_acquire_telegram_getupdates_lock() -> bool:
    """
    On Unix, take a non-blocking exclusive flock so only one process polls Telegram.
    On Windows, skip locking (single-instance is up to the operator).
    """
    global _telegram_poll_lock_file
    if sys.platform == "win32":
        return True
    try:
        import fcntl
    except ImportError:
        return True
    lock_path = SQLITE_PATH.parent / "telegram_getupdates.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    f = open(lock_path, "a+", encoding="utf-8")
    try:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        f.close()
        log.warning(
            "Telegram long-polling skipped: another process holds %s. "
            "Only one getUpdates loop is allowed per bot — close the other uvicorn/run.sh or unset TELEGRAM_POLLING on duplicates.",
            lock_path,
        )
        return False
    _telegram_poll_lock_file = f
    return True


@app.on_event("startup")
def _start_telegram_long_poll() -> None:
    from config import TELEGRAM_POLLING, TELEGRAM_BOT_TOKEN

    if not (TELEGRAM_POLLING and TELEGRAM_BOT_TOKEN):
        return
    if not _try_acquire_telegram_getupdates_lock():
        return
    threading.Thread(target=_telegram_poll_loop, name="telegram-poll", daemon=True).start()


@app.post("/telegram")
async def telegram_webhook(request: Request):
    payload = await request.json()
    message = payload.get("message") or payload.get("edited_message") or {}
    return process_telegram_message(message)


# ── Code Q&A ──────────────────────────────────────────────────────────────────

@app.post("/ask")
async def ask_code(request: Request):
    """Answer a question about the codebase via RAG + LLM, streamed as SSE.

    Routing:
      USE_SOURCEBOT=true  → proxy to self-hosted Sourcebot. Failures surface
                            to the user as SSE error events — no silent
                            Chroma fallback (those answers came from indexed
                            spec markdown, not the actual code, and produced
                            contradictory results).
      USE_SOURCEBOT unset → in-process Chroma retrieval + LiteLLM completion.
                            Kept around for users who haven't adopted Sourcebot
                            yet; dropped once the team is fully on Sourcebot."""
    data = await request.json()
    question = (data.get("question") or "").strip()
    if not question:
        raise HTTPException(400, "question required")

    from agent import sourcebot as sourcebot_adapter
    from agent.tools import retrieve_for_ask
    from config import LLM_MODEL, VERTEXAI_PROJECT, VERTEXAI_LOCATION
    import litellm

    # ── Sourcebot path ──────────────────────────────────────────────────────
    if sourcebot_adapter.is_enabled():
        sb_iter = sourcebot_adapter.stream_ask(question)
        try:
            # Pull the first event eagerly so connection/auth failures surface
            # before we open the SSE response — gives a clean error shape.
            first_event = await sb_iter.__anext__()
        except sourcebot_adapter.SourcebotUnavailable as e:
            err = f"Sourcebot unavailable: {e}"
            print(f"[ask] {err}")

            async def stream_error():
                yield f"data: {json.dumps({'type': 'meta', 'model': 'sourcebot', 'sources': []})}\n\n"
                yield f"data: {json.dumps({'type': 'error', 'error': err})}\n\n"
                yield "data: [DONE]\n\n"
            return StreamingResponse(
                stream_error(),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )
        except StopAsyncIteration:
            err = "Sourcebot returned an empty response"
            print(f"[ask] {err}")

            async def stream_empty():
                yield f"data: {json.dumps({'type': 'meta', 'model': 'sourcebot', 'sources': []})}\n\n"
                yield f"data: {json.dumps({'type': 'error', 'error': err})}\n\n"
                yield "data: [DONE]\n\n"
            return StreamingResponse(
                stream_empty(),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )

        async def stream_sourcebot():
            yield f"data: {json.dumps(first_event)}\n\n"
            async for ev in sb_iter:
                yield f"data: {json.dumps(ev)}\n\n"
            yield "data: [DONE]\n\n"

        return StreamingResponse(
            stream_sourcebot(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # ── Chroma path (only when USE_SOURCEBOT is unset) ──────────────────────
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
