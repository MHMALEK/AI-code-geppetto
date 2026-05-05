"""
Jira REST API v3 client.
Transitions: 21=In Progress, 31=In Review, 41=Done
"""
import base64
from typing import Optional
import requests
from config import JIRA_BASE_URL, JIRA_EMAIL, JIRA_API_TOKEN, JIRA_PROJECT_KEY


def _headers() -> dict:
    token = base64.b64encode(f"{JIRA_EMAIL}:{JIRA_API_TOKEN}".encode()).decode()
    return {"Authorization": f"Basic {token}", "Content-Type": "application/json"}


def _extract_text(node) -> str:
    """Recursively extract plain text from Atlassian Document Format."""
    if not node:
        return ""
    if isinstance(node, str):
        return node
    if node.get("type") == "text":
        return node.get("text", "")
    return " ".join(_extract_text(c) for c in node.get("content", [])).strip()


def _format_issue(raw: dict) -> dict:
    f = raw["fields"]
    return {
        "key":         raw["key"],
        "id":          raw["id"],
        "summary":     f.get("summary", ""),
        "description": _extract_text(f.get("description")),
        "status":      f["status"]["name"],
        "status_key":  f["status"]["statusCategory"]["key"],
        "type":        f["issuetype"]["name"],
        "priority":    (f.get("priority") or {}).get("name", ""),
    }


def list_issues(max_results: int = 30) -> list[dict]:
    jql = f"project={JIRA_PROJECT_KEY} ORDER BY updated DESC"
    r = requests.get(
        f"{JIRA_BASE_URL}/rest/api/3/search/jql",
        headers=_headers(),
        params={"jql": jql, "maxResults": max_results,
                "fields": "summary,description,status,issuetype,priority"},
        timeout=10,
    )
    r.raise_for_status()
    return [_format_issue(i) for i in r.json()["issues"]]


def list_project_issue_keys(max_results: int = 500) -> list[str]:
    """All issue keys in JIRA_PROJECT_KEY (paged)."""
    jql = f"project={JIRA_PROJECT_KEY} ORDER BY key ASC"
    keys: list[str] = []
    start = 0
    batch = 50
    while len(keys) < max_results:
        take = min(batch, max_results - len(keys))
        r = requests.get(
            f"{JIRA_BASE_URL}/rest/api/3/search/jql",
            headers=_headers(),
            params={
                "jql": jql,
                "startAt": start,
                "maxResults": take,
                "fields": "key",
            },
            timeout=30,
        )
        r.raise_for_status()
        data = r.json()
        issues = data.get("issues") or []
        for row in issues:
            keys.append(row["key"])
        if len(issues) < take:
            break
        start += len(issues)
    return keys


def get_available_transitions(key: str) -> list[dict]:
    r = requests.get(
        f"{JIRA_BASE_URL}/rest/api/3/issue/{key}/transitions",
        headers=_headers(),
        timeout=10,
    )
    r.raise_for_status()
    return r.json().get("transitions", [])


def _transition_targets_todo_category(tr: dict) -> bool:
    to_block = tr.get("to") or {}
    cat = (to_block.get("statusCategory") or {}).get("key")
    return cat == "new"


def _transition_name_suggests_start(tr: dict) -> bool:
    name = (tr.get("name") or "").lower()
    to_name = ((tr.get("to") or {}).get("name") or "").lower()
    hints = ("to do", "todo", "backlog", "open", "reopen", "re-open")
    return any((h in name) or (h in to_name) for h in hints)


def move_issue_to_start_status(key: str, max_steps: int = 24) -> tuple[str, int]:
    """
    Walk the workflow toward statusCategory 'new' (typical To Do column) using available transitions.

    Returns (final_status_name, steps_applied). No-op if already in 'new' category or no path found.
    """
    steps = 0
    for _ in range(max_steps):
        cur = get_issue(key)
        if cur.get("status_key") == "new":
            return cur["status"], steps

        transitions = get_available_transitions(key)
        preferred = [t for t in transitions if _transition_targets_todo_category(t)]
        if not preferred:
            preferred = [t for t in transitions if _transition_name_suggests_start(t)]
        if not preferred:
            return cur["status"], steps

        transition_issue(key, preferred[0]["id"])
        steps += 1

    cur = get_issue(key)
    return cur["status"], steps


def move_all_project_issues_to_start_status() -> list[tuple[str, str, int]]:
    """
    For each issue in the project, transition toward To Do / new category.

    Returns list of (issue_key, final_status_name, steps_applied).
    """
    out: list[tuple[str, str, int]] = []
    for key in list_project_issue_keys():
        status, steps = move_issue_to_start_status(key)
        out.append((key, status, steps))
    return out


def get_issue(key: str) -> dict:
    r = requests.get(
        f"{JIRA_BASE_URL}/rest/api/3/issue/{key}",
        headers=_headers(),
        params={"fields": "summary,description,status,issuetype,priority"},
        timeout=10,
    )
    r.raise_for_status()
    return _format_issue(r.json())


def create_issue(summary: str, description: str = "", issue_type: str = "Task") -> dict:
    payload = {
        "fields": {
            "project": {"key": JIRA_PROJECT_KEY},
            "summary": summary,
            "issuetype": {"name": issue_type},
        }
    }
    if description:
        payload["fields"]["description"] = {
            "type": "doc",
            "version": 1,
            "content": [{
                "type": "paragraph",
                "content": [{"type": "text", "text": description}],
            }],
        }

    r = requests.post(
        f"{JIRA_BASE_URL}/rest/api/3/issue",
        headers=_headers(),
        json=payload,
        timeout=10,
    )
    r.raise_for_status()
    return get_issue(r.json()["key"])


def transition_issue(key: str, transition_id: str) -> None:
    r = requests.post(
        f"{JIRA_BASE_URL}/rest/api/3/issue/{key}/transitions",
        headers=_headers(),
        json={"transition": {"id": transition_id}},
        timeout=10,
    )
    r.raise_for_status()


def add_comment(key: str, text: str) -> None:
    r = requests.post(
        f"{JIRA_BASE_URL}/rest/api/3/issue/{key}/comment",
        headers=_headers(),
        json={
            "body": {
                "type": "doc", "version": 1,
                "content": [{"type": "paragraph", "content": [{"type": "text", "text": text}]}],
            }
        },
        timeout=10,
    )
    r.raise_for_status()
