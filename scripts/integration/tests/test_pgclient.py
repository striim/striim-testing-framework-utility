"""Unit + live tests for `inttest.pgclient.PgAdmin` (docs/internals/INTEGRATION-ENGINE.md#token-isolation).

Split from `tests/test_db_cleanup.py` (which tests the end-to-end `dbroutes`-driven
path) along the seam that file already documented: this file drives `PgAdmin`
directly, at the level a unit test can assert Postgres catalog state (roles,
schemas, `pg_tables`) that a passing/failing YAML test alone can't.

FAST tests need no Docker/Postgres. LIVE tests (`@pytest.mark.postgres`,
`@pytest.mark.integration`) exercise a REAL Postgres, same convention as
`test_db_cleanup.py`/`test_services_live.py`: each brings Postgres up itself if
needed via `_postgres()` and leaves it running afterward unless it was the one
that started it.
"""
from __future__ import annotations

import concurrent.futures
import os
from pathlib import Path

import pytest
from filelock import FileLock

from inttest import paths, services
from inttest.pgclient import MissingTokensError, PgAdmin, dsn_from_tokens
from inttest.tokens import build_tokens

# Brings up Docker services: `pytest -m "not docker"` deselects this file (5.4 M1 FS4).
pytestmark = pytest.mark.docker

_HERE = Path(__file__).resolve().parent


# ============================================================================
# FAST: dsn_from_tokens is a pure function; PgAdmin's SQL-shape logic can be
# exercised against an injected fake `connect`, no database involved.
# ============================================================================


_FULL_TOKENS = {
    "POSTGRES_HOST": "h", "POSTGRES_PORT": "1", "POSTGRES_DB": "d",
    "POSTGRES_ADMIN_USER": "au", "POSTGRES_ADMIN_PASSWORD": "ap",
    "POSTGRES_SOURCE_USER": "su", "POSTGRES_SOURCE_PASSWORD": "sp",
    "POSTGRES_SOURCE_SCHEMA": "ss",
    "POSTGRES_TARGET_USER": "tu", "POSTGRES_TARGET_PASSWORD": "tp",
    "POSTGRES_TARGET_SCHEMA": "ts",
}


def test_dsn_from_tokens_maps_every_key():
    dsn = dsn_from_tokens(_FULL_TOKENS)
    assert dsn == {
        "host": "h", "port": "1", "dbname": "d",
        "admin_user": "au", "admin_password": "ap",
        "source_user": "su", "source_password": "sp", "source_schema": "ss",
        "target_user": "tu", "target_password": "tp", "target_schema": "ts",
    }


def test_dsn_from_tokens_names_missing_tokens():
    incomplete = dict(_FULL_TOKENS)
    del incomplete["POSTGRES_ADMIN_PASSWORD"]
    with pytest.raises(MissingTokensError, match="POSTGRES_ADMIN_PASSWORD"):
        dsn_from_tokens(incomplete)


def test_schema_resolves_per_role():
    dsn = dsn_from_tokens(_FULL_TOKENS)
    source = PgAdmin(dsn, role="source")
    target = PgAdmin(dsn, role="target")
    assert source._schema() == "ss"
    assert target._schema() == "ts"

    # An absent explicit schema falls back to the conventional qasource/qatarget name.
    minimal = {k: v for k, v in dsn.items() if k not in ("source_schema", "target_schema")}
    assert PgAdmin(minimal, role="source")._schema() == "qasource"
    assert PgAdmin(minimal, role="target")._schema() == "qatarget"


def test_reset_test_objects_rejects_an_empty_tid():
    dsn = dsn_from_tokens(_FULL_TOKENS)
    admin = PgAdmin(dsn, role="source")
    with pytest.raises(ValueError, match="non-empty"):
        admin.reset_test_objects("")


class _FakeCursor:
    def __init__(self, calls: list):
        self._calls = calls
        self.description = None

    def execute(self, sql, params=None):
        self._calls.append((sql, params))

    def fetchall(self):
        return []

    def close(self):
        pass


class _FakeConn:
    def __init__(self, calls: list):
        self._calls = calls
        self.autocommit = False

    def cursor(self):
        return _FakeCursor(self._calls)

    def close(self):
        pass


def test_reset_test_objects_escapes_like_metacharacters():
    calls: list = []
    dsn = dsn_from_tokens(_FULL_TOKENS)
    admin = PgAdmin(dsn, connect=lambda **kw: _FakeConn(calls), role="source")

    admin.reset_test_objects("we_ird%tid_")

    select_calls = [c for c in calls if c[0].startswith("SELECT")]
    assert len(select_calls) == 2, "one SELECT per schema (source, target)"
    for _, params in select_calls:
        schema, like = params
        assert like == "we\\_ird\\%tid\\_%"
    assert {params[0] for _, params in select_calls} == {"ss", "ts"}


# ============================================================================
# LIVE: exercise PgAdmin against a REAL Postgres. Not gated -- each test brings
# Postgres up itself if needed (see _postgres() below) and leaves it running
# afterward unless it was the one that started it.
#
# Every test below acts on the shared qasource/qatarget schemas via a
# whole-schema CASCADE drop (reset_schemas, through the pg_dsn fixture) or a
# full DROP ROLE (_deprovision, through deprovisioned_pg_dsn) -- unsafe
# alongside any other worker touching Postgres concurrently, so all are marked
# _xdist_unsafe and skipped under pytest-xdist. Both fixtures also clean up
# after the test (pytest runs fixture code after `yield` even when the test
# raises), so nothing here leaves debris in qasource/qatarget for its
# neighbors or for a later regression run (docs/internals/INTEGRATION-ENGINE.md#token-isolation).
# ============================================================================

_xdist_unsafe = pytest.mark.skipif(
    os.environ.get("PYTEST_XDIST_WORKER") is not None,
    reason="acts on the shared qasource/qatarget schemas with a whole-schema "
           "CASCADE drop or a DROP ROLE -- unsafe to run concurrently with "
           "any other xdist worker touching Postgres",
)


def _probe_tokens() -> dict:
    return build_tokens(_HERE, requires=["postgres"], parallel=False)


def _postgres():
    """`services.ensure_up_for_test`, bound to this module's compose lock -- brings
    Postgres up for the duration of a `with` block if it isn't already running,
    and tears it back down after iff this call started it."""
    lock = FileLock(str(paths.state_dir() / ".int-compose.lock"))
    return services.ensure_up_for_test("postgres", lock)


def _admin_conn(dsn: dict):
    import psycopg2
    return psycopg2.connect(
        host=dsn["host"], port=int(dsn["port"]), dbname=dsn["dbname"],
        user=dsn["admin_user"], password=dsn["admin_password"],
    )


def _role_exists(dsn: dict, role: str) -> bool:
    conn = _admin_conn(dsn)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", [role])
            return cur.fetchone() is not None
    finally:
        conn.close()


def _schema_owner(dsn: dict, schema: str) -> str | None:
    conn = _admin_conn(dsn)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT pg_get_userbyid(nspowner) FROM pg_namespace WHERE nspname = %s",
                [schema])
            row = cur.fetchone()
            return row[0] if row else None
    finally:
        conn.close()


def _tables_in(dsn: dict, schema: str) -> set[str]:
    conn = _admin_conn(dsn)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT tablename FROM pg_tables WHERE schemaname = %s", [schema])
            return {r[0] for r in cur.fetchall()}
    finally:
        conn.close()


def _create_table(dsn: dict, schema: str, table: str) -> None:
    conn = _admin_conn(dsn)
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(f'CREATE TABLE "{schema}"."{table}" (id INT)')
    finally:
        conn.close()


def _deprovision(dsn: dict) -> None:
    """Drop the qasource/qatarget schemas AND roles entirely -- stronger than
    `reset_schemas()` (which empties the schemas but leaves the role/schema
    shells in place). Used ahead of the ensure_setup tests below so they
    actually exercise CREATE rather than finding state
    services/postgres/sql/init.sql already left behind: without this, a
    no-op `ensure_setup()` would pass every assertion in those tests
    trivially (confirmed by mutation during review)."""
    conn = _admin_conn(dsn)
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            for schema in ("qasource", "qatarget"):
                cur.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
            for role in ("qasource", "qatarget"):
                cur.execute(
                    f"DO $$ BEGIN DROP OWNED BY {role}; "
                    f"EXCEPTION WHEN undefined_object THEN NULL; END $$")
                cur.execute(f"DROP ROLE IF EXISTS {role}")
    finally:
        conn.close()


@pytest.fixture()
def pg_dsn():
    """Real Postgres dsn, brought up if needed. `ensure_setup` + `reset_schemas`
    both before AND after the test, so every live test using this fixture
    starts from -- and leaves -- an empty, provisioned qasource/qatarget."""
    with _postgres():
        dsn = dsn_from_tokens(_probe_tokens())
        admin = PgAdmin(dsn, role="source")
        admin.ensure_setup()
        admin.reset_schemas()
        yield dsn
        PgAdmin(dsn, role="source").reset_schemas()


@pytest.fixture()
def deprovisioned_pg_dsn():
    """Real Postgres dsn with qasource/qatarget (roles AND schemas) fully
    dropped before the test runs, so `ensure_setup()` genuinely has something
    to create. Restores normal provisioned state afterward so this test
    doesn't leave the shared sandbox unprovisioned for whatever runs next."""
    with _postgres():
        dsn = dsn_from_tokens(_probe_tokens())
        _deprovision(dsn)
        yield dsn
        admin = PgAdmin(dsn, role="source")
        admin.ensure_setup()
        admin.reset_schemas()


@_xdist_unsafe
@pytest.mark.postgres
@pytest.mark.integration
def test_ensure_setup_creates_both_roles_and_schemas(deprovisioned_pg_dsn):
    dsn = deprovisioned_pg_dsn
    assert not _role_exists(dsn, "qasource"), "fixture must have actually deprovisioned"

    PgAdmin(dsn, role="source").ensure_setup()

    assert _role_exists(dsn, "qasource")
    assert _role_exists(dsn, "qatarget")
    assert _schema_owner(dsn, "qasource") == "qasource"
    assert _schema_owner(dsn, "qatarget") == "qatarget"


@_xdist_unsafe
@pytest.mark.postgres
@pytest.mark.integration
def test_ensure_setup_is_idempotent(deprovisioned_pg_dsn):
    dsn = deprovisioned_pg_dsn
    admin = PgAdmin(dsn, role="source")
    admin.ensure_setup()
    admin.ensure_setup()  # must not raise the second time
    assert _role_exists(dsn, "qasource")


@_xdist_unsafe
@pytest.mark.postgres
@pytest.mark.integration
def test_ensure_setup_is_safe_under_concurrent_callers(deprovisioned_pg_dsn):
    """Licenses docs/internals/INTEGRATION-ENGINE.md#token-isolation's decision to defer an
    ensure_provisioned_once-style lock: N uncoordinated concurrent callers,
    racing from a fully-deprovisioned start (so this actually exercises
    CREATE under contention, not a set of no-ops against state that already
    existed), must all succeed with no exception and leave both roles/schemas
    behind. Correctness here routes through `_exec_with_retry`'s bounded retry
    on the specific transient catalog race (observed: contention on the
    shared `GRANT CONNECT ON DATABASE` ACL row), NOT through the DO block
    alone -- the DO block's `EXCEPTION WHEN duplicate_object` clause does not
    name that error. See `PgAdmin.ensure_setup`'s docstring."""
    dsn = deprovisioned_pg_dsn
    assert not _role_exists(dsn, "qasource"), "fixture must have actually deprovisioned"

    def _call():
        PgAdmin(dsn, role="source").ensure_setup()

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(_call) for _ in range(8)]
        for f in futures:
            f.result()  # re-raises if any thread's ensure_setup() failed

    assert _role_exists(dsn, "qasource")
    assert _role_exists(dsn, "qatarget")


@_xdist_unsafe
@pytest.mark.postgres
@pytest.mark.integration
def test_reset_schemas_drops_every_table_and_keeps_the_schemas(pg_dsn):
    dsn = pg_dsn
    _create_table(dsn, "qasource", "reset_schemas_probe")
    _create_table(dsn, "qatarget", "reset_schemas_probe")

    PgAdmin(dsn, role="source").reset_schemas()

    assert _tables_in(dsn, "qasource") == set()
    assert _tables_in(dsn, "qatarget") == set()
    assert _schema_owner(dsn, "qasource") == "qasource"
    assert _schema_owner(dsn, "qatarget") == "qatarget"


@_xdist_unsafe
@pytest.mark.postgres
@pytest.mark.integration
def test_reset_test_objects_drops_only_the_matching_prefix(pg_dsn):
    """The single most important new assertion in the migration: it is what
    guarantees concurrent workers do not clobber each other, and it is the one
    behavior with no prior analogue in this tier."""
    dsn = pg_dsn
    admin = PgAdmin(dsn, role="source")
    _create_table(dsn, "qasource", "t111aaa_products")
    _create_table(dsn, "qasource", "t222bbb_products")
    _create_table(dsn, "qasource", "products")  # unprefixed sibling

    admin.reset_test_objects("t111aaa_")

    remaining = _tables_in(dsn, "qasource")
    assert "t111aaa_products" not in remaining
    assert remaining == {"t222bbb_products", "products"}


@_xdist_unsafe
@pytest.mark.postgres
@pytest.mark.integration
def test_reset_test_objects_covers_both_source_and_target_schemas(pg_dsn):
    dsn = pg_dsn
    admin = PgAdmin(dsn, role="source")
    _create_table(dsn, "qasource", "t333ccc_orders")
    _create_table(dsn, "qatarget", "t333ccc_orders")

    admin.reset_test_objects("t333ccc_")

    assert "t333ccc_orders" not in _tables_in(dsn, "qasource")
    assert "t333ccc_orders" not in _tables_in(dsn, "qatarget")


@_xdist_unsafe
@pytest.mark.postgres
@pytest.mark.integration
def test_reset_test_objects_is_a_no_op_when_nothing_matches(pg_dsn):
    dsn = pg_dsn
    admin = PgAdmin(dsn, role="source")
    _create_table(dsn, "qasource", "t444ddd_unrelated")

    admin.reset_test_objects("t555eee_")  # no table carries this prefix

    assert _tables_in(dsn, "qasource") == {"t444ddd_unrelated"}
