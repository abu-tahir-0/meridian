"""Manifest assembly: the reproducibility contract depends on these hashes."""

from __future__ import annotations

from pathlib import Path

import pytest

from meridian.config import MeridianConfig
from meridian.manifest.build import (
    build_manifest,
    directory_hash,
    environment_facts,
    file_hash,
    git_sha,
)
from meridian.suites.loader import load_suite
from tests.conftest import CHECKOUT_SUITE

pytestmark = pytest.mark.unit


def test_directory_hash_is_stable_across_calls(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("one")
    assert directory_hash(tmp_path) == directory_hash(tmp_path)
    assert directory_hash(tmp_path).startswith("sha256:")


def test_directory_hash_changes_when_content_changes(tmp_path: Path) -> None:
    (tmp_path / "prompt.md").write_text("be helpful")
    before = directory_hash(tmp_path)
    (tmp_path / "prompt.md").write_text("be terse")
    assert directory_hash(tmp_path) != before


def test_directory_hash_changes_when_a_file_is_renamed(tmp_path: Path) -> None:
    (tmp_path / "a.md").write_text("same")
    before = directory_hash(tmp_path)
    (tmp_path / "a.md").rename(tmp_path / "b.md")
    assert directory_hash(tmp_path) != before


def test_directory_hash_ignores_pycache(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("x = 1")
    before = directory_hash(tmp_path)
    cache = tmp_path / "__pycache__"
    cache.mkdir()
    (cache / "a.cpython-312.pyc").write_bytes(b"\x00junk")
    assert directory_hash(tmp_path) == before


def test_file_hash_matches_known_sha256(tmp_path: Path) -> None:
    path = tmp_path / "f"
    path.write_bytes(b"abc")
    assert file_hash(path) == (
        "sha256:ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    )


def test_environment_facts_includes_lock_hash_only_when_lock_exists(tmp_path: Path) -> None:
    assert "uv_lock" not in environment_facts(tmp_path)
    (tmp_path / "uv.lock").write_text("lock")
    assert environment_facts(tmp_path)["uv_lock"] == file_hash(tmp_path / "uv.lock")
    assert "uv_lock" not in environment_facts(None)


def test_git_sha_is_none_outside_a_repository(tmp_path: Path) -> None:
    assert git_sha(tmp_path) is None


def test_build_manifest_is_deterministic_and_covers_every_trial() -> None:
    suite = load_suite(CHECKOUT_SUITE)
    config = MeridianConfig()

    def build() -> object:
        return build_manifest(
            suite=suite, config=config, run_id="run-1", created_unix_ms=1_700_000_000_000
        )

    first = build_manifest(
        suite=suite, config=config, run_id="run-1", created_unix_ms=1_700_000_000_000
    )
    assert first == build()
    assert len(first.tasks) == len(suite.tasks)
    assert len(first.trials) == len(suite.tasks) * config.execution.n_trials
    assert first.n_trials == config.execution.n_trials
    assert first.sut_content_hash is None


def test_build_manifest_seeds_depend_on_run_id() -> None:
    suite = load_suite(CHECKOUT_SUITE)
    config = MeridianConfig()
    a = build_manifest(suite=suite, config=config, run_id="run-a", created_unix_ms=1)
    b = build_manifest(suite=suite, config=config, run_id="run-b", created_unix_ms=1)
    assert [t.seed for t in a.trials] != [t.seed for t in b.trials]


def test_build_manifest_records_docker_version_and_sut_hash(tmp_path: Path) -> None:
    (tmp_path / "agent.py").write_text("print(1)")
    manifest = build_manifest(
        suite=load_suite(CHECKOUT_SUITE),
        config=MeridianConfig(),
        run_id="r",
        created_unix_ms=1,
        sut_root=tmp_path,
        docker_version="27.0.1",
    )
    assert manifest.env["docker"] == "27.0.1"
    assert manifest.sut_content_hash == directory_hash(tmp_path)
