import pytest

from livetest import audit, canon


TQL = """
CREATE TYPE ${TID}row (id int);
CREATE STREAM ${TID}log OF ${TID}row;
CREATE TARGET ${TID}writer USING Global.DatabaseWriter (
  Tables: '${PG_TARGET_SCHEMA}.${TID}audit',
  ExecuteInsertAsUpdate: false,
  ExecuteUpdateAsInsert: false
) INPUT FROM ${TID}log;
"""


def raw():
    return {"audit": {"version": 1}, "lifecycle": {"version": 1, "completion": {"kind": "row-count", "db": "postgres-target", "table": "${PG_TARGET_SCHEMA}.${TID}audit", "expect": 1}}, "exact": {"version": 1},
        "assert": {"data": [{"target": "${PG_TARGET_SCHEMA}.${TID}audit", "db": "postgres-target",
            "match": "expected/rows.csv", "exact": {"columns": {"id": "integer"}},
            "audit": {"writer": "${TID}writer"}}]}}


def test_audit_writer_is_append_only_and_multiset():
    result = audit.normalize(raw(), "p", tql=TQL)
    assert result[0]["writer"] == "${TID}writer"


@pytest.mark.parametrize("change", [lambda r: r.pop("audit"), lambda r: r["audit"].update(unknown=1),
    lambda r: r.pop("lifecycle"), lambda r: r["assert"]["data"][0].pop("exact"),
    lambda r: r["assert"]["data"][0]["exact"].update(order="sequence", order_by=["id"]),
    lambda r: r["assert"]["data"][0]["audit"].update(unknown=1)])
def test_audit_schema_is_strict(change):
    r = raw(); change(r)
    with pytest.raises(ValueError, match="invalid-audit-spec"):
        audit.normalize(r, "p", tql=TQL)


@pytest.mark.parametrize("tql", [TQL.replace("ExecuteInsertAsUpdate: false", "ExecuteInsertAsUpdate: true"),
    TQL.replace("ExecuteUpdateAsInsert: false", "ExecuteUpdateAsInsert: true"),
    TQL.replace("ExecuteInsertAsUpdate: false,", "IgnorableExceptionCode: 'DUPLICATE_ROW_EXISTS',"),
    TQL.replace("INPUT FROM ${TID}log", "INPUT FROM raw_stream")])
def test_merge_or_mutable_input_is_refused(tql):
    with pytest.raises(ValueError, match="audit-writer-not-append-only"):
        audit.normalize(raw(), "p", tql=tql)


@pytest.mark.parametrize("route", ["postgres-source", "postgres-target", "spanner-google", "spanner-postgres", "kafka", "file"])
def test_audit_duplicate_fails_every_exact_route(route):
    r, tql = raw(), TQL
    s = r["assert"]["data"][0]
    if route.startswith("spanner-"):
        adapter = "SpannerWriter" if route == "spanner-google" else "SpannerPGDialectWriter"
        s.update(db=route, target="${TID}audit")
        s["audit"]["delivery_key"] = "delivery_id"
        s["exact"]["ignore"] = ["delivery_id"]
        r["lifecycle"]["completion"].update(db=route, table=s["target"])
        tql = tql.replace("DatabaseWriter", adapter).replace("${PG_TARGET_SCHEMA}.${TID}audit", "${TID}audit")
    elif route == "kafka":
        s.update(db="kafka", target="${KAFKA_TGT_TOPIC}", project="kafka_record", keys=["value.id"])
        s["exact"] = {"columns": {"value.id": "integer"}}
        r["lifecycle"]["completion"] = {"kind": "sentinel"}
        r["lifecycle"]["sentinel"] = {"observe": {"db": "kafka", "topic": "${KAFKA_TGT_TOPIC}"}}
        tql = "CREATE TARGET ${TID}writer USING Global.KafkaWriter VERSION '2.1.0' (Topic:'${KAFKA_TGT_TOPIC}') INPUT FROM log;"
    elif route == "file":
        s = {"path": "${OWNED_DIR}/rows.json", "match": "expected/rows.csv", "exact": {"columns": {"id": "integer"}},
             "audit": {"writer": "${TID}writer"}}
        r["assert"] = {"file": [s]}
        r["lifecycle"]["completion"] = {"kind": "file-lines", "path": s["path"], "lines": 1}
        tql = "CREATE TARGET ${TID}writer USING Global.FileWriter (FileName:'${OWNED_DIR}/rows.json') INPUT FROM log;"
    else:
        s["db"] = route
        r["lifecycle"]["completion"]["db"] = route
    assert audit.normalize(r, "p", tql=tql)[0]["kind"] == ("file" if route == "file" else "data")
    assert canon.check_manifest(r)["specs"]
    d = canon.declaration({"columns": {"id": "integer"}}, db_route=route not in ("kafka", "file"))
    assert not canon.compare([{"id": "1"}], [{"id": 1}, {"id": 1}], d)["equal"]


def test_pg_unique_target_is_refused():
    with pytest.raises(ValueError, match="audit-target-not-append-only"):
        audit.guard_pg(object(), '"qatarget"."audit"', query=lambda *a: [(1,)], run=lambda *a: None)


def test_pg_guard_is_owned_verified_and_never_repaired_after_removal():
    from types import SimpleNamespace
    entries, sql = [], []
    class Ledger:
        ident = SimpleNamespace(attempt="attempt")
        def find(self, *a, **kw): return [e for e in entries if e.get("state") == "confirmed"]
        def add(self, kind, name, route):
            e = {"kind": kind, "name": name, "db": route}; entries.append(e); return e
        def _set(self, e, state): e["state"] = state
        def _drop_entry(self, e): entries.remove(e)
        def persist(self): pass
    definition, trigger = [None], [False]
    def query(statement, *a):
        if "pg_get_functiondef" in statement: return [(definition[0],)]
        if statement.startswith("SELECT count(*) FROM pg_trigger"): return [(int(trigger[0]),)]
        return [(0,)]
    def run(statement, *a):
        sql.append(statement)
        if statement.startswith("CREATE FUNCTION"): definition[0] = statement
        if statement.startswith("ALTER TABLE"): trigger[0] = True
    args = dict(query=query, run=run, ledger=Ledger(), tokens={"TID": "case_"}, route="postgres-target")
    audit.guard_pg(object(), '"qatarget"."audit"', **args)
    assert entries[0]["kind"] == "pg-function" and entries[0]["definitionSha256"].startswith("sha256:")
    assert any("BEFORE UPDATE OR DELETE OR TRUNCATE" in s for s in sql)
    assert any("ENABLE ALWAYS TRIGGER" in s for s in sql)
    assert not any("REVOKE UPDATE" in s for s in sql)
    audit.guard_pg(object(), '"qatarget"."audit"', **args)
    before = len(sql)
    trigger[0] = False
    with pytest.raises(ValueError, match="removed or changed"):
        audit.guard_pg(object(), '"qatarget"."audit"', **args)
    assert len(sql) == before


@pytest.mark.parametrize("dialect,default", [("google_standard_sql", "GENERATE_UUID()"), ("postgresql", "spanner.generate_uuid()")])
def test_spanner_uses_sink_generated_delivery_key(dialect, default):
    from livetest.spanneradmin import SpannerAdmin
    admin = SpannerAdmin({"dialect": dialect})
    calls = []
    def query(sql):
        calls.append(sql)
        return ["column"], [(default,)] if "column_default" in sql else [("delivery_id",)]
    admin._query = query
    admin.check_audit_table("case_audit", "delivery_id")
    assert "key_column_usage" in calls[0] and "PRIMARY KEY" in calls[0]
    admin._query = lambda sql: ([], [(None,)] if "column_default" in sql else [("id",)])
    with pytest.raises(ValueError, match="audit-target-not-append-only"):
        admin.check_audit_table("case_audit", "delivery_id")


def test_audit_completion_must_observe_the_audit_output():
    r = raw(); r["lifecycle"]["completion"]["table"] = "${PG_TARGET_SCHEMA}.${TID}business"
    with pytest.raises(ValueError, match="invalid-audit-spec"):
        audit.normalize(r, "p", tql=TQL)


def test_snapshot_worker_reads_and_joins():
    from livetest.snapshot_observer import Observer
    from types import SimpleNamespace
    s = {"db": "postgres-target", "tables": {"a": {"target": "a", "columns": {"id": "integer"}}},
         "prefixes": [{"a": "rows.csv"}], "interval": "1ms"}
    o = Observer(s, admins={}, tokens={}, case_dir=".",
        inputs=SimpleNamespace(golden=lambda p: b"id\n1\n"), read=lambda *a: {"a": [{"id": 1}]}, timeout=1)
    o.start()
    assert o.finish()[0]["status"] == "passed" and len(o.history) >= 3
    assert not o.thread.is_alive()


def test_marker_goldens_can_use_recorded_input_ids():
    from livetest import lifecycle
    from types import SimpleNamespace
    state = SimpleNamespace(sentinel_ids={"ready": 42, "done": 43})
    tokens = {}
    lifecycle.publish_sentinels(state, tokens, SimpleNamespace(per_test="case", attempt="attempt"))
    rows = canon.parse_golden(b"id\n${SENTINEL_READY_ID}\n${SENTINEL_DONE_ID}\n", db_route=False, tokens=tokens)
    assert rows == [{"id": "42"}, {"id": "43"}]
    assert state.sentinel_bindings == tokens
    assert tokens["SENTINEL_DONE_TAG"] == "slt-case-attempt-done"


def test_owned_guard_function_is_dropped_and_verified_after_tables(tmp_path, monkeypatch):
    from livetest import ownership, runident
    from types import SimpleNamespace
    calls = []
    monkeypatch.setattr(ownership, "make_catalog", lambda admin: SimpleNamespace(
        run=lambda sql, remaining: calls.append(sql), query=lambda *a: [(True,)]))
    ident = runident.derive("audit", {"SLT_RUN_EPOCH": "run"}, attempt="abcd1234")
    ledger = ownership.Ledger.open(ident, state_dir=tmp_path)
    entry = ledger.add("pg-function", "qatarget.case_audit_guard", "postgres-target", "confirmed")
    ledger._delete(entry, client=None, admins={"postgres-target": {"admin": object()}}, ctx=None, tokens={})
    assert calls == ['DROP FUNCTION IF EXISTS "qatarget"."case_audit_guard"()']
    assert ledger._verify(entry, client=None, admins={"postgres-target": {"admin": object()}}, ctx=None)
    assert ownership.CLEANUP_ORDER.index("pg-table") < ownership.CLEANUP_ORDER.index("pg-function")


def test_kafka_audit_accepts_the_shipped_versioned_writer_tql():
    from pathlib import Path
    tql = (Path(__file__).parents[1] / 'regression/services/kafka/kafka-diff/app.tql').read_text()
    r = raw()
    s = r['assert']['data'][0]
    s.update(db='kafka', target='${KAFKA_TGT_TOPIC}', project='kafka_record', keys=['value.c0'])
    s['exact'] = {'columns': {}}
    s['audit'] = {'writer': 'KafkaTarget'}
    r['lifecycle']['completion'] = {'kind':'sentinel'}
    r['lifecycle']['sentinel'] = {'observe':{'db':'kafka','topic':'${KAFKA_TGT_TOPIC}'}}
    assert audit.normalize(r, 'p', tql=tql)[0]['writer'] == 'KafkaTarget'
    with pytest.raises(ValueError, match='audit-writer-not-append-only'):
        audit.normalize(r, 'p', tql=tql.replace("Topic: '${KAFKA_TGT_TOPIC}'", "Topic: '${KAFKA_SRC_TOPIC}'"))


@pytest.mark.parametrize('database', ['${SPANNER_GSQL_DB}', '${OTHER_DB}', 'foreign'])
def test_spanner_audit_binds_the_selected_database_and_exact_table(database):
    r = raw()
    s = r['assert']['data'][0]
    s.update(db='spanner-google', target='${TID}audit')
    s['audit']['delivery_key'] = 'delivery_id'
    s['exact']['ignore'] = ['delivery_id']
    r['lifecycle']['completion'].update(db='spanner-google', table=s['target'])
    tql = TQL.replace('DatabaseWriter', 'SpannerWriter').replace(
        '${PG_TARGET_SCHEMA}.${TID}audit', database + '.${TID}audit')
    if database == '${SPANNER_GSQL_DB}':
        assert audit.normalize(r, 'p', tql=tql)[0]['route'] == 'spanner-google'
        with pytest.raises(ValueError, match='audit-writer-not-append-only'):
            audit.normalize(r, 'p', tql=tql.replace('.${TID}audit', '.${TID}other'))
    else:
        with pytest.raises(ValueError, match='audit-writer-not-append-only'):
            audit.normalize(r, 'p', tql=tql)
