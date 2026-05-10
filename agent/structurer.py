"""
Structured-response post-processor.

Sourcebot returns rich markdown answers. This module distills each answer into
a Pydantic-validated `CodeAnswer` so the dashboard can render summary +
key-files + steps + caveats as discrete UI cards instead of a wall of text.

Pattern borrowed from Sentinel (which uses pydantic-ai for triage). Sentinel
classifies user *requests* into structured fields; we classify the answer
into a presentable structure.

Toggle with STRUCTURED_RESPONSES=true in .env. Disabled = old chunked-markdown
streaming behavior, useful for A/B comparison and as a fallback if pydantic-ai
or Gemini is misconfigured.
"""
from __future__ import annotations

import os
from typing import Optional

from pydantic import BaseModel, Field

# pydantic-ai is optional at import time — if installation fails, /ask falls
# back to streaming raw markdown chunks. Avoids hard-crashing the server when
# the dep is missing.
try:
    from pydantic_ai import Agent
    from pydantic_ai.models.gemini import GeminiModel
    _PYDANTIC_AI_AVAILABLE = True
except ImportError:
    _PYDANTIC_AI_AVAILABLE = False


class CitedFile(BaseModel):
    """One file the answer leans on. Mirrors Sourcebot's citation shape so we
    can pass URLs through unchanged for clickable rendering."""
    repo: str = Field(default="", description="repo identifier, e.g. 'gitlab.com/tract1/.../api'")
    path: str = Field(description="file path relative to repo root")
    start_line: int = Field(default=0, description="first cited line (1-indexed); 0 if whole file")
    end_line: int = Field(default=0, description="last cited line; 0 if single line or whole file")
    role: str = Field(description="One short phrase: this file's role in the answer (NOT a sentence)")
    url: str = Field(default="", description="full Sourcebot browse URL — pass through verbatim from the markdown")


class CodeAnswer(BaseModel):
    summary: str = Field(description="1-2 sentence high-level takeaway. Plain English. No 'Based on the context...' filler")
    key_files: list[CitedFile] = Field(
        default_factory=list,
        description="2-6 most important files cited. Pick by relevance to the question, not order in the markdown",
    )
    steps: list[str] = Field(
        default_factory=list,
        description="ordered flow steps if the question is about a flow/sequence; empty list otherwise",
    )
    caveats: list[str] = Field(
        default_factory=list,
        description="non-obvious gotchas a reader should know; empty list if none",
    )


def is_enabled() -> bool:
    """True when STRUCTURED_RESPONSES=true and the dep is present."""
    if not _PYDANTIC_AI_AVAILABLE:
        return False
    return os.getenv("STRUCTURED_RESPONSES", "").lower() in ("1", "true", "yes")


_PROMPT = """You restructure code-Q&A answers for a developer dashboard.

You receive: (1) a developer question, (2) a markdown answer about a codebase
that may contain inline `[file.ext:lines](url)` citations.

Produce a CodeAnswer object that distills the answer for visual display:

- summary: 1–2 sentences, plain English, no filler. State the concrete answer,
  not "Based on the context, ...".
- key_files: 2–6 of the most important files cited. Preserve each citation's
  URL exactly as it appears in the markdown so it stays clickable. `role` is
  a short phrase (3–8 words), e.g. "JWT validation entry point" — not a
  sentence with subject and verb.
- steps: only fill if the question asks about a flow or sequence. 3–7
  imperative steps. Otherwise empty list.
- caveats: things a reader should be careful about that aren't obvious from
  the answer (caching, env-var coupling, async edge cases). Empty if none.

Stay grounded in the answer text. Do NOT invent files, line numbers, or
behavior. If the answer doesn't contain enough material for a field, leave
that field empty rather than padding."""


async def structure(question: str, markdown_answer: str) -> Optional[CodeAnswer]:
    """Convert a markdown answer into a CodeAnswer. Returns None on failure
    (no key, dep missing, model error, validation error) — caller falls back
    to raw chunked rendering."""
    if not is_enabled():
        return None
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key or not markdown_answer.strip():
        return None

    model_name = os.getenv("STRUCTURER_MODEL", "gemini-2.5-flash")

    try:
        # pydantic-ai reads GEMINI_API_KEY from env, no explicit pass needed.
        # Setting it here in case the parent process didn't export it.
        os.environ.setdefault("GEMINI_API_KEY", api_key)
        model = GeminiModel(model_name)
        agent = Agent(model=model, output_type=CodeAnswer, system_prompt=_PROMPT)
        prompt = f"Question: {question}\n\nAnswer:\n{markdown_answer}"
        result = await agent.run(prompt)
        return result.output
    except Exception as e:
        print(f"[structurer] failed ({type(e).__name__}): {e}")
        return None
