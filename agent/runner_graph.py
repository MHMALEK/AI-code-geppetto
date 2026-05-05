"""
LangGraph-based agent runner — educational alternative to runner.py.

Same public API: run_agent_graph(task_id, task, emit)

Key differences from the for-loop in runner.py:
  • Loop becomes explicit graph edges you can visualise
  • State is a typed dict — every variable is named and traceable
  • Adding nodes (reflection, validation, human-in-the-loop) is structural,
    not buried inside an if-elif chain
  • LangGraph can checkpoint/resume any state transition with zero extra code

Graph shape:
    [START] → call_model ──(tool_calls?)──→ run_tools → reflect ──┐
                   ↑                                                │
                   └────────────────────────────────────────────────┘
                   │                                   (3 error rounds)
              (stop/done)                                    ↓
                   ↓                                       [END]
                [END]

Note: emit is stored in AgentState as Any.  LangGraph won't try to serialise
it unless you attach a checkpointer — which is fine for this demo.
For a production checkpointed graph, store emit in a thread-safe registry
keyed by run_id and look it up inside nodes instead.
"""
from __future__ import annotations

import json
import time
from typing import Any, Callable, TypedDict

import litellm
from langgraph.graph import END, StateGraph

from agent.runner import SYSTEM_PROMPT, _TOOLS, _emit_stats
from agent.tools import TOOL_MAP
from config import LLM_MODEL


# ── State ──────────────────────────────────────────────────────────────────────

class AgentState(TypedDict):
    messages: list[dict]
    task_id: str
    emit: Any            # Callable[[dict], None] — see module docstring
    prompt_tokens: int
    completion_tokens: int
    tool_call_count: int
    start_time: float
    step: int
    done: bool           # terminal flag to short-circuit routing
    consecutive_errors: int  # rounds where every tool call failed; resets on success


# ── Nodes ──────────────────────────────────────────────────────────────────────

def node_call_model(state: AgentState) -> dict:
    """Call the LLM.  Returns partial state update (only changed keys)."""
    emit = state["emit"]

    if state["step"] >= 50:
        _emit_stats(emit, state["prompt_tokens"], state["completion_tokens"],
                    state["tool_call_count"], state["start_time"])
        emit({"type": "error", "message": "Max steps (50) reached"})
        return {"done": True}

    try:
        response = litellm.completion(
            model=LLM_MODEL,
            messages=state["messages"],
            tools=_TOOLS,
            tool_choice="auto",
            max_tokens=8096,
            metadata={"task_id": state["task_id"]},
        )
    except Exception as e:
        _emit_stats(emit, state["prompt_tokens"], state["completion_tokens"],
                    state["tool_call_count"], state["start_time"])
        emit({"type": "error", "message": str(e)})
        return {"done": True}

    usage = response.usage
    new_prompt     = state["prompt_tokens"]     + (usage.prompt_tokens     or 0 if usage else 0)
    new_completion = state["completion_tokens"] + (usage.completion_tokens or 0 if usage else 0)

    msg    = response.choices[0].message
    finish = response.choices[0].finish_reason

    if msg.content:
        emit({"type": "thinking", "text": msg.content})

    new_messages = state["messages"] + [msg.model_dump()]

    if finish in ("stop", "end_turn"):
        _emit_stats(emit, new_prompt, new_completion,
                    state["tool_call_count"], state["start_time"])
        emit({"type": "complete", "message": "Task completed successfully"})
        return {
            "messages": new_messages,
            "prompt_tokens": new_prompt,
            "completion_tokens": new_completion,
            "done": True,
        }

    if finish not in ("tool_calls", "tool_use") or not msg.tool_calls:
        _emit_stats(emit, new_prompt, new_completion,
                    state["tool_call_count"], state["start_time"])
        emit({"type": "complete", "message": f"Stopped: {finish}"})
        return {
            "messages": new_messages,
            "prompt_tokens": new_prompt,
            "completion_tokens": new_completion,
            "done": True,
        }

    return {
        "messages": new_messages,
        "prompt_tokens": new_prompt,
        "completion_tokens": new_completion,
        "step": state["step"] + 1,
    }


def node_run_tools(state: AgentState) -> dict:
    """Execute every tool call in the last assistant message."""
    emit = state["emit"]
    last_msg = state["messages"][-1]

    raw_calls = last_msg.get("tool_calls") or []
    tool_results = []
    count = state["tool_call_count"]

    for tc in raw_calls:
        # tool_calls are dicts after model_dump()
        fn_name = tc["function"]["name"]
        fn_args = json.loads(tc["function"]["arguments"])
        tc_id   = tc["id"]

        count += 1
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
            "tool_call_id": tc_id,
            "content": result_str,
        })

    return {
        "messages": state["messages"] + tool_results,
        "tool_call_count": count,
    }


def node_reflect(state: AgentState) -> dict:
    """Scan the latest tool results; inject a correction hint if errors are found.

    Why this node exists:
    - Without it, the agent silently loops after a failed tool call, often
      repeating the same broken call forever until it hits the step limit.
    - With it, errors are detected structurally (in the graph, not inside the
      model node) and the agent is nudged to try something different.

    Routing after this node:
    - 3 consecutive error rounds → set done=True, which _route_after_reflect
      sends to END.
    - Otherwise → always back to call_model (unconditional or via the router).
    """
    emit = state["emit"]
    messages = state["messages"]

    # Collect the most recent batch of tool results (everything since the last
    # assistant message — that's one "round" of tool calls).
    tool_errors: list[str] = []
    for msg in reversed(messages):
        if msg.get("role") != "tool":
            break
        content = msg.get("content", "")
        if content.startswith("Tool error:") or content.startswith("Unknown tool:"):
            tool_errors.append(content)

    if not tool_errors:
        # Clean round — reset the error counter and continue without touching messages.
        return {"consecutive_errors": 0}

    new_count = state["consecutive_errors"] + 1
    emit({"type": "reflection", "message": f"Tool errors detected (round {new_count})", "errors": tool_errors})

    if new_count >= 3:
        _emit_stats(emit, state["prompt_tokens"], state["completion_tokens"],
                    state["tool_call_count"], state["start_time"])
        emit({"type": "error", "message": "Agent stuck — 3 consecutive error rounds, aborting"})
        return {"done": True, "consecutive_errors": new_count}

    # Inject a user-turn hint so the model sees the failure explicitly and is
    # pushed to try a different approach on the next call_model invocation.
    hint = {
        "role": "user",
        "content": (
            f"The previous tool call(s) returned errors: {'; '.join(tool_errors)}. "
            "Please re-read the relevant file or try a different approach before continuing."
        ),
    }
    return {
        "messages": messages + [hint],
        "consecutive_errors": new_count,
    }


# ── Routing ────────────────────────────────────────────────────────────────────

def _route_after_model(state: AgentState) -> str:
    """After call_model: go to run_tools if there are tool calls, otherwise END."""
    if state["done"]:
        return END
    last = state["messages"][-1]
    if last.get("tool_calls"):
        return "run_tools"
    return END


def _route_after_reflect(state: AgentState) -> str:
    """After reflect: abort if done (too many errors), otherwise back to call_model."""
    if state["done"]:
        return END
    return "call_model"


# ── Compiled graph (module-level singleton) ────────────────────────────────────

def _build() -> Any:
    g = StateGraph(AgentState)
    g.add_node("call_model", node_call_model)
    g.add_node("run_tools",  node_run_tools)
    g.add_node("reflect",    node_reflect)       # ← new node
    g.set_entry_point("call_model")
    g.add_conditional_edges("call_model", _route_after_model, {"run_tools": "run_tools", END: END})
    g.add_edge("run_tools", "reflect")           # ← was: run_tools → call_model
    g.add_conditional_edges("reflect", _route_after_reflect, {"call_model": "call_model", END: END})
    return g.compile()


_graph = _build()


# ── Public entry point ─────────────────────────────────────────────────────────

def run_agent_graph(task_id: str, task: str, emit: Callable[[dict], None]) -> None:
    """Drop-in replacement for runner.run_agent() using a LangGraph StateGraph."""
    emit({"type": "start", "message": f"Agent started (LangGraph) · {LLM_MODEL}"})

    initial: AgentState = {
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user",   "content": task},
        ],
        "task_id":           task_id,
        "emit":              emit,
        "prompt_tokens":     0,
        "completion_tokens": 0,
        "tool_call_count":   0,
        "start_time":        time.time(),
        "step":              0,
        "done":              False,
        "consecutive_errors": 0,
    }

    _graph.invoke(initial)
