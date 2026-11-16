"""Shell commands the grading sandbox runs — pure functions, no Docker."""

from __future__ import annotations

import pytest

from meridian.runtime.pytest_runner import pytest_command, script_command

pytestmark = pytest.mark.unit


def test_pytest_command_for_a_workdir_relative_target() -> None:
    assert (
        pytest_command(workdir="/work", target="/work/tests/test_ok.py", selector=None)
        == 'tar -xf - -C / && cd "/work" && python3 -m pytest -q "tests/test_ok.py"'
    )


def test_pytest_command_quotes_a_target_with_spaces() -> None:
    assert (
        pytest_command(workdir="/work", target="/work/tests/my test.py", selector=None)
        == 'tar -xf - -C / && cd "/work" && python3 -m pytest -q "tests/my test.py"'
    )


def test_pytest_command_appends_the_selector() -> None:
    assert (
        pytest_command(workdir="/work", target="/work/tests/test_ok.py", selector="test_one")
        == 'tar -xf - -C / && cd "/work" && python3 -m pytest -q "tests/test_ok.py::test_one"'
    )


def test_script_command_quotes_paths_and_markers_the_reward() -> None:
    assert (
        script_command(
            workdir="/work",
            script_path="/tests/run tests.sh",
            reward_path="/logs/my reward.txt",
            marker="abc123",
        )
        == 'tar -xf - -C / && mkdir -p "$(dirname "/logs/my reward.txt")" && cd "/work" '
        '&& { if command -v bash >/dev/null 2>&1; then bash "/tests/run tests.sh"; '
        'else sh "/tests/run tests.sh"; fi; }; code=$?; '
        'printf "\\nabc123:%s:abc123\\n" "$(cat "/logs/my reward.txt" 2>/dev/null)"; exit $code'
    )
