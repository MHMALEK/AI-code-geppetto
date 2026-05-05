"""
Agent loop using LiteLLM — Vertex AI (Gemini), Anthropic, or Ollama.
Langfuse tracing activates automatically when LANGFUSE_PUBLIC_KEY is set.

Commit convention enforced via system prompt:
  Branch:  feat/SCRUM-X-short-description
  Commit:  feat(SCRUM-X): what was done
  Types:   feat | fix | refactor | chore | style
"""
import json
import os
from typing import Callable
import litellm
from config import (
    LLM_MODEL, VERTEXAI_PROJECT, VERTEXAI_LOCATION,
    ANTHROPIC_API_KEY, LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY, LANGFUSE_HOST,
)
from agent.tools import TOOL_DEFINITIONS, TOOL_MAP

# ── LiteLLM / Langfuse setup ──────────────────────────────────────────────────

if VERTEXAI_PROJECT:
    os.environ["VERTEXAI_PROJECT"] = VERTEXAI_PROJECT
    os.environ["VERTEXAI_LOCATION"] = VERTEXAI_LOCATION

if ANTHROPIC_API_KEY:
    os.environ["ANTHROPIC_API_KEY"] = ANTHROPIC_API_KEY

if LANGFUSE_PUBLIC_KEY:
    os.environ.update({
        "LANGFUSE_PUBLIC_KEY": LANGFUSE_PUBLIC_KEY,
        "LANGFUSE_SECRET_KEY": LANGFUSE_SECRET_KEY,
        "LANGFUSE_HOST": LANGFUSE_HOST,
    })
    litellm.success_callback = ["langfuse"]
    litellm.failure_callback = ["langfuse"]

_TOOLS = [
    {"type": "function", "function": {"name": t["name"], "description": t["description"], "parameters": t["input_schema"]}}
    for t in TOOL_DEFINITIONS
]

SYSTEM_PROMPT = """\
You are Geppetto — an expert software engineer agent working on a TypeScript/React codebase.

## Workflow
1. search_code   — find relevant code before touching anything
2. read_file     — inspect exact content (never guess)
3. create_branch — naming: feat/SCRUM-X-short-kebab-description
4. edit_file     — precise targeted edits only
5. git_diff      — review changes before committing
6. commit_changes — use conventional commit format (see below)
7. push_and_create_pr — push branch, open PR

## Commit convention (STRICT)
Format:  <type>(<jira-key>): <short description>
Types:   feat | fix | refactor | chore | style
Branch:  <type>/SCRUM-X-short-kebab-description

Examples:
  feat(SCRUM-1): add loading spinner to DataTable
  fix(SCRUM-2): resolve null reference in useSuppliers hook
  refactor(SCRUM-3): extract filter logic into useFilters hook

## Rules
- Always read_file before edit_file — never guess file content
- Follow existing code patterns exactly
- Use existing components and hooks — never reinvent
- Keep changes minimal and focused
- TypeScript types must be correct
"""


def run_agent(task_id: str, task: str, emit: Callable[[dict], None]) -> None:
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": task},
    ]
    emit({"type": "start", "message": f"Agent started · {LLM_MODEL}"})

    for _step in range(30):
        try:
            response = litellm.completion(
                model=LLM_MODEL,
                messages=messages,
                tools=_TOOLS,
                tool_choice="auto",
                max_tokens=8096,
                metadata={"task_id": task_id},
            )
        except Exception as e:
            emit({"type": "error", "message": str(e)})
            return

        msg = response.choices[0].message
        finish = response.choices[0].finish_reason

        if msg.content:
            emit({"type": "thinking", "text": msg.content})

        if finish in ("stop", "end_turn"):
            emit({"type": "complete", "message": "Task completed successfully"})
            return

        if finish in ("tool_calls", "tool_use") and msg.tool_calls:
            tool_results = []

            for tc in msg.tool_calls:
                fn_name = tc.function.name
                fn_args = json.loads(tc.function.arguments)

                emit({"type": "tool_call", "tool": fn_name, "input": fn_args})

                fn = TOOL_MAP.get(fn_name)
                try:
                    result = fn(**fn_args) if fn else f"Unknown tool: {fn_name}"
                except Exception as e:
                    result = f"Tool error: {e}"

                result_str = str(result)
                emit({"type": "tool_result", "tool": fn_name, "result": result_str[:3000]})

                tool_results.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": result_str,
                })

            messages.append(msg.model_dump())
            messages.extend(tool_results)

        else:
            emit({"type": "complete", "message": f"Stopped: {finish}"})
            return

    emit({"type": "error", "message": "Max steps (30) reached"})
