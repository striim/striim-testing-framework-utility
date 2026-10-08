"""DDL/seed execution against a `<svc>-source`/`<svc>-target` ROUTE (docs/INTEGRATION-TESTS.md).

`test.yaml`'s `ddl:`/`seed:` entries name a `db:` route (e.g. `postgres-source`,
`oracle-target`) alongside a token-substituted `.sql` file. This module:

  - `parse_route`      -- splits a route string into (service, direction).
  - `connection_params` -- derives host/port/db/user/password/schema for a route from
    the `tokens` dict `inttest.tokens.build_tokens` assembles (the resolved
    `provides:` tokens for that test's `requires:` services).
  - `run_sql_file` / `run_sql_text` -- render a `.sql` file/string against `tokens`
    (via `inttest.tokens.render`), split it into individual statements, connect with
    the right driver, execute, and commit.

Token names are read from the ACTUAL `provides:` maps in `services/postgres/service.yaml`
and `services/oracle/service.yaml` -- not invented:

  postgres-source -> POSTGRES_HOST, POSTGRES_PORT, POSTGRES_DB,
                      POSTGRES_SOURCE_USER, POSTGRES_SOURCE_PASSWORD, POSTGRES_SOURCE_SCHEMA
  postgres-target -> POSTGRES_HOST, POSTGRES_PORT, POSTGRES_DB,
                      POSTGRES_TARGET_USER, POSTGRES_TARGET_PASSWORD, POSTGRES_TARGET_SCHEMA
  oracle-source   -> ORACLE_HOST, ORACLE_PORT, ORACLE_SERVICE,
                      ORACLE_SOURCE_USER, ORACLE_SOURCE_PASSWORD, ORACLE_SOURCE_SCHEMA
  oracle-target   -> ORACLE_HOST, ORACLE_PORT, ORACLE_SERVICE,
                      ORACLE_TARGET_USER, ORACLE_TARGET_PASSWORD, ORACLE_TARGET_SCHEMA
  spanner-google  -> SPANNER_HOST, SPANNER_GRPC_PORT, SPANNER_PROJECT, SPANNER_INSTANCE,
                      SPANNER_GSQL_DB (dialect: google_standard_sql)
  spanner-postgres -> SPANNER_HOST, SPANNER_GRPC_PORT, SPANNER_PROJECT, SPANNER_INSTANCE,
                      SPANNER_PG_DB (dialect: postgresql)
  sqlserver-target -> SQLSERVER_HOST, SQLSERVER_PORT, SQLSERVER_DB,
                      SQLSERVER_TARGET_USER, SQLSERVER_TARGET_PASSWORD, SQLSERVER_TARGET_SCHEMA
  vertica-target  -> VERTICA_HOST, VERTICA_PORT, VERTICA_DB,
                      VERTICA_TARGET_USER, VERTICA_TARGET_PASSWORD, VERTICA_TARGET_SCHEMA

Spanner's "direction" is a DIALECT, not a source/target pair -- one emulator
instance, two fixed-dialect databases -- so its routes are `spanner-google`/
`spanner-postgres` rather than `spanner-source`/`spanner-target`; see
`_ROUTE_DIRECTIONS` below.

Pure-Python where possible: statement-splitting (`split_statements`) is a plain
string function with no imports, so it -- and the render+split pipeline
(`run_sql_text` minus the actual `connect()` call) -- is unit-testable with no DB.
`psycopg2`/`oracledb`/`pymssql` are imported lazily, inside the functions that actually open a
connection, so importing this module never requires either driver (or a reachable
database) to be installed/running.
"""

from __future__ import annotations

import re
import warnings
from pathlib import Path
from typing import Mapping

from inttest.tokens import render

_ROUTE_RE = re.compile(r"^(?P<service>[A-Za-z0-9]+)-(?P<direction>[A-Za-z0-9]+)$")

# Direction vocabulary per service. Most services are source/target; Spanner's
# "direction" is a DIALECT instead (one emulator, two fixed-dialect databases), so
# spanner-google/spanner-postgres are its only valid routes -- and postgres-google
# / spanner-source are correctly rejected, which a widened source|target|google|
# postgres alternation would not do. Kept in sync with _PARAM_SPECS's own
# "directions" keys by a unit test (test_dbroutes.py).
_DEFAULT_DIRECTIONS = frozenset({"source", "target"})
_ROUTE_DIRECTIONS: dict[str, frozenset] = {"spanner": frozenset({"google", "postgres"})}

_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_$#]*$")


class RouteError(Exception):
    """Raised for an unparsable route, an unmapped service, or a token missing from
    the `tokens` dict that `connection_params` needed to resolve one."""


# ============================================================================
# 1. Route parsing
# ============================================================================


def parse_route(route: str) -> tuple[str, str]:
    """Split `"<svc>-source"`/`"<svc>-target"` into `(service, direction)`.

    Raises `RouteError` with a clear message on anything else -- no bare-route
    default here (unlike scripts/live, which defaults a bare route to
    `postgres-source`): SPEC §3's `db:` key already defaults to `postgres-source` at
    the `test.yaml`-parsing layer, so by the time a route reaches this module it is
    always the fully-qualified `<svc>-<dir>` form.
    """
    if not isinstance(route, str):
        raise RouteError(f"route must be a string, got {type(route).__name__}: {route!r}")
    m = _ROUTE_RE.match(route)
    if not m:
        raise RouteError(
            f"invalid route {route!r}: expected '<service>-source' or '<service>-target' "
            f"(e.g. 'postgres-source', 'oracle-target')"
        )
    service, direction = m.group("service"), m.group("direction")
    allowed = _ROUTE_DIRECTIONS.get(service, _DEFAULT_DIRECTIONS)
    if direction not in allowed:
        raise RouteError(
            f"invalid route {route!r}: service {service!r} accepts direction(s) "
            f"{'/'.join(sorted(allowed))} (e.g. {service}-{sorted(allowed)[0]})"
        )
    return service, direction


# ============================================================================
# 2. Connection parameter derivation
# ============================================================================

# Per-service token mapping, read verbatim off services/<name>/service.yaml's
# `provides:` block (see module docstring). `{DIR}` is replaced with SOURCE/TARGET.
_PARAM_SPECS: dict[str, dict] = {
    "postgres": {
        "driver": "postgres",
        "common": {"host": "POSTGRES_HOST", "port": "POSTGRES_PORT", "dbname": "POSTGRES_DB"},
        "per_direction": {
            "user": "POSTGRES_{DIR}_USER",
            "password": "POSTGRES_{DIR}_PASSWORD",
            "schema": "POSTGRES_{DIR}_SCHEMA",
        },
    },
    "oracle": {
        "driver": "oracle",
        "common": {"host": "ORACLE_HOST", "port": "ORACLE_PORT", "service": "ORACLE_SERVICE"},
        "per_direction": {
            "user": "ORACLE_{DIR}_USER",
            "password": "ORACLE_{DIR}_PASSWORD",
            "schema": "ORACLE_{DIR}_SCHEMA",
        },
    },
    "sqlserver": {
        "driver": "sqlserver",
        "common": {"host": "SQLSERVER_HOST", "port": "SQLSERVER_PORT",
                   "dbname": "SQLSERVER_DB"},
        "per_direction": {
            "user": "SQLSERVER_{DIR}_USER",
            "password": "SQLSERVER_{DIR}_PASSWORD",
            "schema": "SQLSERVER_{DIR}_SCHEMA",
        },
    },
    "teradata": {
        # ⚠ In Teradata a USER IS A DATABASE, so "schema" and "user" are the same name and
        # there is no separate dbname to select -- the URL's DATABASE= merely sets the default.
        # Fixed qasource/qatarget, ${TID}-prefixed tables, teardown by each case's own DROP.
        "driver": "teradata",
        "common": {"host": "TERADATA_HOST", "port": "TERADATA_PORT"},
        "per_direction": {
            "user": "TERADATA_{DIR}_USER",
            "password": "TERADATA_{DIR}_PASSWORD",
            "schema": "TERADATA_{DIR}_SCHEMA",
        },
    },
    "mysql": {
        # T2b-17 (§62/§178). PyMySQL; the same fixed qasource/qatarget schemas as SQL Server,
        # ${TID}-prefixed tables, and -- like SQL Server -- teardown by each case's own
        # `DROP TABLE IF EXISTS` rather than a per-test drop.
        "driver": "mysql",
        "common": {"host": "MYSQL_HOST", "port": "MYSQL_PORT", "dbname": "MYSQL_DB"},
        "per_direction": {
            "user": "MYSQL_{DIR}_USER",
            "password": "MYSQL_{DIR}_PASSWORD",
            "schema": "MYSQL_{DIR}_SCHEMA",
        },
    },
    "vertica": {
        # vertica-python; fixed qasource/qatarget schemas in one database (intdb), as for
        # postgres, ${TID}-prefixed tables, and -- like SQL Server and MySQL -- teardown by each
        # case's own `DROP TABLE IF EXISTS` rather than a per-test drop.
        "driver": "vertica",
        "common": {"host": "VERTICA_HOST", "port": "VERTICA_PORT", "dbname": "VERTICA_DB"},
        "per_direction": {
            "user": "VERTICA_{DIR}_USER",
            "password": "VERTICA_{DIR}_PASSWORD",
            "schema": "VERTICA_{DIR}_SCHEMA",
        },
    },
    "spanner": {
        "driver": "spanner",
        # One emulator instance, two fixed-dialect databases. There is no user/password
        # (SPANNER_EMULATOR_HOST skips auth) and no schema; the route's "direction"
        # picks the database + its dialect instead. Hence `directions` rather than the
        # postgres/oracle `per_direction` {DIR}-template shape -- there is no
        # SPANNER_GOOGLE_DB-style per-direction token to template toward.
        "common": {
            "project": "SPANNER_PROJECT",
            "instance": "SPANNER_INSTANCE",
            "host": "SPANNER_HOST",
            "port": "SPANNER_GRPC_PORT",
        },
        "directions": {
            "google":   {"tokens": {"database": "SPANNER_GSQL_DB"},
                         "const":  {"dialect": "google_standard_sql"}},
            "postgres": {"tokens": {"database": "SPANNER_PG_DB"},
                         "const":  {"dialect": "postgresql"}},
        },
    },
}


def connection_params(route: str, tokens: Mapping[str, str]) -> dict:
    """Derive `{driver, host, port, ..., user, password, schema}` for `route` from
    `tokens` (a `provides:`-token dict, e.g. from `inttest.tokens.build_tokens`).

    `port` is coerced to `int`. Raises `RouteError` if the route doesn't parse, the
    service has no known mapping (e.g. mysql), or any token the mapping needs is
    absent from `tokens`.
    """
    service, direction = parse_route(route)
    spec = _PARAM_SPECS.get(service)
    if spec is None:
        raise RouteError(
            f"no connection-param mapping for service {service!r} (route {route!r}); "
            f"supported services: {', '.join(sorted(_PARAM_SPECS))}"
        )

    dir_upper = direction.upper()
    params: dict = {"driver": spec["driver"]}
    missing: list[str] = []

    for key, token_name in spec["common"].items():
        if token_name not in tokens:
            missing.append(token_name)
            continue
        params[key] = tokens[token_name]

    if "directions" in spec:
        dspec = spec["directions"].get(direction)
        if dspec is None:  # unreachable while parse_route validates against _ROUTE_DIRECTIONS
            raise RouteError(f"route {route!r}: service {service!r} has no direction {direction!r}")
        for key, token_name in dspec["tokens"].items():
            if token_name not in tokens:
                missing.append(token_name)
                continue
            params[key] = tokens[token_name]
        if not missing:
            params.update(dspec["const"])
    else:
        for key, template in spec["per_direction"].items():
            token_name = template.format(DIR=dir_upper)
            if token_name not in tokens:
                missing.append(token_name)
                continue
            params[key] = tokens[token_name]

    if missing:
        raise RouteError(
            f"route {route!r}: missing token(s) in the provided tokens dict: "
            f"{', '.join(sorted(missing))}"
        )

    if "port" in params:
        params["port"] = int(params["port"])
    if params["driver"] == "spanner":
        # SpannerAdmin's dsn shape: {project, instance, database, dialect, emulator_host}.
        params["emulator_host"] = f'{params["host"]}:{params["port"]}'
    return params


# ============================================================================
# 3. Statement splitting (pure, no I/O -- unit-testable without a DB)
# ============================================================================


_DELIMITER_RE = re.compile(r"[ \t\r\n]*DELIMITER[ \t]+(\S+)[ \t]*(?:\r?\n|$)", re.IGNORECASE)


def split_statements(sql: str) -> list[str]:
    """Split a plain-SQL script into individual statements on `;`, honoring
    single-quoted string literals (`''` escape) and stripping `--` line comments /
    `/* ... */` block comments outside of a literal. Blank/comment-only chunks are
    dropped. Adapted from the same string-aware-split idea as
    `scripts/live/livetest/sqlutil.split_sql_statements` (kept independent here
    rather than cross-importing scripts/live into this package).

    Both `psycopg2` (via a single `cursor.execute` per call, though it does accept
    multi-statement strings) and `oracledb` (one statement per `execute()`, no
    trailing `;`) need this: driving both through the same split keeps `run_sql_text`
    driver-agnostic.

    A `DELIMITER <token>` line at the start of a statement changes the terminator, as the
    mysql client does, so a stored-routine body can carry `;` inside it; the directive line
    itself is not a statement. `DELIMITER ;` restores the default.
    """
    out: list[str] = []
    buf: list[str] = []
    in_str = False
    delim = ";"
    i, n = 0, len(sql)
    while i < n:
        if not in_str and not "".join(buf).strip():
            m = _DELIMITER_RE.match(sql, i)
            if m:
                delim = m.group(1)
                buf = []
                i = m.end()
                continue
        ch = sql[i]
        if in_str:
            if ch == "'":
                if i + 1 < n and sql[i + 1] == "'":  # '' escape inside a string
                    buf.append("''")
                    i += 2
                    continue
                in_str = False
            buf.append(ch)
            i += 1
            continue
        # outside a string literal:
        if ch == "'":
            in_str = True
            buf.append(ch)
        elif ch == "-" and i + 1 < n and sql[i + 1] == "-":  # -- line comment: skip to EOL
            nl = sql.find("\n", i)
            i = n if nl == -1 else nl
            continue
        elif ch == "/" and i + 1 < n and sql[i + 1] == "*":  # /* ... */ block comment
            end = sql.find("*/", i + 2)
            i = n if end == -1 else end + 2
            continue
        elif sql.startswith(delim, i):
            s = "".join(buf).strip()
            if s:
                out.append(s)
            buf = []
            i += len(delim)
            continue
        else:
            buf.append(ch)
        i += 1
    s = "".join(buf).strip()
    if s:
        out.append(s)
    return out


def _check_schema_ident(name: str) -> str:
    if not _IDENT_RE.match(name):
        raise RouteError(f"unsafe schema identifier: {name!r}")
    return name


# ============================================================================
# 4. Execution -- lazy driver imports so `import inttest.dbroutes` never requires
#    psycopg2/oracledb (or a reachable DB) to be present.
# ============================================================================


def _run_postgres(params: dict, statements: list[str]) -> None:
    import psycopg2

    conn = psycopg2.connect(
        host=params["host"], port=int(params["port"]), dbname=params["dbname"],
        user=params["user"], password=params["password"],
    )
    try:
        with conn.cursor() as cur:
            schema = params.get("schema")
            if schema:
                cur.execute(f'SET search_path TO "{_check_schema_ident(schema)}"')
            for stmt in statements:
                cur.execute(stmt)
        conn.commit()
    finally:
        conn.close()


def _run_oracle(params: dict, statements: list[str]) -> None:
    import oracledb

    dsn = f'{params["host"]}:{int(params["port"])}/{params["service"]}'
    conn = oracledb.connect(user=params["user"], password=params["password"], dsn=dsn)
    try:
        cur = conn.cursor()
        try:
            for stmt in statements:
                stmt = stmt.rstrip("/").strip()  # oracledb rejects a trailing '/'
                if stmt:
                    cur.execute(stmt)
        finally:
            cur.close()
        conn.commit()
    finally:
        conn.close()


def _run_spanner(params: dict, statements: list[str]) -> None:
    from inttest.spanneradmin import SpannerAdmin  # lazy, same as psycopg2/oracledb above

    SpannerAdmin(params).run_statements(statements)


def _query_postgres(params: dict, sql: str) -> list[list]:
    import psycopg2

    conn = psycopg2.connect(
        host=params["host"], port=int(params["port"]), dbname=params["dbname"],
        user=params["user"], password=params["password"],
    )
    try:
        with conn.cursor() as cur:
            schema = params.get("schema")
            if schema:
                cur.execute(f'SET search_path TO "{_check_schema_ident(schema)}"')
            cur.execute(sql)
            return [list(row) for row in cur.fetchall()]
    finally:
        conn.close()


def _lobs_as_values(cursor, metadata):
    """Fetch CLOB/BLOB columns as str/bytes instead of lazy locators.

    ⚠ A LOB comes back as a LOCATOR that is only readable while its connection is open, and this
    module converts rows to comparable values AFTER closing. So any assertion selecting a CLOB or
    BLOB failed with `DPY-1001: not connected to database` -- naming the connection, not the
    column, and pointing nowhere near the cause.

    It went unnoticed because the only engine whose checkpoint `ddl` column is a LOB is Oracle, and
    every case selecting that column ran on PostgreSQL alone, where it is plain `text`. The §84
    fan-out is what surfaced it (§94.4).
    """
    import oracledb

    if metadata.type_code in (oracledb.DB_TYPE_CLOB, oracledb.DB_TYPE_NCLOB):
        return cursor.var(oracledb.DB_TYPE_LONG, arraysize=cursor.arraysize)
    if metadata.type_code is oracledb.DB_TYPE_BLOB:
        return cursor.var(oracledb.DB_TYPE_LONG_RAW, arraysize=cursor.arraysize)
    return None


def _query_oracle(params: dict, sql: str) -> list[list]:
    import oracledb

    dsn = f'{params["host"]}:{int(params["port"])}/{params["service"]}'
    conn = oracledb.connect(user=params["user"], password=params["password"], dsn=dsn)
    try:
        conn.outputtypehandler = _lobs_as_values
        cur = conn.cursor()
        try:
            cur.execute(sql.rstrip("/").strip())
            return [list(row) for row in cur.fetchall()]
        finally:
            cur.close()
    finally:
        conn.close()


def _query_spanner(params: dict, sql: str) -> list[list]:
    from inttest.spanneradmin import SpannerAdmin  # lazy, same as psycopg2/oracledb above

    return SpannerAdmin(params).query_rows(sql)


def _run_teradata(params: dict, statements: list[str]) -> None:
    import teradatasql

    # ⚠ tmode="ANSI" is correctness, not tuning: TERA mode compares 'ABC' = 'abc', so a DDL or
    # assertion run in it could match a row the writer never wrote. The JDBC URL the writer uses
    # carries TMODE=ANSI for the same reason.
    #
    # ⚠ NO charset= HERE, deliberately. The JDBC URL carries CHARSET=UTF8 because a JDBC session
    # defaults to ASCII and silently replaces what it cannot represent; this Python driver REFUSES
    # the parameter -- "[Error 169] Unable to parse JSON connection parameters" -- because it is
    # UTF-8 natively and has nothing to switch. Measured on int-teradata: tmode alone connects,
    # charset= fails outright, and a non-ASCII round trip through this client is intact.
    conn = teradatasql.connect(
        host=params["host"], dbs_port=str(params["port"]),
        user=params["user"], password=params["password"],
        tmode="ANSI",
    )
    try:
        cur = conn.cursor()
        # No `SET search_path` equivalent: a user IS its database, so an unqualified name
        # already resolves to this role's own. params["schema"] is therefore not applied,
        # exactly as _run_sqlserver ignores it -- and for the same reason, a route whose
        # schema token disagreed with the user would be silently ignored. Every case names
        # its tables fully qualified.
        for stmt in statements:
            try:
                cur.execute(stmt)
            except Exception as e:
                # ⚠ Teradata has NO `DROP TABLE IF EXISTS`. Every other engine's DDL in this tier
                # opens with one, because the tier drops nothing itself -- a case cleans up by
                # re-running its own DDL. Without this, the second run of any Teradata case dies
                # on "Error 3803, Table already exists" from the CREATE that follows a DROP the
                # engine refused with "Error 3807, does not exist".
                #
                # Narrow on purpose: only 3807, and only for a DROP. A 3807 from anything else is
                # a real missing object and must still fail, or a case could silently assert
                # against a table that was never created.
                # ⚠ "[Error 3807]" not "3807": the driver formats an error as
                # "[Session %v] [Teradata Database] [Error %v] [SQLState %v] %v", so the
                # SESSION number is in the message -- a DROP failing with any other error
                # in session 3807 would be silently skipped. The trailing text names the
                # object too, and ${TID} is hex, where 3807 is a legal substring.
                if "[Error 3807]" in str(e) and stmt.lstrip().upper().startswith("DROP"):
                    continue
                raise
    finally:
        conn.close()


def _run_sqlserver(params: dict, statements: list[str]) -> None:
    import pymssql

    conn = pymssql.connect(
        server=params["host"], port=str(params["port"]), database=params["dbname"],
        user=params["user"], password=params["password"], autocommit=True,
    )
    try:
        with conn.cursor() as cur:
            # T-SQL has no `SET search_path`, so unlike _run_postgres this deliberately
            # ignores params["schema"]: the login's DEFAULT_SCHEMA (set when init.sql
            # created the user) already resolves an unqualified name to qasource/qatarget.
            # ⚠ The consequence is that a route whose *_SCHEMA token disagreed with the
            # login's default would be silently ignored rather than honoured. Harmless
            # today because every case names its tables fully qualified, and worth knowing
            # before anyone relies on the token to redirect a write.
            #
            # autocommit, where the postgres/oracle runners commit explicitly: some T-SQL
            # statements refuse to run inside an explicit transaction, and DDL is what this
            # runner exists for.
            for stmt in statements:
                cur.execute(stmt)
    finally:
        conn.close()


def _query_sqlserver(params: dict, sql: str) -> list[list]:
    import pymssql

    conn = pymssql.connect(
        server=params["host"], port=str(params["port"]), database=params["dbname"],
        user=params["user"], password=params["password"],
    )
    try:
        with conn.cursor() as cur:
            # No schema statement here either, for the reason _run_sqlserver gives.
            cur.execute(sql)
            return [list(row) for row in cur.fetchall()]
    finally:
        conn.close()


def _mysql_connect(params: dict, autocommit: bool):
    import pymysql

    # `database` is the SCHEMA the route addresses (qasource/qatarget), not the compose-level
    # `dbname` (intdb): on MySQL a schema IS a database, and the data accounts are granted on
    # their own schema only. An unqualified name in a case's SQL then resolves there, the
    # way SQL Server's DEFAULT_SCHEMA does it.
    return pymysql.connect(
        host=params["host"], port=int(params["port"]), database=params["schema"],
        user=params["user"], password=params["password"], autocommit=autocommit,
    )


def _run_mysql(params: dict, statements: list[str]) -> None:
    conn = _mysql_connect(params, autocommit=True)
    try:
        with conn.cursor() as cur:
            for stmt in statements:
                cur.execute(stmt)
    finally:
        conn.close()


def _query_mysql(params: dict, sql: str) -> list[list]:
    conn = _mysql_connect(params, autocommit=False)
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
            return [list(row) for row in cur.fetchall()]
    finally:
        conn.close()


def _query_teradata(params: dict, sql: str) -> list[list]:
    import teradatasql

    # Same session settings as _run_teradata, and the same reason charset is absent.
    conn = teradatasql.connect(
        host=params["host"], dbs_port=str(params["port"]),
        user=params["user"], password=params["password"],
        tmode="ANSI",
    )
    try:
        cur = conn.cursor()
        cur.execute(sql)
        return [list(row) for row in cur.fetchall()]
    finally:
        conn.close()


def _vertica_connect(params: dict):
    import vertica_python

    # autocommit, as for SQL Server and MySQL: each DDL/seed statement stands on its own. The
    # stock container offers no TLS, and vertica-python warns on every connection then; that
    # warning is expected here.
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", message="TLS is not configured on the server")
        return vertica_python.connect(
            host=params["host"], port=int(params["port"]), database=params["dbname"],
            user=params["user"], password=params["password"], autocommit=True,
            connection_timeout=30,
        )


def _vertica_search_path(cur, params: dict) -> None:
    # As _run_postgres does: an unqualified name in a case's SQL resolves in the route's schema.
    schema = params.get("schema")
    if schema:
        cur.execute(f"SET SEARCH_PATH TO {_check_schema_ident(schema)}, public")


def _run_vertica(params: dict, statements: list[str]) -> None:
    conn = _vertica_connect(params)
    try:
        cur = conn.cursor()
        _vertica_search_path(cur, params)
        for stmt in statements:
            cur.execute(stmt)
    finally:
        conn.close()


def _query_vertica(params: dict, sql: str) -> list[list]:
    conn = _vertica_connect(params)
    try:
        cur = conn.cursor()
        _vertica_search_path(cur, params)
        cur.execute(sql)
        return [list(row) for row in cur.fetchall()]
    finally:
        conn.close()


_RUNNERS = {"postgres": _run_postgres, "oracle": _run_oracle, "spanner": _run_spanner,
            "sqlserver": _run_sqlserver, "mysql": _run_mysql,
            "teradata": _run_teradata, "vertica": _run_vertica}

_QUERIERS = {"postgres": _query_postgres, "oracle": _query_oracle,
             "spanner": _query_spanner, "sqlserver": _query_sqlserver, "mysql": _query_mysql,
             "teradata": _query_teradata, "vertica": _query_vertica}


def query_rows(route: str, sql: str, tokens: Mapping[str, str]) -> list[list]:
    """Token-substitute `sql`, run it as ONE query against `route`, and return its rows.

    The read half of this module, added for the target tier: a Target emits no events, so
    what a case asserts is the state of the database it wrote to -- which means the harness
    has to be able to read it back, not only to seed it.

    Deliberately ONE statement, not `split_statements`. An assertion is a question, and a
    semicolon-separated script would silently assert against whichever half happened to run
    last. A `;` here is rejected rather than split.
    """
    rendered = render(sql, tokens).strip().rstrip(";").strip()
    if not rendered:
        raise RouteError("assert.target query rendered to nothing")
    if ";" in rendered:
        raise RouteError(
            f"assert.target query must be a SINGLE statement, but contains ';': {rendered!r}. "
            f"An assertion reads one result set; a script would assert against only part of "
            f"itself.")
    params = connection_params(route, tokens)
    querier = _QUERIERS.get(params["driver"])
    if querier is None:  # pragma: no cover - unreachable while _PARAM_SPECS <-> _QUERIERS stay in sync
        raise RouteError(f"no SQL query support for driver {params['driver']!r}")
    return querier(params, rendered)


def run_sql_text(route: str, sql: str, tokens: Mapping[str, str]) -> None:
    """Token-substitute `sql` via `tokens`, split it into statements, connect to
    `route`'s database with the right driver, execute every statement, and commit.

    A no-op (no connection opened) if `sql` renders to no statements at all (e.g. an
    empty file, or one that's all comments).
    """
    rendered = render(sql, tokens)
    statements = split_statements(rendered)
    if not statements:
        return
    params = connection_params(route, tokens)
    runner = _RUNNERS.get(params["driver"])
    if runner is None:  # pragma: no cover - unreachable while _PARAM_SPECS <-> _RUNNERS stay in sync
        raise RouteError(f"no SQL execution support for driver {params['driver']!r}")
    runner(params, statements)


def run_sql_file(route: str, sql_path, tokens: Mapping[str, str]) -> None:
    """`run_sql_text` reading its SQL from `sql_path` (str or Path)."""
    text = Path(sql_path).read_text()
    run_sql_text(route, text, tokens)
