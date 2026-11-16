"""Recovering a killed trial's work from the transcript it left behind."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from meridian.models.run import Outcome, TrialSpec
from meridian.runtime.trial_runner import (
    TRANSCRIPT_RELATIVE,
    ContainerPhase,
    TrialRunner,
    efficiency_from_transcript,
)
from meridian.suites.loader import load_suite
from tests.conftest import CHECKOUT_SUITE

pytestmark = pytest.mark.unit


def write_events(state_dir: Path, events: list[object]) -> None:
    path = state_dir / TRANSCRIPT_RELATIVE
    path.parent.mkdir(parents=True)
    path.write_text("\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8")


def test_a_missing_transcript_yields_nothing(tmp_path: Path) -> None:
    assert efficiency_from_transcript(tmp_path) is None


def test_an_empty_transcript_yields_nothing(tmp_path: Path) -> None:
    path = tmp_path / TRANSCRIPT_RELATIVE
    path.parent.mkdir(parents=True)
    path.write_text("", encoding="utf-8")
    assert efficiency_from_transcript(tmp_path) is None


def test_events_are_counted_per_kind(tmp_path: Path) -> None:
    write_events(
        tmp_path,
        [
            {"event": "model_request", "turn": 1},
            {"event": "model_reply", "input_tokens": 120, "output_tokens": 30},
            {"event": "command", "command": "ls"},
            {"event": "model_request", "turn": 2},
            {"event": "model_reply", "input_tokens": 150, "output_tokens": 40},
            {"event": "command", "command": "cat out.txt"},
            {"event": "command", "command": "pytest"},
        ],
    )

    efficiency = efficiency_from_transcript(tmp_path)

    assert efficiency is not None
    assert efficiency.turns == 2
    assert efficiency.tool_calls == 3
    assert efficiency.input_tokens == 270
    assert efficiency.output_tokens == 70


def test_malformed_lines_and_counts_are_ignored(tmp_path: Path) -> None:
    path = tmp_path / TRANSCRIPT_RELATIVE
    path.parent.mkdir(parents=True)
    path.write_text(
        "\n".join(
            [
                "not json",
                json.dumps({"event": "model_request"}),
                json.dumps(["not", "an", "object"]),
                json.dumps({"event": "model_reply", "input_tokens": "nonsense"}),
                json.dumps({"event": "model_reply", "output_tokens": 12}),
            ]
        ),
        encoding="utf-8",
    )

    efficiency = efficiency_from_transcript(tmp_path)

    assert efficiency is not None
    assert efficiency.turns == 1
    assert efficiency.tool_calls == 0
    assert efficiency.input_tokens == 0
    assert efficiency.output_tokens == 12


def test_a_timeout_verdict_reports_the_work_the_agent_recorded(tmp_path: Path) -> None:
    """The trial was killed before result.json existed; the transcript still knows."""
    write_events(
        tmp_path,
        [
            {"event": "model_request", "turn": 1},
            {"event": "model_reply", "input_tokens": 120, "output_tokens": 30},
            {"event": "command", "command": "ls"},
            {"event": "model_request", "turn": 2},
            {"event": "model_reply", "input_tokens": 150, "output_tokens": 40},
        ],
    )
    task = load_suite(CHECKOUT_SUITE).task("happy-path")
    spec = TrialSpec(
        run_id="run-1",
        task_slug="happy-path",
        trial_index=0,
        seed=1,
        adapter_spec="langgraph:checkout_agent.agent:graph",
    )
    runner = TrialRunner(object(), suite_root=tmp_path)  # type: ignore[arg-type]

    result = runner._verdict(
        task,
        spec,
        ContainerPhase(timed_out=True, exit_code=None, container_removed=True),
        tmp_path,
        started=0,
        attempt=1,
    )

    assert result.outcome is Outcome.TIMEOUT
    assert result.efficiency.turns == 2
    assert result.efficiency.tool_calls == 1
    assert result.efficiency.input_tokens == 270
    assert result.efficiency.output_tokens == 70
