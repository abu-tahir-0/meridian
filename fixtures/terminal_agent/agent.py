"""A general terminal agent: a LangGraph loop over one `bash` tool.

    plan → act ⇄ tools → finish

Unlike the checkout fixture, nothing here knows the task. It reads the prompt,
works in the container's working directory through a shell, and stops when the
model stops calling tools. That makes it the system under test for imported
benchmark tasks (see `meridian import-harbor`).

It calls the model through the Meridian proxy, never a provider, with no credential
of its own.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Annotated, Any, TypedDict

import httpx
from langgraph.graph import END, StateGraph

from terminal_agent.tools import TOOL_SCHEMAS, format_result, run_bash
from terminal_agent.transcript import record

# The proxy substitutes the model the operator chose, so this name is only a label.
MODEL = os.environ.get("MERIDIAN_FIXTURE_MODEL", "meridian-model")
MAX_TURNS = 30
REQUEST_TIMEOUT_SECONDS = 180.0
# The trial token budget is a ceiling on the whole trial, not on one reply.
MAX_TOKENS_PER_REPLY = 4096

PROMPTS = Path(__file__).resolve().parent / "prompts"


def _append(existing: list[Any], incoming: list[Any]) -> list[Any]:
    return [*existing, *incoming]


class AgentState(TypedDict, total=False):
    context: dict[str, Any]
    messages: Annotated[list[dict[str, Any]], _append]
    system: str
    turns: int
    tool_calls: Annotated[list[dict[str, Any]], _append]
    input_tokens: int
    output_tokens: int
    error: str | None
    completed: bool


def plan(state: AgentState) -> AgentState:
    context = state["context"]
    return {
        "system": (PROMPTS / "system.md").read_text(encoding="utf-8"),
        "messages": [{"role": "user", "content": context["prompt"]}],
        "turns": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "tool_calls": [],
        "completed": False,
        "error": None,
    }


def act(state: AgentState) -> AgentState:
    """One model turn, through the proxy."""
    context = state["context"]
    body = {
        "model": MODEL,
        "max_tokens": min(
            int(context.get("max_tokens", MAX_TOKENS_PER_REPLY)), MAX_TOKENS_PER_REPLY
        ),
        "system": state.get("system", ""),
        "messages": state.get("messages", []),
        "tools": TOOL_SCHEMAS,
    }
    headers = {
        "x-meridian-task": str(context.get("task_slug", "")),
        "x-meridian-trial": str(context.get("trial_index", 0)),
        "content-type": "application/json",
    }
    url = f"{context['model_base_url']}/v1/messages"
    workdir = str(context.get("workdir", "."))
    asked = time.monotonic()
    record(workdir, "model_request", turn=state.get("turns", 0) + 1)
    try:
        response = httpx.post(url, json=body, headers=headers, timeout=REQUEST_TIMEOUT_SECONDS)
    except httpx.HTTPError as exc:
        record(workdir, "model_error", error=f"{type(exc).__name__}: {exc}")
        return {"error": f"model call failed: {exc}", "completed": False}
    if response.status_code != 200:
        record(workdir, "model_error", status=response.status_code, body=response.text)
        return {
            "error": f"model returned {response.status_code}: {response.text[:400]}",
            "completed": False,
        }

    payload = response.json()
    usage = payload.get("usage") or {}
    reply = payload.get("content") or []
    record(
        workdir,
        "model_reply",
        seconds=round(time.monotonic() - asked, 1),
        stop=payload.get("stop_reason"),
        text=" ".join(
            b.get("text", "") for b in reply if isinstance(b, dict) and b.get("type") == "text"
        ),
        commands=[
            (b.get("input") or {}).get("command")
            for b in reply
            if isinstance(b, dict) and b.get("type") == "tool_use"
        ],
        input_tokens=int(usage.get("input_tokens", 0)),
        output_tokens=int(usage.get("output_tokens", 0)),
    )
    return {
        "messages": [{"role": "assistant", "content": payload.get("content") or []}],
        "turns": state.get("turns", 0) + 1,
        "input_tokens": state.get("input_tokens", 0) + int(usage.get("input_tokens", 0)),
        "output_tokens": state.get("output_tokens", 0) + int(usage.get("output_tokens", 0)),
        "completed": payload.get("stop_reason") != "tool_use",
    }


def tools(state: AgentState) -> AgentState:
    """Run every tool call the model made, in order."""
    workdir = str(state["context"]["workdir"])
    results: list[dict[str, Any]] = []
    recorded: list[dict[str, Any]] = []
    for block in state["messages"][-1].get("content", []):
        if not isinstance(block, dict) or block.get("type") != "tool_use":
            continue
        arguments = dict(block.get("input") or {})
        if block.get("name") != "bash" or not isinstance(arguments.get("command"), str):
            text, ok = f"[unknown tool or bad arguments: {json.dumps(arguments)[:200]}]", False
        else:
            began = time.monotonic()
            result = run_bash(
                arguments["command"],
                workdir=workdir,
                timeout_seconds=arguments.get("timeout_seconds"),
            )
            text, ok = format_result(result), result["error"] is None
            record(
                workdir,
                "command",
                command=arguments["command"],
                exit_code=result["exit_code"],
                error=result["error"],
                seconds=round(time.monotonic() - began, 1),
                output=result["output"],
            )
        recorded.append({"name": str(block.get("name")), "ok": ok})
        results.append(
            {
                "type": "tool_result",
                "tool_use_id": block.get("id"),
                "content": text,
                "is_error": not ok,
            }
        )
    return {"messages": [{"role": "user", "content": results}], "tool_calls": recorded}


def route(state: AgentState) -> str:
    if state.get("error") or state.get("completed"):
        return "finish"
    if state.get("turns", 0) >= MAX_TURNS:
        return "out_of_turns"
    last = state["messages"][-1]
    wants_tools = any(
        isinstance(block, dict) and block.get("type") == "tool_use"
        for block in last.get("content", [])
    )
    return "tools" if wants_tools else "finish"


def out_of_turns(state: AgentState) -> AgentState:
    # An agent failure, not a harness one: it had its turns and did not finish.
    return {"error": f"gave up after {MAX_TURNS} turns", "completed": False}


def finish(state: AgentState) -> AgentState:
    return {"completed": state.get("error") is None}


def build_graph() -> Any:
    builder: StateGraph[AgentState] = StateGraph(AgentState)
    builder.add_node("plan", plan)
    builder.add_node("act", act)
    builder.add_node("tools", tools)
    builder.add_node("out_of_turns", out_of_turns)
    builder.add_node("finish", finish)
    builder.set_entry_point("plan")
    builder.add_edge("plan", "act")
    builder.add_conditional_edges(
        "act",
        route,
        {"tools": "tools", "finish": "finish", "out_of_turns": "out_of_turns"},
    )
    builder.add_edge("tools", "act")
    builder.add_edge("out_of_turns", "finish")
    builder.add_edge("finish", END)
    return builder.compile()


graph = build_graph()
