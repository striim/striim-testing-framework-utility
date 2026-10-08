"""Integration test for inttest.services.ensure_up + inttest.dbroutes +
inttest.spanneradmin against a REAL Spanner emulator to verify provisioning and routing.

Gated: this carries @pytest.mark.spanner, so `inttest/plugin.py`'s
autouse `cleanup_spanner` fixture brings Spanner up (via `plugin._ensure_up`) the
same way `cleanup_postgres`/`pg_admin` does for Postgres. Each test ALSO requests
`spanner_admins` directly (not just relying on the autouse fixture's side
effect): `dbroutes._run_spanner`, unlike `_run_postgres`/`_run_oracle`, needs
the instance+database to already exist --
`services.ensure_up_for_test` only brings the container up, it does not create
either -- so the dependency on provisioning having already happened is real, and
`spanner_admins` makes it visible in the test signature rather than implicit in the
marker. The test body's own `services.ensure_up_for_test("spanner", lock)` is then
usually a no-op pass-through (Spanner is already up by this point) -- kept as an
explicit, self-contained guarantee this test doesn't secretly depend on some other
test's fixture ordering, mirroring test_services_live.py's Postgres test.

The `spanner_admins` fixture is where the actual gate lives:
`plugin._require_service_opt_in("spanner")` skips before anything is provisioned
unless INT_SPANNER=1 (or INT_EMULATORS=1) is set -- mirrors
scripts/live/services/spanner's already-working SLT_SPANNER gate. `test_id` is
also requested for its own sake (the table-name prefix below), independent of
the gate.

Table names are `test_id`-prefixed (not a fixed literal like `smoke`) so
`cleanup_spanner`'s teardown -- `SpannerAdmin.drop_test_tables(prefix)` -- has
something real to match and drop; a literal table name would leave that fixture's
drop path exercising nothing.
"""
from __future__ import annotations

import pytest
from filelock import FileLock

from inttest import paths, services
from inttest.dbroutes import connection_params, run_sql_text
from inttest.tokens import build_tokens

pytestmark = [pytest.mark.spanner, pytest.mark.docker]   # docker: 5.4 M1 FS4


def _lock() -> FileLock:
    return FileLock(str(paths.state_dir() / ".int-compose.lock"))


@pytest.mark.integration
def test_spanner_google_ddl_dml_roundtrip(tmp_path, spanner_admins, test_id):
    """CREATE TABLE + INSERT against the GoogleSQL-dialect database, read back via
    SpannerAdmin._query -- exercises `ensure` (instance + database creation, already
    done via `spanner_admins`), `run_statements`'s DDL/DML classification, and
    `dbroutes` end-to-end. Teardown (dropping the table) is `cleanup_spanner`'s job,
    not this test's -- see module docstring."""
    with services.ensure_up_for_test("spanner", _lock()):
        tokens = build_tokens(tmp_path, requires=["spanner"], parallel=False)
        table = f"{test_id}_gsql"

        sql = (
            f"DROP TABLE IF EXISTS {table};\n"
            f"CREATE TABLE {table} (id INT64 NOT NULL, name STRING(50)) PRIMARY KEY (id);\n"
            f"INSERT INTO {table} (id, name) VALUES (1, 'hello');\n"
        )
        run_sql_text("spanner-google", sql, tokens)

        from inttest.spanneradmin import SpannerAdmin

        admin = SpannerAdmin(connection_params("spanner-google", tokens))
        _, rows = admin._query(f"SELECT id, name FROM {table}")
        # google-cloud-spanner's result rows are list-like, not tuples.
        assert [list(r) for r in rows] == [[1, "hello"]]


@pytest.mark.integration
def test_spanner_postgres_dialect_ddl_is_actually_postgres_dialect(tmp_path, spanner_admins, test_id):
    """DDL that is valid PostgreSQL-dialect syntax and invalid GoogleSQL syntax
    (`bigint`/`varchar`, inline `PRIMARY KEY`) -- if `_PARAM_SPECS["spanner"]
    ["directions"]["postgres"]["const"]["dialect"]` weren't actually reaching
    `SpannerAdmin.ensure`'s `db.create()`, this statement would syntax-error against
    a GoogleSQL-dialect database. A genuinely discriminating check, not just a
    second happy path. Teardown is `cleanup_spanner`'s job -- see module docstring."""
    with services.ensure_up_for_test("spanner", _lock()):
        tokens = build_tokens(tmp_path, requires=["spanner"], parallel=False)
        table = f"{test_id}_pg"

        sql = (
            f"DROP TABLE IF EXISTS {table};\n"
            f"CREATE TABLE {table} (id bigint PRIMARY KEY, name varchar(50));\n"
            f"INSERT INTO {table} (id, name) VALUES (1, 'hello');\n"
        )
        run_sql_text("spanner-postgres", sql, tokens)

        from inttest.spanneradmin import SpannerAdmin

        admin = SpannerAdmin(connection_params("spanner-postgres", tokens))
        _, rows = admin._query(f"SELECT id, name FROM {table}")
        # google-cloud-spanner's result rows are list-like, not tuples.
        assert [list(r) for r in rows] == [[1, "hello"]]


@pytest.mark.integration
def test_spanner_concurrent_ddl_is_serialised_by_the_ddl_lock(tmp_path, spanner_admins, test_id):
    """The emulator refuses a schema change while another is in flight
    (`FAILED_PRECONDITION: ... concurrent schema change operation ... already in progress`),
    and under `-n 2` two cases' DDL collided on every parallel run of the writer tier.
    `SpannerAdmin.run_statements` now holds a per-database OS lock around its DDL and retries
    the refusal (another PROCESS's read-write transaction, which no lock here can see).

    Twenty-four threads each CREATE + seed their own table at once. With the lock every one
    lands; the negative control (the lock replaced by a no-op) is expected to hit the
    emulator's refusal at least once, and the test records which it saw rather than
    asserting the emulator's timing."""
    import contextlib
    import threading

    from inttest.spanneradmin import SpannerAdmin

    with services.ensure_up_for_test("spanner", _lock()):
        tokens = build_tokens(tmp_path, requires=["spanner"], parallel=False)
        params = connection_params("spanner-google", tokens)

        def storm(tag: str, admin_factory):
            errors = []

            def one(i: int):
                table = f"{test_id}_{tag}_{i}"
                try:
                    admin_factory().run_statements([
                        f"CREATE TABLE {table} (id INT64 NOT NULL, v STRING(8)) PRIMARY KEY (id)",
                        f"INSERT INTO {table} (id, v) VALUES ({i}, 'x')",
                    ])
                except Exception as e:  # noqa: BLE001 -- the emulator's refusal is the point
                    errors.append(str(e))

            threads = [threading.Thread(target=one, args=(i,)) for i in range(24)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            return errors

        locked = storm("locked", lambda: SpannerAdmin(params))
        assert locked == [], f"with the DDL lock every concurrent CREATE must land: {locked}"

        class Unlocked(SpannerAdmin):
            @contextlib.contextmanager
            def _ddl_lock(self):
                yield

            def _update_ddl(self, ddl, budget_s=120.0):  # no retry either: the raw emulator
                self._db().update_ddl(ddl).result(120)

        unlocked = storm("unlocked", lambda: Unlocked(params))
        # Not asserted: the emulator's refusal is timing-dependent. Recorded so a run that
        # never sees it says so, and a run that does names the message the lock removes.
        print(f"unlocked storm: {len(unlocked)} refusal(s): {unlocked[:1]}")
