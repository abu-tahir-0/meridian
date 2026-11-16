"""Running verifiers against extracted state, in a throwaway container.

The files being run come from a directory the agent controlled. Running them on
the host would give an evaluated agent code execution on the machine doing the
evaluating, which is the one thing an eval harness must never do.

So grading gets its own container: the task's own snapshot, the same isolation
options as a trial, and the extracted state streamed in on stdin. It is destroyed
afterwards like any other. Two verifier styles share that sandbox: pytest, and a
benchmark-supplied script that writes a reward.
"""

from __future__ import annotations

import contextlib
import io
import secrets
import socket
import tarfile
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

from meridian.grading.graders.pytest_grader import PytestOutcome
from meridian.grading.graders.script_grader import ScriptOutcome
from meridian.runtime.isolation import RUN_LABEL, TRIAL_GID, TRIAL_LABEL, TRIAL_UID

if TYPE_CHECKING:  # pragma: no cover - typing only
    from docker import DockerClient

GRADING_TIMEOUT_SECONDS = 120
PYTEST_INTERNAL_ERROR = 3

# Writable, empty-at-start mounts a verifier may use. /tests is where benchmark
# ecosystems put their hidden test files; /logs is where they write a reward. The
# rootfs stays read-only, so these are the only places a verifier can write besides
# the workdir and /tmp -- and, being fresh tmpfs, an agent cannot pre-populate them.
GRADING_TMPFS = {
    "/tmp": "size=64m,mode=1777",
    "/tests": "size=64m,mode=1777",
    "/logs": "size=16m,mode=1777",
}


def pytest_command(*, workdir: str, target: str, selector: str | None) -> str:
    """The shell command that runs a pytest target inside the grading container."""
    relative = target.removeprefix(workdir).lstrip("/") or "."
    argument = f"{relative}::{selector}" if selector else relative
    return f'tar -xf - -C / && cd "{workdir}" && python3 -m pytest -q "{argument}"'


def script_command(*, workdir: str, script_path: str, reward_path: str, marker: str) -> str:
    """The shell command that runs a benchmark verifier and prints its reward.

    Harbor guarantees the reward directory exists; verifiers write to it without
    creating it, so the sandbox must. The reward is wrapped in per-run markers so
    agent output cannot be mistaken for it.
    """
    return (
        f'tar -xf - -C / && mkdir -p "$(dirname "{reward_path}")" && cd "{workdir}" '
        f'&& {{ if command -v bash >/dev/null 2>&1; then bash "{script_path}"; '
        f'else sh "{script_path}"; fi; }}; code=$?; '
        f'printf "\\n{marker}:%s:{marker}\\n" "$(cat "{reward_path}" 2>/dev/null)"; exit $code'
    )


class GradingContainerError(RuntimeError):
    """The grading container itself failed (not the verifier inside it)."""


def _archive(
    state_dir: Path, grader_files: Mapping[str, bytes] | None = None, *, workdir: str
) -> bytes:
    """Tar the extracted state under `workdir`, then overlay the grader-only files.

    Member names are absolute destinations without the leading slash, so the
    stream extracts with `-C /` and lands the state in the workdir and the grader
    files wherever the task put them (the workdir or /tests).

    A grader file wins over an agent-written file at the same path: tests the
    agent could edit would not be tests.
    """
    prefix = workdir.strip("/")
    files: dict[str, bytes] = {
        f"{prefix}/{path.relative_to(state_dir).as_posix()}".lstrip("/"): path.read_bytes()
        for path in sorted(state_dir.rglob("*"))
        if path.is_file()
    }
    for destination, data in (grader_files or {}).items():
        files[destination.lstrip("/")] = data
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        for name in sorted(files):
            data = files[name]
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            info.mode = 0o644
            info.mtime = 0
            info.uid, info.gid = TRIAL_UID, TRIAL_GID
            tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


class _GradingSandbox:
    """Runs one shell command in a fresh, locked-down container with state loaded."""

    def __init__(
        self,
        client: DockerClient,
        *,
        image: str,
        run_id: str,
        workdir: str,
        grader_files: Mapping[str, bytes] | None,
    ) -> None:
        self._client = client
        self._image = image
        self._run_id = run_id
        self._workdir = workdir
        self._grader_files = dict(grader_files or {})

    def run(
        self,
        state_dir: Path,
        command: str,
        tag: str,
        *,
        environment: Mapping[str, str] | None = None,
        timeout_seconds: int = GRADING_TIMEOUT_SECONDS,
    ) -> tuple[int, str]:
        volume = self._client.volumes.create(
            name=f"meridian-grade-{self._run_id}-{abs(hash(tag)) % 10**8}",
            labels={RUN_LABEL: self._run_id},
        )
        container = None
        try:
            container = self._client.containers.create(
                image=self._image,
                entrypoint=["/bin/sh", "-c"],
                command=[command],
                user=f"{TRIAL_UID}:{TRIAL_GID}",
                read_only=True,
                cap_drop=["ALL"],
                security_opt=["no-new-privileges:true"],
                # Grading runs agent-influenced code, so it gets no network at
                # all -- not even the proxy. A grader has nothing to say to a model.
                network_mode="none",
                pids_limit=128,
                mem_limit="1024m",
                tmpfs=GRADING_TMPFS,
                volumes={volume.name: {"bind": self._workdir, "mode": "rw"}},
                working_dir=self._workdir,
                # Only what the task's verifier declares; never the harness's environment.
                environment=dict(environment or {}),
                labels={RUN_LABEL: self._run_id, TRIAL_LABEL: "grading"},
                stdin_open=True,
                detach=True,
                auto_remove=False,
            )
            container.start()
            self._send(container, _archive(state_dir, self._grader_files, workdir=self._workdir))
            status = container.wait(timeout=timeout_seconds)
            output = container.logs().decode("utf-8", errors="replace")
            return int(status.get("StatusCode", 1)), output
        except Exception as exc:
            raise GradingContainerError(f"{type(exc).__name__}: {exc}") from exc
        finally:
            if container is not None:
                with contextlib.suppress(Exception):
                    container.remove(force=True, v=True)
            with contextlib.suppress(Exception):
                volume.remove(force=True)

    @staticmethod
    def _send(container: Any, payload: bytes) -> None:
        attached = container.attach_socket(params={"stdin": 1, "stream": 1})
        raw = getattr(attached, "_sock", attached)
        response = getattr(attached, "_response", None)
        try:
            raw.sendall(payload)
            raw.shutdown(socket.SHUT_WR)
        finally:
            for closeable in (response, attached, raw):
                if closeable is not None:
                    with contextlib.suppress(Exception):
                        closeable.close()


class ContainerPytestRunner:
    """A `PytestRunner` backed by a throwaway container."""

    def __init__(
        self,
        client: DockerClient,
        *,
        image: str,
        run_id: str,
        workdir: str = "/work",
        grader_files: Mapping[str, bytes] | None = None,
    ) -> None:
        self._workdir = workdir
        self._sandbox = _GradingSandbox(
            client, image=image, run_id=run_id, workdir=workdir, grader_files=grader_files
        )

    def __call__(self, state_dir: Path, target: str, selector: str | None) -> PytestOutcome:
        command = pytest_command(workdir=self._workdir, target=target, selector=selector)
        try:
            exit_code, output = self._sandbox.run(state_dir, command, target)
        except GradingContainerError as exc:
            return PytestOutcome(
                exit_code=PYTEST_INTERNAL_ERROR,
                output=f"the grading container failed: {exc}",
            )
        return PytestOutcome(exit_code=exit_code, output=output)


class ContainerScriptRunner:
    """A `ScriptRunner` backed by a throwaway container.

    Runs a benchmark-supplied verifier script and reads back the reward it wrote.
    The reward is returned between two unguessable markers, so nothing the agent
    manages to print can be mistaken for it.
    """

    def __init__(
        self,
        client: DockerClient,
        *,
        image: str,
        run_id: str,
        workdir: str = "/work",
        grader_files: Mapping[str, bytes] | None = None,
    ) -> None:
        self._workdir = workdir
        self._sandbox = _GradingSandbox(
            client, image=image, run_id=run_id, workdir=workdir, grader_files=grader_files
        )

    def __call__(
        self,
        state_dir: Path,
        script_path: str,
        reward_path: str,
        *,
        env: Mapping[str, str] | None = None,
        timeout_seconds: int = GRADING_TIMEOUT_SECONDS,
    ) -> ScriptOutcome:
        marker = secrets.token_hex(16)
        command = script_command(
            workdir=self._workdir, script_path=script_path, reward_path=reward_path, marker=marker
        )
        try:
            exit_code, output = self._sandbox.run(
                state_dir, command, script_path, environment=env, timeout_seconds=timeout_seconds
            )
        except GradingContainerError as exc:
            return ScriptOutcome(exit_code=1, output="", reward_text=None, infra_error=str(exc))
        return ScriptOutcome(
            exit_code=exit_code,
            output=_strip_reward(output, marker),
            reward_text=_extract_reward(output, marker),
        )


def _extract_reward(output: str, marker: str) -> str | None:
    """The reward between this run's markers, or None if they are absent."""
    start = output.rfind(f"{marker}:")
    if start == -1:
        return None
    start += len(marker) + 1
    end = output.find(f":{marker}", start)
    return output[start:end] if end != -1 else None


def _strip_reward(output: str, marker: str) -> str:
    start = output.rfind(f"\n{marker}:")
    return output[:start] if start != -1 else output
