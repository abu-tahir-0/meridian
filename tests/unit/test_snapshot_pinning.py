"""Pinning rules — pure logic, no daemon required."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from meridian.snapshots.build import rewrite_suite_snapshots
from meridian.snapshots.registry import (
    UnpinnedReferenceError,
    is_digest,
    read_snapshot_ref,
    require_digest,
    resolve_digest,
    write_snapshot_ref,
)
from meridian.suites.loader import load_suite
from meridian.suites.validate import SuiteValidationError
from tests.conftest import CHECKOUT_SUITE

DIGEST = "sha256:" + "0" * 64


@pytest.mark.unit
@pytest.mark.parametrize(
    "reference",
    ["checkout:latest", "checkout", "sha256:short", "sha512:" + "0" * 64, ""],
)
def test_tags_are_not_digests(reference: str) -> None:
    assert not is_digest(reference)
    with pytest.raises(UnpinnedReferenceError):
        require_digest(reference)


@pytest.mark.unit
def test_a_digest_passes_the_pin_check() -> None:
    assert require_digest(DIGEST) == DIGEST


@pytest.mark.unit
def test_the_loader_rejects_a_tagged_snapshot(tmp_path: Path) -> None:
    """The rule is enforced at load time, not only where the container starts."""
    suite = tmp_path / "checkout-agent"
    shutil.copytree(CHECKOUT_SUITE, suite)
    task = suite / "tasks" / "happy-path.yaml"
    pinned = load_suite(suite).task("happy-path").environment.snapshot
    task.write_text(task.read_text().replace(pinned, "checkout:latest"))

    with pytest.raises(SuiteValidationError) as excinfo:
        load_suite(suite)
    assert "unpinned_snapshot" in excinfo.value.codes()


@pytest.mark.unit
def test_snapshot_ref_refuses_to_record_a_tag(tmp_path: Path) -> None:
    with pytest.raises(UnpinnedReferenceError):
        write_snapshot_ref(tmp_path, digest="checkout:latest", tag="t", platform="linux/arm64")


@pytest.mark.unit
def test_repinning_refuses_a_tag(tmp_path: Path) -> None:
    suite = tmp_path / "checkout-agent"
    shutil.copytree(CHECKOUT_SUITE, suite)
    with pytest.raises(UnpinnedReferenceError):
        rewrite_suite_snapshots(suite, "checkout:latest")


class _FakeImages:
    def __init__(self, local: dict[str, str] | None = None) -> None:
        self.local = dict(local or {})
        self.pulled: list[str] = []

    def get(self, reference: str) -> SimpleNamespace:
        if reference not in self.local:
            raise LookupError(reference)
        return SimpleNamespace(id=self.local[reference])

    def pull(self, reference: str) -> None:
        self.pulled.append(reference)


class _FakeClient:
    def __init__(self, local: dict[str, str] | None = None) -> None:
        self.images = _FakeImages(local)


@pytest.mark.unit
def test_a_local_digest_is_returned_without_pulling() -> None:
    client = _FakeClient({DIGEST: DIGEST})
    assert resolve_digest(client, DIGEST) == DIGEST  # type: ignore[arg-type]
    assert client.images.pulled == []


@pytest.mark.unit
def test_a_missing_digest_is_pulled_by_digest() -> None:
    client = _FakeClient()
    assert resolve_digest(client, DIGEST) == DIGEST  # type: ignore[arg-type]
    assert client.images.pulled == [DIGEST]


@pytest.mark.unit
def test_a_tag_resolves_to_the_local_image_digest() -> None:
    digest = "sha256:" + "ab" * 32
    client = _FakeClient({"checkout:latest": digest})
    assert resolve_digest(client, "checkout:latest") == digest  # type: ignore[arg-type]
    assert client.images.pulled == []


@pytest.mark.unit
def test_a_tag_whose_image_id_is_not_a_digest_is_rejected() -> None:
    client = _FakeClient({"checkout:latest": "checkout:latest"})
    with pytest.raises(UnpinnedReferenceError):
        resolve_digest(client, "checkout:latest")  # type: ignore[arg-type]


@pytest.mark.unit
def test_the_snapshot_ref_round_trips(tmp_path: Path) -> None:
    path = write_snapshot_ref(
        tmp_path, digest=DIGEST, tag="checkout:latest", platform="linux/arm64"
    )
    assert read_snapshot_ref(tmp_path) == DIGEST
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "digest": DIGEST,
        "tag": "checkout:latest",
        "platform": "linux/arm64",
    }


@pytest.mark.unit
def test_the_snapshot_ref_is_absent_before_a_build(tmp_path: Path) -> None:
    assert read_snapshot_ref(tmp_path) is None


def test_pinning_handles_quoted_and_bare_digests(tmp_path: Path) -> None:
    """Hand-written tasks quote the digest; importer-generated YAML does not."""
    from meridian.snapshots.build import rewrite_task_snapshot

    old = "sha256:" + "0" * 64
    new = "sha256:" + "ab" * 32
    quoted = tmp_path / "quoted.yaml"
    quoted.write_text(f'environment:\n  snapshot: "{old}"\n  workdir: /work\n')
    bare = tmp_path / "bare.yaml"
    bare.write_text(f"environment:\n  snapshot: {old}\n  workdir: /work\n")
    assert rewrite_task_snapshot(quoted, new) and rewrite_task_snapshot(bare, new)
    assert f'snapshot: "{new}"' in quoted.read_text()
    assert f"snapshot: {new}\n" in bare.read_text()
