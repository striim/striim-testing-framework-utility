"""REST reader boundary controls; no Docker, credentials or cloud access."""
import datetime as dt
import json
import threading
import time
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from livetest import canon, exactdata, lifecycle, ownership, runident
from livetest.bigqueryadmin import BigQueryAdmin, BigQueryError, decode_rows, table_ref


def test_scalar_decoding_never_rounds_through_float():
    kinds = ["INT64", "NUMERIC", "TIMESTAMP", "DATETIME", "DATE", "BYTES", "BOOL", "STRING", "STRING"]
    values = ["9007199254740993", "12.3400", "1700000000000001", "2024-01-02T03:04:05.000001",
              "2024-01-02", "AP8=", "true", "", None]
    schema = {"fields": [{"name": f"c{i}", "type": kind} for i, kind in enumerate(kinds)]}
    row = decode_rows(schema, [{"f": [{"v": v} for v in values]}])[0]
    assert row == dict(zip([f"c{i}" for i in range(9)], [9007199254740993, Decimal("12.3400"),
        dt.datetime(2023, 11, 14, 22, 13, 20, 1, tzinfo=dt.timezone.utc),
        dt.datetime(2024, 1, 2, 3, 4, 5, 1), dt.date(2024, 1, 2), b"\0\xff", True, "", None]))


@pytest.mark.parametrize("field", [{"type": "FLOAT"}, {"type": "JSON"}, {"type": "RECORD"},
                                  {"type": "STRING", "mode": "REPEATED"}])
def test_unsupported_schema_is_refused_even_without_rows(field):
    with pytest.raises(BigQueryError, match="exact-type-unsupported"):
        decode_rows({"fields": [{"name": "x", **field}]}, [])


@pytest.mark.parametrize("name", ["other-project.slt.t", "test-project.other.t", "slt.t;DROP", "`slt.t`"])
def test_table_scope_rejects_sql_and_other_datasets(name):
    with pytest.raises(ValueError):
        table_ref(name, "test-project", "slt")


def test_table_scope_preserves_case():
    assert table_ref("slt.Mixed", "test-project", "slt") == "test-project.slt.Mixed"


@pytest.fixture
def peer():
    state = SimpleNamespace(mode="normal", requests=[], fields=[{"name": "id", "type": "INTEGER"}],
                            rows=[{"f": [{"v": "9007199254740993"}]}])

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            self.reply({})

        def do_DELETE(self):
            if state.mode == "slow-delete":
                time.sleep(0.4)
            self.reply({})

        def do_GET(self):
            if "/tables/" in self.path:
                self.reply({"schema": {"fields": state.fields}})
            else:
                if state.mode == "slow-query":
                    time.sleep(2.2)
                self.reply({"jobComplete": state.mode != "incomplete", "schema": {"fields": state.fields}, "rows": state.rows,
                            **({"pageToken": "again"} if state.mode == "pages" else {})})

        def reply(self, doc):
            state.requests.append(self.path)
            if state.mode == "giant":
                data = b" " * 200_000
            else:
                data = json.dumps(doc).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data) if state.mode != "trickle" else 100000))
            self.end_headers()
            try:
                if state.mode == "trickle":
                    for _ in range(50):
                        self.wfile.write(b" ")
                        self.wfile.flush()
                        time.sleep(0.1)
                else:
                    self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    state.admin = BigQueryAdmin({"host": "127.0.0.1", "port": server.server_port,
                                "project": "test-project", "dataset": "slt"})
    yield state
    server.shutdown()
    server.server_close()
    thread.join()


def test_sdk_rest_uses_local_explicit_endpoint_and_preserves_large_integer(peer, monkeypatch):
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "/nonexistent/credentials.json")
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    assert peer.admin.select_rows("slt.t", remaining=6) == [{"id": 9007199254740993}]
    assert any("/jobs" in r for r in peer.requests)
    assert peer.admin.last_operation["jobId"].startswith("slt_")


@pytest.mark.parametrize("mode,rows,cap", [("giant", 1, 10), ("normal", 1, 10000), ("pages", 1, 10000), ("normal", 2, 1)])
def test_caps_never_return_a_partial_passing_result(peer, mode, rows, cap):
    peer.mode = mode
    if mode == "normal":
        peer.rows *= 2
    with pytest.raises(BigQueryError, match="canonical-limit-exceeded"):
        peer.admin.select_rows("slt.t", max_rows=rows, max_bytes=cap, remaining=6)


def test_trickle_cannot_extend_total_deadline(peer):
    peer.mode = "trickle"
    started = time.monotonic()
    with pytest.raises(BigQueryError, match="exact-read-timeout"):
        peer.admin.select_rows("slt.t", remaining=2.5)
    assert time.monotonic() - started < 3.5
    assert peer.admin.last_operation["cancellation"] in ("response", "failed", "timed-out")


def test_missing_service_fails_before_read():
    with pytest.raises(exactdata.ExactReadError, match="exact-route-unrequired"):
        exactdata._db_ref({}, "bigquery-target", {}, "slt.t")


def test_bigquery_initial_load_owns_exact_target_and_refuses_cdc():
    source = {"db": "postgres-source", "table": "qasource.t_src"}
    target = {"db": "bigquery-target", "table": "slt.t_tgt"}
    block = {"version": 1, "mode": "initial-load", "sink": "db",
             "readiness": {"kind": "baseline-landed", "source": source, "target": target},
             "completion": {"kind": "source-count", "source": source, "target": target}}
    spec = lifecycle.parse_spec(block, "test.yaml")
    assert not spec.uses_bigquery_cdc()
    tokens = {"BIGQUERY_PROJECT": "test-project", "BIGQUERY_DATASET": "slt"}
    seen = []
    assert lifecycle.check_witness_refs(spec, tokens, lambda *args: seen.append(args) or True) is None
    assert ("bigquery-table", "bigquery-target", "test-project.slt.t_tgt") in seen
    assert "witness-not-owned" in lifecycle.check_witness_refs(spec, tokens, lambda *args: args[0] == "pg-table")
    with pytest.raises(lifecycle.LifecycleSpecError, match="BigQuery"):
        lifecycle.parse_spec(dict(block, mode="cdc", readiness={"kind": "source-progress", "db": "postgres-source"},
                                  completion={"kind": "row-count", "db": "bigquery-target", "table": "slt.t_tgt", "expect": 2}), "test.yaml")


def test_bigquery_ledger_collision_confirmation_and_binding(tmp_path):
    ident = runident.derive("bq", {"SLT_RUN_EPOCH": "reader-test"})
    ledger = ownership.Ledger.open(ident, state_dir=tmp_path, env={})
    tables = set()
    admin = BigQueryAdmin({"host": "127.0.0.1", "port": 9050, "project": "test-project", "dataset": "slt"})
    admin.table_present = lambda name: name in tables
    name = admin.ref(ident.tid + "target")
    admin.run_sql = lambda sql: tables.add(name)
    admin.drop_table = lambda name: tables.remove(name)
    admins = {"bigquery-target": {"admin": admin, "schema": "slt"}}
    ledger.bind(admins, None)
    tables.add(name)
    sql = f"CREATE TABLE `{name}` (id INT64)"
    with pytest.raises(ownership.OwnershipError, match="collision:bigquery-table"):
        ledger._bigquery_ddl(sql, "bigquery-target", "ddl.sql", admins, {"TID": ident.tid})
    assert tables == {name} and not ledger.entries
    tables.clear()
    ledger._bigquery_ddl(sql, "bigquery-target", "ddl.sql", admins, {"TID": ident.tid})
    assert ledger.owns("bigquery-table", "bigquery-target", name)
    entry = ledger.entries[0]
    now = ownership.snapshot_targets(admins, None)
    assert ledger.target_problem(entry, now) is None
    now["routes"]["bigquery-target"]["endpoint"] = "http://127.0.0.1:9052"
    assert "binding changed" in ledger.target_problem(entry, now)
    foreign = admin.ref(ident.tid + "foreign")
    tables.add(foreign)
    ledger._delete(entry, client=None, admins=admins, ctx=None, tokens={}, fault=False)
    assert ledger._verify(entry, client=None, admins=admins, ctx=None)
    assert tables == {foreign}


def test_page_budget_rejects_repeated_empty_pages(peer):
    peer.mode, peer.rows = "pages", []
    with pytest.raises(BigQueryError, match="REST page cap exceeded"):
        peer.admin.select_rows("slt.t", remaining=6)
    assert len([p for p in peer.requests if "/queries/" in p]) == 128


def test_query_id_stays_in_assertion_detail_with_existing_canonical_evidence_schema():
    from livetest import evidence
    decl = canon.declaration({'columns': {'id': 'integer'}}, db_route=True)
    comp = canon.compare([{'id': '1'}], [{'id': 1}], decl)
    comp['_read_detail'] = 'BigQuery job slt_control at http://127.0.0.1:9050'
    prepared = exactdata._Prepared('data', 0, {}, decl, 'test-project.slt.Mixed', 'bigquery-target', True,
                                   None, source='expected.csv', template_sha='sha256:' + 'a' * 64)
    prepared.done(comp)
    evidence._check_comparison(prepared.result[2], 0)
    assert 'slt_control' in prepared.record()['detail']
    assert '_read_detail' not in prepared.result[2] and 'reads' not in prepared.result[2]


def test_bigquery_diff_source_witness_preserves_case_and_requires_owned_identity():
    from livetest import evidence
    source = 'test-project.slt.Mixed'
    comp = {'expected': {'source': 'bigquery-target:' + source}, 'actual': {'owned': True}}
    record = {'spec': {'source_db': 'bigquery-target', 'source': source}}
    owned = [{'kind': 'bigquery-table', 'db': 'bigquery-target', 'name': source, 'state': 'confirmed'}]
    assert evidence._diff_source().fullmatch(comp['expected']['source'])
    assert evidence._diff_source_bound(comp, record, {}, owned)
    owned[0]['name'] = source.lower()
    assert not evidence._diff_source_bound(comp, record, {}, owned)
    assert not evidence._diff_source().fullmatch('bigquery-target:slt.Mixed')


def test_reader_deletes_its_job_on_success_and_limit_error(peer):
    peer.admin.select_rows('slt.t', remaining=6)
    first = peer.admin.last_operation['jobId']
    assert any('/jobs/' + first + '/delete' in p for p in peer.requests)
    assert peer.admin.last_operation['jobDelete'] == 'response'
    peer.rows *= 2
    with pytest.raises(Exception, match='canonical-limit-exceeded'):
        peer.admin.select_rows('slt.t', max_rows=1, remaining=6)
    second = peer.admin.last_operation['jobId']
    assert second != first
    assert any('/jobs/' + second + '/delete' in p for p in peer.requests)
    assert peer.admin.last_operation['jobDelete'] == 'response'


def test_timestamp_uses_integer_microseconds_for_negative_fraction(peer):
    peer.fields = [{'name': 'at', 'type': 'TIMESTAMP'}]
    peer.rows = [{'f': [{'v': '-1'}]}]
    rows = peer.admin.select_rows('slt.t', remaining=6)
    assert rows == [{'at': dt.datetime(1969,12,31,23,59,59,999999,tzinfo=dt.timezone.utc)}]
    from urllib.parse import parse_qs, urlsplit
    query = next(p for p in peer.requests if '/queries/' in p)
    assert parse_qs(urlsplit(query).query)['formatOptions.useInt64Timestamp'] == ['true']


def test_bigquery_ddl_records_rendered_input_without_running_twice(tmp_path):
    ident = runident.derive('ddl-evidence', {'SLT_RUN_EPOCH': 'ddl-evidence-test'})
    ledger = ownership.Ledger.open(ident, state_dir=tmp_path, env={})
    admin = BigQueryAdmin({'host':'127.0.0.1','port':9050,'project':'test-project','dataset':'slt'})
    tables, sent, recorded = set(), [], []
    name = admin.ref(ident.tid+'target')
    admin.table_present = lambda n: n in tables
    def execute(sql):
        sent.append(sql)
        tables.add(name)
    admin.run_sql = execute
    sql = f'CREATE TABLE `{name}` (id INT64) OPTIONS(description="a;b");\n'
    ledger.run_ddl_files([('bigquery-target','ddl.sql')], admins={'bigquery-target':{'admin':admin}},
        tokens={'TID':ident.tid}, render_file=lambda _:sql,
        run_one=lambda *_: pytest.fail('recording must not re-execute the DDL'),
        record=lambda db, filename, rendered: recorded.append((db, filename, rendered)))
    assert len(sent) == 1
    assert recorded == [('bigquery-target','ddl.sql',sql)]
    assert ledger.owns('bigquery-table','bigquery-target',name)


def test_page_read_can_use_the_callers_budget_beyond_two_seconds(peer):
    peer.mode = 'slow-query'
    assert peer.admin.select_rows('slt.t', remaining=6) == [{'id':9007199254740993}]


def test_short_bigquery_stability_read_has_worker_startup_budget():
    admin = BigQueryAdmin({'host':'127.0.0.1','port':9050,'project':'test-project','dataset':'slt'})
    budgets = []
    def count(table, remaining):
        budgets.append(remaining)
        assert remaining >= 3
        return 1
    admin.count_rows = count
    observe, _ = lifecycle._count_observer({'bigquery-target':{'admin':admin}},
                                          {'db':'bigquery-target','table':'slt.t'}, {})
    record, held = lifecycle._hold(SimpleNamespace(stability_s=0.001), lifecycle.Clock, observe, lambda n:n==1)
    assert held and record['value']==1 and budgets


def test_job_delete_can_finish_within_the_original_budget(peer):
    peer.mode = 'slow-delete'
    started = time.monotonic()
    assert peer.admin.select_rows('slt.t', remaining=6) == [{'id':9007199254740993}]
    assert peer.admin.last_operation['jobDelete'] == 'response'
    assert time.monotonic() - started < 6


def test_cdc_dataset_collision_cleanup_and_binding(tmp_path):
    ident = runident.derive('merge', {'SLT_RUN_EPOCH': 'dataset-control'})
    original = BigQueryAdmin({'host': '127.0.0.1', 'port': 9050, 'project': 'test-project', 'dataset': 'slt'})
    admin = original.for_cdc(ident)
    assert admin.dataset == 'slt_' + ident.per_test and original.dataset == 'slt'
    admins = {'bigquery-target': {'admin': admin}}
    ledger = ownership.Ledger.open(ident, state_dir=tmp_path, env={})
    ledger.bind(admins, None)
    datasets = {admin.dataset, 'slt', 'foreign'}
    admin.dataset_present = lambda: admin.dataset in datasets
    admin.create_dataset = lambda: datasets.add(admin.dataset)
    admin.drop_dataset = lambda: datasets.remove(admin.dataset)
    with pytest.raises(ownership.OwnershipError, match='collision:bigquery-dataset'):
        ledger.acquire_bigquery_dataset(admins)
    assert not ledger.entries and datasets == {admin.dataset, 'slt', 'foreign'}
    datasets.remove(admin.dataset)
    entry = ledger.acquire_bigquery_dataset(admins)
    assert entry['state'] == 'confirmed'
    now = ownership.snapshot_targets(admins, None)
    assert ledger.target_problem(entry, now) is None
    now['routes']['bigquery-target']['dataset'] = 'slt'
    assert 'binding changed' in ledger.target_problem(entry, now)
    ledger._delete(entry, client=None, admins=admins, ctx=None, tokens={}, fault=False)
    assert ledger._verify(entry, client=None, admins=admins, ctx=None)
    assert datasets == {'slt', 'foreign'}
    with pytest.raises(ownership.OwnershipError, match='per-test dataset'):
        ledger.acquire_bigquery_dataset({'bigquery-target': {'admin': original}})


def test_sentinel_key_is_a_typed_parameter(peer, monkeypatch):
    sent = []
    monkeypatch.setattr(peer.admin, '_operation', lambda op, remaining, **kw:
                        sent.append(kw) or {'schema': {'fields': [{'name': 'n', 'type': 'INT64'}]},
                                           'rows': [{'f': [{'v': '1'}]}]})
    assert peer.admin.count_key('slt.t', 'id', 9007199254740993, 5) == 1
    assert '@sentinel' in sent[0]['sql'] and '9007199254740993' not in sent[0]['sql']
    assert sent[0]['parameters'] == [{'name': 'sentinel', 'parameterType': {'type': 'INT64'},
                                     'parameterValue': {'value': '9007199254740993'}}]
    with pytest.raises(ValueError):
        peer.admin.count_key('slt.t', 'id;DROP', 1, 5)


def test_cdc_dataset_failed_create_never_adopts_concurrent_owner(tmp_path):
    ident = runident.derive('merge-race', {'SLT_RUN_EPOCH': 'dataset-control'})
    admin = BigQueryAdmin({'host': '127.0.0.1', 'port': 9050, 'project': 'test-project', 'dataset': 'slt'}).for_cdc(ident)
    present = []
    admin.dataset_present = lambda: bool(present)
    def race():
        present.append('concurrent owner')
        raise RuntimeError('conflict')
    admin.create_dataset = race
    admins = {'bigquery-target': {'admin': admin}}
    ledger = ownership.Ledger.open(ident, state_dir=tmp_path, env={})
    ledger.bind(admins, None)
    with pytest.raises(RuntimeError, match='conflict'):
        ledger.acquire_bigquery_dataset(admins)
    assert ledger.entries[0]['state'] == 'intended' and present == ['concurrent owner']
    assert ledger.entries[0]['state'] not in ownership.DELETABLE_STATES


def test_run_sql_preserves_googlesql_quoted_semicolons(monkeypatch):
    admin = BigQueryAdmin({'host':'127.0.0.1','port':9050,'project':'test-project','dataset':'slt'})
    sent = []
    monkeypatch.setattr(admin, '_operation', lambda *a, **kw: sent.append(kw['sql']))
    statements = [r"SELECT 'x\'; y'", 'SELECT "a;b"', 'SELECT `a;b`']
    admin.run_sql('; '.join(statements)+';')
    assert sent == statements


def test_incomplete_job_poll_cap_is_a_timeout(peer):
    peer.mode = 'incomplete'
    with pytest.raises(BigQueryError, match='exact-read-timeout: .*job remained incomplete'):
        peer.admin.select_rows('slt.t', remaining=12)
