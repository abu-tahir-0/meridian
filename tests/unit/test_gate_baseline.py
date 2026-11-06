"""Baseline resolution against a real throwaway git repository."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from meridian.gate.baseline import BaselineError, merge_base, worktree_at

pytestmark = pytest.mark.unit


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q", "-b", "main")
    (tmp_path / "f.txt").write_text("base")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-q", "-m", "base")
    return tmp_path


def test_merge_base_finds_the_divergence_point(repo: Path) -> None:
    base = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "-b", "feature")
    (repo / "f.txt").write_text("head")
    _git(repo, "commit", "-q", "-am", "head")
    assert merge_base(repo, "main") == base


def test_merge_base_is_none_for_an_unknown_ref(repo: Path) -> None:
    assert merge_base(repo, "does-not-exist") is None


def test_worktree_checks_out_the_sha_and_cleans_up(repo: Path) -> None:
    base = _git(repo, "rev-parse", "HEAD")
    (repo / "f.txt").write_text("changed")
    _git(repo, "commit", "-q", "-am", "next")

    with worktree_at(repo, base) as tree:
        assert (tree / "f.txt").read_text() == "base"
        parent = tree.parent
    assert not parent.exists()
    assert (repo / "f.txt").read_text() == "changed"
    assert "meridian-baseline" not in _git(repo, "worktree", "list")


def test_worktree_on_a_missing_sha_is_a_harness_error(repo: Path) -> None:
    with pytest.raises(BaselineError, match="fetch-depth"), worktree_at(repo, "0" * 40):
        pass
