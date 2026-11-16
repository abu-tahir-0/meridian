"""The terminal agent's limits, with the graph library stubbed out.

The agent only needs `StateGraph` and `END` from LangGraph to load; the nodes are
plain functions, so the limits can be exercised on the host without the image.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "fixtures"))

if "langgraph" not in sys.modules:  # the trial image ships it; a unit run need not
    package = types.ModuleType("langgraph")
    graph = types.ModuleType("langgraph.graph")
    graph.END = "__end__"

    class StateGraph:  # the builder is not under test here
        def __init__(self, *_: object) -> None:
            pass

        def __class_getitem__(cls, _: object) -> type[StateGraph]:
            return cls

        def add_node(self, *_: object, **__: object) -> None:
            pass

        def set_entry_point(self, *_: object) -> None:
            pass

        def add_edge(self, *_: object) -> None:
            pass

        def add_conditional_edges(self, *_: object) -> None:
            pass

        def compile(self) -> StateGraph:
            return self

    graph.StateGraph = StateGraph
    package.graph = graph
    sys.modules["langgraph"] = package
    sys.modules["langgraph.graph"] = graph

from terminal_agent import agent
from terminal_agent.tools import agent_limits

pytestmark = pytest.mark.unit


def context(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "prompt": "list the files",
        "workdir": ".",
        "model_base_url": "http://proxy",
    }
    return {**base, **overrides}


def test_plan_resolves_the_limits_from_the_trial_context(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MERIDIAN_AGENT_MAX_TURNS", raising=False)
    state = agent.plan({"context": context(max_turns=4, max_output_chars=120)})
    assert state["limits"]["max_turns"] == 4
    assert state["limits"]["max_output_chars"] == 120


class FakeResponse:
    status_code = 200

    def json(self) -> dict[str, object]:
        return {
            "usage": {"input_tokens": 10, "output_tokens": 2},
            "content": [{"type": "text", "text": "done"}],
            "stop_reason": "end_turn",
        }


def test_act_uses_the_configured_request_timeout_and_tool_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, object] = {}

    def fake_post(url: str, **kwargs: object) -> FakeResponse:
        seen["url"] = url
        seen.update(kwargs)
        return FakeResponse()

    monkeypatch.setattr(agent.httpx, "post", fake_post)
    ctx = context(
        workdir=str(tmp_path),
        request_timeout_seconds=2.5,
        default_command_timeout_seconds=5,
    )
    state = {**{"context": ctx}, **agent.plan({"context": ctx})}
    state["turns"] = 0
    result = agent.act(state)

    assert seen["timeout"] == 2.5
    body = seen["json"]
    assert isinstance(body, dict)
    description = body["tools"][0]["input_schema"]["properties"]["timeout_seconds"][
        "description"
    ]
    assert "default 5" in description
    assert result["turns"] == 1 and result["completed"] is True


def test_route_gives_up_at_the_configured_turn_limit() -> None:
    state = agent.plan({"context": context(max_turns=2)})
    state["turns"] = 2
    assert agent.route(state) == "out_of_turns"
    assert agent.out_of_turns(state)["error"] == "gave up after 2 turns"


def test_the_shell_tool_receives_the_configured_output_and_time_limits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, object] = {}

    def fake_run(command: str, **kwargs: object) -> dict[str, object]:
        seen["command"] = command
        seen.update(kwargs)
        return {"exit_code": 0, "output": "ok", "error": None}

    monkeypatch.setattr(agent, "run_bash", fake_run)
    ctx = context(
        workdir=str(tmp_path),
        max_output_chars=100,
        default_command_timeout_seconds=3,
        max_command_timeout_seconds=9,
    )
    state = {**{"context": ctx}, **agent.plan({"context": ctx})}
    state["messages"] = [
        {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "t1", "name": "bash", "input": {"command": "ls"}}
            ],
        }
    ]
    agent.tools(state)

    assert seen["command"] == "ls"
    assert seen["max_output_chars"] == 100
    assert seen["default_timeout_seconds"] == 3
    assert seen["max_timeout_seconds"] == 9


def test_the_defaults_are_the_old_constants() -> None:
    limits = agent_limits(context())
    assert limits["max_turns"] == 30
    assert limits["request_timeout_seconds"] == 180.0
    assert limits["max_output_chars"] == 8000
