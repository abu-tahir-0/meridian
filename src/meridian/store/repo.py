"""Data access. No ORM objects leak upward; callers see domain models or plain rows.

Everything here is optional. Meridian's correctness — isolation, grading,
statistics, manifests, the gate — does not depend on a database being reachable,
and a harness that refuses to run because Postgres is down would be a worse tool.
The store adds queryable history, which is a different job.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any

from meridian.models.manifest import Manifest
from meridian.models.run import RunResult
from meridian.models.suite import Suite
from meridian.stats.passk import pass_at_k, pass_hat_k

if TYPE_CHECKING:  # pragma: no cover - typing only
    from psycopg import Connection

SCHEMA_FILE = Path(__file__).parent / "schema.sql"

# Single tenant for the MVP. The column exists everywhere so that adding real
# tenancy is an additive migration rather than a rewrite.
DEFAULT_TENANT_ID = "tenant-default"
DEFAULT_TENANT_NAME = "default"

PROBABILITY_PLACES = Decimal("0.000001")


class StoreUnavailableError(RuntimeError):
    """The database could not be reached. Never fatal to a run."""


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:20]}"


def _probability(value: float) -> Decimal:
    """Probabilities land in NUMERIC columns, quantized, never as floats."""
    return Decimal(repr(value)).quantize(PROBABILITY_PLACES)


@contextmanager
def connect(database_url: str) -> Iterator[Connection[Any]]:
    """Open a connection, or raise a harness error explaining what is unreachable."""
    import psycopg

    try:
        connection = psycopg.connect(database_url, autocommit=False)
    except Exception as exc:
        raise StoreUnavailableError(
            f"could not connect to {database_url.split('@')[-1]}: {exc}. Meridian runs "
            f"without a database; only queryable history is lost."
        ) from exc
    try:
        yield connection
    finally:
        connection.close()


def apply_schema(connection: Connection[Any]) -> None:
    """Create the schema from the canonical file. Used by tests and by bootstrap."""
    connection.execute(SCHEMA_FILE.read_text(encoding="utf-8"))
    connection.commit()


def ensure_tenant(connection: Connection[Any]) -> str:
    connection.execute(
        "INSERT INTO tenant (id, name) VALUES (%s, %s) ON CONFLICT (id) DO NOTHING",
        (DEFAULT_TENANT_ID, DEFAULT_TENANT_NAME),
    )
    return DEFAULT_TENANT_ID


def upsert_suite_version(connection: Connection[Any], suite: Suite, tenant_id: str) -> str:
    row = connection.execute(
        "SELECT id FROM suite_version WHERE tenant_id = %s AND slug = %s AND version = %s",
        (tenant_id, suite.slug, suite.version),
    ).fetchone()
    if row is not None:
        return str(row[0])

    suite_version_id = _id("sv")
    connection.execute(
        "INSERT INTO suite_version (id, tenant_id, slug, version, content_hash, adapter_spec) "
        "VALUES (%s, %s, %s, %s, %s, %s)",
        (
            suite_version_id,
            tenant_id,
            suite.slug,
            suite.version,
            suite.content_hash(),
            suite.adapter_spec,
        ),
    )

    for task in suite.tasks:
        task_id = _id("task")
        connection.execute(
            "INSERT INTO task (id, tenant_id, suite_version_id, slug, definition_hash, "
            "snapshot_digest, state, role, definition) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                task_id,
                tenant_id,
                suite_version_id,
                task.slug,
                task.definition_hash(),
                task.environment.snapshot,
                task.state,
                task.role,
                json.dumps(task.model_dump(mode="json")),
            ),
        )
        provenance = task.provenance
        connection.execute(
            "INSERT INTO task_provenance (task_id, trace_id, session_id, failure_code, "
            "synthetic_reason) VALUES (%s, %s, %s, %s, %s)",
            (
                task_id,
                provenance.trace_id,
                provenance.session_id,
                provenance.failure_code,
                provenance.synthetic_reason,
            ),
        )
    return suite_version_id


def record_run(
    connection: Connection[Any],
    *,
    suite: Suite,
    manifest: Manifest,
    result: RunResult,
) -> str:
    """Persist a run, its trials, and its per-task scores."""
    tenant_id = ensure_tenant(connection)
    suite_version_id = upsert_suite_version(connection, suite, tenant_id)

    connection.execute(
        "INSERT INTO run (id, tenant_id, suite_version_id, status, n_trials, k, commit_sha, "
        "sut_commit_sha, manifest_hash, manifest, cost_cents, duration_ms) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
        "ON CONFLICT (id) DO NOTHING",
        (
            result.run_id,
            tenant_id,
            suite_version_id,
            str(result.status),
            result.n_requested,
            result.k,
            manifest.commit_sha,
            manifest.sut_commit_sha,
            manifest.manifest_hash(),
            json.dumps(manifest.model_dump(mode="json")),
            result.cost_cents,
            result.duration_ms,
        ),
    )

    for task in result.tasks:
        for t in task.trials:
            connection.execute(
                "INSERT INTO trial (id, tenant_id, run_id, task_slug, trial_index, seed, "
                "outcome, detail, attempts, cassette_hash, turns, tool_calls, input_tokens, "
                "output_tokens, duration_ms, error_cause) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (run_id, task_slug, trial_index) DO NOTHING",
                (
                    _id("trial"),
                    tenant_id,
                    result.run_id,
                    task.task_slug,
                    t.trial_index,
                    t.seed,
                    str(t.outcome),
                    t.detail[:4000],
                    t.attempts,
                    t.cassette_hash,
                    t.efficiency.turns,
                    t.efficiency.tool_calls,
                    t.efficiency.input_tokens,
                    t.efficiency.output_tokens,
                    t.efficiency.duration_ms,
                    None if t.error_cause is None else str(t.error_cause),
                ),
            )

        if task.n == 0:
            # A task Meridian could not run has no score. Writing a zero would
            # make an infrastructure blip indistinguishable from a regression.
            continue
        effective_k = min(result.k, task.n)
        connection.execute(
            "INSERT INTO score (id, tenant_id, run_id, task_slug, n, c, k, pass_at_k, "
            "pass_hat_k) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (run_id, task_slug) DO NOTHING",
            (
                _id("score"),
                tenant_id,
                result.run_id,
                task.task_slug,
                task.n,
                task.c,
                effective_k,
                _probability(pass_at_k(task.n, task.c, effective_k)),
                _probability(pass_hat_k(task.n, task.c, effective_k)),
            ),
        )

    connection.commit()
    return result.run_id


def suite_history(
    connection: Connection[Any], suite_slug: str, limit: int = 20
) -> list[dict[str, Any]]:
    """Suite-level pass^k over time — the query the store exists for."""
    rows = connection.execute(
        """
        SELECT r.id, r.started_at, r.commit_sha, r.status,
               AVG(s.pass_hat_k) AS suite_pass_hat_k,
               COUNT(s.id) AS scored_tasks
        FROM run r
        JOIN suite_version sv ON sv.id = r.suite_version_id
        LEFT JOIN score s ON s.run_id = r.id
        WHERE sv.slug = %s
        GROUP BY r.id, r.started_at, r.commit_sha, r.status
        ORDER BY r.started_at DESC
        LIMIT %s
        """,
        (suite_slug, limit),
    ).fetchall()
    return [
        {
            "run_id": row[0],
            "started_at": row[1],
            "commit_sha": row[2],
            "status": row[3],
            "suite_pass_hat_k": float(row[4]) if row[4] is not None else None,
            "scored_tasks": row[5],
        }
        for row in rows
    ]
