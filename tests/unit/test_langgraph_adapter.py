"""The LangGraph adapter with a fake graph — LangGraph is not installed."""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

from meridian.adapters.base import AdapterSpec, AdapterSpecError
from meridian.adapters.langgraph_adapter import RECURSION_LIMIT, LangGraphAdapter
from meridian.models.adapter import TrialContext

pytestmark = pytest.mark.unit

GRAPH_MODULE = """
class FakeGraph:
    def __init__(self):
        self.last_payload = None
        self.last_config = None

    async def ainvoke(self, payload, config):
        self.last_payload = payload
        self.last_config = config
        return {
            "completed": True,
            "turns": 2,
            "tool_calls": [
                {"name": "write_file", "ok": True},
                {"name": "run_tests", "ok": False},
            ],
            "input_tokens": 11,
            "output_tokens": 5,
            "error": None,
        }


graph = FakeGraph()
not_a_graph = object()
"""


def make_spec(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, attribute: str) -> AdapterSpec:
    monkeypatch.syspath_prepend(str(tmp_path))
    (tmp_path / "fake_langgraph.py").write_text(GRAPH_MODULE, encoding="utf-8")
    return AdapterSpec.parse(f"langgraph:fake_langgraph:{attribute}")


def context(tmp_path: Path) -> TrialContext:
    return TrialContext(
        run_id="run-1",
        task_slug="happy-path",
        trial_index=0,
        seed=7,
        workdir=str(tmp_path),
        prompt="do the task",
        model_base_url="http://proxy:8080",
        max_tokens=1000,
        deadline_unix_ms=0,
    )


async def test_the_final_state_maps_to_an_adapter_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = LangGraphAdapter(make_spec(tmp_path, monkeypatch, "graph"))

    result = await adapter.invoke(context(tmp_path))

    assert result.completed
    assert result.turns == 2
    assert [(call.name, call.ok) for call in result.tool_calls] == [
        ("write_file", True),
        ("run_tests", False),
    ]
    assert result.input_tokens == 11
    assert result.output_tokens == 5
    assert result.error is None
    assert result.transcript_path == f"{tmp_path}/.meridian/transcript.json"

    module = importlib.import_module("fake_langgraph")
    assert module.graph.last_payload["context"]["run_id"] == "run-1"
    assert module.graph.last_config == {"recursion_limit": RECURSION_LIMIT}


async def test_a_target_without_invoke_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = LangGraphAdapter(make_spec(tmp_path, monkeypatch, "not_a_graph"))

    with pytest.raises(AdapterSpecError):
        await adapter.invoke(context(tmp_path))
