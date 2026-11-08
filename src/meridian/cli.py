"""The Typer app — the only user-facing entry point.

Progress goes to stderr, results go to stdout, and the two are never interleaved
(`HANDOFF §9.3`) so that `meridian run ... > results.txt` stays useful.
"""

from __future__ import annotations

import sys
from pathlib import Path

import typer

from meridian import exit_codes
from meridian.assets import MissingAssetError, resolve_sut, write_proxy_build_context
from meridian.docker_client import DockerUnavailableError, get_client
from meridian.runtime.secrets import write_redacted
from meridian.snapshots.build import (
    SnapshotBuildError,
    build_snapshot,
    current_platform,
    rewrite_suite_snapshots,
)
from meridian.snapshots.registry import is_unbuilt, read_snapshot_ref
from meridian.suites.loader import load_suite
from meridian.suites.validate import SuiteValidationError
from meridian.version import __version__

app = typer.Typer(
    name="meridian",
    help="Agent evaluation with per-trial isolation, outcome grading, and pass^k.",
    no_args_is_help=True,
    add_completion=False,
)

suite_app = typer.Typer(help="Author, validate, and publish task suites.", no_args_is_help=True)
app.add_typer(suite_app, name="suite")

snapshot_app = typer.Typer(
    help="Build environment snapshots and pin them by digest.", no_args_is_help=True
)
app.add_typer(snapshot_app, name="snapshot")


def err(message: str) -> None:
    """Progress and diagnostics — stderr, always."""
    print(message, file=sys.stderr)


def out(message: str) -> None:
    """Results — stdout, always."""
    print(message)


def _report_validation_failure(exc: SuiteValidationError) -> None:
    err(f"suite validation failed with {len(exc.issues)} issue(s):")
    width = max(len(issue.code) for issue in exc.issues)
    for issue in exc.issues:
        location = issue.file if issue.task_slug is None else f"{issue.file} [{issue.task_slug}]"
        field = f".{issue.field}" if issue.field and issue.field != "<root>" else ""
        err(f"  {issue.code:<{width}}  {location}{field}")
        err(f"  {'':<{width}}  └─ {issue.message}")


def _write_drop_reasons(path: Path, reasons: dict[str, str]) -> None:
    """Leave each build failure beside the task it removed, not only on the terminal."""
    sections = [
        "# Dropped tasks",
        "",
        "These tasks failed to build and were moved out of the suite by "
        "`meridian suite build-images --drop-failed`. The tail of each build log:",
        "",
    ]
    for slug in sorted(reasons):
        sections += [f"## {slug}", "", "```text", reasons[slug].rstrip(), "```", ""]
    path.write_text("\n".join(sections), encoding="utf-8")


@app.command()
def version() -> None:
    """Print the harness version that will be written into run manifests."""
    out(__version__)


@suite_app.command("build-images")
def suite_build_images(
    path: Path = typer.Argument(..., help="Path to the suite directory."),
    only: list[str] = typer.Option([], "--only", help="Build just these task slugs."),
    drop_failed: bool = typer.Option(
        False,
        "--drop-failed",
        help="Move tasks whose image fails to build out of the suite (into dropped/) and "
        "carry on with the rest, instead of failing the whole command.",
    ),
) -> None:
    """Build each task's image (images/<slug>/) and pin its digest into the task.

    For suites with one image per task, such as those from `import-harbor`. A task is
    built when it has an `images/<slug>/Dockerfile`; tasks sharing one image are
    pinned with `snapshot build --update-suite` instead.
    """
    from meridian.snapshots.build import rewrite_task_snapshot

    try:
        suite = load_suite(path)
    except SuiteValidationError as exc:
        _report_validation_failure(exc)
        raise typer.Exit(exit_codes.HARNESS_ERROR) from exc

    root = Path(path)
    todo = [t for t in suite.tasks if (root / "images" / t.slug / "Dockerfile").is_file()]
    if only:
        todo = [t for t in todo if t.slug in set(only)]
    if not todo:
        err("no task has an images/<slug>/Dockerfile to build")
        raise typer.Exit(exit_codes.HARNESS_ERROR)

    try:
        client = get_client()
    except DockerUnavailableError as exc:
        err(f"cannot reach Docker: {exc}")
        raise typer.Exit(exit_codes.HARNESS_ERROR) from exc

    failures: dict[str, str] = {}
    for task in todo:
        try:
            digest = build_snapshot(
                client, root / "images" / task.slug, tag=f"meridian-{suite.slug}-{task.slug}:dev"
            )
        except SnapshotBuildError as exc:
            failures[task.slug] = str(exc)
            err(f"  FAILED  {task.slug}: {exc}")
            continue
        rewrite_task_snapshot(root / "tasks" / f"{task.slug}.yaml", digest)
        out(f"{task.slug} {digest}")
    err(f"built {len(todo) - len(failures)} of {len(todo)} image(s)")
    if drop_failed and failures:
        dropped = root / "dropped"
        dropped.mkdir(exist_ok=True)
        for slug in failures:
            (root / "tasks" / f"{slug}.yaml").rename(dropped / f"{slug}.yaml")
        _write_drop_reasons(dropped / "REASONS.md", failures)
        names = ", ".join(sorted(failures))
        out(f"dropped {len(failures)} task(s) whose image did not build: {names}")
        if len(failures) == len(todo):
            raise typer.Exit(exit_codes.HARNESS_ERROR)
        return
    if failures:
        raise typer.Exit(exit_codes.HARNESS_ERROR)


@suite_app.command("publish")
def suite_publish(
    path: Path = typer.Argument(..., help="Path to the suite directory."),
) -> None:
    """Validate a suite and print its content hash.

    The hash is what the manifest pins. It is stable across checkouts of the same
    commit and changes when any task changes — that is the whole contract.
    """
    try:
        suite = load_suite(path)
    except SuiteValidationError as exc:
        _report_validation_failure(exc)
        raise typer.Exit(exit_codes.HARNESS_ERROR) from exc

    err(f"suite {suite.slug} v{suite.version} — {len(suite.tasks)} task(s)")
    scored = suite.scored_tasks
    excluded = suite.excluded_tasks

    out(f"suite:         {suite.slug}")
    out(f"version:       {suite.version}")
    out(f"adapter:       {suite.adapter_spec}")
    out(f"content_hash:  {suite.content_hash()}")
    out("")
    out(f"{'task':<26} {'state':<12} {'role':<14} assertions")
    out("-" * 68)
    for task in suite.tasks:
        out(f"{task.slug:<26} {task.state:<12} {task.role:<14} {len(task.outcome_assertions)}")
    out("")
    out(f"scored tasks:   {len(scored)}")
    if excluded:
        names = ", ".join(t.slug for t in excluded)
        out(f"excluded:       {len(excluded)} ({names})")


@app.command("import-harbor")
def import_harbor(
    path: Path = typer.Argument(..., help="A Harbor task directory, or a directory of them."),
    out_dir: Path = typer.Option(..., "--out", help="Suite directory to write into."),
    suite_slug: str = typer.Option("harbor-import", "--suite-slug", help="Slug for a new suite."),
    limit: int | None = typer.Option(None, "--limit", help="Convert at most this many tasks."),
    only: list[str] = typer.Option(
        [], "--only", help="Only tasks whose directory name is one of these (repeatable)."
    ),
    max_timeout: int | None = typer.Option(
        None,
        "--max-timeout",
        help="Cap each task's agent timeout, in seconds. Shortens the benchmark's own "
        "limit, so say so when reporting results.",
    ),
    rewrite_boilerplate: bool = typer.Option(
        False,
        "--rewrite-uv-boilerplate",
        help="Rewrite the standard `install uv; uvx pytest` verifier preamble to use the "
        "pytest already in the image (the grading container has no network).",
    ),
) -> None:
    """Convert Harbor-format tasks into a Meridian suite.

    Tasks that need something Meridian cannot honour (multi-step runs, GPUs, MCP
    servers, a verifier that needs the network, ...) are skipped with the reason,
    never converted into something that runs under different rules. Images are not
    built here; the converted tasks carry a placeholder snapshot until they are.
    """
    from meridian.importers import harbor

    try:
        task_dirs = harbor.find_task_dirs(path)
        if only:
            task_dirs = [d for d in task_dirs if d.name in set(only)]
        if limit is not None:
            task_dirs = task_dirs[:limit]
        tasks, unreadable = harbor.load_all(
            task_dirs, rewrite_boilerplate=rewrite_boilerplate, max_timeout_seconds=max_timeout
        )
    except harbor.HarborImportError as exc:
        err(f"import failed: {exc}")
        raise typer.Exit(exit_codes.HARNESS_ERROR) from exc

    converted, skipped = harbor.write_suite(tasks, out_dir, suite_slug=suite_slug)
    skipped = [*skipped, *unreadable]

    out(
        f"read {len(tasks) + len(unreadable)} task(s): "
        f"{len(converted)} converted, {len(skipped)} skipped"
    )
    for done in converted:
        notes = f"  ({'; '.join(w.code for w in done.warnings)})" if done.warnings else ""
        out(f"  converted  {done.slug}{notes}")
    for refused in skipped:
        reasons = "; ".join(f"{i.code}: {i.message}" for i in refused.issues)
        out(f"  skipped    {refused.name}  -- {reasons}")
    if not converted:
        err("nothing could be converted")
        raise typer.Exit(exit_codes.HARNESS_ERROR)
    err(f"wrote {len(converted)} task(s) to {out_dir}; build their images before running")


@app.command()
def doctor(
    check_upstream: bool = typer.Option(
        False,
        "--check-upstream",
        help="Also send one tiny request to the configured model provider (costs a few tokens).",
    ),
) -> None:
    """Check that this machine can run Meridian, and say how to fix what is not."""
    import httpx

    from meridian import doctor as doctor_module
    from meridian.runtime.proxy.session import PROXY_IMAGE_TAG

    def connect(url: str) -> None:
        import psycopg

        psycopg.connect(url, connect_timeout=3).close()

    checks = doctor_module.run(
        get_client=get_client,
        proxy_tag=PROXY_IMAGE_TAG,
        connect=connect,
        post=httpx.post if check_upstream else None,
    )
    out(doctor_module.render(checks))
    code = doctor_module.exit_code(checks)
    if code:
        raise typer.Exit(code)


@app.command()
def setup() -> None:
    """Build the model-proxy image from the installed package.

    Run once after `pip install meridian-eval`. Inside a repository checkout,
    `make images` does the same and also builds the reference suite's image.
    """
    import tempfile

    from meridian.runtime.proxy.session import PROXY_IMAGE_TAG

    try:
        client = get_client()
        with tempfile.TemporaryDirectory(prefix="meridian-proxy-ctx-") as tmp:
            context = Path(tmp)
            env_dir = write_proxy_build_context(context)
            digest = build_snapshot(client, env_dir, tag=PROXY_IMAGE_TAG, context=context)
    except (DockerUnavailableError, SnapshotBuildError, MissingAssetError) as exc:
        err(f"setup failed: {exc}")
        raise typer.Exit(exit_codes.HARNESS_ERROR) from exc
    err(f"built {PROXY_IMAGE_TAG} for {current_platform()}")
    out(digest)


@snapshot_app.command("build")
def snapshot_build(
    path: Path = typer.Argument(..., help="Path to the environment directory."),
    tag: str = typer.Option(..., "--tag", help="Human-readable tag for the built image."),
    write_ref: bool = typer.Option(
        False, "--write-ref", help="Record the digest in <env>/snapshot-ref.json."
    ),
    context: Path | None = typer.Option(
        None,
        "--context",
        help="Build context, when the image ships files from outside its own directory.",
    ),
    update_suite: Path | None = typer.Option(
        None,
        "--update-suite",
        help="Rewrite every task in this suite to pin the digest just built.",
    ),
) -> None:
    """Build an environment image and print its digest.

    An image ID is architecture-specific, so the digest committed from a laptop
    will not exist on an amd64 runner. `--update-suite` is how CI closes that
    gap: rebuild, then repin, then run.
    """
    try:
        client = get_client()
        digest = build_snapshot(client, path, tag=tag, write_ref=write_ref, context=context)
    except (DockerUnavailableError, SnapshotBuildError) as exc:
        err(f"snapshot build failed: {exc}")
        raise typer.Exit(exit_codes.HARNESS_ERROR) from exc

    err(f"built {tag} for {current_platform()}")
    if update_suite is not None:
        changed = rewrite_suite_snapshots(update_suite, digest)
        err(f"repinned {len(changed)} task file(s) in {update_suite}")
    out(digest)


@snapshot_app.command("show")
def snapshot_show(
    path: Path = typer.Argument(..., help="Path to the environment directory."),
) -> None:
    """Print the digest most recently built for this environment."""
    digest = read_snapshot_ref(path)
    if digest is None:
        err(f"no snapshot-ref.json in {path}; run `meridian snapshot build --write-ref` first")
        raise typer.Exit(exit_codes.HARNESS_ERROR)
    out(digest)


@suite_app.command("audit")
def suite_audit(
    suite: Path = typer.Option(..., "--suite", help="Path to the suite directory."),
    sut: Path = typer.Option(Path("fixtures"), "--sut", help="System under test root."),
    out: Path | None = typer.Option(None, "--out", help="Write the findings as JSON."),
) -> None:
    """Check whether any task can be passed without doing the work.

    Runs the suite against agents that are known not to have solved anything — one
    that touches nothing, one that creates an empty output directory, one that
    writes well-formed JSON full of invented values. Any task that still passes is
    measuring something weaker than it claims.

    This is the contamination probe's idea turned on the *user's* assertions
    rather than on Meridian's isolation: a check that cannot fail proves nothing.

    **Exit code.** Always 0 unless the harness itself broke. A weak task is a
    finding about the suite, and `1` is reserved exclusively for `meridian gate`
    returning FAIL (`HANDOFF §8.4`) — CI must never have to guess which of the two
    it is looking at. Gate on the JSON from `--out` instead.
    """
    import json as json_module

    from meridian.config import RunMode, load_config
    from meridian.models.run import Outcome
    from meridian.models.suite import Suite
    from meridian.runtime.orchestrator import RunRequest, default_cassette_dir
    from meridian.runtime.shutdown import execute_cleanly
    from meridian.suites.audit import ADVERSARIES, audit_report, findings

    try:
        loaded = load_suite(suite)
    except SuiteValidationError as exc:
        _report_validation_failure(exc)
        raise typer.Exit(exit_codes.HARNESS_ERROR) from exc

    # One trial per task: these adversaries are deterministic, so a second trial
    # would cost time and tell us nothing.
    config = load_config(
        "meridian.yaml",
        overrides={"execution": {"n_trials": 1, "k": 1, "proxy_mode": "replay"}},
    )

    try:
        config.validate_for(RunMode.EXPLORATORY)
        client = get_client()
    except (ValueError, DockerUnavailableError) as exc:
        err(str(exc))
        raise typer.Exit(exit_codes.HARNESS_ERROR) from exc

    # The scored tasks, not every task: `include_probes=False` below means the
    # contamination pair never runs, and counting them would overclaim coverage.
    audited = [task.slug for task in loaded.scored_tasks]
    passes: dict[str, list[str]] = {}

    for adversary in ADVERSARIES:
        err(f"running the {adversary.name} adversary over {len(audited)} task(s)")
        # Every task keeps its assertions and its environment and loses only its
        # agent. That is the whole experiment: same measuring instrument, an agent
        # that certainly did not earn a pass.
        rigged = Suite(
            header=loaded.header,
            tasks=tuple(
                task.model_copy(update={"adapter": adversary.adapter}) for task in loaded.tasks
            ),
            root=loaded.root,
        )
        request = RunRequest(
            suite=rigged,
            config=config,
            cassette_dir=default_cassette_dir(rigged, None),
            sut_root=resolve_sut(sut),
            mode=RunMode.EXPLORATORY,
            include_probes=False,
            use_stub_provider=True,
            progress=None,
        )
        try:
            outcome = execute_cleanly(client, request)
        except Exception as exc:
            err(f"the {adversary.name} adversary could not be run: {type(exc).__name__}: {exc}")
            raise typer.Exit(exit_codes.HARNESS_ERROR) from exc

        # An adversary that never actually ran passes nothing, and "passed
        # nothing" is exactly what a clean bill of health looks like. Left
        # unchecked, a bad --sut, a Docker hiccup, or a missing snapshot prints
        # "No task passed for an agent that did no work" and exits 0 — a check
        # that cannot fail, shipped by the command whose whole thesis is that a
        # check which cannot fail proves nothing.
        unevaluated = sorted(
            task.task_slug
            for task in outcome.result.tasks
            if not any(trial.outcome is not Outcome.HARNESS_ERROR for trial in task.trials)
        )
        missing = sorted(set(audited) - {task.task_slug for task in outcome.result.tasks})
        if unevaluated or missing:
            err(
                f"the {adversary.name} adversary produced no verdict for: "
                f"{', '.join(unevaluated + missing)}. The audit cannot report a "
                f"result it did not measure."
            )
            raise typer.Exit(exit_codes.HARNESS_ERROR)

        passes[adversary.name] = [
            task.task_slug
            for task in outcome.result.tasks
            if any(trial.outcome is Outcome.PASS for trial in task.trials)
        ]

    found = findings(passes)
    print(audit_report(found, audited))

    if out is not None:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            json_module.dumps(
                {
                    "audited_tasks": audited,
                    "adversaries": [a.name for a in ADVERSARIES],
                    "findings": [
                        {
                            "task_slug": f.task_slug,
                            "adversary": f.adversary,
                            "why_it_should_fail": f.why_it_should_fail,
                        }
                        for f in found
                    ],
                    "weak_task_count": len({f.task_slug for f in found}),
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        err(f"wrote {out}")


@app.command()
def run(
    suite: Path = typer.Option(..., "--suite", help="Path to the suite directory."),
    n: int | None = typer.Option(None, "--n", help="Trials per task."),
    k: int | None = typer.Option(None, "--k", help="The k in pass^k."),
    proxy_mode: str | None = typer.Option(
        None, "--proxy-mode", help="record | replay | passthrough."
    ),
    sut: Path = typer.Option(Path("fixtures"), "--sut", help="System under test root."),
    cassettes: Path | None = typer.Option(None, "--cassettes", help="Cassette directory."),
    budget: int | None = typer.Option(None, "--budget", help="Run cost cap, in cents."),
    concurrency: int | None = typer.Option(None, "--concurrency", help="Concurrent trials."),
    include_probes: bool = typer.Option(
        False, "--include-probes", help="Also run the harness's own contamination probes."
    ),
    unsafe_shared_env: bool = typer.Option(
        False,
        "--unsafe-shared-env",
        help="Share one workdir across every trial. Breaks Rule 1 on purpose, so the "
        "contamination probe has a failing direction. Rejected in gate mode.",
    ),
    gate_mode: bool = typer.Option(
        False, "--gate-mode", help="Apply gate-mode configuration rules."
    ),
    out: Path | None = typer.Option(None, "--out", help="Write the run result as JSON."),
    html_out: Path | None = typer.Option(
        None, "--html", help="Write a self-contained HTML report here."
    ),
    live_provider: bool = typer.Option(
        False,
        "--live-provider",
        help="Record against the real provider instead of the deterministic stub.",
    ),
) -> None:
    """Run a suite and print per-task pass@k and pass^k.

    Exits non-zero only if the harness itself failed. An agent that performed
    badly is a result, not an error — see the exit-code contract in HANDOFF §8.4.
    """

    from meridian.config import RunMode, load_config
    from meridian.report import table
    from meridian.runtime.orchestrator import RunRequest, default_cassette_dir
    from meridian.runtime.shutdown import execute_cleanly

    try:
        loaded = load_suite(suite)
    except SuiteValidationError as exc:
        _report_validation_failure(exc)
        raise typer.Exit(exit_codes.HARNESS_ERROR) from exc

    unbuilt = [t.slug for t in loaded.tasks if is_unbuilt(t.environment.snapshot)]
    if unbuilt:
        err(
            f"{len(unbuilt)} task(s) have no built image yet ({', '.join(unbuilt[:5])}"
            f"{', ...' if len(unbuilt) > 5 else ''}). Run `meridian suite build-images "
            f"{suite}` first."
        )
        raise typer.Exit(exit_codes.HARNESS_ERROR)

    config = load_config(
        "meridian.yaml",
        overrides={
            "execution": {
                "n_trials": n,
                "k": k,
                "proxy_mode": proxy_mode,
                "budget_cents": budget,
                "max_concurrent_trials": concurrency,
                "unsafe_shared_env": unsafe_shared_env or None,
            }
        },
    )
    mode = RunMode.GATE if gate_mode else RunMode.EXPLORATORY

    try:
        config.validate_for(mode)
        client = get_client()
    except (ValueError, DockerUnavailableError) as exc:
        err(str(exc))
        raise typer.Exit(exit_codes.HARNESS_ERROR) from exc

    request = RunRequest(
        suite=loaded,
        config=config,
        cassette_dir=default_cassette_dir(loaded, cassettes),
        sut_root=resolve_sut(sut),
        mode=mode,
        include_probes=include_probes,
        use_stub_provider=not live_provider,
        progress=err,
    )

    try:
        outcome = execute_cleanly(client, request)
    except Exception as exc:
        err(f"run failed: {type(exc).__name__}: {exc}")
        raise typer.Exit(exit_codes.HARNESS_ERROR) from exc

    from meridian.manifest import store as run_store

    archived = run_store.save(outcome.manifest, outcome.result)
    err(f"archived {outcome.result.run_id} to {archived}")
    _record_to_database(config, loaded, outcome)

    print(table.render(outcome.result))
    detail = table.failures(outcome.result)
    if detail:
        print("")
        print("failing trials:")
        print(detail)
    print("")
    print(f"manifest: {outcome.manifest.manifest_hash()}")

    if out is not None:
        out.parent.mkdir(parents=True, exist_ok=True)
        write_redacted(out, outcome.result.model_dump_json(indent=2))
        err(f"wrote {out}")

    if html_out is not None:
        from meridian.report import html as html_report

        html_out.parent.mkdir(parents=True, exist_ok=True)
        write_redacted(
            html_out,
            html_report.render(outcome.result, manifest_hash=outcome.manifest.manifest_hash()),
        )
        err(f"wrote {html_out}")


def _record_to_database(config: object, suite: object, outcome: object) -> None:
    """Persist the run if a database is configured, and shrug if it is not.

    Meridian's correctness does not depend on Postgres being reachable. A harness
    that refused to report a finished run because a database was down would be a
    worse tool, so this failure is a note on stderr and nothing more.
    """
    from meridian.config import MeridianConfig
    from meridian.store import repo

    assert isinstance(config, MeridianConfig)
    database_url = config.store.database_url
    if not database_url:
        return
    try:
        with repo.connect(database_url) as connection:
            repo.record_run(
                connection,
                suite=suite,  # type: ignore[arg-type]
                manifest=outcome.manifest,  # type: ignore[attr-defined]
                result=outcome.result,  # type: ignore[attr-defined]
            )
    except Exception as exc:
        err(f"note: the run was not persisted ({type(exc).__name__}: {exc})")
    else:
        err("recorded to the database")


@app.command()
def history(
    suite_slug: str = typer.Argument(..., help="Suite slug, e.g. checkout-agent."),
    limit: int = typer.Option(20, "--limit"),
) -> None:
    """Suite-level pass^k over time, newest first."""
    from meridian.config import load_config
    from meridian.store import repo

    config = load_config("meridian.yaml")
    if not config.store.database_url:
        err("no database configured; set MERIDIAN_DATABASE_URL or store.database_url")
        raise typer.Exit(exit_codes.HARNESS_ERROR)

    try:
        with repo.connect(config.store.database_url) as connection:
            rows = repo.suite_history(connection, suite_slug, limit=limit)
    except repo.StoreUnavailableError as exc:
        err(str(exc))
        raise typer.Exit(exit_codes.HARNESS_ERROR) from exc

    if not rows:
        err(f"no recorded runs for suite {suite_slug!r}")
        return

    out(f"{'run':<28} {'when':<20} {'commit':<12} {'status':<14} suite pass^k")
    for row in rows:
        score = row["suite_pass_hat_k"]
        when = str(row["started_at"])[:19]
        commit = str(row["commit_sha"] or "-")[:12]
        rendered = f"{score:.3f}" if score is not None else "—"
        out(f"{row['run_id']:<28} {when:<20} {commit:<12} {row['status']!s:<14} {rendered}")


@app.command()
def gate(
    suite: Path = typer.Option(..., "--suite", help="Path to the suite directory."),
    baseline_ref: str | None = typer.Option(
        None, "--baseline-ref", help="Commit or ref to compare against (usually the merge base)."
    ),
    baseline_run: str | None = typer.Option(
        None, "--baseline-run", help="An archived run id to compare against instead."
    ),
    n: int | None = typer.Option(None, "--n", help="Trials per task."),
    k: int | None = typer.Option(None, "--k", help="The k in pass^k."),
    tolerance: float | None = typer.Option(None, "--tolerance", help="Allowed pass^k drop."),
    require_significance: bool = typer.Option(
        True, "--require-significance/--no-require-significance"
    ),
    fail_on_inconclusive: bool = typer.Option(False, "--fail-on-inconclusive"),
    proxy_mode: str = typer.Option(
        "record",
        "--proxy-mode",
        help="record (default) runs the agent for real; replay only works when the "
        "agent has not changed since the cassettes were recorded.",
    ),
    sut: Path = typer.Option(Path("fixtures"), "--sut", help="System under test root."),
    cassettes: Path | None = typer.Option(None, "--cassettes", help="Cassette root."),
    comment_file: Path | None = typer.Option(
        None, "--comment-file", help="Write the PR comment here."
    ),
    live_provider: bool = typer.Option(False, "--live-provider"),
) -> None:
    """Compare head against a baseline and decide whether to block the merge.

    The only command that returns a non-zero exit code for a product verdict:
    1 for FAIL, 0 for everything else. CI must never confuse "the agent got
    worse" with "the tool crashed".
    """

    from meridian.config import RunMode, load_config
    from meridian.gate import comment as comment_module
    from meridian.gate.baseline import Baseline, BaselineError, merge_base, worktree_at
    from meridian.gate.decide import GateInput, decide, flipped_tasks
    from meridian.manifest import store as run_store
    from meridian.runtime.orchestrator import RunRequest, default_cassette_dir
    from meridian.runtime.shutdown import execute_cleanly
    from meridian.stats.significance import paired_regression_p_value

    repo_root = Path.cwd()

    try:
        loaded = load_suite(suite)
    except SuiteValidationError as exc:
        _report_validation_failure(exc)
        raise typer.Exit(exit_codes.HARNESS_ERROR) from exc

    config = load_config(
        "meridian.yaml",
        overrides={
            "execution": {"n_trials": n, "k": k, "proxy_mode": proxy_mode},
            "gate": {
                "tolerance": str(tolerance) if tolerance is not None else None,
                "require_significance": require_significance,
                "fail_on_inconclusive": fail_on_inconclusive or None,
            },
        },
    )

    try:
        config.validate_for(RunMode.GATE)
        client = get_client()
    except (ValueError, DockerUnavailableError) as exc:
        err(str(exc))
        raise typer.Exit(exit_codes.HARNESS_ERROR) from exc

    def request(sut_root: Path, run_id_suffix: str) -> RunRequest:
        return RunRequest(
            suite=loaded,
            config=config,
            cassette_dir=default_cassette_dir(loaded, cassettes),
            sut_root=resolve_sut(sut_root),
            mode=RunMode.GATE,
            use_stub_provider=not live_provider,
            repo_root=repo_root,
            progress=err,
        )

    # --- baseline -------------------------------------------------------
    baseline_scores: dict[str, float] = {}
    baseline_suite: float | None = None
    baseline_info = Baseline(run_id=None, source="none")

    if baseline_run:
        try:
            recorded = run_store.load_result(baseline_run)
        except run_store.RunNotFoundError as exc:
            err(str(exc))
            raise typer.Exit(exit_codes.HARNESS_ERROR) from exc
        baseline_scores = comment_module.per_task_scores(recorded)
        baseline_suite = comment_module.suite_score(recorded)
        baseline_info = Baseline(run_id=baseline_run, source="archive")
    elif baseline_ref:
        sha = merge_base(repo_root, baseline_ref)
        if sha is None:
            err(f"could not resolve {baseline_ref!r}; proceeding with no baseline")
        else:
            # Comparable, not merely same-commit: a baseline run at a different
            # n, k, or suite version yields a difference that is an artefact of
            # the configuration and looks exactly like a regression.
            archived = run_store.find_by_commit(
                sha,
                n_trials=config.execution.n_trials,
                k=config.execution.k,
                suite_content_hash=loaded.content_hash(),
            )
            if archived is not None:
                recorded = run_store.load_result(archived)
                baseline_scores = comment_module.per_task_scores(recorded)
                baseline_suite = comment_module.suite_score(recorded)
                baseline_info = Baseline(run_id=archived, source="archive")
                err(f"baseline from archived run {archived} at {sha[:12]}")
            else:
                err(f"no archived run at {sha[:12]}; running the baseline from a worktree")
                try:
                    with worktree_at(repo_root, sha) as checkout:
                        base_sut = checkout / sut
                        if not base_sut.is_dir():
                            err(f"{sut} does not exist at {sha[:12]}; treating as no baseline")
                        else:
                            outcome = execute_cleanly(client, request(base_sut, "baseline"))
                            run_store.save(outcome.manifest, outcome.result)
                            baseline_scores = comment_module.per_task_scores(outcome.result)
                            baseline_suite = comment_module.suite_score(outcome.result)
                            baseline_info = Baseline(
                                run_id=outcome.result.run_id, source="worktree"
                            )
                except BaselineError as exc:
                    err(str(exc))
                    raise typer.Exit(exit_codes.HARNESS_ERROR) from exc
                except Exception as exc:
                    err(f"baseline run failed: {type(exc).__name__}: {exc}")
                    raise typer.Exit(exit_codes.HARNESS_ERROR) from exc

    # --- head -----------------------------------------------------------
    try:
        head = execute_cleanly(client, request(sut, "head"))
    except Exception as exc:
        err(f"head run failed: {type(exc).__name__}: {exc}")
        raise typer.Exit(exit_codes.HARNESS_ERROR) from exc

    run_store.save(head.manifest, head.result)
    head_scores = comment_module.per_task_scores(head.result)
    head_suite = comment_module.suite_score(head.result)

    if baseline_suite is not None:
        # Re-average both sides over the tasks they share. The verdict is the
        # difference of these two numbers, and a difference of means taken over
        # different denominators is not a comparison.
        baseline_suite, head_suite = comment_module.paired_suite_scores(
            baseline_scores, head_scores
        )

    verdict, reason = decide(
        GateInput(
            baseline_suite_passhat_k=baseline_suite,
            head_suite_passhat_k=head_suite,
            per_task_baseline=baseline_scores,
            per_task_head=head_scores,
            harness_error_rate=head.result.harness_error_rate,
            run_status=str(head.result.status),
            tolerance=config.gate.tolerance_value,
            require_significance=config.gate.require_significance,
            significance_level=config.gate.significance_value,
            fail_on_inconclusive=config.gate.fail_on_inconclusive,
            max_harness_error_rate=config.gate.max_harness_error_rate_value,
            stale_cassette_rate=comment_module.stale_cassette_rate(head.result),
        ),
        lambda base, head_scores_: paired_regression_p_value(
            base, head_scores_, seed=config.stats.bootstrap_seed
        ),
    )

    flipped = flipped_tasks(baseline_scores, head_scores)
    body = comment_module.render(
        verdict=verdict,
        reason=reason,
        flipped=flipped,
        head=head.result,
        baseline_scores=baseline_scores,
        head_scores=head_scores,
        baseline_suite=baseline_suite,
        head_suite=head_suite,
        baseline_run_id=baseline_info.run_id,
        manifest_hash=head.manifest.manifest_hash(),
        tolerance=config.gate.tolerance_value,
    )

    if comment_file is not None:
        comment_file.parent.mkdir(parents=True, exist_ok=True)
        write_redacted(comment_file, body + "\n")
        err(f"wrote {comment_file}")

    print(body)
    err(f"verdict: {verdict} ({baseline_info.source} baseline)")

    if verdict.blocks_merge:
        raise typer.Exit(exit_codes.GATE_FAIL)


@app.command()
def replay(
    run_id: str = typer.Argument(..., help="The archived run to re-materialize."),
    cassettes: Path | None = typer.Option(None, "--cassettes", help="Cassette root."),
    sut: Path = typer.Option(Path("fixtures"), "--sut", help="System under test root."),
    suite: Path | None = typer.Option(
        None, "--suite", help="Suite directory (defaults to ./suites/<slug>)."
    ),
) -> None:
    """Re-run an archived run from its manifest and report replay fidelity.

    Replay is a correctness check on the harness, not a convenience. Drift means
    something outside the manifest is influencing results.
    """

    from meridian.config import load_config
    from meridian.manifest import store as run_store
    from meridian.manifest.replay import ReplayError, compare, verify_images_available
    from meridian.runtime.orchestrator import RunRequest, default_cassette_dir
    from meridian.runtime.shutdown import execute_cleanly

    try:
        manifest = run_store.load_manifest(run_id)
        recorded = run_store.load_result(run_id)
    except run_store.RunNotFoundError as exc:
        err(str(exc))
        raise typer.Exit(exit_codes.HARNESS_ERROR) from exc

    suite_path = suite or Path("suites") / manifest.suite_slug

    try:
        loaded = load_suite(suite_path)
    except SuiteValidationError as exc:
        _report_validation_failure(exc)
        raise typer.Exit(exit_codes.HARNESS_ERROR) from exc

    if loaded.content_hash() != manifest.suite_content_hash:
        err(
            "the suite on disk does not match the one this run used; replaying it "
            "would compare two different suites and call the difference drift"
        )
        raise typer.Exit(exit_codes.HARNESS_ERROR)

    config = load_config(
        "meridian.yaml",
        overrides={
            "execution": {
                "n_trials": manifest.n_trials,
                "k": manifest.k,
                # Replay is the only honest mode here: recording again would
                # measure the provider, not the harness.
                "proxy_mode": "replay",
            }
        },
    )

    try:
        client = get_client()
        verify_images_available(client, manifest)
    except (DockerUnavailableError, ReplayError) as exc:
        err(str(exc))
        raise typer.Exit(exit_codes.HARNESS_ERROR) from exc

    request = RunRequest(
        suite=loaded,
        config=config,
        cassette_dir=default_cassette_dir(loaded, cassettes),
        sut_root=resolve_sut(sut),
        # The same run id reproduces the same derived seeds.
        run_id=run_id,
        progress=err,
    )

    try:
        outcome = execute_cleanly(client, request)
    except Exception as exc:
        err(f"replay failed: {type(exc).__name__}: {exc}")
        raise typer.Exit(exit_codes.HARNESS_ERROR) from exc

    # No expected_hash: `compare` uses the hash the archived run persisted, which
    # is the only value here independent of the manifest being verified.
    report = compare(manifest, recorded, outcome.result)
    print(report.render())
    if not report.exact:
        err("replay did not reproduce exactly; something outside the manifest moved")


@app.command("runs")
def list_runs_command() -> None:
    """List archived runs, oldest first."""
    from meridian.manifest import store as run_store

    ids = run_store.list_runs()
    if not ids:
        err("no archived runs")
        return
    out(f"{'run':<28} {'suite':<26} {'n':>3} {'k':>3}  commit")
    for run_id in ids:
        summary = run_store.summarize(run_id)
        commit = str(summary["commit"] or "-")[:12]
        out(
            f"{run_id:<28} {summary['suite']!s:<26} "
            f"{summary['n']!s:>3} {summary['k']!s:>3}  {commit}"
        )


@app.command()
def sweep(
    run_id: str | None = typer.Option(None, "--run-id", help="Limit the sweep to one run."),
) -> None:
    """Remove containers and volumes a previous run left behind.

    An interrupted harness leaves containers holding CPU and memory and volumes
    holding disk, and neither announces itself. Run this after a crash, and it
    runs automatically before each new run.
    """
    from meridian.runtime.trial_runner import sweep_orphans

    try:
        client = get_client()
    except DockerUnavailableError as exc:
        err(f"sweep failed: {exc}")
        raise typer.Exit(exit_codes.HARNESS_ERROR) from exc

    removed = sweep_orphans(client, run_id=run_id)
    if not removed:
        err("nothing to sweep")
        return
    for name in removed:
        out(name)
    err(f"removed {len(removed)} orphaned resource(s)")


if __name__ == "__main__":  # pragma: no cover
    app()
