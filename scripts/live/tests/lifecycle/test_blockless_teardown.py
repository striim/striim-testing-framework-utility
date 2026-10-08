"""A case without a `lifecycle:` block keeps its pre-ledger teardown for the kinds the ownership ledger
never deletes: non-Postgres ddl tables by this run's ${TID} prefix, per-test GCS
buckets and Kafka topics (incl. `kafka_cleanup_topics:`), op.upload files and the <path>* file parts.
Under SLT_KEEP_RESOURCES none of it runs."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from livetest import plugin

from .exec_harness import CONFTEST, run_case  # noqa: F401 - run_case is a fixture

pytest_plugins = ["pytester"]

TOKENS = {"TID": "t1234567890_", "TID_ORACLE": "T1234567890_", "NS": "SLT_x"}


class _Admin:
    def __init__(self, calls, name):
        self.calls, self.name = calls, name

    def drop_test_tables(self, prefix=""):
        self.calls.append(("drop", self.name, prefix))

    def delete_bucket(self, bucket):
        self.calls.append(("bucket", bucket))

    def delete_topic(self, topic):
        self.calls.append(("topic", topic))


def _run(calls, **over):
    admins = {"postgres-source": {"admin": _Admin(calls, "pg")},
              "oracle-source": {"admin": _Admin(calls, "oracle")},
              "mysql-source": {"admin": _Admin(calls, "mysql")},
              "mssql-target": {"admin": _Admin(calls, "mssql")}}
    kw = dict(admins=admins,
              ddl_files=[("postgres-source", "a.sql"), ("oracle-source", "b.sql"), ("mysql-source", "c.sql"),
                         ("mssql-target", "d.sql")],
              tokens=TOKENS,
              gcs_cleanup=[(_Admin(calls, "gcs"), "slt-t1234567890-bucket")],
              kafka_cleanup=[(_Admin(calls, "kafka"), "SLT_x_PersistentStream"),
                             (_Admin(calls, "kafka"), "SLT_x_PersistentStream_CHECKPOINT")],
              file_spec=[{"path": "/out/${NS}/rows.json", "match": "expected/rows.csv"}],
              clear_files=lambda p: calls.append(("files", p)),
              upload_names=["t1234567890_op.conf"],
              delete_uploads=lambda names: calls.append(("uploads", tuple(names))))
    kw.update(over)
    _run.out = plugin.teardown_unowned(**kw)
    return calls


def test_each_unowned_kind_is_torn_down_by_this_runs_prefix():
    calls = _run([])
    assert ("drop", "oracle", "T1234567890_") in calls
    assert ("drop", "mysql", "t1234567890_") in calls
    assert ("drop", "mssql", "t1234567890_") in calls
    assert not [c for c in calls if c[:2] == ("drop", "pg")]          # the ledger owns Postgres
    assert ("bucket", "slt-t1234567890-bucket") in calls
    assert ("topic", "SLT_x_PersistentStream") in calls and ("topic", "SLT_x_PersistentStream_CHECKPOINT") in calls
    assert ("uploads", ("t1234567890_op.conf",)) in calls
    assert ("files", "/out/SLT_x/rows.json") in calls


def test_no_drop_is_ever_unprefixed():
    # an empty prefix drops every table in the fixed test schema -- another run's tables too
    assert all(c[2] for c in _run([]) if c[0] == "drop")


def test_one_failure_does_not_stop_the_rest():
    calls = []

    def boom(_names):
        raise RuntimeError("upload delete failed")
    _run(calls, delete_uploads=boom)
    assert ("files", "/out/SLT_x/rows.json") in calls and ("topic", "SLT_x_PersistentStream") in calls


def test_kafka_cleanup_topics_join_the_teardown_delete():
    # regression/framework/framework-kafka-cleanup: its listed topics are in kafka_cleanup, which is deleted
    import inspect
    src = inspect.getsource(plugin.LiveItem._runtest)
    assert "for t in m.kafka_cleanup_topics]" in src
    assert src.index("_slt_ownership.run_cleanup(") < src.index("teardown_unowned(admins, m.ddl_files, tokens, "
                                                                "gcs_cleanup, kafka_cleanup,")


# ---- executed through the real plugin: called for a block-less case, never under keep ----------------

_SPY = '''

_slt_real_teardown_unowned = P.teardown_unowned
def _slt_spy(*a, **k):
    log("teardown_unowned")
    return _slt_real_teardown_unowned(*a, **k)
P.teardown_unowned = _slt_spy
'''


def _spy(root, name):
    conftest = root / "conftest.py"
    conftest.write_text(conftest.read_text() + _SPY)


def test_exec_blockless_case_runs_the_unowned_teardown(run_case):
    run = run_case("legacy", prepare=_spy)
    assert run.ret == 0, run.text
    assert "teardown_unowned" in run.names()


def test_exec_blockless_case_under_keep_leaves_everything(run_case):
    run = run_case("legacy", prepare=_spy, env={"SLT_KEEP_RESOURCES": "1"})
    assert run.ret == 0, run.text
    assert "teardown_unowned" not in run.names()


@pytest.mark.parametrize("active", [False, True])
@pytest.mark.parametrize("keep_flag", [None, "SLT_KEEP_RESOURCES", "SLT_KEEP_RESOURCES_ON_ERROR", "both"])
def test_exec_failed_case_slot_cleanup_respects_keep_policy(run_case, keep_flag, active):
    def prepare(root, name):
        (root / "cases" / "legacy" / "slot.sql").write_text(
            "CREATE TABLE ${TID}debug (id int);\n"
            "SELECT pg_create_logical_replication_slot('${PG_SLOT}', 'wal2json');\n")
        conftest = root / "conftest.py"
        conftest.write_text(conftest.read_text() + r'''
_slot_run_sql = FakePgAdmin.run_sql
def slot_run_sql(self, sql):
    SLOTS.update(re.findall(r"pg_create_logical_replication_slot\('(\w+)'", sql))
    _slot_run_sql(self, sql)
FakePgAdmin.run_sql = slot_run_sql
_slot_api = Api.post_tungsten_line
def slot_api(self, line, timeout=None):
    if line == "LIST APPLICATIONS;":
        ident = runident.derive(os.environ["XTR_CASE_NAME"], os.environ)
        return [{"output": [ident.app]}]
    return _slot_api(self, line)
Api.post_tungsten_line = slot_api
def slot_stop(self, app):
    STATE["running"] = False
    log("stop_kept_app", app=app)
FakeClient.stop_app = slot_stop
_slot_teardown = FakeClient.teardown_namespace
def slot_teardown(self, ns):
    STATE["running"] = False
    _slot_teardown(self, ns)
FakeClient.teardown_namespace = slot_teardown
def slot_drop(self, name, on_active=None):
    log("try_drop_slot", name=name)
    if SC.get("active_slot") and STATE["running"]:
        assert on_active is not None
        on_active(10.0)
    SLOTS.discard(name)
    log("drop_slot", name=name)
FakePgAdmin.drop_replication_slot = slot_drop
def slot_fail(*a, **kw):
    raise RuntimeError("verification failed")
P.assert_smoke = slot_fail
''')

    env = ({"SLT_KEEP_RESOURCES": "1", "SLT_KEEP_RESOURCES_ON_ERROR": "1"}
           if keep_flag == "both" else {keep_flag: "1"} if keep_flag else {})
    run = run_case("legacy", prepare=prepare, env=env, scenario={"active_slot": active},
                   edit=lambda text: text + "\nddl:\n  - {db: postgres-source, file: slot.sql}\n")
    assert run.ret == 1 and "verification failed" in run.text
    exploring = keep_flag in ("SLT_KEEP_RESOURCES", "both")
    assert bool(run.dump()["slots"]) == exploring
    assert ("drop_slot" in run.names()) == (not exploring)
    if keep_flag:
        assert run.dump()["tables"] and run.dump()["namespaces"]
        assert ("stop_kept_app" in run.names()) == (active and not exploring)
        if exploring:
            assert "replication slots and app state kept for exploration" in run.text
        else:
            assert "SELECT pg_create_logical_replication_slot('" in run.text and "'wal2json')" in run.text
            assert "then START APPLICATION" in run.text
            assert ("STOP sent to apps:" in run.text) == active
            assert ("No apps stopped for slot cleanup" in run.text) == (not active)
            assert run.names().index("try_drop_slot") < (run.names().index("stop_kept_app")
                                                       if active else run.names().index("drop_slot"))
        assert "drop_table" not in run.names() and "teardown_namespace" not in run.names()
    else:
        assert run.dump()["tables"] == {} and run.dump()["namespaces"] == []


def test_mysql_setup_reset_is_this_runs_prefix_only():
    import inspect
    src = inspect.getsource(plugin.LiveItem._runtest)
    assert "_mysql.reset_test_objects(tid)" in src
    assert "_mysql.reset_schemas()" not in src                       # never the whole-schema wipe


# ---- R3 N2: the teardown's outcome reaches the cleanup record and the evidence ------------------------

def test_teardown_unowned_returns_what_it_attempted_and_what_failed():
    calls = []

    class _Boom(_Admin):
        def delete_topic(self, topic):
            raise RuntimeError("broker down")
    _run(calls, kafka_cleanup=[(_Boom(calls, "kafka"), "SLT_x_PersistentStream")])
    out = _run.out
    assert set(out["attempted"]) == {"engine-tables oracle-source", "engine-tables mysql-source",
                                     "engine-tables mssql-target", "bucket slt-t1234567890-bucket",
                                     "topic SLT_x_PersistentStream", "upload t1234567890_op.conf",
                                     "file-output /out/SLT_x/rows.json*"}
    assert out["failed"] == {"topic SLT_x_PersistentStream": "RuntimeError('broker down')"}


def test_never_deleted_gaps_become_best_effort_deletions_and_failures_are_gaps():
    resources = {
        "verificationGaps": [
            "engine-tables oracle-source: the tables of b.sql are not acquired and never deleted for this resource type "
            "(no bounded catalog for this engine); left in place",
            "topic SLT_x_T: a framework-derived name is not acquired for this resource type, so it is never deleted (unsupported resource type)",
            "bucket slt-b: a framework-derived name is not acquired for this resource type, so it is never deleted (unsupported resource type)",
            "engine-tables extdb-source: the tables of m.sql are not acquired and never deleted …; left in place",
            "pg-table qasource.t1_src: something else"],
        "foreign": [{"kind": "file-output", "name": "/out/SLT_x/rows.json.1",
                     "note": "matches the output glob but is not this run's exact path; preserved"}]}
    outcome = {"attempted": ["engine-tables oracle-source", "topic SLT_x_T", "bucket slt-b",
                             "file-output /out/SLT_x/rows.json*", "upload u.conf"],
               "failed": {"topic SLT_x_T": "RuntimeError('broker down')", "upload u.conf": "OSError('x')"}}
    got = plugin.note_unowned_teardown(resources, outcome)
    assert got["verificationGaps"] == [
        "engine-tables oracle-source: deleted best-effort by the pre-ledger teardown",
        "topic SLT_x_T: pre-ledger teardown failed: RuntimeError('broker down')",
        "bucket slt-b: deleted best-effort by the pre-ledger teardown",
        "engine-tables extdb-source: the tables of m.sql are not acquired and never deleted …; left in place",
        "pg-table qasource.t1_src: something else",
        "upload u.conf: pre-ledger teardown failed: OSError('x')"]       # a failure with no ledger line
    assert got["foreign"][0]["note"] == "deleted best-effort by the pre-ledger teardown (/out/SLT_x/rows.json*)"
    assert not any("never deleted" in g for g in got["verificationGaps"] if g.startswith(("engine-tables oracle",
                                                                                           "topic", "bucket")))


def test_runtest_stores_the_noted_resources_for_the_evidence():
    import inspect
    src = inspect.getsource(plugin.LiveItem._runtest)
    assert "self._slt_resources = note_unowned_teardown(self._slt_resources, _unowned)" in src
    assert src.index("self._slt_resources = cleanup.resources()") < src.index("note_unowned_teardown(")
