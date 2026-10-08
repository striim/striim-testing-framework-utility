"""End-to-end tests for `IntYamlItem`'s Postgres isolation, driven through the
REAL `dbroutes.run_sql_text` path -- the same function `IntYamlItem.runtest()`
uses for `ddl:`/`seed:` files (docs/internals/INTEGRATION-ENGINE.md#token-isolation). Unit-level
`PgAdmin` behavior (role/schema creation, prefix-scoped drop, concurrency safety)
lives in `tests/test_pgclient.py`; this file instead proves the pieces compose
correctly through the actual query path a regression fixture uses.

`IntYamlItem` is a raw `pytest.Item`, so pytest fills NO fixtures for it -- the
autouse `cleanup_postgres`/`cleanup_oracle` fixtures in `inttest/plugin.py` never
fire for a YAML test. `inttest/plugin.py` section 9b revives that cleanup intent
imperatively (`_pg_admin`, `pgclient.PgAdmin.ensure_setup`/`reset_schemas`/
`reset_test_objects`, `_drop_oracle_test_prefix`), fired directly from
`IntYamlItem.runtest()`'s own try/finally rather than through the fixture system.

The end-to-end regression proof (a full `test.yaml` with `requires: [postgres]` +
`ddl:`/`seed:`, run through the real `IntYamlItem` pipeline) lives at
`regression/op/referenceop/referenceop-db-isolation/`.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest
from filelock import FileLock

from inttest import paths, services
from inttest.dbroutes import run_sql_text
from inttest.pgclient import PgAdmin, dsn_from_tokens
from inttest.tokens import build_tokens

# Brings up Docker services: `pytest -m "not docker"` deselects this file (5.4 M1 FS4).
pytestmark = pytest.mark.docker

_HERE = Path(__file__).resolve().parent

# Every test below acts on the shared qasource/qatarget schemas via
# admin.reset_schemas() (a whole-schema CASCADE drop) -- unsafe alongside any
# other worker touching Postgres concurrently. See test_pgclient.py's
# identically-named marker for the full rationale.
_xdist_unsafe = pytest.mark.skipif(
    os.environ.get("PYTEST_XDIST_WORKER") is not None,
    reason="acts on the shared qasource/qatarget schemas with a whole-schema "
           "CASCADE drop -- unsafe to run concurrently with any other xdist "
           "worker touching Postgres",
)


def _probe_tokens(*, parallel: bool = False) -> dict:
    return build_tokens(_HERE, requires=["postgres"], parallel=parallel)


def _postgres():
    """`services.ensure_up_for_test`, bound to this module's compose lock -- brings
    Postgres up for the duration of a `with` block if it isn't already running,
    and tears it back down after iff this call started it."""
    lock = FileLock(str(paths.state_dir() / ".int-compose.lock"))
    return services.ensure_up_for_test("postgres", lock)


def _admin(tokens: dict) -> PgAdmin:
    return PgAdmin(dsn_from_tokens(tokens), role="source")


@_xdist_unsafe
@pytest.mark.postgres
@pytest.mark.integration
def test_ddl_and_seed_land_in_the_fixed_qasource_schema():
    """Inversion of the pre-migration assertion: with POSTGRES_SOURCE_SCHEMA at
    its service.yaml default (qasource, never overridden), a ddl:+seed: pair
    through dbroutes.run_sql_text lands the row in qasource -- that is now the
    EXPECTED location, not the forbidden one."""
    with _postgres():
        tokens = _probe_tokens()
        admin = _admin(tokens)
        admin.ensure_setup()
        admin.reset_schemas()  # clean slate; qasource/qatarget both empty
        try:
            run_sql_text(
                "postgres-source",
                "CREATE TABLE cleanup_probe (id INT PRIMARY KEY, name TEXT);",
                tokens,
            )
            run_sql_text(
                "postgres-source",
                "INSERT INTO cleanup_probe (id, name) VALUES (1, 'hello');",
                tokens,
            )

            import psycopg2

            conn = psycopg2.connect(**{
                k: v for k, v in zip(
                    ("host", "port", "dbname", "user", "password"),
                    (tokens["POSTGRES_HOST"], int(tokens["POSTGRES_PORT"]), tokens["POSTGRES_DB"],
                     tokens["POSTGRES_ADMIN_USER"], tokens["POSTGRES_ADMIN_PASSWORD"]))
            })
            try:
                with conn.cursor() as cur:
                    cur.execute("SELECT id, name FROM qasource.cleanup_probe")
                    rows = cur.fetchall()
            finally:
                conn.close()
            assert rows == [(1, "hello")]
        finally:
            admin.reset_schemas()  # leave no debris for a neighboring test/run


@_xdist_unsafe
@pytest.mark.postgres
@pytest.mark.integration
def test_rerunning_the_same_ddl_twice_self_heals():
    """The direct regression test for docs/internals/INTEGRATION-ENGINE.md#token-isolation's fixed
    defect: the CURRENT (pre-migration) mechanism's `_create_pg_test_schema`
    never drops first, and the per-test schema name is deterministic, so a
    failed or --slt-keep-resources run's leftover schema makes the NEXT run of
    that same test fail with "relation already exists". The migrated DDL shape
    (DROP TABLE IF EXISTS ahead of every CREATE TABLE, per POSTGRES_ISOLATION_
    docs/INTEGRATION-TESTS.md) plus the setup-time reset make a serial rerun self-healing
    instead: this runs the same DDL+seed pair twice back to back through the
    real dbroutes path, with no reset in between, and both must succeed."""
    with _postgres():
        tokens = _probe_tokens()
        admin = _admin(tokens)
        admin.ensure_setup()
        admin.reset_schemas()
        try:
            ddl = "DROP TABLE IF EXISTS rerun_probe; CREATE TABLE rerun_probe (id INT PRIMARY KEY);"
            seed = "INSERT INTO rerun_probe (id) VALUES (1);"
            for iteration in range(2):
                run_sql_text("postgres-source", ddl, tokens)
                run_sql_text("postgres-source", seed, tokens)

                import psycopg2

                conn = psycopg2.connect(**{
                    k: v for k, v in zip(
                        ("host", "port", "dbname", "user", "password"),
                        (tokens["POSTGRES_HOST"], int(tokens["POSTGRES_PORT"]), tokens["POSTGRES_DB"],
                         tokens["POSTGRES_SOURCE_USER"], tokens["POSTGRES_SOURCE_PASSWORD"]))
                })
                try:
                    with conn.cursor() as cur:
                        cur.execute("SET search_path TO qasource")
                        cur.execute("SELECT id FROM rerun_probe")
                        rows = cur.fetchall()
                finally:
                    conn.close()
                assert rows == [(1,)], f"iteration {iteration}"
        finally:
            admin.reset_schemas()  # leave no debris for a neighboring test/run


@_xdist_unsafe
@pytest.mark.postgres
@pytest.mark.integration
def test_reset_test_objects_leaves_a_sibling_tids_table_alone_through_the_real_route():
    """End-to-end counterpart to test_pgclient.py's unit-level
    test_reset_test_objects_drops_only_the_matching_prefix: two token tables
    differing only in TID, each running the SAME ddl:+seed: shape through the
    real dbroutes path (mirroring how two concurrently-running fixtures would
    each render their own ${TID}-prefixed config.json/DDL), reset one, and
    confirm the OTHER's table and rows are untouched -- this is what actually
    guarantees two xdist workers running different Postgres fixtures at the
    same time don't clobber each other."""
    with _postgres():
        base_tokens = _probe_tokens()
        admin = _admin(base_tokens)
        admin.ensure_setup()
        admin.reset_schemas()
        try:
            tid_a, tid_b = "ta1111aaa_", "tb2222bbb_"
            tokens_a = {**base_tokens, "TID": tid_a}
            tokens_b = {**base_tokens, "TID": tid_b}
            ddl = "CREATE TABLE ${TID}sibling_probe (id INT PRIMARY KEY);"
            seed = "INSERT INTO ${TID}sibling_probe (id) VALUES (1);"
            run_sql_text("postgres-source", ddl, tokens_a)
            run_sql_text("postgres-source", seed, tokens_a)
            run_sql_text("postgres-source", ddl, tokens_b)
            run_sql_text("postgres-source", seed, tokens_b)

            admin.reset_test_objects(tid_a)

            import psycopg2

            conn = psycopg2.connect(**{
                k: v for k, v in zip(
                    ("host", "port", "dbname", "user", "password"),
                    (base_tokens["POSTGRES_HOST"], int(base_tokens["POSTGRES_PORT"]),
                     base_tokens["POSTGRES_DB"], base_tokens["POSTGRES_ADMIN_USER"],
                     base_tokens["POSTGRES_ADMIN_PASSWORD"]))
            })
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT 1 FROM information_schema.tables "
                        "WHERE table_schema = 'qasource' AND table_name = %s",
                        [f"{tid_a}sibling_probe"])
                    a_exists = cur.fetchone() is not None
                    cur.execute(f'SELECT id FROM qasource."{tid_b}sibling_probe"')
                    b_rows = cur.fetchall()
            finally:
                conn.close()
            assert not a_exists, "tid_a's table should have been dropped"
            assert b_rows == [(1,)], "tid_b's table/rows must survive tid_a's reset untouched"
        finally:
            # tid_a's table is already gone (that's what the test proves); tid_b's
            # survives on purpose, so it needs an explicit reset to avoid leaving
            # debris for a neighboring test/run.
            admin.reset_schemas()


@_xdist_unsafe
@pytest.mark.postgres
@pytest.mark.integration
def test_no_tables_remain_after_a_reset():
    """The teardown-leaves-no-debris property (docs/internals/INTEGRATION-ENGINE.md#token-isolation),
    as an assertion: after ensure_setup + reset_schemas, a direct pg_tables
    query for qasource/qatarget returns nothing. Creates a table unconditionally
    before resetting -- rather than the earlier, non-discriminating version of
    this test, which only called reset_schemas() `if rows:` and so could pass
    trivially (with reset_schemas() never even invoked) whenever the schemas
    already happened to be empty."""
    with _postgres():
        tokens = _probe_tokens()
        admin = _admin(tokens)
        admin.ensure_setup()
        run_sql_text(
            "postgres-source",
            "CREATE TABLE debris_probe (id INT PRIMARY KEY);",
            tokens,
        )

        admin.reset_schemas()

        _, rows = admin._query(  # noqa: SLF001 - test-only introspection
            "SELECT tablename FROM pg_tables WHERE schemaname IN ('qasource', 'qatarget')",
            which="admin")
        assert rows == []
