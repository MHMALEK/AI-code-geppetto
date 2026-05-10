"""
Sourcebot adapter — calls a self-hosted Sourcebot instance for code Q&A.

Sourcebot is a code-search engine (Zoekt + LLM Ask) that we run alongside
Geppetto. When USE_SOURCEBOT=true, /ask routes through here instead of the
local Chroma index. Geppetto's editing/agent tools are unaffected.

API contract (observed from Sentinel's adapter, since Sourcebot's REST docs
are thin):
  POST {SOURCEBOT_BASE_URL}/api/ask    body: {"question": "..."}
  → SSE stream of events, each "data: {...}\n\n", with types:
      text-delta         {"type":"text-delta","textDelta":"..."}
      citation           {"type":"citation","citation":{repo,path,startLine,endLine,...}}
      message-metadata   {"type":"message-metadata", ...}
  Stream ends with EOF (no [DONE] sentinel — close on stream end).

We re-emit those events in the same shape the dashboard already understands
({"type":"meta","sources":[...]} then {"type":"chunk","text":"..."}) so no UI
changes are required.
"""
from __future__ import annotations

import json
import os
from typing import AsyncIterator

import httpx


class SourcebotUnavailable(RuntimeError):
    """Raised when Sourcebot is configured but not reachable. Lets /ask
    decide whether to fall back to Chroma or surface the error."""


def is_enabled() -> bool:
    return os.getenv("USE_SOURCEBOT", "").lower() in ("1", "true", "yes")


def _base_url() -> str:
    url = os.getenv("SOURCEBOT_BASE_URL", "http://127.0.0.1:3001").rstrip("/")
    return url


def _headers() -> dict[str, str]:
    h = {"Content-Type": "application/json", "Accept": "text/event-stream"}
    key = os.getenv("SOURCEBOT_API_KEY")
    if key:
        h["Authorization"] = f"Bearer {key}"
    return h


def _normalize_citation(c: dict) -> dict:
    """Map Sourcebot's citation shape to the source-dict shape the dashboard
    chip renderer already understands (file_path / start_line / repo / etc.)."""
    return {
        "repo": c.get("repo") or c.get("repository") or "",
        "file_path": c.get("path") or c.get("filename") or c.get("file") or "",
        "start_line": c.get("startLine") or c.get("start_line") or 1,
        "end_line": c.get("endLine") or c.get("end_line") or 0,
        "chunk_type": "code",
        "name": (c.get("path") or "").rsplit("/", 1)[-1],
        "score": c.get("score", 0.0),
        "url": c.get("url", ""),
        "revision": c.get("revision", ""),
    }


async def health_check(timeout: float = 2.0) -> bool:
    """Quick GET / to confirm Sourcebot is up before sending real queries."""
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.get(_base_url() + "/")
            return r.status_code < 500
    except Exception:
        return False


async def stream_ask(question: str) -> AsyncIterator[dict]:
    """Yield dashboard-shaped events: one {"type":"meta",...} followed by
    zero-or-more {"type":"chunk","text":"..."} as the answer streams in.

    Citations arriving mid-stream get appended to the meta event's sources
    list — but the meta event has already been sent, so we instead emit a
    fresh {"type":"meta",...} update each time the citation set changes.
    The dashboard's last-meta-wins behavior handles this naturally."""
    if not is_enabled():
        raise SourcebotUnavailable("USE_SOURCEBOT is not enabled")

    url = _base_url() + "/api/ask"
    citations: list[dict] = []
    citation_keys: set[str] = set()  # dedup on (repo, path, startLine)
    sent_meta = False

    payload = {"question": question}

    async with httpx.AsyncClient(timeout=httpx.Timeout(60.0, read=300.0)) as client:
        try:
            async with client.stream("POST", url, headers=_headers(), json=payload) as resp:
                if resp.status_code >= 400:
                    body = (await resp.aread()).decode("utf-8", errors="replace")[:500]
                    raise SourcebotUnavailable(
                        f"Sourcebot returned {resp.status_code}: {body}"
                    )

                # Initial meta — model name unknown until Sourcebot tells us;
                # use a placeholder so the dashboard renders something.
                yield {"type": "meta", "model": "sourcebot", "sources": []}
                sent_meta = True

                buf = ""
                async for raw in resp.aiter_text():
                    buf += raw
                    while "\n\n" in buf:
                        event, buf = buf.split("\n\n", 1)
                        for line in event.split("\n"):
                            if not line.startswith("data:"):
                                continue
                            data = line[5:].strip()
                            if not data or data == "[DONE]":
                                continue
                            try:
                                d = json.loads(data)
                            except json.JSONDecodeError:
                                continue

                            etype = d.get("type")
                            if etype == "text-delta":
                                text = d.get("textDelta") or d.get("text") or ""
                                if text:
                                    yield {"type": "chunk", "text": text}
                            elif etype == "citation":
                                c = d.get("citation") or {}
                                norm = _normalize_citation(c)
                                key = (norm["repo"], norm["file_path"], norm["start_line"])
                                if key in citation_keys:
                                    continue
                                citation_keys.add(key)
                                citations.append(norm)
                                # Re-emit meta with cumulative citations so the
                                # source-chip bar updates as the model cites.
                                yield {
                                    "type": "meta",
                                    "model": "sourcebot",
                                    "sources": list(citations),
                                }
                            elif etype == "error":
                                err = d.get("error") or "Sourcebot error"
                                yield {"type": "error", "error": str(err)}
                                return
                            # message-metadata, finish, etc. — ignore for now.
        except httpx.ConnectError as e:
            raise SourcebotUnavailable(f"Cannot reach Sourcebot at {url}: {e}") from e
        except httpx.ReadTimeout as e:
            if not sent_meta:
                raise SourcebotUnavailable(f"Sourcebot timed out: {e}") from e
            yield {"type": "error", "error": "Sourcebot stream timed out"}
