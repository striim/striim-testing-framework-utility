"""Real Cloud Spanner mode of the integration tier's Spanner service (INT_SPANNER_CREDENTIALS).

Hermetic: every Google object is a fake, and the key file is an empty temp file the code only
checks exists. Nothing here connects anywhere or reads a credential.
"""
from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from inttest import dbroutes, plugin, spanneradmin
from inttest.spanneradmin import RealSpannerError, SpannerAdmin
from inttest.tokens import ServiceConfigError, build_tokens

_SERVICES = Path(__file__).resolve().parents[1] / "services"
TID = "t0123abcde_"


@pytest.fixture
def key(tmp_path):
    path = tmp_path / "sa-key-placeholder.json"
    path.write_text("")          # only its existence is checked
    return str(path)


@pytest.fixture
def real_env(key):
    return {"INT_SPANNER_CREDENTIALS": key, "INT_SPANNER_PROJECT": "proj",
            "INT_SPANNER_INSTANCE": "inst", "INT_SPANNER_GSQL_DB": "gdb", "INT_SPANNER_PG_DB": "pdb"}


def _tokens(tmp_path, env):
    return build_tokens(tmp_path, ["spanner"], env=env, services_dir=_SERVICES)


# --- settings -----------------------------------------------------------------------------------

def test_emulator_mode_is_the_default_and_unchanged(tmp_path):
    tok = _tokens(tmp_path, {})
    assert "SPANNER_CREDENTIALS" not in tok
    assert tok["SPANNER_GSQL_URL"].endswith("/databases/gsql?autoConfigEmulator=true")
    assert dbroutes.connection_params("spanner-google", tok)["emulator_host"] == "localhost:19010"


def test_real_mode_tokens_name_the_real_databases_and_key_path(tmp_path, real_env, key):
    tok = _tokens(tmp_path, real_env)
    assert tok["SPANNER_GSQL_URL"] == f"jdbc:cloudspanner:/projects/proj/instances/inst/databases/gdb?credentials={key}"
    assert tok["SPANNER_PG_URL"] == f"jdbc:cloudspanner:/projects/proj/instances/inst/databases/pdb?credentials={key}"
    assert (tok["SPANNER_PROJECT"], tok["SPANNER_INSTANCE"]) == ("proj", "inst")
    assert (tok["SPANNER_GSQL_DB"], tok["SPANNER_PG_DB"]) == ("gdb", "pdb")
    assert tok["SPANNER_CREDENTIALS"] == key
    assert not any("autoConfigEmulator" in v or "localhost" in v for v in tok.values())


@pytest.mark.parametrize("setting", ["INT_SPANNER_HOST", "INT_SPANNER_PORT", "INT_SPANNER_ADMIN_PORT",
                                     "SPANNER_EMULATOR_HOST"])
def test_real_mode_refuses_an_emulator_setting(tmp_path, real_env, setting):
    with pytest.raises(ServiceConfigError, match=setting):
        _tokens(tmp_path, {**real_env, setting: "localhost:19010"})


def test_real_mode_names_every_missing_setting(tmp_path, key):
    with pytest.raises(ServiceConfigError) as e:
        _tokens(tmp_path, {"INT_SPANNER_CREDENTIALS": key, "INT_SPANNER_PROJECT": "proj"})
    for name in ("INT_SPANNER_INSTANCE", "INT_SPANNER_GSQL_DB", "INT_SPANNER_PG_DB"):
        assert name in str(e.value)


def test_real_mode_refuses_a_key_path_that_is_not_a_file(tmp_path, real_env):
    missing = str(tmp_path / "nowhere.json")
    with pytest.raises(ServiceConfigError, match="nowhere.json"):
        _tokens(tmp_path, {**real_env, "INT_SPANNER_CREDENTIALS": missing})


def test_real_mode_refuses_one_database_for_both_dialects(tmp_path, real_env):
    with pytest.raises(ServiceConfigError, match="INT_SPANNER_PG_DB"):
        _tokens(tmp_path, {**real_env, "INT_SPANNER_PG_DB": "gdb"})


def test_real_mode_connection_params_carry_key_and_tid_and_no_emulator(tmp_path, real_env, key):
    tok = {**_tokens(tmp_path, real_env), "TID": TID}
    p = dbroutes.connection_params("spanner-postgres", tok)
    assert p["credentials"] == key and p["tid"] == TID
    assert (p["project"], p["instance"], p["database"], p["dialect"]) == ("proj", "inst", "pdb", "postgresql")
    assert "emulator_host" not in p


# --- the client ---------------------------------------------------------------------------------

def _real_dsn(key, **kw):
    return {"project": "proj", "instance": "inst", "database": "gdb", "dialect": "google_standard_sql",
            "credentials": key, "tid": TID, **kw}


def test_real_client_refuses_spanner_emulator_host(monkeypatch, key):
    monkeypatch.setenv("SPANNER_EMULATOR_HOST", "localhost:19010")
    with pytest.raises(RealSpannerError, match="SPANNER_EMULATOR_HOST"):
        SpannerAdmin(_real_dsn(key))._spanner_client()


def test_real_client_uses_the_key_path_and_never_sets_emulator_host(monkeypatch, key):
    monkeypatch.delenv("SPANNER_EMULATOR_HOST", raising=False)
    from google.cloud import spanner
    from google.oauth2 import service_account
    seen = {}
    monkeypatch.setattr(service_account.Credentials, "from_service_account_file",
                        lambda path: seen.setdefault("path", path) and "CREDS")
    monkeypatch.setattr(spanner, "Client", lambda **kw: seen.setdefault("client", kw))
    SpannerAdmin(_real_dsn(key))._spanner_client()
    assert seen["path"] == key
    assert seen["client"] == {"project": "proj", "credentials": "CREDS"}
    assert "SPANNER_EMULATOR_HOST" not in os.environ


# --- ensure: check only -------------------------------------------------------------------------

def _fake_client(*, instance=True, database=True, dialect=None):
    from google.cloud.spanner_admin_database_v1 import DatabaseDialect
    db = MagicMock()
    db.exists.return_value = database
    db.database_dialect = dialect or DatabaseDialect.GOOGLE_STANDARD_SQL
    inst = MagicMock()
    inst.exists.return_value = instance
    inst.database.return_value = db
    client = MagicMock()
    client.instance.return_value = inst
    return client, inst, db


def _never_created(inst, db):
    inst.create.assert_not_called()
    db.create.assert_not_called()
    db.drop.assert_not_called()


def test_real_ensure_passes_when_both_exist_and_creates_nothing(key):
    client, inst, db = _fake_client()
    SpannerAdmin(_real_dsn(key), client=client).ensure()
    _never_created(inst, db)


def test_real_ensure_names_a_missing_instance(key):
    client, inst, db = _fake_client(instance=False)
    with pytest.raises(RealSpannerError, match="instance 'inst'.*project 'proj'"):
        SpannerAdmin(_real_dsn(key), client=client).ensure()
    _never_created(inst, db)


def test_real_ensure_names_a_missing_database(key):
    client, inst, db = _fake_client(database=False)
    with pytest.raises(RealSpannerError, match="database 'gdb'"):
        SpannerAdmin(_real_dsn(key), client=client).ensure()
    _never_created(inst, db)


def test_real_ensure_refuses_a_wrong_dialect_instead_of_recreating(key):
    from google.cloud.spanner_admin_database_v1 import DatabaseDialect
    client, inst, db = _fake_client(dialect=DatabaseDialect.POSTGRESQL)
    with pytest.raises(RealSpannerError, match="database 'gdb'.*dialect"):
        SpannerAdmin(_real_dsn(key), client=client).ensure()
    _never_created(inst, db)


def test_real_ensure_is_not_retried(monkeypatch, key):
    calls = []
    admin = SpannerAdmin(_real_dsn(key), client=object())

    def missing():
        calls.append(1)
        raise RealSpannerError("no")
    monkeypatch.setattr(admin, "ensure", missing)
    monkeypatch.setattr(spanneradmin.time, "sleep", lambda s: None)
    with pytest.raises(RealSpannerError):
        spanneradmin.ensure(admin)
    assert calls == [1]


def test_admins_for_refuses_real_mode_without_a_tid(tmp_path, real_env, monkeypatch):
    monkeypatch.setattr(spanneradmin, "ensure", lambda admin: pytest.fail("ensure must not run"))
    tok = {**_tokens(tmp_path, real_env), "TID": ""}
    with pytest.raises(RealSpannerError, match="SLT_PARALLEL=1"):
        spanneradmin.admins_for(tok)


# --- the TID guard ------------------------------------------------------------------------------

def _guarded(key, dialect="google_standard_sql"):
    db = MagicMock()
    admin = SpannerAdmin(_real_dsn(key, dialect=dialect), database=db, client=object())
    return admin, db


@pytest.mark.parametrize("stmt", [
    "CREATE TABLE t0123abcde_emp (id INT64) PRIMARY KEY (id)",
    "CREATE TABLE IF NOT EXISTS public.t0123abcde_emp (id bigint primary key)",
    'CREATE TABLE "t0123abcde_Emp" (id bigint primary key)',
    "create table T0123ABCDE_EMP (id INT64) PRIMARY KEY (id)",
    "CREATE TABLE t0123abcde_kid (id INT64, k INT64) PRIMARY KEY (id, k), INTERLEAVE IN PARENT t0123abcde_emp",
    "CREATE UNIQUE NULL_FILTERED INDEX t0123abcde_ix ON t0123abcde_emp (id)",
    "CREATE INDEX IF NOT EXISTS t0123abcde_ix ON public.t0123abcde_emp (id)",
    'DROP TABLE "public"."t0123abcde_emp"',
    "DROP TABLE t0123abcde_emp;",
    "CREATE CHANGE STREAM t0123abcde_cs FOR t0123abcde_emp",
    "ALTER TABLE t0123abcde_emp ADD COLUMN c STRING(10)",
    "ALTER TABLE t0123abcde_kid ADD FOREIGN KEY (k) REFERENCES t0123abcde_emp (id)",
    "DROP TABLE IF EXISTS t0123abcde_emp",
    "DROP INDEX t0123abcde_ix",
    "INSERT INTO t0123abcde_emp (id) VALUES (1)",
    "INSERT OR UPDATE INTO t0123abcde_emp (id) VALUES (1)",
    "UPDATE t0123abcde_emp SET c = 'x' WHERE id = 1",
    "DELETE FROM t0123abcde_emp WHERE true",
])
def test_guard_allows_this_tests_objects(key, stmt):
    admin, db = _guarded(key)
    admin.run_statements([stmt])
    assert db.method_calls, "an allowed statement must reach the database"


@pytest.mark.parametrize("stmt", [
    "DROP TABLE customers",
    "DROP TABLE IF EXISTS t9999999999_emp",
    "DROP TABLE t0123abcde",                      # the prefix needs its trailing _
    "CREATE TABLE emp (id INT64) PRIMARY KEY (id)",
    "ALTER TABLE customers ADD COLUMN c STRING(10)",
    "CREATE INDEX t0123abcde_ix ON customers (id)",
    "CREATE TABLE t0123abcde_kid (id INT64) PRIMARY KEY (id), INTERLEAVE IN PARENT customers",
    "ALTER TABLE t0123abcde_emp ADD FOREIGN KEY (k) REFERENCES customers (id)",
    "CREATE CHANGE STREAM everything FOR ALL",
    "DROP CHANGE STREAM everything",
    "DELETE FROM customers WHERE true",
    "UPDATE customers SET c = 'x' WHERE true",
    "INSERT INTO customers (id) VALUES (1)",
    "DROP SCHEMA sales",
    "ALTER DATABASE gdb SET OPTIONS (version_retention_period = '1d')",
    "GRANT SELECT ON TABLE t0123abcde_emp TO ROLE r",
    "TRUNCATE TABLE t0123abcde_emp",
    "DROP TABLE t0123abcde_emp; DROP TABLE customers",   # a second statement in one entry
    "DROP TABLE t0123abcde_emp, customers",
    "ALTER TABLE t0123abcde_emp ADD SYNONYM customers",
    'DROP TABLE "t0123abcde_emp"."customers"',
    "CREATE TABLE sales.t0123abcde_emp (id INT64) PRIMARY KEY (id)",  # teardown lists '' and public only
])
def test_guard_refuses_anything_else_before_any_statement_runs(key, stmt):
    admin, db = _guarded(key)
    with pytest.raises(RealSpannerError, match="refus"):
        admin.run_statements(["CREATE TABLE t0123abcde_ok (id INT64) PRIMARY KEY (id)", stmt])
    assert not db.method_calls, "nothing may run when any statement is refused"


@pytest.mark.parametrize("sql", [
    # split_statements honours only single quotes, so GoogleSQL's double-quoted literal hides the ;
    "INSERT INTO t0123abcde_emp (s) VALUES (\"O'Brien\");\nDROP TABLE customers;\nSELECT 'x';",
    # and a DELIMITER line makes the rest one entry
    "DELIMITER //\nDROP TABLE t0123abcde_emp; DROP TABLE customers //\n",
])
def test_guard_refuses_statements_split_merged_into_one_entry(key, sql):
    admin, db = _guarded(key)
    statements = dbroutes.split_statements(sql)
    assert len(statements) == 1
    with pytest.raises(RealSpannerError, match="refus"):
        admin.run_statements(statements)
    assert not db.method_calls


def test_guard_refuses_everything_without_a_tid(key):
    db = MagicMock()
    admin = SpannerAdmin(_real_dsn(key, tid=""), database=db, client=object())
    with pytest.raises(RealSpannerError, match="SLT_PARALLEL=1"):
        admin.run_statements(["CREATE TABLE emp (id INT64) PRIMARY KEY (id)"])
    assert not db.method_calls


def test_emulator_mode_has_no_guard():
    db = MagicMock()
    admin = SpannerAdmin({"project": "p", "instance": "i", "database": "d", "dialect": "google_standard_sql",
                          "emulator_host": "localhost:19010"}, database=db, client=object())
    admin._ddl_lock = MagicMock()
    admin.run_statements(["DROP TABLE customers"])
    db.update_ddl.assert_called_once_with(["DROP TABLE customers"])


@pytest.mark.parametrize("prefix", ["", "t", "t9999999999_", "customers"])
def test_real_drop_test_tables_refuses_any_prefix_but_this_tests_tid(key, prefix):
    admin, db = _guarded(key)
    with pytest.raises(RealSpannerError, match="refus"):
        admin.drop_test_tables(prefix)
    assert not db.method_calls


# Teardown's INFORMATION_SCHEMA, as the fake `_query` hands it back: this test's objects of every
# kind that blocks DROP TABLE on Spanner, beside another owner's of each kind.
_SCHEMA = {
    "change_streams": [["t0123abcde_cs"], ["all_cs"]],
    "views": [["t0123abcde_v"], ["cust_v"]],
    "table_constraints": [["FK_t0123abcde_grand_t0123abcde_parent_1", "t0123abcde_grand"],
                          ["FK_customers_orders_1", "customers"]],
    "indexes": [["PRIMARY_KEY", "t0123abcde_parent", "PRIMARY_KEY", False],
                ["t0123abcde_ix", "t0123abcde_parent", "INDEX", False],
                ["IDX_t0123abcde_grand_managed", "t0123abcde_grand", "INDEX", True],  # an FK's own
                ["t0123abcde_six", "t0123abcde_parent", "SEARCH", "NO"],
                ["cust_ix", "customers", "INDEX", False]],
    "sequences": [["t0123abcde_seq"], ["cust_seq"]],
    "parent_table_name": [["t0123abcde_parent", None], ["t0123abcde_child", "t0123abcde_parent"],
                          ["t0123abcde_grand", "t0123abcde_child"], ["customers", None]],
    "tables": [["t0123abcde_parent"], ["t0123abcde_child"], ["t0123abcde_grand"], ["customers"],
               ["t0123abcde_v"], ["cust_v"]],
}

def _teardown(q="`"):
    """The batch teardown should send, with names quoted `q` (backticks GoogleSQL, " PostgreSQL)."""
    return [
        f"DROP CHANGE STREAM {q}t0123abcde_cs{q}",
        f"DROP VIEW {q}t0123abcde_v{q}",
        f"ALTER TABLE {q}t0123abcde_grand{q} DROP CONSTRAINT {q}FK_t0123abcde_grand_t0123abcde_parent_1{q}",
        f"DROP INDEX {q}t0123abcde_ix{q}",
        f"DROP SEARCH INDEX {q}t0123abcde_six{q}",
        f"DROP TABLE {q}t0123abcde_grand{q}",      # interleaved children before their parents
        f"DROP TABLE {q}t0123abcde_child{q}",
        f"DROP TABLE {q}t0123abcde_parent{q}",
        f"DROP SEQUENCE {q}t0123abcde_seq{q}",     # after the tables whose defaults may use it
    ]


def _fake_schema(admin, monkeypatch, failing=(), schema=None):
    """A fake INFORMATION_SCHEMA that loses an object when a DROP of it succeeds."""
    schema = {k: [list(r) for r in v] for k, v in (schema or _SCHEMA).items()}

    def query(sql):
        low = sql.lower()
        kind = next(k for k in schema if k in low)
        if kind in failing:
            raise RuntimeError(f"no {kind} here")
        return [], [list(r) for r in schema[kind]]

    def update_ddl(statements):
        for stmt in statements:
            gone = stmt.rsplit(" ", 1)[-1].strip('"`')
            for rows in schema.values():
                rows[:] = [r for r in rows if r[0] != gone]
        return MagicMock()

    monkeypatch.setattr(admin, "_query", query)
    admin._ddl_lock = MagicMock()
    admin._database.update_ddl.side_effect = update_ddl
    return schema


def _ddl_calls(db):
    return [c.args[0] for c in db.update_ddl.call_args_list]


def _emulator_admin(dialect="postgresql"):
    db = MagicMock()
    return SpannerAdmin({"project": "p", "instance": "i", "database": "d", "dialect": dialect,
                         "emulator_host": "localhost:19010"}, database=db, client=object()), db


def test_real_teardown_drops_every_blocking_kind_in_order_in_one_batch(key, monkeypatch):
    admin, db = _guarded(key)
    _fake_schema(admin, monkeypatch)
    admin.drop_test_tables(TID)
    assert _ddl_calls(db) == [_teardown("`")]


def test_real_teardown_never_names_another_owners_object(key, monkeypatch):
    admin, db = _guarded(key)
    _fake_schema(admin, monkeypatch)
    admin.drop_test_tables(TID)
    named = " ".join(s for call in _ddl_calls(db) for s in call)
    for other in ("all_cs", "cust_v", "customers", "cust_ix", "cust_seq", "PRIMARY_KEY",
                  "IDX_t0123abcde_grand_managed"):
        assert other not in named


def test_emulator_teardown_uses_the_same_order_quoted_for_postgresql(monkeypatch):
    admin, db = _emulator_admin("postgresql")
    _fake_schema(admin, monkeypatch)
    admin.drop_test_tables(TID)
    assert _ddl_calls(db) == [_teardown('"')]


def test_postgresql_teardown_keeps_the_case_of_a_stored_name(key, monkeypatch):
    # PostgreSQL folds an unquoted name to lower case, so T0123ABCDE_Parent must be quoted to be found.
    admin, db = _guarded(key, dialect="postgresql")
    _fake_schema(admin, monkeypatch, schema={**{k: [] for k in _SCHEMA}, "tables": [["T0123ABCDE_Parent"]]})
    admin.drop_test_tables(TID)
    assert _ddl_calls(db) == [['DROP TABLE "T0123ABCDE_Parent"']]


def test_real_teardown_drops_the_rest_then_refuses_to_pass_over_a_kind_it_cannot_list(key, monkeypatch):
    admin, db = _guarded(key)
    _fake_schema(admin, monkeypatch, failing=("sequences",))
    with pytest.raises(RealSpannerError, match="sequences.*no sequences here"):
        admin.drop_test_tables(TID)
    assert _ddl_calls(db) == [[s for s in _teardown() if "SEQUENCE" not in s]]


def test_teardown_lists_no_synonyms(key, monkeypatch):
    # The emulator has no information_schema.table_synonyms, and a synonym goes with its table
    # (measured on the emulator, both dialects), so teardown does not ask for it.
    admin, db = _guarded(key)
    _fake_schema(admin, monkeypatch, failing=("table_synonyms",))
    admin.drop_test_tables(TID)
    assert _ddl_calls(db) == [_teardown()]


def test_emulator_teardown_warns_about_a_kind_it_cannot_list(monkeypatch, capsys):
    admin, db = _emulator_admin()
    _fake_schema(admin, monkeypatch, failing=("sequences",))
    admin.drop_test_tables(TID)
    assert _ddl_calls(db) == [[s for s in _teardown('"') if "SEQUENCE" not in s]]
    assert "WARNING" in capsys.readouterr().out


def test_teardown_falls_back_to_one_statement_at_a_time(key, monkeypatch):
    admin, db = _guarded(key)
    _fake_schema(admin, monkeypatch)
    fake = db.update_ddl.side_effect
    calls = []

    def batch_refused(statements):
        calls.append(statements)
        if len(calls) == 1:
            raise RuntimeError("batch refused")
        return fake(statements)
    db.update_ddl.side_effect = batch_refused
    admin.drop_test_tables(TID)               # nothing left, so nothing to report
    assert calls == [_teardown()] + [[s] for s in _teardown()]


def test_real_teardown_names_what_it_could_not_drop(key, monkeypatch):
    admin, db = _guarded(key)
    _fake_schema(admin, monkeypatch)
    fake = db.update_ddl.side_effect

    def stuck(statements):
        if len(statements) > 1 or "t0123abcde_parent" in statements[0].split()[-1]:
            raise RuntimeError("still referenced")
        return fake(statements)
    db.update_ddl.side_effect = stuck
    with pytest.raises(RealSpannerError, match="DROP TABLE `t0123abcde_parent`") as e:
        admin.drop_test_tables(TID)
    assert "DROP TABLE `t0123abcde_child`" not in str(e.value)


def test_emulator_teardown_warns_naming_what_it_could_not_drop(monkeypatch, capsys):
    admin, db = _emulator_admin()
    _fake_schema(admin, monkeypatch)
    db.update_ddl.side_effect = RuntimeError("nothing drops")
    admin.drop_test_tables(TID)
    out = capsys.readouterr().out
    assert "WARNING" in out and 'DROP TABLE "t0123abcde_parent"' in out


# --- the plugin ---------------------------------------------------------------------------------

def test_provision_requires_starts_no_emulator_in_real_mode(monkeypatch, real_env):
    for k, v in real_env.items():
        monkeypatch.setenv(k, v)
    for k in ("INT_SPANNER_HOST", "INT_SPANNER_PORT", "INT_SPANNER_ADMIN_PORT", "SPANNER_EMULATOR_HOST"):
        monkeypatch.delenv(k, raising=False)
    docker = MagicMock()
    docker.SUPPORTED_SERVICES = {"spanner"}
    monkeypatch.setattr(plugin, "_docker_mod", docker)
    monkeypatch.setattr(plugin, "_require_service_opt_in", lambda svc: None)
    plugin._provision_requires("case-x", ["spanner"], lock=MagicMock())
    docker.ensure_up.assert_not_called()
    docker.run_pre_up.assert_not_called()


def test_provision_requires_fails_a_mixed_real_and_emulator_config(monkeypatch, real_env):
    for k, v in real_env.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("INT_SPANNER_HOST", "localhost")
    docker = MagicMock()
    docker.SUPPORTED_SERVICES = {"spanner"}
    monkeypatch.setattr(plugin, "_docker_mod", docker)
    monkeypatch.setattr(plugin, "_require_service_opt_in", lambda svc: None)
    with pytest.raises(pytest.fail.Exception, match="INT_SPANNER_HOST"):
        plugin._provision_requires("case-x", ["spanner"], lock=MagicMock())
    docker.ensure_up.assert_not_called()


def _teardown_item(**kw):
    from types import SimpleNamespace
    config = SimpleNamespace(getoption=lambda *a, **k: False)
    return SimpleNamespace(config=config, _pg_admin=None, _oracle_prefix=None, _spanner_prefix=TID,
                           tokens={"TID": TID, "TID_ORACLE": ""},
                           manifest=SimpleNamespace(name="case-x", requires=["spanner"]), **kw)


@pytest.mark.parametrize("item_class", ["IntYamlItem", "PerfYamlItem"])
def test_real_teardown_leftovers_fail_the_case(monkeypatch, item_class):
    monkeypatch.delenv("INT_KEEP_RESOURCES", raising=False)
    admin = MagicMock()
    admin.drop_test_tables.side_effect = RealSpannerError("real Spanner teardown: still there: DROP TABLE x")
    monkeypatch.setattr(spanneradmin, "admins_for", lambda tokens: [(admin, "spanner-google")])
    with pytest.raises(RealSpannerError, match="still there"):
        getattr(plugin, item_class)._teardown_db_isolation(_teardown_item(), False)


@pytest.mark.parametrize("item_class", ["IntYamlItem", "PerfYamlItem"])
def test_emulator_teardown_errors_stay_warnings(monkeypatch, capsys, item_class):
    monkeypatch.delenv("INT_KEEP_RESOURCES", raising=False)
    admin = MagicMock()
    admin.drop_test_tables.side_effect = RuntimeError("emulator went away")
    monkeypatch.setattr(spanneradmin, "admins_for", lambda tokens: [(admin, "spanner-google")])
    getattr(plugin, item_class)._teardown_db_isolation(_teardown_item(), False)
    assert "WARNING" in capsys.readouterr().out
