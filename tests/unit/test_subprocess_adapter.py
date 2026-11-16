"""The subprocess adapter against tiny real programs."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from meridian.adapters.base import AdapterSpec, AdapterSpecError
from meridian.adapters.subprocess_adapter import SubprocessAdapter
from meridian.models.adapter import TrialContext

pytestmark = pytest.mark.unit

AGENT_MODULE = """
import sys


def succeed(ctx):
    return [sys.executable, "-c", "print('agent out')"]


def fail(ctx):
    return [sys.executable, "-c", "import sys; print('boom', file=sys.stderr); sys.exit(3)"]


def both(ctx):
    return [
        sys.executable,
        "-c",
        "import sys; print('to stdout'); print('to stderr', file=sys.stderr)",
    ]


def empty(ctx):
    return []


NOT_CALLABLE = 1
"""


def make_spec(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, attribute: str) -> AdapterSpec:
    monkeypatch.syspath_prepend(str(tmp_path))
    (tmp_path / "agent_under_test.py").write_text(AGENT_MODULE, encoding="utf-8")
    return AdapterSpec.parse(f"subprocess:agent_under_test:{attribute}")


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


async def test_a_successful_program_is_completed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = SubprocessAdapter(make_spec(tmp_path, monkeypatch, "succeed"))

    result = await adapter.invoke(context(tmp_path))

    assert result.completed
    assert result.exit_code == 0
    assert result.error is None
    assert result.turns == 1
    assert "agent out" in result.stdout_tail
    assert result.tool_calls[0].name == sys.executable
    assert result.tool_calls[0].ok


async def test_a_non_zero_exit_is_an_agent_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = SubprocessAdapter(make_spec(tmp_path, monkeypatch, "fail"))

    result = await adapter.invoke(context(tmp_path))

    assert not result.completed
    assert result.exit_code == 3
    assert result.error == "exit code 3"
    assert result.tool_calls[0].ok is False
    assert "boom" in result.stdout_tail


async def test_stdout_and_stderr_are_captured_together(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = SubprocessAdapter(make_spec(tmp_path, monkeypatch, "both"))

    result = await adapter.invoke(context(tmp_path))

    assert "to stdout" in result.stdout_tail
    assert "to stderr" in result.stdout_tail


async def test_a_target_that_is_not_callable_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = SubprocessAdapter(make_spec(tmp_path, monkeypatch, "NOT_CALLABLE"))

    with pytest.raises(AdapterSpecError):
        await adapter.invoke(context(tmp_path))


async def test_an_empty_argv_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = SubprocessAdapter(make_spec(tmp_path, monkeypatch, "empty"))

    with pytest.raises(AdapterSpecError):
        await adapter.invoke(context(tmp_path))
