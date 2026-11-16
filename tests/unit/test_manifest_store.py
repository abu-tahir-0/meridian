"""Saving and loading a run archive — no daemon, no network."""

from __future__ import annotations

from pathlib import Path

import pytest

from meridian.manifest import store
from meridian.models.manifest import Manifest, ManifestModel, ManifestTask, ManifestTrial
from meridian.models.run import Outcome, RunResult, RunStatus, TaskResult, TrialResult

pytestmark = pytest.mark.unit


def manifest() -> Manifest:
    return Manifest(
        meridian_version="0.1.0",
        created_unix_ms=1_700_000_000_000,
        commit_sha="abc123",
        suite_slug="checkout-agent",
        suite_version=1,
        suite_content_hash="sha256:" + "11" * 32,
        config_hash="sha256:" + "22" * 32,
        adapter_spec="subprocess",
        n_trials=1,
        k=1,
        tasks=(
            ManifestTask(
                slug="happy-path",
                definition_hash="sha256:" + "33" * 32,
                snapshot_digest="sha256:" + "44" * 32,
            ),
        ),
        trials=(ManifestTrial(task_slug="happy-path", trial_index=0, seed=7),),
        models=(ManifestModel(model_id="stub", provider="stub"),),
    )


def result() -> RunResult:
    return RunResult(
        run_id="run-1",
        suite_slug="checkout-agent",
        suite_version=1,
        status=RunStatus.COMPLETE,
        k=1,
        n_requested=1,
        tasks=(
            TaskResult(
                task_slug="happy-path",
                trials=(
                    TrialResult(
                        run_id="run-1",
                        task_slug="happy-path",
                        trial_index=0,
                        seed=7,
                        outcome=Outcome.PASS,
                    ),
                ),
            ),
        ),
    )


def test_saving_and_loading_a_run_round_trips(tmp_path: Path) -> None:
    manifest_before = manifest()
    result_before = result()

    directory = store.save(manifest_before, result_before, root=tmp_path)

    assert directory == tmp_path / "run-1"
    assert store.load_manifest("run-1", root=tmp_path) == manifest_before
    assert store.load_result("run-1", root=tmp_path) == result_before


def test_loading_a_missing_manifest_raises(tmp_path: Path) -> None:
    with pytest.raises(store.RunNotFoundError):
        store.load_manifest("nope", root=tmp_path)


def test_loading_a_missing_result_raises(tmp_path: Path) -> None:
    store.save(manifest(), result(), root=tmp_path)
    (tmp_path / "run-1" / store.RESULT_FILE).unlink()

    with pytest.raises(store.RunNotFoundError):
        store.load_result("run-1", root=tmp_path)


def test_loading_a_corrupt_manifest_raises(tmp_path: Path) -> None:
    directory = tmp_path / "run-1"
    directory.mkdir()
    (directory / store.MANIFEST_FILE).write_text("{not json", encoding="utf-8")

    with pytest.raises(ValueError):
        store.load_manifest("run-1", root=tmp_path)


def test_loading_a_corrupt_result_raises(tmp_path: Path) -> None:
    store.save(manifest(), result(), root=tmp_path)
    (tmp_path / "run-1" / store.RESULT_FILE).write_text("{not json", encoding="utf-8")

    with pytest.raises(ValueError):
        store.load_result("run-1", root=tmp_path)
