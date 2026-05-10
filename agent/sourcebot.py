"""
Sourcebot adapter — calls a self-hosted Sourcebot v4 instance for code Q&A.

Sourcebot v4 reorganized its API around a chat-thread model. The cleanest
programmatic entry point is `POST /api/chat/blocking`, which is documented in
the source as "designed for MCP and other integrations" — it creates a chat
thread, runs the agent to completion, returns the full answer in one JSON.

Endpoint contract (verified against sourcebot v4.17.x):

    POST /api/chat/blocking
    {
      "query":           "what does authentication look like?",
      "repos":           ["gitlab.com/.../frontend"],     // optional scope
      "languageModel":   {"provider":"google-generative-ai","model":"gemini-2.5-flash"},
      "visibility":      "PUBLIC"                          // optional
    }
    →
    {
      "answer":        "<markdown>",
      "chatId":        "...",
      "chatUrl":       "http://localhost:3000/chat/...",
      "languageModel": {...}
    }

The answer markdown contains inline citation links of the form
    [path/to/file.py:start-end](http://.../browse/repo/.../blob/path)
which the dashboard renders as clickable links — so we don't need a separate
sources array. We re-emit the response in the dashboard's SSE shape (one meta
event with chatUrl, one or more chunk events with the answer text) so the UI
renders identically whether retrieval came from Chroma or Sourcebot.

Configuration:
    USE_SOURCEBOT=true
    SOURCEBOT_BASE_URL=http://127.0.0.1:3001
    SOURCEBOT_MODEL=gemini-2.5-flash         # optional; default = first configured
    SOURCEBOT_API_KEY=                       # only if Sourcebot requires auth
"""
from __future__ import annotations

import json
import os
import re
from typing import AsyncIterator

import httpx


class SourcebotUnavailable(RuntimeError):
    """Sourcebot is configured but not reachable / returned a non-recoverable
    error before any answer was produced. Lets /ask decide whether to fall
    back to Chroma or surface the error to the user."""


def is_enabled() -> bool:
    return os.getenv("USE_SOURCEBOT", "").lower() in ("1", "true", "yes")


def _base_url() -> str:
    return os.getenv("SOURCEBOT_BASE_URL", "http://127.0.0.1:3001").rstrip("/")


def _headers() -> dict[str, str]:
    h = {"Content-Type": "application/json"}
    key = os.getenv("SOURCEBOT_API_KEY")
    if key:
        h["Authorization"] = f"Bearer {key}"
    return h


# Match Sourcebot's inline citation links inside the answer markdown:
#   [src/auth.py:12-40](http://localhost:3000/browse/repo/.../blob/src%2Fauth.py?highlightRange=12%2C40)
_CITATION_RE = re.compile(
    r"\[([^\]]+\.\w+):?(\d+)?(?:[-–](\d+))?\]\((http[^\)]+/browse/[^\)]+)\)"
)


def _extract_citations(answer: str, chat_url: str) -> list[dict]:
    """Pull file:line markers out of the answer and emit one source dict per
    unique citation. Falls back to a single chatUrl source if none found, so
    the dashboard's chip bar never goes empty."""
    seen: set[tuple[str, int]] = set()
    out: list[dict] = []
    for m in _CITATION_RE.finditer(answer):
        path = m.group(1)
        start = int(m.group(2) or 1)
        url = m.group(4)
        # Best-effort repo extraction from the URL (.../browse/<repo>/-/blob/...)
        repo_match = re.search(r"/browse/([^/]+(?:/[^/]+)*?)/-/blob/", url)
        repo = repo_match.group(1) if repo_match else ""
        key = (path, start)
        if key in seen:
            continue
        seen.add(key)
        out.append(
            {
                "repo": repo,
                "file_path": path,
                "start_line": start,
                "end_line": int(m.group(3) or 0),
                "chunk_type": "code",
                "name": path.rsplit("/", 1)[-1],
                "score": 1.0,
                "url": url,
            }
        )
    if not out:
        out.append(
            {
                "repo": "",
                "file_path": "(see Sourcebot chat)",
                "start_line": 0,
                "end_line": 0,
                "chunk_type": "chat",
                "name": "open in Sourcebot",
                "score": 1.0,
                "url": chat_url,
            }
        )
    return out


def _chunkify(text: str, size: int = 200):
    """Yield the answer in modest slices so the dashboard renders progressively
    instead of dumping the whole answer at once. /api/chat/blocking has no
    native streaming, so this is purely a UX nicety."""
    for i in range(0, len(text), size):
        yield text[i : i + size]


async def stream_ask(question: str) -> AsyncIterator[dict]:
    """Yield dashboard-shaped events:
        {"type": "meta",  "model": "...", "sources": [...]}
        {"type": "chunk", "text":  "..."}   (one or more)
    On a fatal/early failure raises SourcebotUnavailable so /ask can fall
    back to Chroma. After meta has been yielded, errors come through as
    {"type": "error", ...} events."""
    if not is_enabled():
        raise SourcebotUnavailable("USE_SOURCEBOT is not enabled")

    payload: dict[str, object] = {"query": question}
    model = os.getenv("SOURCEBOT_MODEL")
    if model:
        payload["languageModel"] = {
            "provider": os.getenv("SOURCEBOT_PROVIDER", "google-generative-ai"),
            "model": model,
        }

    url = _base_url() + "/api/chat/blocking"

    timeout = httpx.Timeout(connect=5.0, read=180.0, write=10.0, pool=5.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        try:
            resp = await client.post(url, headers=_headers(), json=payload)
        except httpx.ConnectError as e:
            raise SourcebotUnavailable(f"Cannot reach Sourcebot at {url}: {e}") from e
        except httpx.ReadTimeout as e:
            raise SourcebotUnavailable(f"Sourcebot timed out: {e}") from e

        if resp.status_code >= 400:
            body = resp.text[:500]
            raise SourcebotUnavailable(
                f"Sourcebot returned {resp.status_code}: {body}"
            )

        try:
            data = resp.json()
        except json.JSONDecodeError as e:
            raise SourcebotUnavailable(f"Sourcebot returned non-JSON: {e}") from e

    answer: str = data.get("answer") or ""
    chat_url: str = data.get("chatUrl") or ""
    lm = data.get("languageModel") or {}
    model_label = (
        lm.get("displayName")
        or f"{lm.get('provider', '')}/{lm.get('model', '')}".strip("/")
        or "sourcebot"
    )

    sources = _extract_citations(answer, chat_url)

    yield {"type": "meta", "model": model_label, "sources": sources, "chatUrl": chat_url}

    if not answer:
        yield {"type": "error", "error": "Sourcebot returned an empty answer."}
        return

    for piece in _chunkify(answer):
        yield {"type": "chunk", "text": piece}
