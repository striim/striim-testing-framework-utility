from types import SimpleNamespace

import pytest

from livetest import ownership, runident


def ledger(tmp_path):
    ident = runident.derive("sink", {"SLT_RUN_EPOCH": "run"}, attempt="abcd1234")
    return ownership.Ledger(tmp_path / "ledger.json", ident, {"entries": [], "bindings": {}})


def test_topic_collision_is_refused_before_clear(tmp_path):
    l = ledger(tmp_path)
    admin = SimpleNamespace(topic_exists=lambda name: True)
    with pytest.raises(ownership.OwnershipError, match="collision:kafka-topic"):
        l.acquire_topic(admin, "rows", lambda: pytest.fail("must not clear"))


def test_fresh_topic_is_confirmed(tmp_path):
    l = ledger(tmp_path)
    names = set()
    admin = SimpleNamespace(topic_exists=lambda name: name in names)
    l.acquire_topic(admin, "rows", lambda: names.add("rows"))
    assert l.owns("topic", "kafka", "rows")


def test_spanner_collision_is_refused_before_ddl(tmp_path):
    l = ledger(tmp_path)
    admin = SimpleNamespace(list_owned_tables=lambda: {"test_rows"})
    with pytest.raises(ownership.OwnershipError, match="collision:spanner-table"):
        l.run_ddl_files([("spanner-google", "ddl.sql")], admins={"spanner-google": {"admin": admin}},
            tokens={}, acquire_sinks=True, render_file=lambda name: "CREATE TABLE test_rows (id INT64) PRIMARY KEY(id);",
            run_one=lambda *a: pytest.fail("must not run ddl"))


@pytest.mark.parametrize("table", ["Orders_test", "public.test_rows", '"test_rows"'])
def test_spanner_postgres_unsupported_names_refuse_before_ddl(tmp_path, table):
    l = ledger(tmp_path)
    admin = SimpleNamespace(list_owned_tables=lambda: set())
    message = "schema qualification" if "." in table else "spanner-postgres.*unquoted lowercase"
    with pytest.raises(ownership.OwnershipError, match=message):
        l.run_ddl_files([("spanner-postgres", "ddl.sql")], admins={"spanner-postgres": {"admin": admin}},
            tokens={}, acquire_sinks=True, render_file=lambda name: f"CREATE TABLE {table} (id bigint PRIMARY KEY);",
            run_one=lambda *a: pytest.fail("invalid name must be refused before execution"))
    assert not l.entries


@pytest.mark.parametrize("route", ["spanner-google", "spanner-postgres"])
def test_spanner_qualified_name_refuses_before_ddl(tmp_path, route):
    l = ledger(tmp_path)
    admin = SimpleNamespace(list_owned_tables=lambda: set())
    with pytest.raises(ownership.OwnershipError, match="schema qualification"):
        l.run_ddl_files([(route, "ddl.sql")], admins={route: {"admin": admin}}, tokens={}, acquire_sinks=True,
            render_file=lambda name: "CREATE TABLE public.test_rows (id bigint PRIMARY KEY);",
            run_one=lambda *a: pytest.fail("qualified names cannot be confirmed by the bare-name catalog"))


@pytest.mark.parametrize("route", ["spanner-google", "spanner-postgres"])
def test_spanner_ddl_exact_read_and_verified_cleanup(tmp_path, route):
    from livetest import exactdata, inputs
    from livetest.manifest import load_manifest
    from livetest.spanneradmin import SpannerAdmin
    import yaml
    events, tables = [], set()
    class Snapshot:
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def execute_sql(self, sql, **kw):
            if "information_schema.tables" in sql:
                events.append("inventory")
                rows, cols = [(t,) for t in tables], ["table_name"]
            else:
                assert "FROM test_rows" in sql
                events.append("read")
                rows, cols = [(1,)], ["id"]
            class Result(list): pass
            result = Result(rows)
            result.fields = [SimpleNamespace(name=c) for c in cols]
            return result
    class Database:
        def snapshot(self): return Snapshot()
        def update_ddl(self, statements):
            for sql in statements:
                if sql.startswith("DROP TABLE"):
                    events.append("drop")
                    tables.remove("test_rows")
                else:
                    events.append("create")
                    tables.add("test_rows")
            return SimpleNamespace(result=lambda timeout: None)
    admin = SpannerAdmin({"dialect": "postgresql" if route.endswith("postgres") else "google_standard_sql"}, database=Database())
    admins = {route: {"admin": admin}}
    l = ledger(tmp_path)
    ddl = ("CREATE TABLE test_rows (id bigint PRIMARY KEY);" if route == "spanner-postgres"
           else "CREATE TABLE test_rows (id INT64) PRIMARY KEY(id);")
    l.run_ddl_files([(route, "ddl.sql")], admins=admins, tokens={}, acquire_sinks=True,
        render_file=lambda name: ddl, run_one=lambda db, name: admin.run_sql(ddl))
    assert l.owns("spanner-table", route, "test_rows")
    (tmp_path / "app.tql").write_text("-- test app\n")
    (tmp_path / "rows.csv").write_text("id\n1\n")
    raw = {"name": "owned-spanner", "tql": "app.tql", "requires": ["spanner"], "exact": {"version": 1},
           "assert": {"data": [{"target": "test_rows", "db": route, "match": "rows.csv", "exact": {"columns": {"id": "integer"}}}]}}
    (tmp_path / "test.yaml").write_text(yaml.safe_dump(raw))
    m = load_manifest(tmp_path / "test.yaml")
    m.timeout = 0
    comparisons = []
    records = exactdata.assert_exact_data(admins, exactdata.partition(m.assert_["data"], m, "data")[1], m,
        tokens={}, owned=l.owns, lifecycle=True, inputs=inputs.snapshot(m, tmp_path / "test.yaml"), collector=comparisons)
    assert records[0]["status"] == "passed"
    assert comparisons[0]["equal"] is True and comparisons[0]["actual"]["owned"] is True
    entry = l.entries[0]
    l._delete(entry, client=None, admins=admins, ctx=None, tokens={})
    assert l._verify(entry, client=None, admins=admins, ctx=None) is True
    assert events == ["inventory", "create", "inventory", "read", "drop", "inventory"]
    assert not tables
