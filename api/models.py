import json
import sqlite3
import uuid
from datetime import datetime
from typing import Optional
from pydantic import BaseModel
from config import SQLITE_PATH


class TaskCreate(BaseModel):
    title: str
    description: str
    jira_id: Optional[str] = None


class JiraIssueCreate(BaseModel):
    """Create an issue in Jira (REST). Run the agent separately via POST /tasks or dashboard."""

    summary: str
    description: str = ""
    issue_type: str = "Task"


class Task(BaseModel):
    id: str
    title: str
    description: str
    jira_id: Optional[str] = None
    status: str  # pending | running | completed | failed
    created_at: str
    updated_at: str
    events: list[dict] = []


def _db():
    conn = sqlite3.connect(str(SQLITE_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with _db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS tasks (
                id         TEXT PRIMARY KEY,
                title      TEXT NOT NULL,
                description TEXT NOT NULL,
                jira_id    TEXT,
                status     TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                events     TEXT NOT NULL DEFAULT '[]'
            )
        """)


def _row_to_task(row) -> Task:
    return Task(
        id=row["id"],
        title=row["title"],
        description=row["description"],
        jira_id=row["jira_id"],
        status=row["status"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        events=json.loads(row["events"]),
    )


def create_task(data: TaskCreate) -> Task:
    task_id = str(uuid.uuid4())
    now = datetime.utcnow().isoformat()
    with _db() as conn:
        conn.execute(
            "INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?)",
            (task_id, data.title, data.description, data.jira_id, "pending", now, now, "[]"),
        )
    return Task(id=task_id, title=data.title, description=data.description,
                jira_id=data.jira_id, status="pending", created_at=now, updated_at=now)


def get_task(task_id: str) -> Optional[Task]:
    row = _db().execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    return _row_to_task(row) if row else None


def list_tasks() -> list[Task]:
    rows = _db().execute("SELECT * FROM tasks ORDER BY created_at DESC").fetchall()
    return [_row_to_task(r) for r in rows]


def update_status(task_id: str, status: str):
    now = datetime.utcnow().isoformat()
    with _db() as conn:
        conn.execute("UPDATE tasks SET status=?, updated_at=? WHERE id=?", (status, now, task_id))


def append_event(task_id: str, event: dict):
    db = _db()
    row = db.execute("SELECT events FROM tasks WHERE id=?", (task_id,)).fetchone()
    if not row:
        return
    events = json.loads(row["events"])
    events.append(event)
    now = datetime.utcnow().isoformat()
    with db:
        db.execute(
            "UPDATE tasks SET events=?, updated_at=? WHERE id=?",
            (json.dumps(events), now, task_id),
        )
