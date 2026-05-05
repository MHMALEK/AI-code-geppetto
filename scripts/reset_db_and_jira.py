#!/usr/bin/env python3
"""
Clear local SQLite (tasks + telegram dedupe) and move all Jira issues in JIRA_PROJECT_KEY
toward the start column (status category *new* — usually To Do).
Run from repo root:  python scripts/reset_db_and_jira.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from api.models import init_db, reset_local_sqlite  # noqa: E402
from config import JIRA_BASE_URL, JIRA_EMAIL, JIRA_API_TOKEN, JIRA_PROJECT_KEY  # noqa: E402


def main() -> None:
    init_db()
    reset_local_sqlite()
    print("SQLite: tasks and telegram_processed cleared.")

    if not (JIRA_BASE_URL and JIRA_EMAIL and JIRA_API_TOKEN):
        print("Jira env incomplete (JIRA_BASE_URL, JIRA_EMAIL, JIRA_API_TOKEN); skipping Jira.")
        return

    from api.jira import move_all_project_issues_to_start_status  # noqa: E402

    results = move_all_project_issues_to_start_status()
    for key, status, steps in results:
        suffix = f" — {steps} transition(s)" if steps else " — (already start / no path)"
        print(f"  {key}: {status}{suffix}")
    print(f"Jira: processed {len(results)} issue(s) in project {JIRA_PROJECT_KEY}.")


if __name__ == "__main__":
    main()
