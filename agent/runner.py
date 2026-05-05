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
import time
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


def _emit_stats(emit: Callable, prompt_tokens: int, completion_tokens: int, tool_calls: int, start_time: float) -> None:
    total = prompt_tokens + completion_tokens
    duration = round(time.time() - start_time, 1)
    try:
        # LiteLLM cost helper — best-effort, may return 0 for some models
        cost = litellm.cost_per_token(model=LLM_MODEL, prompt_tokens=prompt_tokens, completion_tokens=completion_tokens)
        cost_usd = round((cost[0] * prompt_tokens + cost[1] * completion_tokens), 4)
    except Exception:
        cost_usd = 0.0
    emit({
        "type": "stats",
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total,
        "tool_calls": tool_calls,
        "cost_usd": cost_usd,
        "duration_s": duration,
    })


def run_agent(task_id: str, task: str, emit: Callable[[dict], None]) -> None:
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": task},
    ]
    emit({"type": "start", "message": f"Agent started · {LLM_MODEL}"})

    start_time = time.time()
    prompt_tokens = 0
    completion_tokens = 0
    tool_call_count = 0

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
            _emit_stats(emit, prompt_tokens, completion_tokens, tool_call_count, start_time)
            emit({"type": "error", "message": str(e)})
            return

        # Accumulate token usage
        if response.usage:
            prompt_tokens     += response.usage.prompt_tokens or 0
            completion_tokens += response.usage.completion_tokens or 0

        msg = response.choices[0].message
        finish = response.choices[0].finish_reason

        if msg.content:
            emit({"type": "thinking", "text": msg.content})

        if finish in ("stop", "end_turn"):
            _emit_stats(emit, prompt_tokens, completion_tokens, tool_call_count, start_time)
            emit({"type": "complete", "message": "Task completed successfully"})
            return

        if finish in ("tool_calls", "tool_use") and msg.tool_calls:
            tool_results = []

            for tc in msg.tool_calls:
                fn_name = tc.function.name
                fn_args = json.loads(tc.function.arguments)

                tool_call_count += 1
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
            _emit_stats(emit, prompt_tokens, completion_tokens, tool_call_count, start_time)
            emit({"type": "complete", "message": f"Stopped: {finish}"})
            return

    _emit_stats(emit, prompt_tokens, completion_tokens, tool_call_count, start_time)
    emit({"type": "error", "message": "Max steps (30) reached"})
