"""
Agent loop using LiteLLM — works with Vertex AI (Gemini), Anthropic, or Ollama.
Switch backends by changing LLM_MODEL in .env, zero code changes.

Langfuse tracing activates automatically when LANGFUSE_PUBLIC_KEY is set.
Every task becomes a Langfuse trace with full tool call history, token counts, and cost.
"""
import os
from typing import Callable
import litellm
from config import (
    LLM_MODEL, VERTEXAI_PROJECT, VERTEXAI_LOCATION,
    ANTHROPIC_API_KEY, LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY, LANGFUSE_HOST,
)
from agent.tools import TOOL_DEFINITIONS, TOOL_MAP

# ── LiteLLM setup ─────────────────────────────────────────────────────────────

if VERTEXAI_PROJECT:
    os.environ["VERTEXAI_PROJECT"] = VERTEXAI_PROJECT
    os.environ["VERTEXAI_LOCATION"] = VERTEXAI_LOCATION

if ANTHROPIC_API_KEY:
    os.environ["ANTHROPIC_API_KEY"] = ANTHROPIC_API_KEY

# Langfuse: one line to get full observability on every LLM call
if LANGFUSE_PUBLIC_KEY:
    os.environ["LANGFUSE_PUBLIC_KEY"] = LANGFUSE_PUBLIC_KEY
    os.environ["LANGFUSE_SECRET_KEY"] = LANGFUSE_SECRET_KEY
    os.environ["LANGFUSE_HOST"] = LANGFUSE_HOST
    litellm.success_callback = ["langfuse"]
    litellm.failure_callback = ["langfuse"]

# Convert Anthropic-style tool defs to OpenAI/LiteLLM format
_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": t["name"],
            "description": t["description"],
            "parameters": t["input_schema"],
        },
    }
    for t in TOOL_DEFINITIONS
]

SYSTEM_PROMPT = """\
You are Geppetto — an expert software engineer agent working on a TypeScript/React codebase.

## Workflow (follow this order)
1. search_code — understand existing code before touching anything
2. read_file — inspect files returned by search to see exact content
3. create_branch — create a descriptive branch (e.g. feat/DEV-123-short-description)
4. edit_file / create_file — make precise, minimal changes
5. git_diff — review your changes
6. commit_changes — conventional commit message
7. push_and_create_pr — push + open PR with clear description

## Rules
- Never guess file content — always read_file before edit_file
- Follow existing patterns exactly (naming, imports, style)
- Use existing components/hooks — don't reinvent
- Keep changes minimal and focused on the task
- TypeScript types must be correct
- Write a clear PR description explaining what changed and why
"""


def run_agent(task_id: str, task: str, emit: Callable[[dict], None]) -> None:
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": task},
    ]
    emit({"type": "start", "message": f"Agent started ({LLM_MODEL})"})

    for step in range(30):
        try:
            response = litellm.completion(
                model=LLM_MODEL,
                messages=messages,
                tools=_TOOLS,
                tool_choice="auto",
                max_tokens=8096,
                metadata={"task_id": task_id},  # shows up in Langfuse trace
            )
        except Exception as e:
            emit({"type": "error", "message": str(e)})
            return

        msg = response.choices[0].message
        finish = response.choices[0].finish_reason

        # Emit reasoning text
        if msg.content:
            emit({"type": "thinking", "text": msg.content})

        if finish in ("stop", "end_turn"):
            emit({"type": "complete", "message": "Task completed"})
            return

        if finish in ("tool_calls", "tool_use") and msg.tool_calls:
            tool_results = []

            for tc in msg.tool_calls:
                import json
                fn_name = tc.function.name
                fn_args = json.loads(tc.function.arguments)

                emit({"type": "tool_call", "tool": fn_name, "input": fn_args})

                fn = TOOL_MAP.get(fn_name)
                try:
                    result = fn(**fn_args) if fn else f"Unknown tool: {fn_name}"
                except Exception as e:
                    result = f"Tool error: {e}"

                emit({
                    "type": "tool_result",
                    "tool": fn_name,
                    "result": str(result)[:3000],
                })

                tool_results.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": str(result),
                })

            # Append assistant message + tool results in OpenAI format
            messages.append(msg.model_dump())
            messages.extend(tool_results)

        else:
            emit({"type": "complete", "message": f"Stopped: {finish}"})
            return

    emit({"type": "error", "message": "Max steps reached"})
