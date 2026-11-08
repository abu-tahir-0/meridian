"""`suite build-images` keeps the reason a task was dropped, not just the warning."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from meridian import cli
from meridian.cli import app
from meridian.snapshots.build import SnapshotBuildError

pytestmark = pytest.mark.unit

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "invalid-suites"


def _suite_with_one_image(tmp_path: Path) -> Path:
    suite = tmp_path / "suite"
    shutil.copytree(FIXTURES / "_valid-control", suite)
    tasks = suite / "tasks"
    (tasks / "valid-task.yaml").write_bytes((tasks / "valid-task-0.yaml").read_bytes())
    (tasks / "valid-task-0.yaml").unlink()
    (suite / "images" / "valid-task").mkdir(parents=True)
    (suite / "images" / "valid-task" / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
    return suite


def test_a_dropped_task_leaves_its_reason_in_the_suite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    suite = _suite_with_one_image(tmp_path)
    monkeypatch.setattr(cli, "get_client", lambda: object())

    def fail_build(*args: object, **kwargs: object) -> str:
        raise SnapshotBuildError(
            'failed to solve: process "/bin/sh -c apk add git" did not complete'
        )

    monkeypatch.setattr(cli, "build_snapshot", fail_build)

    result = CliRunner().invoke(app, ["suite", "build-images", str(suite), "--drop-failed"])

    assert result.exit_code == 2  # the suite's only task failed
    reasons = (suite / "dropped" / "REASONS.md").read_text(encoding="utf-8")
    assert "valid-task" in reasons
    assert "failed to solve" in reasons
    assert not (suite / "tasks" / "valid-task.yaml").exists()
