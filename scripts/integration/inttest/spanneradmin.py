"""Spanner admin client for the integration tier -- ported from
`scripts/live/livetest/spanneradmin.py`,
trimmed to what this tier actually needs.

The live tier's `SpannerAdmin` also reads rows back for its `assert.file`/`assert.json`
tiers (`count_rows`, `select_rows`, `select_json_rows`, plus the `_rows_to_dicts`/
`_jsonify` helpers that back them). This tier asserts exclusively through
`waevent.compare` on emitted events and never reads a DB row directly, so that half is
deliberately NOT ported here -- porting it would also drag in
`livetest.sqlutil.coerce_cell`, which has no `inttest` equivalent.

What IS ported, verbatim except as noted:
  - `ensure()`      -- idempotent instance+database creation, with the dialect-mismatch
                       self-heal (a long-lived `isolation: none` emulator can otherwise
                       get stuck with a database created under the wrong dialect).
  - `run_sql`       -- as `run_statements(list[str])`, since `dbroutes.run_sql_text`
                       already splits the rendered SQL into statements; this drops the
                       live version's internal `_split_ddl` re-split.
  - `drop_test_tables` -- teardown, used with the per-test `${TID}` prefix.

`ensure`/`admins_for` (module-level, ported from `livetest/plugin.py`'s `_spanner_ensure`/
`_spanner_admins`) live here rather than in `inttest/plugin.py` because both
`dbroutes._run_spanner` and `plugin.py`'s per-test provisioning need them, and
`dbroutes` must not import `plugin`.
"""

from __future__ import annotations

import base64
import contextlib
import fcntl
import os
import re
import time
from pathlib import Path

from inttest import paths

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_$#]*$")


# --- real Cloud Spanner -------------------------------------------------------------------------
#
# INT_SPANNER_CREDENTIALS (a service-account key file path) switches the service from the emulator
# to a real project, instance and two databases that someone else owns and may share. Three rules
# follow, and each is enforced here rather than left to the case:
#   1. Nothing is created or deleted above table level: `ensure` only checks that the instance and
#      both databases exist with the right dialect, naming the missing one.
#   2. Every statement the harness sends must name only objects carrying this test's ${TID}
#      prefix, and teardown drops only that prefix. A serial run's empty ${TID} is refused, since
#      its teardown contract is "drop every table".
#   3. The emulator settings cannot be mixed in: SPANNER_EMULATOR_HOST would silently send Google's
#      client to an emulator, and INT_SPANNER_HOST/PORT would name one.

class RealSpannerError(RuntimeError):
    """A real-Spanner setting, check or guard refused; the message names what to fix."""


#: (project, instance, database, dialect) already checked by `SpannerAdmin.ensure` this session.
_CHECKED: set = set()
#: What the existence and dialect check calls need.
_CHECK_PERMISSIONS = "spanner.instances.get, spanner.databases.get and spanner.databases.getDdl"


#: The shape `tokens.isolation_tokens` gives ${TID} in a parallel run.
_TID = re.compile(r"^t[0-9a-f]{9}_$")
REAL_HOST, REAL_PORT = "spanner.googleapis.com", "443"
_EMULATOR_SETTINGS = ("INT_SPANNER_HOST", "INT_SPANNER_PORT", "INT_SPANNER_ADMIN_PORT",
                      "SPANNER_EMULATOR_HOST")
_REAL_SETTINGS = (("project", "INT_SPANNER_PROJECT"), ("instance", "INT_SPANNER_INSTANCE"),
                  ("gsql_db", "INT_SPANNER_GSQL_DB"), ("pg_db", "INT_SPANNER_PG_DB"))


def real_config(env) -> dict | None:
    """The real-Spanner settings from `env`, or None in emulator mode (INT_SPANNER_CREDENTIALS
    unset). Raises RealSpannerError naming every problem. The key file is checked to exist and
    is never opened here."""
    key = (env.get("INT_SPANNER_CREDENTIALS") or "").strip()
    if not key:
        return None
    problems = []
    mixed = [n for n in _EMULATOR_SETTINGS if (env.get(n) or "").strip()]
    if mixed:
        problems.append(f"unset the emulator setting(s) {', '.join(mixed)}")
    # Absolute, since the module under test runs in a JVM whose working directory is the case's.
    cfg = {"credentials": os.path.abspath(os.path.expanduser(key))}
    missing = []
    for field, name in _REAL_SETTINGS:
        cfg[field] = (env.get(name) or "").strip()
        if not cfg[field]:
            missing.append(name)
    if missing:
        problems.append(f"set {', '.join(missing)} (no defaults apply to a real instance)")
    if not os.path.isfile(cfg["credentials"]):
        problems.append(f"INT_SPANNER_CREDENTIALS names {key}, which is not a file")
    if cfg["gsql_db"] and cfg["gsql_db"] == cfg["pg_db"]:
        problems.append("INT_SPANNER_GSQL_DB and INT_SPANNER_PG_DB name the same database; "
                        "each dialect needs its own")
    if problems:
        raise RealSpannerError("real Spanner (INT_SPANNER_CREDENTIALS is set): " + "; ".join(problems))
    return cfg


def real_tokens(tokens: dict, env) -> dict:
    """`tokens` (the service's emulator `provides:`) rewritten for real Spanner, or unchanged in
    emulator mode. Adds SPANNER_CREDENTIALS, the key path, which `dbroutes.connection_params`
    reads as the mode."""
    cfg = real_config(env)
    if cfg is None:
        return tokens
    base = f"jdbc:cloudspanner:/projects/{cfg['project']}/instances/{cfg['instance']}/databases/"
    return {**tokens,
            "SPANNER_PROJECT": cfg["project"], "SPANNER_INSTANCE": cfg["instance"],
            "SPANNER_GSQL_DB": cfg["gsql_db"], "SPANNER_PG_DB": cfg["pg_db"],
            "SPANNER_HOST": REAL_HOST, "SPANNER_GRPC_PORT": REAL_PORT,
            "SPANNER_GSQL_URL": f"{base}{cfg['gsql_db']}?credentials={cfg['credentials']}",
            "SPANNER_PG_URL": f"{base}{cfg['pg_db']}?credentials={cfg['credentials']}",
            "SPANNER_CREDENTIALS": cfg["credentials"]}


def _require_tid(tid: str) -> None:
    if not _TID.match(tid or ""):
        raise RealSpannerError(
            f"real Spanner needs a per-test ${{TID}} (got {tid!r}) so that only this test's objects "
            "are created and dropped: run in parallel mode (SLT_PARALLEL=1)")


_PART = r'(?:"[^"]+"|`[^`]+`|[A-Za-z_][\w$#]*)'
_NAME = rf"({_PART}(?:\s*\.\s*{_PART})*)"
_KIND = (r"(TABLE|(?:UNIQUE\s+)?(?:NULL_FILTERED\s+)?INDEX|SEARCH\s+INDEX|VECTOR\s+INDEX"
         r"|CHANGE\s+STREAM|VIEW|SEQUENCE)")
_DDL = re.compile(rf"\s*(CREATE(?:\s+OR\s+REPLACE)?|DROP|ALTER)\s+{_KIND}\s+"
                  rf"(?:IF\s+(?:NOT\s+)?EXISTS\s+)?{_NAME}", re.I)
_DML = re.compile(rf"\s*(?:INSERT(?:\s+OR\s+(?:IGNORE|UPDATE))?(?:\s+INTO)?|UPDATE|DELETE(?:\s+FROM)?)"
                  rf"\s+{_NAME}", re.I)
# A keyword and the name after it: whitespace, or none before a quoted name (ON`x`).
_GAP = r"""(?:\s+|\s*(?=["`]))"""
_REFS = re.compile(rf"\b(?:(?:INTERLEAVE\s+IN(?:\s+PARENT)?|REFERENCES|RENAME\s+TO){_GAP}"
                   rf"|SYNONYM\s*\(\s*|SYNONYM{_GAP}){_NAME}", re.I)
_INDEX_ON = re.compile(rf"\bON{_GAP}{_NAME}", re.I)
_STREAM_FOR = re.compile(rf"\bFOR{_GAP}(.*?)(?:\bOPTIONS\b|$)", re.I | re.S)
# Sequence functions: the sequence they advance or read must be this test's.
_SEQ_ANY = re.compile(r"\b(?:GET_NEXT_SEQUENCE_VALUE|GET_INTERNAL_SEQUENCE_STATE|NEXTVAL|SETVAL|CURRVAL)\s*\(",
                      re.I)
_SEQ_GSQL = re.compile(rf"\b(?:GET_NEXT_SEQUENCE_VALUE|GET_INTERNAL_SEQUENCE_STATE)\s*\(\s*SEQUENCE{_GAP}{_NAME}",
                       re.I)
_SEQ_PG = re.compile(r"\b(?:NEXTVAL|SETVAL|CURRVAL|GET_INTERNAL_SEQUENCE_STATE)\s*\(\s*'([^']*)'", re.I)
# A view body's tables: each FROM or JOIN, and each name after a comma in a FROM list.
_FROM = re.compile(rf"\b(?:FROM|JOIN){_GAP}{_NAME}", re.I)
_ALIAS = re.compile(r"""\s+(?:AS\s+)?(?:"[^"]+"|`[^`]+`|[A-Za-z_]\w*)""", re.I)
_FROM_NEXT = re.compile(rf"\s*,\s*{_NAME}")


def _view_tables(body: str) -> list[str]:
    names = []
    for m in _FROM.finditer(body):
        names.append(m.group(1))
        pos = m.end()
        while True:
            alias = _ALIAS.match(body, pos)
            nxt = _FROM_NEXT.match(body, alias.end() if alias else pos)
            if not nxt:
                break
            names.append(nxt.group(1))
            pos = nxt.end()
    return names


def _carries(name: str, tid: str) -> bool:
    # The last part must carry the TID; a qualifier may only be the default schema, since
    # teardown lists tables in '' and public only.
    parts = [p for q in re.findall(_PART, name) for p in q.strip('"`').split(".")]
    return (bool(parts) and parts[-1].lower().startswith(tid.lower())
            and all(p.lower() == "public" for p in parts[:-1]))


def _refusal(stmt: str, tid: str) -> str | None:
    """Why `stmt` may not run against a shared real database, or None when every object it
    creates, alters, drops, writes or attaches to carries `tid`."""
    # split_statements honours only single quotes, so a GoogleSQL double-quoted literal or a
    # DELIMITER line can leave several statements in one entry; the checks below read one.
    if re.search(r";\s*\S", stmt):
        return "a ';' inside an entry (a second statement, or a ';' in a literal) is refused"
    if re.search(r"#|--|/\*", stmt):
        # GoogleSQL starts a comment at '#', which split_statements (shared with every engine,
        # where '#' is an operator or a comment of its own) does not strip, and a caller that
        # did not split leaves -- and /* in: the checks below would read what Spanner skips.
        # Refusing a comment is safer than stripping it here.
        return "a comment ('#', '--' or '/*') inside an entry is refused"
    seq = [m.group(1) for m in _SEQ_GSQL.finditer(stmt)] + [m.group(1) for m in _SEQ_PG.finditer(stmt)]
    if len(seq) < len(_SEQ_ANY.findall(stmt)):
        return "a sequence function whose sequence the guard cannot read"
    ddl = _DDL.match(stmt)
    if ddl:
        if ddl.group(1).upper() == "DROP" and stmt[ddl.end():].strip(" \t\r\n;"):
            return "a DROP names one object and nothing else"
        kind = " ".join(ddl.group(2).upper().split())
        names = [ddl.group(3)] + [m.group(1) for m in _REFS.finditer(stmt)]
        if kind.endswith("INDEX"):
            on = _INDEX_ON.search(stmt, ddl.end())
            if on:
                names.append(on.group(1))
        if kind == "VIEW" and ddl.group(1).upper() != "DROP":
            names += _view_tables(stmt[ddl.end():])
        if kind == "CHANGE STREAM":
            clause = _STREAM_FOR.search(stmt, ddl.end())
            if clause:
                flat = re.sub(r"\([^()]*\)", "", clause.group(1))
                names += [item.strip() for item in flat.split(",")]
    else:
        dml = _DML.match(stmt)
        if not dml:
            return "not a statement the real-Spanner guard recognises"
        names = [dml.group(1)]
    names += seq
    foreign = [n for n in names if not _carries(n, tid)]
    if foreign:
        return (f"{', '.join(foreign)} does not carry this test's ${{TID}} prefix {tid!r} "
                "(or is outside the default schema)")
    return None


class SpannerAdmin:
    def __init__(self, dsn: dict, database=None, client=None):
        # dsn: {project, instance, database, dialect, emulator_host}
        #   dialect: "google_standard_sql" | "postgresql"
        # Real Spanner (dbroutes.connection_params with SPANNER_CREDENTIALS): {project, instance,
        #   database, dialect, credentials, tid} -- a key path and this test's ${TID}, no emulator.
        self.dsn = dsn
        self._database = database  # injectable google Database (query/ddl) -- unit-test seam
        self._client = client  # injectable google Client (ensure) -- unit-test seam

    @property
    def real(self) -> bool:
        return bool(self.dsn.get("credentials"))

    # --- google client plumbing (skipped when fakes are injected) ---------------
    def _spanner_client(self):
        if self._client is not None:
            return self._client
        if self.real:
            return self._real_client()
        os.environ.setdefault("SPANNER_EMULATOR_HOST", self.dsn["emulator_host"])
        from google.cloud import spanner

        return spanner.Client(project=self.dsn["project"])

    def _real_client(self):
        # Checked again here, not only at settings time: an emulator admin earlier in this
        # process may have set it (`setdefault` above), and Google's client would then quietly
        # talk to the emulator with the real project's names.
        if os.environ.get("SPANNER_EMULATOR_HOST"):
            raise RealSpannerError("real Spanner: SPANNER_EMULATOR_HOST is set in this process, "
                                   "which sends Google's client to an emulator; unset it")
        from google.cloud import spanner
        from google.oauth2 import service_account

        creds = service_account.Credentials.from_service_account_file(self.dsn["credentials"])
        return spanner.Client(project=self.dsn["project"], credentials=creds)

    def _db(self):
        if self._database is not None:
            return self._database
        return self._spanner_client().instance(self.dsn["instance"]).database(self.dsn["database"])

    # --- service bootstrap ------------------------------------------------------
    def ensure(self) -> None:
        # Create the emulator instance (idempotent) and this database with its dialect.
        if self.real:
            key = (self.dsn["project"], self.dsn["instance"], self.dsn["database"], self.dsn["dialect"])
            if key in _CHECKED:  # a real database does not vanish mid-session: check it once
                return
            try:
                self._check_real()
            except RealSpannerError:
                raise
            except Exception as e:  # noqa: BLE001 -- named, so the case fails saying what to fix
                denied = type(e).__name__ in ("PermissionDenied", "Forbidden") or "403" in str(e)
                hint = (f"; the key needs {_CHECK_PERMISSIONS}" if denied else "")
                raise RealSpannerError(f"real Spanner: checking database {self.dsn['database']!r} "
                                       f"failed: {e!r}{hint}") from e
            _CHECKED.add(key)
            return
        client = self._spanner_client()
        cfg = f"projects/{self.dsn['project']}/instanceConfigs/emulator-config"
        inst = client.instance(self.dsn["instance"], configuration_name=cfg, display_name="slt")
        if not inst.exists():
            inst.create().result(120)
        from google.cloud.spanner_admin_database_v1 import DatabaseDialect

        dialect = (
            DatabaseDialect.POSTGRESQL
            if self.dsn["dialect"] == "postgresql"
            else DatabaseDialect.GOOGLE_STANDARD_SQL
        )
        db = inst.database(self.dsn["database"], database_dialect=dialect)
        if db.exists():
            # The dialect is fixed at creation and can't be changed in place. On a
            # long-lived shared emulator (isolation: none) a database created before
            # dialect-aware provisioning existed -- or under the wrong dialect from a
            # stray earlier run -- stays stuck: every DDL statement written for the
            # intended dialect syntax-errors against the wrong grammar forever. Detect
            # and self-heal rather than silently reusing a mismatched database.
            db.reload()
            if db.database_dialect != dialect:
                db.drop()
                db = inst.database(self.dsn["database"], database_dialect=dialect)
                db.create().result(120)
        else:
            db.create().result(120)

    def _check_real(self) -> None:
        """Real Spanner: check that the instance and this database exist with its dialect.
        Never creates, recreates or drops either; each failure names what is missing."""
        from google.cloud.spanner_admin_database_v1 import DatabaseDialect

        project, instance, database = self.dsn["project"], self.dsn["instance"], self.dsn["database"]
        postgres = self.dsn["dialect"] == "postgresql"
        setting = "INT_SPANNER_PG_DB" if postgres else "INT_SPANNER_GSQL_DB"
        want = DatabaseDialect.POSTGRESQL if postgres else DatabaseDialect.GOOGLE_STANDARD_SQL
        never = "the framework never creates or deletes instances or databases on real Spanner"
        inst = self._spanner_client().instance(instance)
        if not inst.exists():
            raise RealSpannerError(f"real Spanner: instance {instance!r} does not exist in project "
                                   f"{project!r}; create it or fix INT_SPANNER_INSTANCE ({never})")
        db = inst.database(database)
        if not db.exists():
            raise RealSpannerError(f"real Spanner: database {database!r} does not exist in instance "
                                   f"{instance!r}; create it with the {want.name} dialect or fix "
                                   f"{setting} ({never})")
        db.reload()
        if db.database_dialect != want:
            got = getattr(db.database_dialect, "name", db.database_dialect)
            raise RealSpannerError(f"real Spanner: database {database!r} has the {got} dialect, not "
                                   f"{want.name}; point {setting} at a {want.name} database ({never})")

    def _guard(self, statements: list[str]) -> None:
        # Every statement is checked before any runs, so a refused one leaves nothing half-applied.
        tid = self.dsn.get("tid") or ""
        _require_tid(tid)
        for stmt in statements:
            why = _refusal(stmt, tid)
            if why:
                raise RealSpannerError(f"real Spanner: refused {stmt[:200]!r}: {why}")

    # --- ddl + dml (uniform-ish with _run_postgres/_run_oracle) -----------------
    def run_statements(self, statements: list[str]) -> None:
        # Route DDL (CREATE/DROP/ALTER) through update_ddl and DML (INSERT/UPDATE/DELETE)
        # through a read-write transaction (execute_update). Statements arrive
        # pre-split (dbroutes.split_statements already did that), unlike the live
        # tier's run_sql, which splits internally.
        if self.real:
            self._guard(statements)
        ddl, dml = [], []
        for stmt in statements:
            head = stmt.split(None, 1)[0].upper() if stmt.split() else ""
            (ddl if head in ("CREATE", "DROP", "ALTER") else dml).append(stmt)
        if ddl:
            # ⚠ Serialised across xdist workers. The emulator refuses a
            # schema change while another schema change OR read-write transaction is in
            # flight on the database -- `FAILED_PRECONDITION: Schema change operation
            # rejected because a concurrent schema change operation or read-write
            # transaction is already in progress` -- and under `-n 2` two cases' DDL
            # collide often enough that every parallel run of the writer tier failed
            # exactly one case this way (a different one each time, always green serially).
            # A per-database OS lock around the DDL, and around the seed DML that follows
            # it in the same call, is what the emulator itself lacks. Real Spanner queues
            # schema changes; the lock is then merely unnecessary.
            with self._ddl_lock():
                self._update_ddl(ddl)
                if dml:
                    self._run_dml(dml)
            return
        if dml:
            self._run_dml(dml)

    #: The emulator's refusal. Real Spanner never says this for a queued schema change.
    _CONCURRENT = "concurrent schema change operation or read-write transaction"

    def _update_ddl(self, ddl: list[str], budget_s: float = 120.0) -> None:
        # ⚠ The lock is half of the fix. The emulator ALSO refuses a schema change while a
        # READ-WRITE TRANSACTION is open on the database, and under `-n 2` that transaction
        # is the other worker's writer JVM mid-window -- a process no harness-side lock can
        # serialise. Measured on the first locked `-n 2` run: two cases still failed, both on
        # spanner-postgres, both with the refusal. A writer's transaction is short (one
        # window), so the refusal is retried with a small backoff inside a budget; anything
        # else, or the budget spent, raises as before.
        deadline = time.monotonic() + budget_s
        delay = 0.25
        while True:
            try:
                self._db().update_ddl(ddl).result(120)
                return
            except Exception as e:  # noqa: BLE001 -- narrowed by message below
                if self._CONCURRENT not in str(e) or time.monotonic() >= deadline:
                    raise
                time.sleep(delay)
                delay = min(delay * 2, 4.0)

    def _run_dml(self, dml: list[str]) -> None:
        def _txn(txn):
            for s in dml:
                txn.execute_update(s)

        self._db().run_in_transaction(_txn)

    def _ddl_lock_path(self) -> Path:
        # One lock file per emulator database, beside the harness's other state files.
        # Keyed by EMULATOR as well as database, and stack-scoped like every other state
        # file: two INT_STACK_PREFIX stacks run two emulators with the same instance and
        # database names, and one lock across them would let a retry sleeping under it (up
        # to budget_s, waiting on its own emulator's transaction) stall the other stack's
        # DDL for nothing.
        from inttest.services import state_name  # lazy: services imports docker deps

        key = f"{self.dsn.get('emulator_host', '')}-{self.dsn['instance']}-{self.dsn['database']}"
        name = re.sub(r"[^A-Za-z0-9_.-]", "_", key)
        return paths.state_dir() / state_name(f".int-spanner-ddl-{name}.lock")

    @contextlib.contextmanager
    def _ddl_lock(self):
        # fcntl, like the session lock: released by the kernel if the holder dies, so a
        # SIGKILLed worker cannot strand the others.
        with open(self._ddl_lock_path(), "w") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)

    def query_rows(self, sql: str) -> list[list]:
        """One query's rows, for `dbroutes.query_rows` -- the target tier's read half."""
        _, rows = self._query(sql)
        return [list(row) for row in rows]

    def drop_test_tables(self, prefix: str = "") -> None:
        """See `_drop_test_tables`. Real Spanner: any failure is a RealSpannerError, which fails
        the case (inttest.plugin re-raises it), since objects may be left in a shared database."""
        if not self.real:
            return self._drop_test_tables(prefix)
        try:
            return self._drop_test_tables(prefix)
        except RealSpannerError:
            raise
        except Exception as e:  # noqa: BLE001
            raise RealSpannerError(f"real Spanner teardown of {prefix!r} in {self.dsn['database']} "
                                   f"failed: {e!r}") from e

    def _drop_test_tables(self, prefix: str = "") -> None:
        """Teardown cleanup: drop this test's objects in this admin's database. prefix ""
        drops ALL user objects (serial runs); a non-empty prefix (the per-test ${TID})
        drops only this test's so a concurrent sibling's survive. Filters to the user
        schema ('' for GoogleSQL, 'public' for the PostgreSQL dialect) so
        information_schema/spanner_sys never match.

        Spanner refuses DROP TABLE while a change stream watches the table, a view names
        it, a foreign key references it, an index is on it or a child is interleaved in
        it, so those go first, in that order, then the tables (children first), then
        sequences. A synonym goes with its table (measured on the emulator, both dialects,
        which also has no information_schema.table_synonyms to list). One batch; if it
        fails, one statement at a time, multi-pass until no progress, then a re-list
        names what is left.

        Real Spanner: only this test's own ${TID} is accepted as the prefix; "" and anything
        else are refused before a table is listed."""
        if self.real:
            tid = self.dsn.get("tid") or ""
            if not (_TID.match(prefix or "") and prefix.lower() == tid.lower()):
                raise RealSpannerError(f"real Spanner: refused to drop tables with prefix {prefix!r}: "
                                       f"only this test's ${{TID}} ({tid!r}) may be dropped")
        unlisted: dict = {}
        statements = self._teardown_ddl(prefix, unlisted)
        left = []
        if statements:
            if self.real:
                self._guard(statements)
            if not self._apply_teardown(statements):
                left = self._teardown_ddl(prefix, unlisted)  # what the fallback could not drop
        problems = [f"could not list {kind}: {err!r}" for kind, err in unlisted.items()]
        if left:
            problems.append("still there: " + "; ".join(left))
        if problems:
            msg = f"Spanner teardown of {prefix!r} in {self.dsn['database']}: " + "; ".join(problems)
            if self.real:  # a shared database: leftovers fail the case rather than pass quietly
                raise RealSpannerError(f"real {msg}")
            print(f"[integration] WARNING: {msg}")

    def _apply_teardown(self, statements: list[str]) -> bool:
        """One batch, True if it applied; else one statement at a time, multi-pass."""
        try:
            with self._ddl_lock():  # a sibling's CREATE must not collide
                self._update_ddl(statements, budget_s=30.0)
            return True
        except Exception:  # noqa: BLE001 -- an order this listing missed; one at a time below
            pass
        remaining = statements
        while remaining:
            failed = []
            for stmt in remaining:
                try:
                    with self._ddl_lock():
                        self._update_ddl([stmt], budget_s=30.0)
                except Exception:  # noqa: BLE001 -- dependency ordering; retried below
                    failed.append(stmt)
            if len(failed) == len(remaining):
                break  # no progress -- the caller re-lists and reports what is left
            remaining = failed
        return False

    _USER_SCHEMA = "table_schema IN ('', 'public')"

    def _teardown_ddl(self, prefix: str, unlisted: dict) -> list[str]:
        """The DROP statements for every object `prefix` owns, in an order Spanner accepts. Names
        are quoted, since PostgreSQL folds an unquoted one to lower case. A kind that cannot be
        listed is recorded in `unlisted` and skipped."""
        low = prefix.lower()
        mark = '"' if self.dsn.get("dialect") == "postgresql" else "`"

        def q(name) -> str:
            return f"{mark}{name}{mark}"

        def owned(name) -> bool:
            return bool(name) and bool(_IDENT.match(name)) and name.lower().startswith(low)

        def rows(sql) -> list:
            try:
                return self._query(sql)[1]
            except Exception as e:  # noqa: BLE001 -- reported by drop_test_tables
                unlisted[sql.split(" FROM ", 1)[1].split()[0]] = e
                return []

        _, listed = self._query(f"SELECT table_name FROM information_schema.tables WHERE {self._USER_SCHEMA}")
        views = [r[0] for r in rows(f"SELECT table_name FROM information_schema.views WHERE {self._USER_SCHEMA}")
                 if owned(r[0])]
        tables = [r[0] for r in listed if owned(r[0]) and r[0] not in views]
        parent = {r[0]: r[1] for r in rows("SELECT table_name, parent_table_name FROM information_schema.tables "
                                           f"WHERE {self._USER_SCHEMA}")}

        def depth(t, seen=()) -> int:
            p = parent.get(t)
            return 0 if not p or p in seen else 1 + depth(p, (*seen, t))

        out = [f"DROP CHANGE STREAM {q(r[0])}"
               for r in rows("SELECT change_stream_name FROM information_schema.change_streams") if owned(r[0])]
        out += [f"DROP VIEW {q(v)}" for v in views]
        # A foreign key is named by Spanner (FK_...) unless the DDL named it; it belongs to its table.
        out += [f"ALTER TABLE {q(r[1])} DROP CONSTRAINT {q(r[0])}"
                for r in rows("SELECT constraint_name, table_name FROM information_schema.table_constraints "
                              f"WHERE constraint_type = 'FOREIGN KEY' AND {self._USER_SCHEMA}")
                if owned(r[1]) and _IDENT.match(r[0] or "")]
        drop_index = {"INDEX": "DROP INDEX", "SEARCH": "DROP SEARCH INDEX", "VECTOR": "DROP VECTOR INDEX"}
        for name, table, kind, managed in rows(
                "SELECT index_name, table_name, index_type, spanner_is_managed FROM information_schema.indexes "
                f"WHERE {self._USER_SCHEMA}"):
            # Managed indexes (a foreign key's own) go with their key; the primary key with its table.
            if (str(kind).upper() in drop_index and not (managed is True or str(managed).upper() == "YES")
                    and owned(name) and owned(table)):
                out.append(f"{drop_index[str(kind).upper()]} {q(name)}")
        out += [f"DROP TABLE {q(t)}" for t in sorted(tables, key=depth, reverse=True)]
        sequences = ("SELECT sequence_name FROM information_schema.sequences WHERE sequence_schema = 'public'"
                     if self.dsn.get("dialect") == "postgresql" else
                     "SELECT name FROM information_schema.sequences WHERE schema = ''")
        out += [f"DROP SEQUENCE {q(r[0])}" for r in rows(sequences) if owned(r[0])]
        return out

    def _query(self, sql: str):
        with self._db().snapshot() as snap:
            rs = snap.execute_sql(sql)
            rows = list(rs)
            cols = [f.name for f in rs.fields]
            return cols, _decode_bytes_columns(rs.fields, rows)


def _decode_bytes_columns(fields, rows: list) -> list:
    """Base64-decode BYTES columns, which this client hands back still encoded.

    ⚠ MEASURED, and it read as a WRITER defect for a day. Spanner's wire format carries BYTES
    base64-encoded, and the Python client does NOT decode it: a column holding the nine bytes
    `00 01 02 03 ff fe fd ca 7e` comes back as `b'AAECA//+/cp+'` -- the base64 TEXT, as bytes.
    `_jsonable_rows` then renders those twelve ASCII bytes as hex, so an `assert.target:`
    comparison saw `41414543412f2f2b2f63702b` and reported stored data that differs from what
    was written.

    The database is correct. Reading the same rows over JDBC returns the original nine bytes,
    which is what proved the corruption was on this side. Without this decode the harness cannot
    assert about a binary column on Spanner at all -- it compares its own encoding artefact.

    Only fields the result set TYPES as BYTES are decoded; a STRING column that happens to look
    like base64 is left alone.
    """
    from google.cloud.spanner_v1 import TypeCode  # lazy, as everywhere else in this module

    indexes = [i for i, f in enumerate(fields) if f.type_.code == TypeCode.BYTES]
    if not indexes:
        return rows
    out = []
    for row in rows:
        cells = list(row)
        for i in indexes:
            if cells[i] is not None:
                cells[i] = base64.b64decode(cells[i], validate=True)
        out.append(cells)
    return out


def ensure(admin: SpannerAdmin, attempts: int = 20, delay: float = 1.5) -> None:
    """Retry `admin.ensure()` until it succeeds or `attempts` is exhausted.

    `services/spanner/compose.yaml` declares NO healthcheck, so `docker compose up
    -d --wait` returns as soon as the container is *running*, not once the emulator's
    gRPC endpoint is actually accepting connections -- this retry IS the readiness
    gate for the whole service, not a defensive nicety.
    """
    if admin.real:
        admin.ensure()  # a check of something that exists or not: retrying only delays the answer
        return
    last = None
    for _ in range(attempts):
        try:
            admin.ensure()
            return
        except Exception as e:  # noqa: BLE001 -- re-raised after retries
            last = e
            time.sleep(delay)
    raise last


def admins_for(tokens) -> list:
    """One `SpannerAdmin` per dialect, both backed by the single emulator instance,
    built from a `provides:`-token dict (`inttest.tokens.build_tokens`'s output, or the
    `spanner_config` fixture's). Ensures the instance + both databases exist.

    `dbroutes.connection_params("spanner-google"/"spanner-postgres", tokens)` already
    returns exactly `SpannerAdmin`'s dsn shape, so building both admins through it (a
    simplification over the live tier, which assembles the dsn dicts by hand from
    `resolved.base`) means the routes and the admins can't drift apart.
    """
    from inttest.dbroutes import connection_params

    google = SpannerAdmin(connection_params("spanner-google", tokens))
    pg = SpannerAdmin(connection_params("spanner-postgres", tokens))
    if google.real:
        _require_tid(google.dsn.get("tid", ""))
    ensure(google)  # creates the instance + the GoogleSQL database
    ensure(pg)  # instance already exists; creates the PostgreSQL database
    return [(google, "spanner-google"), (pg, "spanner-postgres")]
