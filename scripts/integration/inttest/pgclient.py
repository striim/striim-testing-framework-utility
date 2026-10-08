"""Postgres admin client for `scripts/integration/`'s Postgres test-data isolation
(docs/internals/INTEGRATION-ENGINE.md#token-isolation) -- a scoped port of
`scripts/live/livetest/pgclient.py::PgAdmin`. Read that file alongside this one; a
reviewer diffing the two should find only the deliberate deviations listed in
docs/internals/INTEGRATION-ENGINE.md#token-isolation:

  1. No REPLICATION in ensure_setup's CREATE ROLE -- no CDC/logical-decoding in
     this tier (services/postgres/sql/init.sql says so directly).
  2. No drop_replication_slot, no run_sql, no create_schema/drop_schema, no
     _qualified/count_rows/select_rows, no livetest.sqlutil import -- this tier
     has no assert.diff equivalent to need the read helpers, no replication slot
     to drop, and dbroutes.py::_run_postgres already IS run_sql (SET search_path
     then execute, driven by the POSTGRES_{SOURCE,TARGET}_SCHEMA token).
  3. reset_test_objects("") raises ValueError instead of silently matching every
     table -- live's serial branch never passes "" (it calls reset_schemas
     instead), so live never exercises the degenerate LIKE '%' case; this tier
     makes that unreachable-by-construction rather than merely unreached, the
     same posture Spanner's teardown already takes for an empty prefix.
  4. A new dsn_from_tokens(tokens) adapter -- integration's call sites hold the
     published-token shape (POSTGRES_HOST, POSTGRES_SOURCE_USER, ...) rather than
     live's docker_defaults shape, so dsn construction needs one extra mapping
     step live doesn't.
  5. ensure_setup is called unconditionally rather than through an
     ensure_provisioned_once-style lock (docs/internals/INTEGRATION-ENGINE.md#token-isolation,
     deferred) -- made safe under uncoordinated concurrent callers by
     `_exec_with_retry` rather than by the DO block alone (see ensure_setup's
     docstring: a plain `_exec` was NOT enough, confirmed by
     test_ensure_setup_is_safe_under_concurrent_callers), so the lock remains
     an optimization, not a correctness requirement -- but the correctness
     argument routes through retry-on-transient-failure, not through the DO
     block being sufficient on its own.
  6. Teardown call sites (in plugin.py, not here) print "[integration] WARNING:
     ..." on failure rather than swallowing silently, matching the sibling
     Oracle/Spanner branches in the same methods.

No imports from `plugin.py`: `plugin.py` imports this module, so the reverse
import would be circular, and a zero-`plugin` dependency is what makes this
module unit-testable in isolation against a real Postgres before `plugin.py` is
touched at all.
"""
from __future__ import annotations

import os
import random
import re
import time
from typing import Mapping

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
# A role password safe to inline into CREATE ROLE (letters/digits/underscore).
_SIMPLE_PW = re.compile(r"^[A-Za-z0-9_]+$")

# services/postgres/service.yaml's docker_defaults.dbname -- the harness's own
# disposable sandbox database. reset_schemas() runs DROP SCHEMA ... CASCADE as
# superuser; services/postgres/service.yaml's live_env lets INT_PG_DB (and the
# rest of INT_PG_*) redirect a real test run at ANY Postgres today, including a
# shared/real one, independent of the (still-deferred) live_override_env mode
# switch. Refusing to run the CASCADE drop unless the dsn still points at the
# sandbox dbname -- or the caller explicitly opts in -- keeps an INT_PG_* override
# from silently wiping a database the harness does not own.
_SANDBOX_DBNAME = "intdb"
_ALLOW_DESTRUCTIVE_RESET_ENV = "INT_PG_ALLOW_DESTRUCTIVE_RESET"


class UnsafeResetError(Exception):
    """Raised by `reset_schemas` when the target dsn's dbname is not the
    harness's own sandbox database and the caller has not explicitly opted in
    via `INT_PG_ALLOW_DESTRUCTIVE_RESET=1`."""


def _assert_reset_is_safe(dsn: Mapping[str, str]) -> None:
    dbname = dsn.get("dbname")
    if dbname == _SANDBOX_DBNAME:
        return
    if os.environ.get(_ALLOW_DESTRUCTIVE_RESET_ENV) == "1":
        return
    raise UnsafeResetError(
        f"refusing reset_schemas(): dbname {dbname!r} is not the harness's own "
        f"sandbox database ({_SANDBOX_DBNAME!r}). This looks like an override "
        f"(INT_PG_DB / INT_PG_HOST) pointed at a real or shared Postgres, and "
        f"reset_schemas() runs DROP SCHEMA ... CASCADE as superuser against "
        f"qasource/qatarget on it. Set {_ALLOW_DESTRUCTIVE_RESET_ENV}=1 to confirm "
        f"this database is safe to wipe.")

# The dsn keys PgAdmin reads, and the per-test token each is sourced from.
_TOKEN_TO_DSN_KEY = {
    "POSTGRES_HOST": "host",
    "POSTGRES_PORT": "port",
    "POSTGRES_DB": "dbname",
    "POSTGRES_ADMIN_USER": "admin_user",
    "POSTGRES_ADMIN_PASSWORD": "admin_password",
    "POSTGRES_SOURCE_USER": "source_user",
    "POSTGRES_SOURCE_PASSWORD": "source_password",
    "POSTGRES_SOURCE_SCHEMA": "source_schema",
    "POSTGRES_TARGET_USER": "target_user",
    "POSTGRES_TARGET_PASSWORD": "target_password",
    "POSTGRES_TARGET_SCHEMA": "target_schema",
}


class MissingTokensError(Exception):
    """Raised by `dsn_from_tokens` naming every absent token, rather than letting
    a `KeyError` name only the first one a dict access happens to hit."""


def _default_connect(**kw):
    import psycopg2
    return psycopg2.connect(**kw)


def _check(name: str) -> str:
    if not _IDENT.match(name):
        raise ValueError(f"unsafe identifier: {name!r}")
    return name


# The transients this tier has actually observed under concurrent
# ensure_setup() calls (see _exec_with_retry's docstring), matched by
# substring rather than exception type since neither maps to a class narrow
# enough to retry wholesale on its own:
#   - "tuple concurrently updated": a Postgres catalog serialization failure
#     on the shared GRANT CONNECT ACL row, surfaced by psycopg2 as
#     InternalError_ (Postgres XX000).
#   - "pg_authid_rolname_index": two truly-concurrent CREATE ROLE statements
#     racing past the DO block's own duplicate_object check -- Postgres can
#     resolve that race as either a duplicate_object (42710, which the DO
#     block's EXCEPTION clause already swallows) or a unique_violation
#     (23505) on this index, depending on exact MVCC timing. Found by test
#     (test_ensure_setup_is_safe_under_concurrent_callers) once that test was
#     fixed to actually race 8 callers against a role that does not yet
#     exist, rather than 8 callers all hitting the DO block's cheap
#     already-exists path.
#   - "pg_namespace_nspname_index": the exact same class of race, one
#     statement later in the same list -- `CREATE SCHEMA IF NOT EXISTS` has no
#     EXCEPTION-swallowing DO block at all, so two truly-concurrent sessions
#     that both see "doesn't exist yet" can have Postgres resolve the race as
#     a unique_violation on the schema catalog's own unique index, rather than
#     the no-op `IF NOT EXISTS` implies. The schema-catalog analogue of
#     `pg_authid_rolname_index` above.
_TRANSIENT_RETRY_SNIPPETS = (
    "tuple concurrently updated", "pg_authid_rolname_index", "pg_namespace_nspname_index",
)


def _is_transient_catalog_race(exc: Exception) -> bool:
    text = str(exc)
    return any(snippet in text for snippet in _TRANSIENT_RETRY_SNIPPETS)


def tid_like_pattern(tid: str) -> str:
    """The `LIKE ... ESCAPE '\\'` pattern matching every table whose name carries
    this `${TID}` prefix -- module-level (rather than a `PgAdmin` method) so its
    only caller, `reset_test_objects`, and any future direct caller share one
    escaping implementation. `perf.py`'s liveness probe does NOT use this: it
    only checks schema reachability, not table names (see `_probe_once`'s
    docstring for why an emptiness/name check there was wrong). Postgres
    lowercases unquoted identifiers and `${TID}` is already
    lowercase; underscores in the tid are escaped so they aren't LIKE
    wildcards. `tid` already includes its own trailing '_' separator (e.g.
    `t3f9a2b1c4_`), so it IS the full table-name prefix -- the returned pattern
    is `<tid>%`, not `<tid>_%`."""
    prefix = tid.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"{prefix}%"


def dsn_from_tokens(tokens: Mapping[str, str]) -> dict:
    """Maps the per-test token table's published Postgres names (POSTGRES_HOST,
    POSTGRES_SOURCE_USER, ...) onto `PgAdmin`'s dsn key shape (host, source_user,
    ...) -- the one adaptation live's call sites don't need, since live hands
    `PgAdmin` a dict already in dsn shape. Raises `MissingTokensError` naming
    every absent token if any are missing, rather than a bare `KeyError` naming
    only the first."""
    missing = [tok for tok in _TOKEN_TO_DSN_KEY if tok not in tokens]
    if missing:
        raise MissingTokensError(
            f"dsn_from_tokens: missing token(s): {', '.join(sorted(missing))}")
    return {dsn_key: tokens[tok] for tok, dsn_key in _TOKEN_TO_DSN_KEY.items()}


class PgAdmin:
    """Postgres admin for the integration harness's own connection (setup +
    per-test reset). One instance drives ONE data role, chosen by `role`
    ("source" -> qasource, "target" -> qatarget): fixed schemas, not a per-test
    schema. `ddl:`/`seed:` execution itself stays on `dbroutes.py::_run_postgres`
    (§4, non-goals) -- this class only owns setup and reset.

    The dsn carries `host`/`port`/`dbname`, `admin_user`/`admin_password`,
    `source_user`/`source_password`/`source_schema`,
    `target_user`/`target_password`/`target_schema`. Missing optional keys fall
    back to the `source_*` values so a minimal single-user dsn still works,
    mirroring live's `PgAdmin`.
    """

    def __init__(self, dsn: dict, connect=None, role: str = "source") -> None:
        self.dsn = dsn
        self._connect = connect or _default_connect
        self.role = role

    def _creds(self, which: str):
        # which: "admin" | "source" | "target". Fall back to source_* for any absent key.
        user = self.dsn.get(f"{which}_user", self.dsn.get("source_user"))
        pw = self.dsn.get(f"{which}_password", self.dsn.get("source_password"))
        return user, pw

    def _schema(self) -> str:
        default = "qasource" if self.role == "source" else "qatarget"
        return _check(self.dsn.get(f"{self.role}_schema", default))

    def _open(self, which: str | None = None, attempts: int = 30, delay: float = 2.0):
        # Retry the connect through Postgres cold-start transients: the container's
        # healthcheck can report ready a beat before the very first client connection
        # succeeds. `which` selects the credential set (defaults to this instance's role).
        user, password = self._creds(which or self.role)
        last = None
        for _ in range(attempts):
            try:
                conn = self._connect(
                    host=self.dsn["host"], port=int(self.dsn["port"]),
                    dbname=self.dsn["dbname"], user=user, password=password,
                )
                conn.autocommit = True
                return conn
            except Exception as e:      # psycopg2.OperationalError on a cold/refused socket
                last = e
                time.sleep(delay)
        raise last

    def _exec(self, statements: list[str], which: str | None = None) -> None:
        conn = self._open(which)
        try:
            cur = conn.cursor()
            try:
                for s in statements:
                    cur.execute(s)
            finally:
                cur.close()
        finally:
            conn.close()

    def _exec_with_retry(self, statements: list[str], which: str | None = None,
                          attempts: int = 10, delay: float = 0.2) -> None:
        """Like `_exec`, but retries the WHOLE statement list, up to `attempts`
        times with linear backoff PLUS random jitter (each attempt sleeps
        `delay * (attempt + 1) * uniform(0.5, 1.5)`), WHEN AND ONLY WHEN the
        failure matches `_is_transient_catalog_race` -- any other exception (a bad
        password, a network partition, a genuine SQL error) propagates immediately on the
        first attempt. Only safe to use when every statement in `statements` is
        independently idempotent (true of `ensure_setup`'s DO-block-guarded
        CREATE ROLE, GRANT CONNECT, and CREATE SCHEMA IF NOT EXISTS -- re-running
        an already-applied one is a no-op, not an error) -- re-running the whole
        list after a partial failure is then safe regardless of how far the
        failed attempt got. Exists because the DO block only swallows
        `duplicate_object`; real concurrent contention on the same catalog rows
        (observed under test: 8 threads calling `ensure_setup` simultaneously)
        can throw errors the DO block's EXCEPTION clause does not name --
        `_TRANSIENT_RETRY_SNIPPETS` above documents each one found so far and
        why it's safe to retry.

        Deliberately NOT a bare `except Exception`: `_exec` -> `_open` already
        retries a cold/refused connect up to 30x at 2.0s (`_open`'s own
        `attempts`/`delay`), so composing that with an unconditional retry here
        would turn one permanently-bad credential into ~10 * 30 = 300 connect
        attempts and several minutes of silence before the real error surfaces
        -- confirmed by measurement. Narrowing the predicate to the one named
        transient keeps the fix and removes that amplification: a bad password
        still takes only `_open`'s own ~60s to surface, matching this tier's
        pre-migration connect-retry behavior.

        The jitter (not just linear backoff) matters under this test's own 8-way
        concurrency: without it, threads that start `ensure_setup` within
        milliseconds of each other retry in near lockstep and keep re-colliding
        on the same catalog row every attempt -- observed as an occasional
        `attempts`-exhaustion failure even though every individual retry
        correctly matched `_is_transient_catalog_race`. Randomizing each
        thread's wait desynchronizes the retry schedule so contending threads
        stop re-colliding after an attempt or two."""
        last = None
        for attempt in range(attempts):
            try:
                self._exec(statements, which=which)
                return
            except Exception as e:
                if not _is_transient_catalog_race(e):
                    raise
                last = e
                time.sleep(delay * (attempt + 1) * random.uniform(0.5, 1.5))
        raise last

    def _query(self, sql: str, params=None, which: str | None = None):
        conn = self._open(which)
        try:
            cur = conn.cursor()
            try:
                if params is None:
                    cur.execute(sql)
                else:
                    cur.execute(sql, params)
                cols = [d[0] for d in (cur.description or [])]
                return cols, cur.fetchall()
            finally:
                cur.close()
        finally:
            conn.close()

    def _role_password(self, which: str) -> str:
        _, pw = self._creds(which)
        if not _SIMPLE_PW.match(pw or ""):
            raise ValueError(f"unsafe {which} role password for CREATE ROLE: {pw!r}")
        return pw

    def ensure_setup(self) -> None:
        """Create the qasource/qatarget login roles + their owned schemas,
        idempotently, as the admin (superuser). Postgres has no CREATE ROLE IF
        NOT EXISTS, and an IF NOT EXISTS(...) guard is a TOCTOU race under
        concurrent workers (two see "absent" then both CREATE -> "role already
        exists"). Instead CREATE unconditionally and swallow duplicate_object in
        the DO block. The (validated) password is inlined because it can't be a
        bind param.

        Idempotent, and safe under uncoordinated concurrent callers WITHOUT an
        ensure_provisioned_once-style lock (docs/internals/INTEGRATION-ENGINE.md#token-isolation) --
        but getting there needs `_exec_with_retry`, not just the DO block.
        CONFIRMED BY TEST (test_pgclient.py::
        test_ensure_setup_is_safe_under_concurrent_callers, 8 concurrent
        callers, racing from a fully deprovisioned start): the DO block's
        EXCEPTION WHEN duplicate_object clause only names ONE failure mode.
        Real concurrent contention on the same catalog rows also throws (at
        least) two OTHER, genuinely transient errors whose contract is "retry
        and it succeeds," not "the desired state already exists, treat as
        success" -- see `_TRANSIENT_RETRY_SNIPPETS`'s comment for both. Every
        statement here is independently idempotent (the DO block itself,
        GRANT CONNECT, CREATE SCHEMA IF NOT EXISTS), so retrying the whole
        list from the top is safe regardless of how far a failed attempt got.

        No REPLICATION grant (unlike live's PgAdmin): this tier has no CDC/
        logical-decoding, per services/postgres/sql/init.sql's own comment.
        No GRANT ALL ON SCHEMA after AUTHORIZATION {role} either -- ownership
        already subsumes it."""
        stmts = []
        for which in ("source", "target"):
            role = _check(self.dsn.get(f"{which}_user", which))
            pw = self._role_password(which)
            schema = _check(self.dsn.get(f"{which}_schema", role))
            stmts.append(
                f"DO $$ BEGIN CREATE ROLE {role} LOGIN PASSWORD '{pw}'; "
                f"EXCEPTION WHEN duplicate_object THEN NULL; END $$")
            stmts.append(f'GRANT CONNECT ON DATABASE "{_check(self.dsn["dbname"])}" TO {role}')
            stmts.append(f'CREATE SCHEMA IF NOT EXISTS "{schema}" AUTHORIZATION {role}')
        self._exec_with_retry(stmts, which="admin")

    def reset_schemas(self) -> None:
        """Per-test clean slate for a SERIAL run, fixed schemas: drop + recreate
        the qasource and qatarget schemas as the admin. CASCADE clears any
        tables a prior test left. Safe only because a serial run has no
        concurrent sibling to clobber -- the parallel-safe analogue is
        `reset_test_objects`.

        Refuses to run against a dsn whose dbname is not the harness's own
        sandbox database unless explicitly overridden -- see
        `_assert_reset_is_safe`/`UnsafeResetError`."""
        _assert_reset_is_safe(self.dsn)
        stmts = []
        for which in ("source", "target"):
            role = _check(self.dsn.get(f"{which}_user", which))
            schema = _check(self.dsn.get(f"{which}_schema", role))
            stmts.append(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
            stmts.append(f'CREATE SCHEMA "{schema}" AUTHORIZATION {role}')
        self._exec(stmts, which="admin")

    def reset_test_objects(self, tid: str) -> None:
        """Per-test clean slate SAFE under concurrent workers: drop only THIS
        test's ${tid}-prefixed tables in the shared qasource/qatarget schemas --
        NOT the whole-schema DROP CASCADE (`reset_schemas`), which would clobber
        a sibling worker's tables mid-run. A same-test re-run reuses the random
        tid only within that one run (tokens.isolation_tokens generates a fresh
        one per call), so a stale same-tid table from a PRIOR run cannot exist
        here; the migrated fixtures' own `DROP TABLE IF EXISTS` covers a rerun
        with a different tid inside the same (serial-mode, unprefixed) name.

        Postgres lowercases unquoted identifiers and `${TID}` is already
        lowercase; underscores in the tid are escaped so they aren't LIKE
        wildcards. `tid` already includes its own trailing '_' separator (e.g.
        `t3f9a2b1c4_`), so it IS the full table-name prefix -- match `<tid>%`,
        not `<tid>_%`. (Caveat, as with any prefix scheme: a tid that is a
        prefix of another test's tid could over-match -- tokens._random_id
        returns a fixed-width 9-hex-char body, so one tid can never be a strict
        prefix of another.)

        Deliberate deviation from live's PgAdmin: an empty `tid` raises
        ValueError instead of degrading the LIKE pattern to '%' (drop every
        table in both schemas). Live gets away with accepting "" because its
        serial branch calls `reset_schemas` instead and never passes "" here;
        making the empty case a hard error instead of merely unreached matches
        the refusal this tier already documents for Spanner's teardown ("a
        blanket drop of every user table ... is a footgun with no upside
        here")."""
        if not tid:
            raise ValueError(
                "reset_test_objects: tid must be non-empty -- an empty prefix would "
                "match every table in qasource/qatarget (call reset_schemas for the "
                "serial-mode whole-schema reset instead)")
        for which in ("source", "target"):
            role = _check(self.dsn.get(f"{which}_user", which))
            schema = _check(self.dsn.get(f"{which}_schema", role))
            drops = [f'DROP TABLE IF EXISTS "{schema}"."{_check(t)}" CASCADE'
                     for t in self.tables_in(schema, tid)]
            if drops:
                self._exec(drops, which="admin")

    def tables_in(self, schema: str, tid: str | None = None) -> list[str]:
        """Table names in `schema` (a real schema name, e.g. `"qasource"`),
        optionally filtered to those carrying `tid`'s `${TID}` prefix (via
        `tid_like_pattern`). Read-only. `reset_test_objects` is the only
        production caller (always with a real `tid`, to find what to drop);
        `perf.py`'s liveness probe does not call this (it checks schema
        reachability only) -- the `tid=None` branch exists for general
        introspection/tests, not because anything in this tier's runtime path
        needs an unfiltered listing."""
        if tid:
            _, rows = self._query(
                "SELECT tablename FROM pg_tables WHERE schemaname = %s AND tablename LIKE %s ESCAPE '\\'",
                params=(_check(schema), tid_like_pattern(tid)), which="admin")
        else:
            _, rows = self._query(
                "SELECT tablename FROM pg_tables WHERE schemaname = %s",
                params=(_check(schema),), which="admin")
        return [t for (t,) in rows]
