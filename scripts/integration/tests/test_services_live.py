"""Integration test for inttest.services.ensure_up + inttest.dbroutes against a REAL
Postgres (docs/INTEGRATION-TESTS.md -- service lifecycle + DDL/seed execution).

Not gated: this test carries @pytest.mark.postgres, so `inttest/plugin.py`'s autouse
`cleanup_postgres` fixture resolves the session-scoped `pg_admin` fixture during
SETUP, which itself calls `plugin._ensure_up("postgres", ...)` -- bringing Postgres
up (skipping the test via `pytest.skip` if Docker itself isn't available, rather than
failing) before this test body ever runs. The test body's own
`services.ensure_up_for_test("postgres", lock)` is then usually a no-op pass-through
(Postgres is already up by this point) -- kept as an explicit, self-contained
guarantee this test doesn't secretly depend on `pg_admin` having run first, and as
the pattern other standalone (unmarked, or fixture-free) tests should copy. Either
way, whichever call is the one that actually starts Postgres from down is also the
one that tears it back down afterward; an already-running service (a prior test, a
developer's `python -m inttest.cli start postgres`, `INT_KEEP_SERVICES`) is left
running exactly as found.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from filelock import FileLock

from inttest import paths, services
from inttest.dbroutes import connection_params, run_sql_file, run_sql_text
from inttest.tokens import build_tokens

# Brings up Docker services: `pytest -m "not docker"` deselects this file (5.4 M1 FS4).
pytestmark = pytest.mark.docker

_HERE = Path(__file__).resolve().parent
_INTTEST_DIR = _HERE.parent / "inttest"


@pytest.mark.postgres
@pytest.mark.integration
def test_ensure_up_then_run_sql_file_creates_and_drops_a_table(tmp_path):
    lock = FileLock(str(paths.state_dir() / ".int-compose.lock"))

    with services.ensure_up_for_test("postgres", lock):
        tokens = build_tokens(tmp_path, requires=["postgres"], parallel=False)
        assert tokens["TID"] == ""  # serial run: no per-test prefix, matches ${TID}throwaway below

        ddl_path = tmp_path / "ddl_throwaway.sql"
        ddl_path.write_text(
            "DROP TABLE IF EXISTS ${TID}throwaway;\n"
            "CREATE TABLE ${TID}throwaway (id INT, name TEXT);\n"
            "INSERT INTO ${TID}throwaway (id, name) VALUES (1, 'hello');\n"
        )

        try:
            run_sql_file("postgres-source", ddl_path, tokens)

            # Verify via a direct query using the same connection_params this module
            # derived -- dogfeeding connection_params rather than hand-rolling a second
            # set of credentials.
            import psycopg2

            params = connection_params("postgres-source", tokens)
            conn = psycopg2.connect(
                host=params["host"], port=params["port"], dbname=params["dbname"],
                user=params["user"], password=params["password"],
            )
            try:
                with conn.cursor() as cur:
                    cur.execute(f'SET search_path TO "{params["schema"]}"')
                    cur.execute("SELECT id, name FROM throwaway")
                    rows = cur.fetchall()
            finally:
                conn.close()
            assert rows == [(1, "hello")]
        finally:
            # Cleanup: do NOT leave the throwaway table behind, regardless of outcome.
            run_sql_text("postgres-source", "DROP TABLE IF EXISTS ${TID}throwaway;", tokens)
