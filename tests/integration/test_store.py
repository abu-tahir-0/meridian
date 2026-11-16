"""Persistence, and the guarantee that the readable schema is the real one.

`test_migrations_match_the_canonical_schema` is §16's gotcha 11. Without it,
`schema.sql` and the migration chain drift, and the readable copy becomes a lie —
which is worse than having no readable copy, because people trust it.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from meridian.manifest.build import build_manifest
from meridian.models.run import (
    ErrorCause,
    Outcome,
    RunResult,
    RunStatus,
    TaskResult,
    TrialResult,
)
from meridian.store import repo
from meridian.suites.loader import load_suite
from tests.conftest import CHECKOUT_SUITE, REPO_ROOT

pytestmark = pytest.mark.integration

ADMIN_URL = os.environ.get(
    "MERIDIAN_DATABASE_URL", "postgresql://meridian:meridian@localhost:5433/meridian"
)


def postgres_available() -> bool:
    try:
        import psycopg

        with psycopg.connect(ADMIN_URL, connect_timeout=3):
            return True
    except Exception:
        return False


requires_postgres = pytest.mark.skipif(
    not postgres_available(),
    reason="Postgres is not reachable; start it with `docker compose up -d postgres`",
)

pytestmark = [pytest.mark.integration, requires_postgres]


def _database_url(name: str) -> str:
    base, _, _ = ADMIN_URL.rpartition("/")
    return f"{base}/{name}"


@pytest.fixture
def scratch_database() -> Iterator[str]:
    """A throwaway database, dropped afterwards."""
    import psycopg

    name = f"meridian_test_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(ADMIN_URL, autocommit=True) as admin:
        admin.execute(f'CREATE DATABASE "{name}"')
    try:
        yield _database_url(name)
    finally:
        with psycopg.connect(ADMIN_URL, autocommit=True) as admin:
            admin.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s",
                (name,),
            )
            admin.execute(f'DROP DATABASE IF EXISTS "{name}"')


# Alembic's own bookkeeping table is not part of the schema anyone reads.
ALEMBIC_TABLE = "alembic_version"


def introspect(url: str) -> dict[str, Any]:
    """Everything about a schema that a migration could get wrong."""
    import psycopg

    with psycopg.connect(url) as connection:
        columns = connection.execute(
            """
            SELECT table_name, column_name, data_type, is_nullable, column_default
            FROM information_schema.columns
            WHERE table_schema = 'public'
            ORDER BY table_name, column_name
            """
        ).fetchall()
        checks = connection.execute(
            """
            SELECT rel.relname, con.conname, pg_get_constraintdef(con.oid)
            FROM pg_constraint con
            JOIN pg_class rel ON rel.oid = con.conrelid
            JOIN pg_namespace ns ON ns.oid = rel.relnamespace
            WHERE ns.nspname = 'public'
            ORDER BY rel.relname, con.conname
            """
        ).fetchall()
        indexes = connection.execute(
            """
            SELECT tablename, indexname, indexdef
            FROM pg_indexes WHERE schemaname = 'public'
            ORDER BY tablename, indexname
            """
        ).fetchall()
    return {
        "columns": [tuple(row) for row in columns if row[0] != ALEMBIC_TABLE],
        "constraints": [tuple(row) for row in checks if row[0] != ALEMBIC_TABLE],
        "indexes": [tuple(row) for row in indexes if row[0] != ALEMBIC_TABLE],
    }


def _alembic_config(url: str) -> Any:
    from alembic.config import Config

    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "src/meridian/store/migrations"))
    os.environ["MERIDIAN_DATABASE_URL"] = url
    return config


def run_migrations(url: str, revision: str = "head") -> None:
    from alembic import command

    config = _alembic_config(url)
    try:
        command.upgrade(config, revision)
    finally:
        os.environ["MERIDIAN_DATABASE_URL"] = ADMIN_URL


def downgrade_migrations(url: str, revision: str) -> None:
    from alembic import command

    config = _alembic_config(url)
    try:
        command.downgrade(config, revision)
    finally:
        os.environ["MERIDIAN_DATABASE_URL"] = ADMIN_URL


def test_migrations_match_the_canonical_schema(scratch_database: str) -> None:
    """§16 gotcha 11: the readable copy must be the real one.

    Applies the migration chain to one database and `schema.sql` to another, then
    diffs columns, constraints, and indexes. The moment someone alters a table and
    forgets the readable copy, this fails.
    """
    import psycopg

    migrated = scratch_database
    run_migrations(migrated)

    name = f"meridian_direct_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(ADMIN_URL, autocommit=True) as admin:
        admin.execute(f'CREATE DATABASE "{name}"')
    direct = _database_url(name)
    try:
        with repo.connect(direct) as connection:
            repo.apply_schema(connection)
        assert introspect(migrated) == introspect(direct)
    finally:
        with psycopg.connect(ADMIN_URL, autocommit=True) as admin:
            admin.execute(f'DROP DATABASE IF EXISTS "{name}"')


def test_the_schema_refuses_an_unpinned_snapshot(scratch_database: str) -> None:
    """A product rule the application cannot bypass, because it is a constraint."""
    import psycopg

    run_migrations(scratch_database)
    with repo.connect(scratch_database) as connection:
        tenant = repo.ensure_tenant(connection)
        connection.execute(
            "INSERT INTO suite_version (id, tenant_id, slug, version, content_hash, "
            "adapter_spec) VALUES ('sv1', %s, 's', 1, 'sha256:x', 'a:b:c')",
            (tenant,),
        )
        with pytest.raises(psycopg.errors.CheckViolation):
            connection.execute(
                "INSERT INTO task (id, tenant_id, suite_version_id, slug, definition_hash, "
                "snapshot_digest, definition) VALUES ('t1', %s, 'sv1', 'x', 'h', "
                "'checkout:latest', '{\"outcome_assertions\": [1]}')",
                (tenant,),
            )


def test_the_schema_refuses_a_task_with_no_outcome_assertion(scratch_database: str) -> None:
    import psycopg

    run_migrations(scratch_database)
    with repo.connect(scratch_database) as connection:
        tenant = repo.ensure_tenant(connection)
        connection.execute(
            "INSERT INTO suite_version (id, tenant_id, slug, version, content_hash, "
            "adapter_spec) VALUES ('sv1', %s, 's', 1, 'sha256:x', 'a:b:c')",
            (tenant,),
        )
        with pytest.raises(psycopg.errors.CheckViolation):
            connection.execute(
                "INSERT INTO task (id, tenant_id, suite_version_id, slug, definition_hash, "
                "snapshot_digest, definition) VALUES ('t1', %s, 'sv1', 'x', 'h', "
                "'sha256:aa', '{\"outcome_assertions\": []}')",
                (tenant,),
            )


def test_the_schema_requires_provenance(scratch_database: str) -> None:
    """MD-FR-02, enforced where it cannot be argued with."""
    import psycopg

    run_migrations(scratch_database)
    with repo.connect(scratch_database) as connection:
        tenant = repo.ensure_tenant(connection)
        connection.execute(
            "INSERT INTO suite_version (id, tenant_id, slug, version, content_hash, "
            "adapter_spec) VALUES ('sv1', %s, 's', 1, 'sha256:x', 'a:b:c')",
            (tenant,),
        )
        connection.execute(
            "INSERT INTO task (id, tenant_id, suite_version_id, slug, definition_hash, "
            "snapshot_digest, definition) VALUES ('t1', %s, 'sv1', 'x', 'h', "
            "'sha256:aa', '{\"outcome_assertions\": [1]}')",
            (tenant,),
        )
        with pytest.raises(psycopg.errors.CheckViolation):
            connection.execute("INSERT INTO task_provenance (task_id) VALUES ('t1')")


def make_result(run_id: str) -> RunResult:
    trials = tuple(
        TrialResult(
            run_id=run_id,
            task_slug="happy-path",
            trial_index=i,
            seed=1000 + i,
            outcome=Outcome.PASS if i < 4 else Outcome.FAIL,
            detail="" if i < 4 else "order row still pending",
        )
        for i in range(5)
    )
    return RunResult(
        run_id=run_id,
        suite_slug="checkout-agent",
        suite_version=1,
        status=RunStatus.COMPLETE,
        k=3,
        n_requested=5,
        tasks=(TaskResult(task_slug="happy-path", trials=trials),),
        cost_cents=51,
        duration_ms=34_900,
    )


def test_a_run_round_trips_and_its_scores_are_queryable(scratch_database: str) -> None:
    from meridian.config import load_config

    run_migrations(scratch_database)
    suite = load_suite(CHECKOUT_SUITE)
    config = load_config(overrides={"execution": {"n_trials": 5, "k": 3}})
    manifest = build_manifest(
        suite=suite,
        config=config,
        run_id="run-store",
        created_unix_ms=1_756_000_000_000,
        repo_root=Path(REPO_ROOT),
    )
    result = make_result("run-store")

    with repo.connect(scratch_database) as connection:
        repo.record_run(connection, suite=suite, manifest=manifest, result=result)
        # Writing twice must not double-count: a re-run of the same archived run
        # is a normal thing to do.
        repo.record_run(connection, suite=suite, manifest=manifest, result=result)

        rows = connection.execute(
            "SELECT n, c, k, pass_at_k, pass_hat_k FROM score WHERE run_id = 'run-store'"
        ).fetchall()
        assert len(rows) == 1
        n, c, k, at_k, hat_k = rows[0]
        assert (n, c, k) == (5, 4, 3)
        assert float(at_k) == pytest.approx(1.0)
        # The headline number, stored as an exact decimal rather than a float.
        assert float(hat_k) == pytest.approx(0.4)

        history = repo.suite_history(connection, "checkout-agent")
        assert history[0]["run_id"] == "run-store"
        assert history[0]["suite_pass_hat_k"] == pytest.approx(0.4)


def test_the_error_cause_migration_adds_the_column_to_an_existing_database(
    scratch_database: str,
) -> None:
    import psycopg

    def error_cause_column() -> list[tuple[str, str]]:
        with psycopg.connect(scratch_database) as connection:
            return connection.execute(
                "SELECT data_type, is_nullable FROM information_schema.columns "
                "WHERE table_name = 'trial' AND column_name = 'error_cause'"
            ).fetchall()

    run_migrations(scratch_database)
    downgrade_migrations(scratch_database, "0001")
    assert error_cause_column() == []

    run_migrations(scratch_database)
    assert error_cause_column() == [("text", "YES")]


def test_a_harness_error_keeps_its_cause_in_the_database(scratch_database: str) -> None:
    from meridian.config import load_config

    run_migrations(scratch_database)
    suite = load_suite(CHECKOUT_SUITE)
    config = load_config(overrides={"execution": {"n_trials": 2, "k": 1}})
    manifest = build_manifest(
        suite=suite,
        config=config,
        run_id="run-cause",
        created_unix_ms=1_756_000_000_000,
        repo_root=Path(REPO_ROOT),
    )
    trials = (
        TrialResult(
            run_id="run-cause",
            task_slug="happy-path",
            trial_index=0,
            seed=1,
            outcome=Outcome.PASS,
        ),
        TrialResult(
            run_id="run-cause",
            task_slug="happy-path",
            trial_index=1,
            seed=2,
            outcome=Outcome.HARNESS_ERROR,
            error_cause=ErrorCause.SANDBOX,
            detail="the container image could not be pulled",
            attempts=2,
        ),
    )
    result = RunResult(
        run_id="run-cause",
        suite_slug="checkout-agent",
        suite_version=1,
        status=RunStatus.COMPLETE,
        k=1,
        n_requested=2,
        tasks=(TaskResult(task_slug="happy-path", trials=trials),),
    )

    with repo.connect(scratch_database) as connection:
        repo.record_run(connection, suite=suite, manifest=manifest, result=result)
        rows = connection.execute(
            "SELECT trial_index, outcome, error_cause FROM trial "
            "WHERE run_id = 'run-cause' ORDER BY trial_index"
        ).fetchall()

    assert rows == [(0, "pass", None), (1, "harness_error", "sandbox")]


def test_an_unreachable_database_is_not_fatal_to_a_run() -> None:
    """Meridian's correctness does not depend on Postgres being up."""
    with (
        pytest.raises(repo.StoreUnavailableError, match="runs without a database"),
        repo.connect("postgresql://nobody@127.0.0.1:1/none"),
    ):
        pass
