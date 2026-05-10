"""
Query curator — pre-retrieval prompt restructuring.

Sentinel's pattern: before sending a developer's raw question to the
code-intelligence backend, restructure it into a Context/Observed/Question
form that nudges the agent toward the right kinds of artifacts (regex
patterns, validators, Pydantic models, decorators, etc.).

Why it helps: a vague UI-flavored question like
    "what special characters are not allowed in Farm Name?"
gets a vague answer because the agent doesn't know that "Farm Name" is a
display label that maps to internal identifiers like `node_name`,
`nodeName`, etc. The curator adds that bridge so the retrieval agent
searches for the actual code constructs.

Toggle with CURATED_QUERIES=true. Disabled = the raw question goes to
Sourcebot unchanged. Cost: +1 fast LLM call per /ask (~0.5-1 sec, fractions
of a cent on Flash).
"""
from __future__ import annotations

import os
from typing import Optional

from pydantic import BaseModel, Field

try:
    from pydantic_ai import Agent
    from pydantic_ai.models.gemini import GeminiModel
    _PYDANTIC_AI_AVAILABLE = True
except ImportError:
    _PYDANTIC_AI_AVAILABLE = False


class CuratedQuery(BaseModel):
    """Structured restatement of a developer question, designed to give a
    code-search agent enough scaffolding to find the right files."""
    context: str = Field(
        description="1-2 sentences about the system's likely architecture (e.g. 'multi-repo monorepo with React frontend and FastAPI backend; UI labels often differ from internal field names')"
    )
    observed: str = Field(
        description="What the user actually asked, expanded with likely synonyms or naming variants (e.g. 'Farm Name (likely internal: node_name, nodeName, farm_name)')"
    )
    question: str = Field(
        description="The sharpened question that names specific code-construct kinds to look for (e.g. 'find regex patterns / validator functions / Pydantic field constraints applied to these fields')"
    )

    def to_query(self) -> str:
        """Format as a single string for the downstream agent."""
        return (
            f"Context: {self.context}\n\n"
            f"Observed: {self.observed}\n\n"
            f"Question: {self.question}"
        )


def is_enabled() -> bool:
    if not _PYDANTIC_AI_AVAILABLE:
        return False
    return os.getenv("CURATED_QUERIES", "").lower() in ("1", "true", "yes")


_PROMPT = """You restructure developer questions for a code-search agent.

The agent searches across multiple repos in a polyglot monorepo:
- frontend  — React/TypeScript app (UI labels, form validation, Zod schemas,
              react-router routes, navigation/sidebar config)
- api       — FastAPI/Python backend (Pydantic models, validators, decorators,
              regex patterns, role checkers, route protection)
- data      — Airflow/Python pipelines (DAGs, custom operators)
- data-cloud-functions — GCP Cloud Functions (Python)

Common pitfalls the agent stumbles on (your `question` field should pre-empt
them):

1. UI labels vs internal names — "Farm Name" → node_name/nodeName, "Reference
   ID" → external_id/referenceId. Validation/permissions usually live on the
   internal name.
2. Validation lives in many places: regex *_PATTERN constants, verify_/
   check_/validate_* functions, Pydantic Field constraints, Zod schemas.
3. Auth/authz: Keycloak + OIDC + JWT + JWKS on backend; role-gated routes/
   nav/sidebar on frontend. Both usually matter.
4. **Permission/access questions almost always need multi-layer coverage:**
   route guards, navigation visibility, sidebar config, AND backend
   permission checks. A "yes/no" answer without naming all layers is
   incomplete.
5. Multi-repo answers usually need both client (frontend) and server (api).

Produce a CuratedQuery whose `question` field is **explicit, multi-aspect, and
demanding**. Tell the agent to:

- Give a direct answer first.
- Enumerate every enforcement/definition layer that matters (don't stop at
  the first one found).
- Note related-but-distinct concepts that could confuse the reader (e.g.
  "Master Data section under Data Manager is separate from the primary
  Settings area").
- Call out caveats, edge cases, and discrepancies between layers (e.g.
  "frontend hides the link, but the backend API still allows the request").

Keep `context` and `observed` short (1-2 sentences each). The `question`
field can be 3-6 sentences listing what the answer should cover.

Do not invent specific file paths or function names — your job is to suggest
*kinds* of things to look for and *aspects* the answer must cover.

If the question is already specific (names a concrete function or file), keep
the curation light — just restate it more precisely."""


async def curate(raw_question: str) -> Optional[CuratedQuery]:
    """Convert a raw user question into a Context/Observed/Question structure.
    Returns None on failure — caller should fall back to the raw question."""
    if not is_enabled():
        return None
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key or not raw_question.strip():
        return None

    model_name = os.getenv("CURATOR_MODEL", "gemini-2.5-flash")

    try:
        os.environ.setdefault("GEMINI_API_KEY", api_key)
        model = GeminiModel(model_name)
        agent = Agent(model=model, output_type=CuratedQuery, system_prompt=_PROMPT)
        result = await agent.run(raw_question)
        return result.output
    except Exception as e:
        print(f"[curator] failed ({type(e).__name__}): {e}")
        return None
