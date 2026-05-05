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


def get_issue(key: str) -> dict:
    r = requests.get(
        f"{JIRA_BASE_URL}/rest/api/3/issue/{key}",
        headers=_headers(),
        params={"fields": "summary,description,status,issuetype,priority"},
        timeout=10,
    )
    r.raise_for_status()
    return _format_issue(r.json())


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
