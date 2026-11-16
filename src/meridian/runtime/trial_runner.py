"""The trial lifecycle — the heart of Rule 1.

The lifecycle is fixed and the ordering is load-bearing:

    resolve → create → start → materialize → wait → extract → destroy → grade

Extraction happens **before** removal and inside the same `try`. Destruction
happens in a `finally`, unconditionally, and whether it succeeded is recorded —
a leaked container is a harness error, not a silent condition. Grading happens
outside the container, against extracted state, so a compromised agent cannot
influence its own verdict.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import shutil
import socket
import tempfile
import time
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from meridian.adapters.base import AdapterSpec, AdapterSpecError
from meridian.models.adapter import AdapterResult
from meridian.models.run import (
    AssertionOutcome,
    AssertionResult,
    Efficiency,
    ErrorCause,
    Outcome,
    TrialResult,
    TrialSpec,
)
from meridian.models.task import TaskDefinition
from meridian.runtime import payload as payload_module
from meridian.runtime.isolation import (
    ISOLATED,
    RUN_LABEL,
    IsolationPolicy,
    container_kwargs,
)
from meridian.snapshots.registry import require_digest

if TYPE_CHECKING:  # pragma: no cover - typing only
    from docker import DockerClient

# A grader takes the task and the extracted final state and returns one result
# per assertion. It never sees the container, and it never sees the transcript
# as an input to the verdict (Rule 2).
Grader = Callable[[TaskDefinition, Path, AdapterResult], Sequence[AssertionResult]]

HARNESS_RETRIES = 1
STOP_GRACE_SECONDS = 2
PAYLOAD_POLL_SECONDS = 0.05


@dataclass(frozen=True)
class UpstreamState:
    """A trial's cumulative upstream-call record, as the proxy reports it."""

    failures: int = 0
    last_failed: bool = False
    kind: str | None = None
    waited_ms: int = 0


UpstreamProbe = Callable[[str, int], Awaitable[UpstreamState]]


# Time the proxy spends backing off from a throttling provider is not the agent's
# time. It is added to the deadline, but bounded: a provider that never recovers
# must end the trial, not hang it.
MAX_THROTTLE_EXTENSION_FACTOR = 2.0
WAIT_POLL_SECONDS = 2.0


def effective_deadline_seconds(timeout_seconds: float, throttled_seconds: float) -> float:
    """The task's deadline plus the provider backoff it lost, capped at 2x the deadline."""
    extension = min(max(throttled_seconds, 0.0), timeout_seconds * MAX_THROTTLE_EXTENSION_FACTOR)
    return timeout_seconds + extension


TRANSCRIPT_RELATIVE = ".meridian/transcript.jsonl"
TRANSCRIPT_TAIL_CHARS = 1800


def transcript_tail(state_dir: Path, limit: int = TRANSCRIPT_TAIL_CHARS) -> str:
    """The end of the agent's own transcript, if it kept one, else an empty string.

    A timeout verdict that says only "exceeded the deadline" cannot be debugged. If
    the agent recorded what it was doing, the last of it explains where the time
    went. Lines are cut on a boundary so the tail stays readable.
    """
    path = state_dir / TRANSCRIPT_RELATIVE
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    if len(text) > limit:
        text = text[-limit:]
        text = text[text.find("\n") + 1 :] if "\n" in text else text
    return text.strip()


def _with_transcript(detail: str, state_dir: Path) -> str:
    tail = transcript_tail(state_dir)
    return f"{detail}; last agent activity:\n{tail}" if tail else detail


def _as_int(value: object) -> int:
    if value is None:
        return 0
    if not isinstance(value, (int, float, str)):
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def efficiency_from_transcript(state_dir: Path) -> Efficiency | None:
    """What a killed agent did, recovered from its own transcript.

    A trial stopped at its deadline never gets to write `result.json`, so its
    turns, tool calls and tokens would otherwise read zero — indistinguishable
    from an agent that did nothing. The transcript is appended and flushed as
    the agent works, so it survives the kill. Returns None when there is no
    readable transcript, which keeps a missing file from looking like zero work.
    """
    try:
        lines = (state_dir / TRANSCRIPT_RELATIVE).read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    turns = tool_calls = input_tokens = output_tokens = 0
    events = 0
    for line in lines:
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        events += 1
        kind = event.get("event")
        if kind == "model_request":
            turns += 1
        elif kind == "command":
            tool_calls += 1
        elif kind == "model_reply":
            input_tokens += _as_int(event.get("input_tokens"))
            output_tokens += _as_int(event.get("output_tokens"))
    if events == 0:
        return None
    return Efficiency(
        turns=turns,
        tool_calls=tool_calls,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )


def attribute_provider_fault(
    result: TrialResult, before: UpstreamState | None, after: UpstreamState
) -> TrialResult:
    """Reclassify a failed trial as a provider fault when the provider is to blame.

    A trial that did not pass and whose *last* upstream call failed (after the
    proxy's own retries) never got a fair attempt: scoring it `fail` would blame
    the agent for a rate limit. It becomes a `harness_error`, which is excluded
    from the denominator and retried once like any other.

    Deliberately narrow, because the opposite mistake -- hiding real agent
    failures as infrastructure noise -- is the one this product exists to
    prevent:

    - only `fail` and `timeout` are considered; a pass stands;
    - the failure must have happened during this attempt (the counter rose);
    - the last call must have failed, so an agent that recovered and then failed
      on its merits keeps its `fail`;
    - requests the provider rejected as invalid are not provider faults.
    """
    if result.outcome not in (Outcome.FAIL, Outcome.TIMEOUT) or before is None:
        return result
    if after.failures <= before.failures or not after.last_failed:
        return result
    cause = ErrorCause.MODEL_PROVIDER if after.kind == "provider" else ErrorCause.INTERNAL
    return result.model_copy(
        update={
            "outcome": Outcome.HARNESS_ERROR,
            "error_cause": cause,
            "detail": (
                f"{cause.value}: {after.failures - before.failures} upstream call(s) failed "
                f"and the last one never recovered; original verdict {result.outcome.value}: "
                f"{result.detail}"
            )[:4000],
        }
    )


class HarnessFault(RuntimeError):
    """Meridian could not run the trial. Retried once, then reported as such.

    Deliberately distinct from any agent-side failure. The two demand opposite
    responses — one is a page, the other is a code review — and a harness that
    conflates them reports numbers better than reality.
    """

    def __init__(
        self,
        message: str,
        *,
        container_removed: bool = True,
        cause: ErrorCause = ErrorCause.SANDBOX,
    ) -> None:
        super().__init__(message)
        self.container_removed = container_removed
        self.cause = cause


@dataclass(frozen=True)
class ContainerPhase:
    """What the container phase established, before anything is graded."""

    timed_out: bool
    exit_code: int | None
    container_removed: bool
    # Read from the daemon rather than inferred from the exit code, which cannot
    # tell an OOM kill from any other SIGKILL — including the one Meridian sends
    # to enforce a timeout. See `_wait`.
    oom_killed: bool = False
    # Provider backoff added to the deadline, in seconds (0 when none was needed).
    throttle_extension_s: float = 0.0


def _now_ms() -> int:
    return int(time.time() * 1000)


def _no_grader(
    task: TaskDefinition, state_dir: Path, result: AdapterResult
) -> Sequence[AssertionResult]:
    """Default grader: records that nothing was graded, rather than passing."""
    return tuple(
        AssertionResult(
            kind=assertion.kind,
            outcome=AssertionOutcome.ERROR,
            detail="no grader configured for this run",
        )
        for assertion in task.outcome_assertions
    )


async def _to_thread(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    return await asyncio.to_thread(fn, *args, **kwargs)


class TrialRunner:
    """Runs one trial at a time, in its own container, and cleans up after itself."""

    def __init__(
        self,
        client: DockerClient,
        *,
        suite_root: Path,
        sut_root: Path | None = None,
        harness_root: Path | None = None,
        grader: Grader | None = None,
        policy: IsolationPolicy = ISOLATED,
        network_name: str | None = None,
        upstream: UpstreamProbe | None = None,
    ) -> None:
        self._upstream = upstream
        self._client = client
        self._suite_root = suite_root
        self._sut_root = sut_root
        self._harness_root = harness_root or Path(__file__).resolve().parents[1]
        self._grade = grader or _no_grader
        self._policy = policy
        self._network_name = network_name

    # -- lifecycle ---------------------------------------------------------

    async def run_trial(self, task: TaskDefinition, spec: TrialSpec) -> TrialResult:
        """Run one trial, retrying only harness faults, and only once."""
        attempt = 0
        last: TrialResult | None = None
        while attempt <= HARNESS_RETRIES:
            attempt += 1
            result = await self._attempt(task, spec, attempt)
            if result.outcome is not Outcome.HARNESS_ERROR:
                # A `fail` or a `timeout` is a statement about the agent, and
                # retrying it is how a harness reports a number better than
                # reality. This return is the enforcement of that rule.
                return result
            last = result
        assert last is not None
        return last

    async def _attempt(self, task: TaskDefinition, spec: TrialSpec, attempt: int) -> TrialResult:
        started = _now_ms()
        state_dir = Path(tempfile.mkdtemp(prefix=f"meridian-{spec.task_slug}-"))
        # Counters are cumulative per (task, trial), so a retry of the same trial
        # is judged on what happened during *this* attempt only.
        before = await self._upstream(spec.task_slug, spec.trial_index) if self._upstream else None
        try:
            try:
                phase = await self._run_container(task, spec, state_dir)
            except AdapterSpecError as exc:
                return self._harness_error(
                    spec, started, attempt, f"adapter spec: {exc}", ErrorCause.ADAPTER_CONFIG
                )
            except HarnessFault as exc:
                return self._harness_error(
                    spec, started, attempt, str(exc), exc.cause, removed=exc.container_removed
                )
            except Exception as exc:
                return self._harness_error(
                    spec, started, attempt, f"{type(exc).__name__}: {exc}", ErrorCause.INTERNAL
                )

            result = self._verdict(task, spec, phase, state_dir, started, attempt)
            if self._upstream is None:
                return result
            after = await self._upstream(spec.task_slug, spec.trial_index)
            waited = max(after.waited_ms - (before.waited_ms if before else 0), 0)
            result = result.model_copy(
                update={
                    "efficiency": result.efficiency.model_copy(update={"upstream_wait_ms": waited})
                }
            )
            if result.outcome not in (Outcome.FAIL, Outcome.TIMEOUT):
                return result
            return attribute_provider_fault(result, before, after)
        finally:
            shutil.rmtree(state_dir, ignore_errors=True)

    async def _run_container(
        self, task: TaskDefinition, spec: TrialSpec, state_dir: Path
    ) -> ContainerPhase:
        """create → start → materialize → wait → extract → destroy.

        Extraction is inside the try and before destruction; destruction is in
        the finally and unconditional. Reversing either turns a timeout into an
        unexplained empty result.
        """
        digest = require_digest(task.environment.snapshot)
        AdapterSpec.parse(spec.adapter_spec)

        volume_name = self._policy.workdir_volume(spec.run_id, spec.task_slug, spec.trial_index)
        container: Any = None
        volume: Any = None
        removed = True
        try:
            volume = await self._create_volume(volume_name, spec.run_id)
            container = await self._create(task, spec, digest, volume_name)
            await _to_thread(container.start)
            await self._materialize(container, task, spec)

            throttled: Callable[[], Awaitable[float]] | None = None
            if self._upstream is not None:
                probe = self._upstream
                baseline = (await probe(spec.task_slug, spec.trial_index)).waited_ms

                async def throttled() -> float:
                    now = await probe(spec.task_slug, spec.trial_index)
                    return max(now.waited_ms - baseline, 0) / 1000.0

            timed_out, exit_code, oom_killed, extension_s = await self._wait(
                container, task.limits.timeout_seconds, throttled
            )
            await self._extract(container, task, state_dir)
        except HarnessFault as fault:
            if container is not None:
                removed = await self._destroy(container)
                container = None
            await self._destroy_volume(volume)
            volume = None
            raise HarnessFault(str(fault), container_removed=removed, cause=fault.cause) from fault
        finally:
            if container is not None:
                removed = await self._destroy(container)
            # The volume outlives the container by exactly as long as it takes to
            # extract, and not one step longer.
            await self._destroy_volume(volume)

        return ContainerPhase(
            timed_out=timed_out,
            exit_code=exit_code,
            container_removed=removed,
            oom_killed=oom_killed,
            throttle_extension_s=extension_s,
        )

    def _verdict(
        self,
        task: TaskDefinition,
        spec: TrialSpec,
        phase: ContainerPhase,
        state_dir: Path,
        started: int,
        attempt: int,
    ) -> TrialResult:
        try:
            adapter_result = self._read_adapter_result(state_dir, task, phase.exit_code)
        except HarnessFault as exc:
            if not (phase.timed_out or phase.oom_killed):
                return self._harness_error(
                    spec,
                    started,
                    attempt,
                    str(exc),
                    exc.cause,
                    removed=phase.container_removed,
                )
            # A killed container is SIGKILLed mid-write, so a half-written
            # result.json is the *expected* outcome of a timeout or an OOM rather
            # than evidence of a broken harness. Reported as a harness error it
            # would be retried — and a retried timeout is exactly the thing the
            # retry policy exists to forbid, arriving through the back door.
            adapter_result = AdapterResult(
                completed=False,
                error="the agent was killed before it finished writing its result",
                exit_code=phase.exit_code,
            )

        if phase.timed_out:
            # Never retried: the agent had its wall clock and did not finish.
            return TrialResult(
                run_id=spec.run_id,
                task_slug=spec.task_slug,
                trial_index=spec.trial_index,
                seed=spec.seed,
                outcome=Outcome.TIMEOUT,
                detail=_with_transcript(
                    f"exceeded the {task.limits.timeout_seconds}s deadline"
                    + (
                        f" (extended by {phase.throttle_extension_s:.0f}s of provider throttling)"
                        if phase.throttle_extension_s > 0
                        else ""
                    ),
                    state_dir,
                ),
                attempts=attempt,
                efficiency=self._efficiency(adapter_result, started, state_dir=state_dir),
                container_removed=phase.container_removed,
                started_unix_ms=started,
            )

        if phase.oom_killed:
            # Never retried, for the same reason a timeout is not. The ceiling is
            # set by the *task* — `limits_come_from_the_task_not_the_agent` — so
            # an agent that walks into it has failed the task under the budget it
            # was given. Retrying would buy it a second attempt the ranking never
            # shows, which is how a harness reports better than reality.
            return TrialResult(
                run_id=spec.run_id,
                task_slug=spec.task_slug,
                trial_index=spec.trial_index,
                seed=spec.seed,
                outcome=Outcome.FAIL,
                detail=(
                    f"exceeded the {task.environment.resources.memory_mb}MB memory "
                    f"limit and was killed by the cgroup"
                ),
                attempts=attempt,
                efficiency=self._efficiency(adapter_result, started),
                container_removed=phase.container_removed,
                started_unix_ms=started,
            )

        if phase.exit_code == _entrypoint_failure_code():
            return self._harness_error(
                spec,
                started,
                attempt,
                self._entrypoint_failure_detail(state_dir),
                ErrorCause.ENTRYPOINT,
                removed=phase.container_removed,
            )

        try:
            assertions = tuple(self._grade(task, state_dir, adapter_result))
            outcome, detail = self._classify(assertions, adapter_result)
        except HarnessFault as exc:
            return self._harness_error(
                spec,
                started,
                attempt,
                str(exc),
                exc.cause,
                removed=phase.container_removed,
                efficiency=self._efficiency(adapter_result, started),
            )
        except Exception as exc:
            # A grader that raises means *the assertion* is broken, not the
            # agent. Calling that a failure makes a broken assertion look like a
            # regression and sends you debugging the wrong system.
            return self._harness_error(
                spec,
                started,
                attempt,
                f"grader raised {type(exc).__name__}: {exc}",
                ErrorCause.GRADER,
                removed=phase.container_removed,
                efficiency=self._efficiency(adapter_result, started),
            )

        return TrialResult(
            run_id=spec.run_id,
            task_slug=spec.task_slug,
            trial_index=spec.trial_index,
            seed=spec.seed,
            outcome=outcome,
            detail=detail,
            attempts=attempt,
            assertions=assertions,
            efficiency=self._efficiency(adapter_result, started),
            container_removed=phase.container_removed,
            started_unix_ms=started,
        )

    # -- steps -------------------------------------------------------------

    async def _create_volume(self, name: str, run_id: str) -> Any:
        """Create the trial's only writable mount.

        Labelled so the orphan sweeper can reclaim it if the harness dies before
        the finally block runs.
        """
        try:
            return await _to_thread(
                self._client.volumes.create, name=name, labels={RUN_LABEL: run_id}
            )
        except Exception as exc:
            raise HarnessFault(
                f"workdir volume create failed: {type(exc).__name__}: {exc}"
            ) from exc

    async def _destroy_volume(self, volume: Any) -> None:
        if volume is None or not self._policy.volume_is_per_trial:
            # A shared volume belongs to the run, not the trial — removing it here
            # would quietly restore the isolation the flag exists to remove.
            return
        with contextlib.suppress(Exception):
            await _to_thread(volume.remove, force=True)

    async def _create(
        self, task: TaskDefinition, spec: TrialSpec, digest: str, volume_name: str
    ) -> Any:
        kwargs = container_kwargs(
            task,
            spec.run_id,
            spec.trial_index,
            workdir_volume=volume_name,
            policy=self._policy,
            network_name=self._network_name,
        )
        kwargs["image"] = digest
        kwargs["name"] = f"meridian-{spec.task_slug}-{spec.trial_index}-{uuid.uuid4().hex[:8]}"
        try:
            return await _to_thread(self._client.containers.create, **kwargs)
        except Exception as exc:
            raise HarnessFault(f"container create failed: {type(exc).__name__}: {exc}") from exc

    def _send_payload(self, container: Any, archive: bytes) -> None:
        """Stream the payload into the container's stdin and close the write end.

        `put_archive` is not an option: Docker refuses the archive API outright
        on a read-only rootfs. stdin is a pipe, so neither the read-only rootfs
        nor the tmpfs workdir applies, and closing it is what tells the
        bootstrap's `tar -xf -` that the payload is complete.
        """
        attached = container.attach_socket(params={"stdin": 1, "stream": 1})
        raw = getattr(attached, "_sock", attached)
        # docker-py hands back a socket wrapping a live HTTPResponse. Closing the
        # socket first leaves the response flushing a closed file at GC time and
        # spraying "Exception ignored" over otherwise clean test output, so the
        # response goes first.
        response = getattr(attached, "_response", None)
        try:
            raw.sendall(archive)
            raw.shutdown(socket.SHUT_WR)
        finally:
            for closeable in (response, attached, raw):
                if closeable is not None:
                    with contextlib.suppress(Exception):
                        closeable.close()

    async def _materialize(self, container: Any, task: TaskDefinition, spec: TrialSpec) -> None:
        deadline = _now_ms() + task.limits.timeout_seconds * 1000
        try:
            archive = payload_module.build_payload(
                task,
                spec,
                suite_root=self._suite_root,
                harness_root=self._harness_root,
                sut_root=self._sut_root,
                deadline_unix_ms=deadline,
            )
            await _to_thread(self._send_payload, container, archive)
        except Exception as exc:
            raise HarnessFault(f"materializing inputs failed: {type(exc).__name__}: {exc}") from exc

    async def _wait(
        self,
        container: Any,
        timeout_seconds: int,
        throttled_seconds: Callable[[], Awaitable[float]] | None = None,
    ) -> tuple[bool, int | None, bool, float]:
        """Wait for the container, killing it if the deadline passes.

        Cancelling the Python future does not stop a container: it keeps running,
        keeps holding resources, and keeps spending tokens. The container is what
        gets killed.

        The deadline moves with provider throttling: `throttled_seconds` reports how
        long the proxy has spent backing off on this trial's behalf, and that time is
        added to the deadline (see `effective_deadline_seconds`). Without it, a
        rate-limited provider turns an agent that finished its work into a timeout.

        Returns `(timed_out, exit_code, oom_killed, extension_seconds)`. The OOM flag
        comes from the daemon, never from the exit code: a cgroup kill and
        Meridian's own timeout kill both surface as 137, so classifying on the code
        alone would report every timeout as an out-of-memory failure.
        """
        waiter = asyncio.ensure_future(_to_thread(container.wait))
        started = time.monotonic()
        extension = 0.0
        try:
            while True:
                deadline = effective_deadline_seconds(float(timeout_seconds), extension)
                remaining = deadline - (time.monotonic() - started)
                if remaining <= 0:
                    await self._kill(container)
                    return True, None, False, max(deadline - timeout_seconds, 0.0)
                done, _ = await asyncio.wait({waiter}, timeout=min(remaining, WAIT_POLL_SECONDS))
                if done:
                    status = waiter.result()
                    break
                if throttled_seconds is not None:
                    extension = await throttled_seconds()
        except HarnessFault:
            raise
        except Exception as exc:
            raise HarnessFault(
                f"waiting on the container failed: {type(exc).__name__}: {exc}"
            ) from exc
        finally:
            if not waiter.done():
                waiter.cancel()
        code = status.get("StatusCode") if isinstance(status, dict) else None
        return (
            False,
            int(code) if code is not None else None,
            await self._oom_killed(container),
            max(
                effective_deadline_seconds(float(timeout_seconds), extension) - timeout_seconds, 0.0
            ),
        )

    async def _oom_killed(self, container: Any) -> bool:
        """Ask the daemon whether the cgroup killed this container.

        `container.attrs` is a snapshot taken at creation, so it has to be
        refreshed before `State` says anything about how the container ended.

        A daemon that will not answer is not grounds for failing the trial: the
        question is only ever used to make a verdict *more* specific, so an
        unanswerable one falls back to the ordinary exit-code path.
        """
        try:
            await _to_thread(container.reload)
            state = container.attrs.get("State", {})
        except Exception:
            return False
        return bool(state.get("OOMKilled", False))

    async def _kill(self, container: Any) -> None:
        # Already dead is fine; `_destroy` still force-removes.
        with contextlib.suppress(Exception):
            await _to_thread(container.kill)

    async def _extract(self, container: Any, task: TaskDefinition, destination: Path) -> None:
        try:
            stream, _stat = await _to_thread(container.get_archive, task.environment.workdir)
            payload_module.extract_archive(stream, destination)
        except Exception as exc:
            raise HarnessFault(
                f"extracting final state failed: {type(exc).__name__}: {exc}"
            ) from exc

    async def _destroy(self, container: Any) -> bool:
        """Remove the container unconditionally. Returns whether it worked."""
        try:
            await _to_thread(container.remove, force=True, v=True)
        except Exception:
            return False
        return True

    # -- classification ----------------------------------------------------

    def _read_adapter_result(
        self, state_dir: Path, task: TaskDefinition, exit_code: int | None
    ) -> AdapterResult:
        path = state_dir / payload_module.PAYLOAD_DIR / "result.json"
        if not path.is_file():
            return AdapterResult(
                completed=False,
                error="the agent produced no result",
                exit_code=exit_code,
            )
        try:
            result = AdapterResult.model_validate_json(path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise HarnessFault(
                f"unreadable result.json: {exc}", cause=ErrorCause.ENTRYPOINT
            ) from exc
        if result.exit_code is None and exit_code is not None:
            result = result.model_copy(update={"exit_code": exit_code})
        return result

    def _entrypoint_failure_detail(self, state_dir: Path) -> str:
        """Say *what* was misconfigured, not merely that something was."""
        from meridian.adapters.entrypoint import HARNESS_ERROR_FILE

        recorded = state_dir / payload_module.PAYLOAD_DIR / HARNESS_ERROR_FILE
        if recorded.is_file():
            return f"the entrypoint could not run: {recorded.read_text(encoding='utf-8').strip()}"
        return "the entrypoint could not run inside the container"

    def _classify(
        self, assertions: Sequence[AssertionResult], result: AdapterResult
    ) -> tuple[Outcome, str]:
        errored = [a for a in assertions if a.outcome is AssertionOutcome.ERROR]
        if errored:
            raise HarnessFault(f"assertion error: {errored[0].detail}", cause=ErrorCause.GRADER)

        failed = [a for a in assertions if a.outcome is AssertionOutcome.FAIL]
        if failed:
            # The detail names actual versus expected, because it ends up in the
            # PR comment and is the only thing a developer reads.
            return Outcome.FAIL, "; ".join(a.detail for a in failed)

        if result.error:
            # The agent raised or gave up. Assertions may still have passed by
            # luck; they did not, or we would not be here.
            return Outcome.FAIL, f"agent error: {result.error}"

        return Outcome.PASS, ""

    def _efficiency(
        self, result: AdapterResult, started_ms: int, *, state_dir: Path | None = None
    ) -> Efficiency:
        duration_ms = _now_ms() - started_ms
        recorded = efficiency_from_transcript(state_dir) if state_dir is not None else None
        if recorded is None:
            return Efficiency(
                turns=result.turns,
                tool_calls=len(result.tool_calls),
                input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
                duration_ms=duration_ms,
            )
        # A timed-out agent has no result file, so `result` is the empty
        # placeholder. Prefer whatever the agent did manage to report, and fill
        # the rest from what it recorded before it was killed.
        return recorded.model_copy(
            update={
                "turns": result.turns or recorded.turns,
                "tool_calls": len(result.tool_calls) or recorded.tool_calls,
                "input_tokens": result.input_tokens or recorded.input_tokens,
                "output_tokens": result.output_tokens or recorded.output_tokens,
                "duration_ms": duration_ms,
            }
        )

    def _harness_error(
        self,
        spec: TrialSpec,
        started: int,
        attempt: int,
        detail: str,
        cause: ErrorCause,
        *,
        removed: bool = True,
        efficiency: Efficiency | None = None,
    ) -> TrialResult:
        return TrialResult(
            run_id=spec.run_id,
            task_slug=spec.task_slug,
            trial_index=spec.trial_index,
            seed=spec.seed,
            outcome=Outcome.HARNESS_ERROR,
            detail=detail,
            attempts=attempt,
            container_removed=removed,
            started_unix_ms=started,
            error_cause=cause,
            efficiency=efficiency or Efficiency(),
        )


def _entrypoint_failure_code() -> int:
    """The exit code the in-container entrypoint uses when it cannot run at all."""
    from meridian.adapters.entrypoint import EXIT_ENTRYPOINT_FAILED

    return EXIT_ENTRYPOINT_FAILED


def sweep_orphans(client: DockerClient, *, run_id: str | None = None) -> list[str]:
    """Remove containers and volumes a previous harness left behind.

    Written on day one because it is needed within a day of starting: a crashed
    or interrupted run leaves containers holding CPU and memory and volumes
    holding disk, and neither announces itself.
    """
    label = RUN_LABEL if run_id is None else f"{RUN_LABEL}={run_id}"
    removed: list[str] = []
    for container in client.containers.list(all=True, filters={"label": label}):
        with contextlib.suppress(Exception):
            container.remove(force=True, v=True)
            removed.append(f"container:{container.name}")
    for volume in client.volumes.list(filters={"label": label}):
        with contextlib.suppress(Exception):
            volume.remove(force=True)
            removed.append(f"volume:{volume.name}")
    return removed
