"""Unit tests for inttest.dbroutes -- route parsing, connection-param derivation,
and the render+split pipeline (docs/INTEGRATION-TESTS.md).

FAST: no Docker, no DB, no network. `psycopg2`/`oracledb` are only imported lazily
inside the runner functions this suite never calls (those live paths are covered by
the opt-in tests/test_services_live.py instead).
"""
from __future__ import annotations

import pytest

from inttest.dbroutes import (
    _NON_SQL_DRIVERS,
    _PARAM_SPECS,
    _QUERIERS,
    _RUNNERS,
    _ROUTE_DIRECTIONS,
    RouteError,
    assert_target,
    assert_target_rows,
    connection_params,
    parse_route,
    query_rows,
    read_cells,
    run_sql_text,
    setup_bigtable,
    teardown_bigtable,
    split_statements,
)
from inttest.tokens import SubstitutionError, render


# ============================================================================
# parse_route
# ============================================================================


@pytest.mark.parametrize(
    "route,expected",
    [
        ("postgres-source", ("postgres", "source")),
        ("postgres-target", ("postgres", "target")),
        ("oracle-source", ("oracle", "source")),
        ("oracle-target", ("oracle", "target")),
        ("spanner-google", ("spanner", "google")),
        ("spanner-postgres", ("spanner", "postgres")),
        ("sqlserver-source", ("sqlserver", "source")),
        ("sqlserver-target", ("sqlserver", "target")),
        ("vertica-source", ("vertica", "source")),
        ("vertica-target", ("vertica", "target")),
    ],
)
def test_parse_route_valid(route, expected):
    assert parse_route(route) == expected


@pytest.mark.parametrize(
    "route",
    [
        "postgres",             # no direction suffix
        "postgres-",            # empty direction
        "-source",              # empty service
        "postgres-foo",         # not source/target
        "postgres_source",      # underscore, not hyphen
        "POSTGRES-SOURCE",      # uppercase direction not accepted
        "",
        "postgres-source-extra",
        "spanner-source",       # spanner's directions are google/postgres, not source/target
        "spanner-target",
        "spanner-foo",
        # Proves the direction vocabulary is genuinely PER-SERVICE, not a single
        # widened global alternation (source|target|google|postgres would wrongly
        # accept both of these):
        "postgres-google",
        "oracle-postgres",
    ],
)
def test_parse_route_invalid_shape_raises(route):
    with pytest.raises(RouteError, match="invalid route"):
        parse_route(route)


def test_parse_route_non_string_raises():
    with pytest.raises(RouteError, match="must be a string"):
        parse_route(123)


# ============================================================================
# connection_params
# ============================================================================

_TOKENS = {
    "POSTGRES_HOST": "localhost",
    "POSTGRES_PORT": "15432",
    "POSTGRES_DB": "intdb",
    "POSTGRES_SOURCE_USER": "qasource",
    "POSTGRES_SOURCE_PASSWORD": "srcpw",
    "POSTGRES_SOURCE_SCHEMA": "qasource",
    "POSTGRES_TARGET_USER": "qatarget",
    "POSTGRES_TARGET_PASSWORD": "tgtpw",
    "POSTGRES_TARGET_SCHEMA": "qatarget",
    "ORACLE_HOST": "localhost",
    "ORACLE_PORT": "11521",
    "ORACLE_SERVICE": "FREEPDB1",
    "ORACLE_SOURCE_USER": "qasource",
    "ORACLE_SOURCE_PASSWORD": "orasrcpw",
    "ORACLE_SOURCE_SCHEMA": "QASOURCE",
    "ORACLE_TARGET_USER": "qatarget",
    "ORACLE_TARGET_PASSWORD": "oratgtpw",
    "ORACLE_TARGET_SCHEMA": "QATARGET",
    "SPANNER_PROJECT": "test-project",
    "SPANNER_INSTANCE": "test-inst",
    "SPANNER_HOST": "localhost",
    "SPANNER_GRPC_PORT": "19010",
    "SPANNER_GSQL_DB": "gsql",
    "SPANNER_PG_DB": "pgdb",
    "SQLSERVER_HOST": "localhost",
    "SQLSERVER_PORT": "11433",
    "SQLSERVER_DB": "intdb",
    "SQLSERVER_SOURCE_USER": "qasource",
    "SQLSERVER_SOURCE_PASSWORD": "mssrcpw",
    "SQLSERVER_SOURCE_SCHEMA": "qasource",
    "SQLSERVER_TARGET_USER": "qatarget",
    "SQLSERVER_TARGET_PASSWORD": "mstgtpw",
    "SQLSERVER_TARGET_SCHEMA": "qatarget",
}


def test_connection_params_postgres_source_pulls_source_tokens():
    params = connection_params("postgres-source", _TOKENS)
    assert params == {
        "driver": "postgres",
        "host": "localhost",
        "port": 15432,
        "dbname": "intdb",
        "user": "qasource",
        "password": "srcpw",
        "schema": "qasource",
    }


def test_connection_params_postgres_target_pulls_target_tokens():
    params = connection_params("postgres-target", _TOKENS)
    assert params["user"] == "qatarget"
    assert params["password"] == "tgtpw"
    assert params["schema"] == "qatarget"
    # host/port/dbname are shared (not per-direction)
    assert params["host"] == "localhost"
    assert params["port"] == 15432
    assert params["dbname"] == "intdb"


def test_connection_params_sqlserver_target_pulls_target_tokens():
    params = connection_params("sqlserver-target", _TOKENS)
    assert params == {
        "driver": "sqlserver",
        "host": "localhost",
        "port": 11433,
        "dbname": "intdb",
        "user": "qatarget",
        "password": "mstgtpw",
        "schema": "qatarget",
    }


def test_connection_params_oracle_source_pulls_source_tokens():
    params = connection_params("oracle-source", _TOKENS)
    assert params == {
        "driver": "oracle",
        "host": "localhost",
        "port": 11521,
        "service": "FREEPDB1",
        "user": "qasource",
        "password": "orasrcpw",
        "schema": "QASOURCE",
    }


def test_connection_params_oracle_target_pulls_target_tokens():
    params = connection_params("oracle-target", _TOKENS)
    assert params["user"] == "qatarget"
    assert params["password"] == "oratgtpw"
    assert params["schema"] == "QATARGET"
    assert params["service"] == "FREEPDB1"


def test_connection_params_port_is_int():
    params = connection_params("postgres-source", _TOKENS)
    assert isinstance(params["port"], int)


def test_connection_params_unmapped_service_raises():
    # kafka: a service with no JDBC route. (mysql was the example until T2b-17 mapped it.)
    with pytest.raises(RouteError, match="no connection-param mapping"):
        connection_params("kafka-source", _TOKENS)


def test_connection_params_mysql_target():
    # T2b-17 (§178): PyMySQL, the route's `schema` IS the database it connects to.
    tokens = dict(_TOKENS, MYSQL_HOST="localhost", MYSQL_PORT="13306", MYSQL_DB="intdb",
                  MYSQL_TARGET_USER="qatarget", MYSQL_TARGET_PASSWORD="striim",
                  MYSQL_TARGET_SCHEMA="qatarget")
    params = connection_params("mysql-target", tokens)
    assert params == {
        "driver": "mysql", "host": "localhost", "port": 13306, "dbname": "intdb",
        "user": "qatarget", "password": "striim", "schema": "qatarget",
    }


def test_connection_params_vertica_target():
    tokens = dict(_TOKENS, VERTICA_HOST="localhost", VERTICA_PORT="15433", VERTICA_DB="intdb",
                  VERTICA_TARGET_USER="qatarget", VERTICA_TARGET_PASSWORD="striim",
                  VERTICA_TARGET_SCHEMA="qatarget")
    params = connection_params("vertica-target", tokens)
    assert params == {
        "driver": "vertica", "host": "localhost", "port": 15433, "dbname": "intdb",
        "user": "qatarget", "password": "striim", "schema": "qatarget",
    }


def test_connection_params_spanner_google():
    params = connection_params("spanner-google", _TOKENS)
    assert params == {
        "driver": "spanner",
        "project": "test-project",
        "instance": "test-inst",
        "host": "localhost",
        "port": 19010,
        "database": "gsql",
        "dialect": "google_standard_sql",
        "emulator_host": "localhost:19010",
    }


def test_connection_params_spanner_postgres():
    params = connection_params("spanner-postgres", _TOKENS)
    assert params == {
        "driver": "spanner",
        "project": "test-project",
        "instance": "test-inst",
        "host": "localhost",
        "port": 19010,
        "database": "pgdb",
        "dialect": "postgresql",
        "emulator_host": "localhost:19010",
    }


def test_connection_params_spanner_port_is_int():
    assert isinstance(connection_params("spanner-google", _TOKENS)["port"], int)


def test_connection_params_spanner_missing_token_raises_and_names_it():
    tokens = dict(_TOKENS)
    del tokens["SPANNER_GSQL_DB"]
    with pytest.raises(RouteError, match="SPANNER_GSQL_DB"):
        connection_params("spanner-google", tokens)


# ============================================================================
# Internal consistency: _ROUTE_DIRECTIONS / _PARAM_SPECS / _RUNNERS must stay
# in sync with each other, or parse_route/connection_params silently drift.
# ============================================================================


def test_spanner_route_directions_match_param_spec_directions():
    assert set(_ROUTE_DIRECTIONS["spanner"]) == set(_PARAM_SPECS["spanner"]["directions"])


def test_every_param_spec_driver_has_a_runner():
    # `bigtable` has a driver but NO SQL surface (its rows are read whole by the admin, not
    # queried -- see dbroutes._NON_SQL_DRIVERS), so it is the one driver exempted from this
    # invariant; every *other* driver must have both a runner and a querier.
    drivers = {spec["driver"] for spec in _PARAM_SPECS.values()}
    sql = drivers - _NON_SQL_DRIVERS
    assert sql <= set(_RUNNERS)
    assert sql <= set(_QUERIERS)
    # the exempted engine must REALLY have no SQL surface, or the exemption would mask a gap:
    # nothing non-SQL may appear in the runner/querier tables.
    assert _NON_SQL_DRIVERS.isdisjoint(_RUNNERS)
    assert _NON_SQL_DRIVERS.isdisjoint(_QUERIERS)
    # and the driver that carries the exemption actually exists as a param spec.
    assert _NON_SQL_DRIVERS <= drivers


def test_connection_params_missing_token_raises_and_names_it():
    tokens = dict(_TOKENS)
    del tokens["POSTGRES_SOURCE_PASSWORD"]
    with pytest.raises(RouteError, match="POSTGRES_SOURCE_PASSWORD"):
        connection_params("postgres-source", tokens)


def test_connection_params_bad_route_propagates_route_error():
    with pytest.raises(RouteError, match="invalid route"):
        connection_params("not-a-route-shape", _TOKENS)


# ============================================================================
# split_statements (pure -- the render+split pipeline minus the actual connect())
# ============================================================================


def test_split_statements_simple():
    sql = "CREATE TABLE t (a int);\nINSERT INTO t VALUES (1);"
    assert split_statements(sql) == ["CREATE TABLE t (a int)", "INSERT INTO t VALUES (1)"]


def test_split_statements_ignores_semicolon_in_string_literal():
    sql = "INSERT INTO t (s) VALUES ('a;b');"
    assert split_statements(sql) == ["INSERT INTO t (s) VALUES ('a;b')"]


def test_split_statements_handles_escaped_quote_in_literal():
    sql = "INSERT INTO t (s) VALUES ('it''s; here');"
    assert split_statements(sql) == ["INSERT INTO t (s) VALUES ('it''s; here')"]


def test_split_statements_strips_line_comments():
    sql = "-- a leading comment\nCREATE TABLE t (a int); -- trailing comment\n"
    assert split_statements(sql) == ["CREATE TABLE t (a int)"]


def test_split_statements_strips_block_comments():
    sql = "/* block\ncomment */ CREATE TABLE t (a int);"
    assert split_statements(sql) == ["CREATE TABLE t (a int)"]


def test_split_statements_drops_blank_and_comment_only_chunks():
    sql = ";;  ;\n-- just a comment\n;"
    assert split_statements(sql) == []


def test_split_statements_no_trailing_semicolon_still_captured():
    sql = "CREATE TABLE t (a int)"
    assert split_statements(sql) == ["CREATE TABLE t (a int)"]


def test_split_statements_empty_string():
    assert split_statements("") == []


# ============================================================================
# render() + split_statements() together -- the substitute-then-split logic
# run_sql_text uses, exercised without connecting to anything.
# ============================================================================


def test_render_then_split_substitutes_tokens_before_splitting():
    sql = "CREATE TABLE ${TID}throwaway (id INT);\nINSERT INTO ${TID}throwaway VALUES (${VAL});"
    tokens = {"TID": "t1_", "VAL": "42"}
    rendered = render(sql, tokens)
    statements = split_statements(rendered)
    assert statements == [
        "CREATE TABLE t1_throwaway (id INT)",
        "INSERT INTO t1_throwaway VALUES (42)",
    ]


def test_render_missing_token_raises_before_split():
    sql = "CREATE TABLE ${TID}throwaway (id INT);"
    with pytest.raises(SubstitutionError, match="TID"):
        render(sql, {})


def test_split_statements_delimiter_directive_keeps_a_routine_body_whole():
    sql = (
        "CREATE TABLE t (a int);\n"
        "DELIMITER $$\n"
        "CREATE FUNCTION f() RETURNS INT DETERMINISTIC\n"
        "BEGIN\n  DECLARE x INT;\n  SET x = 1;\n  RETURN x;\nEND$$\n"
        "DELIMITER ;\n"
        "INSERT INTO t VALUES (1);\n"
    )
    assert split_statements(sql) == [
        "CREATE TABLE t (a int)",
        "CREATE FUNCTION f() RETURNS INT DETERMINISTIC\nBEGIN\n  DECLARE x INT;\n  SET x = 1;\n  RETURN x;\nEND",
        "INSERT INTO t VALUES (1)",
    ]


def test_split_statements_delimiter_word_inside_a_statement_is_not_a_directive():
    sql = "INSERT INTO t (note) VALUES ('DELIMITER $$');\nSELECT 1;"
    assert split_statements(sql) == ["INSERT INTO t (note) VALUES ('DELIMITER $$')", "SELECT 1"]


# ============================================================================
# bigtable routes: connection_params, read_cells, assert_target, setup/teardown
#
# These build a BigtableAdmin through `inttest.bigtableadmin.BigtableAdmin`, which the tests below
# replace with a recording fake returning canned `read_all` rows, so nothing connects. The admin's
# own behaviour is covered by scripts/live/tests/test_bigtableadmin*.py.
# ============================================================================

# Two rows, newest cell per (family, qualifier), values as hex.
_EMP = [
    {"row": "01", "cells": {"emp:name": "6a6b6c", "emp:age": "1f"}},
    {"row": "02", "cells": {"emp:name": "6b6b"}},
]

# An emulator `bigtable-target` route: no BIGTABLE_CREDENTIALS.
_EMULATOR_TOKENS = {
    "BIGTABLE_PROJECT": "test-project",
    "BIGTABLE_INSTANCE": "test-instance",
    "BIGTABLE_HOST": "localhost",
    "BIGTABLE_GRPC_PORT": "19470",
}


class _FakeBigtableAdmin:
    """A `BigtableAdmin` stand-in with canned reads; every action is a recording no-op."""
    _TABLES: dict = {}

    def __init__(self, params, client=None, data_client=None):
        self.params = params
        self.created = []
        self.written = []
        self.dropped = []

    def create_table(self, table, families, gc_rules=None):
        self.created.append((table, families, gc_rules))

    def write_cells(self, table, rows):
        self.written.append((table, list(rows)))

    def read_all(self, table, cells_per_column_limit=1):
        return list(self._TABLES.get(table, []))

    def drop_table(self, table):
        self.dropped.append(table)


def _with_fake(tables, monkeypatch):
    """Point the routes at a fresh `_FakeBigtableAdmin`; return a dict holding the last admin built."""
    _FakeBigtableAdmin._TABLES = dict(tables)
    last = {}

    def factory(params, client=None, data_client=None):
        inst = _FakeBigtableAdmin(params, client=client, data_client=data_client)
        last["admin"] = inst
        return inst

    monkeypatch.setattr("inttest.bigtableadmin.BigtableAdmin", factory, raising=False)
    return last


def test_connection_params_bigtable_emulator_dsn():
    # No credentials: no TID, and the admin dials host:port, as the Spanner emulator routes do.
    params = connection_params("bigtable-target", _EMULATOR_TOKENS)
    assert params["driver"] == "bigtable"
    assert params["emulator_host"] == "localhost:19470"
    assert params["port"] == 19470          # coerced to int
    assert "credentials" not in params and "tid" not in params


def test_read_cells_shape(monkeypatch):
    last = _with_fake({"emp": _EMP}, monkeypatch)
    got = read_cells("bigtable-target", "emp", _EMULATOR_TOKENS)
    assert got == {"table": "emp", "rows": _EMP}
    assert last["admin"].params["emulator_host"] == "localhost:19470"


def test_assert_target_passes_when_the_table_matches(monkeypatch):
    _with_fake({"emp": _EMP}, monkeypatch)
    assert_target("bigtable-target", "emp", _EMP, _EMULATOR_TOKENS)      # no raise


def test_assert_target_raises_when_a_row_is_short(monkeypatch):
    _with_fake({"emp": [_EMP[0]]}, monkeypatch)
    with pytest.raises(AssertionError, match="mismatch"):
        assert_target("bigtable-target", "emp", _EMP, _EMULATOR_TOKENS)


def test_assert_target_raises_when_a_cell_value_differs(monkeypatch):
    got = [dict(_EMP[0], cells=dict(_EMP[0]["cells"], **{"emp:age": "20"})), _EMP[1]]
    _with_fake({"emp": got}, monkeypatch)
    with pytest.raises(AssertionError, match="mismatch"):
        assert_target("bigtable-target", "emp", _EMP, _EMULATOR_TOKENS)


def test_assert_target_rows_passes_when_the_table_matches(monkeypatch):
    _with_fake({"emp": _EMP}, monkeypatch)
    assert_target_rows("bigtable-target", "emp", _EMP, _EMULATOR_TOKENS)


def test_setup_bigtable_creates_and_seeds(monkeypatch):
    last = _with_fake({}, monkeypatch)
    seed = [{"row": "01", "cells": {"emp:name": b"abc"}}]
    setup_bigtable("emp", {"emp": {}}, seed=seed, route="bigtable-target", tokens=_EMULATOR_TOKENS)
    a = last["admin"]
    assert [c[0] for c in a.created] == ["emp"]
    assert a.written == [("emp", seed)]


def test_teardown_bigtable_drops_at_the_exact_name(monkeypatch):
    last = _with_fake({}, monkeypatch)
    teardown_bigtable("emp", route="bigtable-target", tokens=_EMULATOR_TOKENS)
    assert last["admin"].dropped == ["emp"]


def test_a_credentials_route_is_refused_before_a_client_is_built():
    # The real admin: it refuses in its constructor, so nothing is dialled.
    tokens = {**_EMULATOR_TOKENS, "BIGTABLE_CREDENTIALS": "/keys/sa.json", "TID": "t1_"}
    with pytest.raises(ValueError, match="emulator-only"):
        setup_bigtable("t1_rows", {"cf": None}, tokens=tokens)


@pytest.mark.parametrize("call", [lambda: query_rows("bigtable-target", "SELECT 1", _EMULATOR_TOKENS),
                                  lambda: run_sql_text("bigtable-target", "SELECT 1", _EMULATOR_TOKENS)],
                         ids=["query_rows", "run_sql_text"])
def test_a_bigtable_route_has_no_sql_surface(call):
    with pytest.raises(RouteError, match="no SQL .* support for driver 'bigtable'"):
        call()
