"""The terminal agent's shell tool, run on the host in a temp directory."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "fixtures"))

from terminal_agent import tools

pytestmark = pytest.mark.unit


def test_runs_in_the_working_directory(tmp_path: Path) -> None:
    (tmp_path / "hello.txt").write_text("hi")
    result = tools.run_bash("cat hello.txt && pwd", workdir=str(tmp_path))
    assert result["exit_code"] == 0 and result["error"] is None
    assert "hi" in result["output"] and str(tmp_path) in result["output"]


def test_a_failing_command_is_a_result_not_an_exception(tmp_path: Path) -> None:
    result = tools.run_bash("echo oops >&2; exit 3", workdir=str(tmp_path))
    assert result["exit_code"] == 3 and "oops" in result["output"]
    assert "[exit code 3]" in tools.format_result(result)


def test_a_command_that_runs_too_long_is_killed(tmp_path: Path) -> None:
    result = tools.run_bash("echo started; sleep 30", workdir=str(tmp_path), timeout_seconds=1)
    assert result["exit_code"] is None and "timed out" in result["error"]
    assert "started" in result["output"]


def test_the_timeout_is_bounded(tmp_path: Path) -> None:
    # a model asking for an hour is clamped, so it cannot stall a trial
    result = tools.run_bash("true", workdir=str(tmp_path), timeout_seconds=10**9)
    assert result["exit_code"] == 0


def test_a_missing_working_directory_is_reported(tmp_path: Path) -> None:
    result = tools.run_bash("true", workdir=str(tmp_path / "nope"))
    assert result["exit_code"] is None and result["error"]


def test_long_output_keeps_head_and_tail() -> None:
    text = "A" * 5000 + "MIDDLE" + "Z" * 5000
    out = tools.truncate(text, limit=100)
    assert out.startswith("A" * 50) and out.endswith("Z" * 50)
    assert "characters omitted" in out and len(out) < 200


def test_short_output_is_untouched() -> None:
    assert tools.truncate("short") == "short"


def test_commands_do_not_inherit_the_harness_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GROQ_API_KEY", "gsk_should_not_leak_into_commands")
    result = tools.run_bash("env", workdir=str(tmp_path))
    assert "gsk_should_not_leak" not in result["output"]
    assert "HOME=/tmp" in result["output"]


def test_stdin_is_closed_so_interactive_commands_cannot_hang(tmp_path: Path) -> None:
    result = tools.run_bash("cat", workdir=str(tmp_path), timeout_seconds=5)
    assert result["exit_code"] == 0 and result["output"] == ""


def test_format_result_for_no_output() -> None:
    assert tools.format_result({"exit_code": 0, "output": "", "error": None}) == "[no output]"


def test_schema_names_the_one_tool() -> None:
    assert [t["name"] for t in tools.TOOL_SCHEMAS] == ["bash"]
    assert tools.TOOL_SCHEMAS[0]["input_schema"]["required"] == ["command"]


def test_the_trial_context_wins_over_the_environment_and_bad_values_are_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MERIDIAN_AGENT_MAX_TURNS", "12")
    assert tools.agent_limits({})["max_turns"] == 12
    assert tools.agent_limits({"max_turns": 7})["max_turns"] == 7
    assert tools.agent_limits({"max_turns": 0})["max_turns"] == 12
    assert tools.agent_limits({"max_turns": "many"})["max_turns"] == 12

    monkeypatch.delenv("MERIDIAN_AGENT_MAX_TURNS")
    assert tools.agent_limits({})["max_turns"] == tools.MAX_TURNS
    assert tools.agent_limits({"request_timeout_seconds": 2.5})["request_timeout_seconds"] == 2.5


def test_every_limit_has_a_context_key_and_an_environment_variable() -> None:
    limits = tools.agent_limits({})
    assert set(limits) == {
        "max_turns",
        "request_timeout_seconds",
        "max_output_chars",
        "default_command_timeout_seconds",
        "max_command_timeout_seconds",
    }
    assert all(value > 0 for value in limits.values())


def test_the_schema_advertises_the_configured_timeout() -> None:
    [bash] = tools.tool_schemas(5, 9)
    description = bash["input_schema"]["properties"]["timeout_seconds"]["description"]
    assert "at most 9" in description and "default 5" in description


def test_run_bash_honours_the_configured_limits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import subprocess

    seen: dict[str, object] = {}

    def fake(argv: object, **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        seen.update(kwargs)
        return subprocess.CompletedProcess(argv, 0, b"A" * 500, b"")

    monkeypatch.setattr(tools.subprocess, "run", fake)
    result = tools.run_bash(
        "true",
        workdir=str(tmp_path),
        timeout_seconds=999,
        max_timeout_seconds=7,
        max_output_chars=40,
    )
    assert seen["timeout"] == 7, "the model cannot raise the operator's ceiling"
    assert "characters omitted" in result["output"] and len(result["output"]) < 120


# -- the transcript --------------------------------------------------------------------

from terminal_agent import transcript  # noqa: E402


def test_transcript_lines_survive_and_are_clipped(tmp_path: Path) -> None:
    transcript.record(str(tmp_path), "command", command="ls", output="x" * 1000, exit_code=0)
    lines = (tmp_path / transcript.TRANSCRIPT_RELATIVE).read_text().splitlines()
    import json

    entry = json.loads(lines[0])
    assert entry["event"] == "command" and entry["exit_code"] == 0
    assert entry["output"].startswith("x" * 400) and "more]" in entry["output"]


def test_a_transcript_can_never_fail_a_trial() -> None:
    # an unwritable location is swallowed, not raised
    transcript.record("/proc/definitely/not/writable", "command", command="ls")
