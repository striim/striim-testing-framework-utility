from pathlib import Path
from types import SimpleNamespace

import pytest

from livetest import transactions


def steps():
    return [{"session": "a", "op": "begin", "db": "postgres-source", "when": "post_start"},
            {"session": "b", "op": "begin", "db": "postgres-source", "when": "post_start"},
            {"session": "a", "op": "sql", "file": "a.sql", "when": "post_start"},
            {"session": "a", "op": "savepoint", "name": "keep", "when": "post_start"},
            {"session": "a", "op": "sql", "file": "discard.sql", "when": "post_start"},
            {"session": "a", "op": "rollback_to", "name": "keep", "when": "post_start"},
            {"session": "b", "op": "commit", "when": "post_start"},
            {"session": "a", "op": "commit", "when": "post_recover"}]


class Connection:
    def __init__(self, name, log): self.name, self.log, self.closed = name, log, False
    def cursor(self): return self
    def execute(self, sql, params=None): self.log.append((self.name, sql))
    def commit(self): self.log.append((self.name, "commit"))
    def rollback(self): self.log.append((self.name, "rollback"))
    def close(self): self.closed = True


def test_overlapping_sessions_hold_through_recover_and_commit_in_chosen_order():
    log, conns = [], []
    def connect(admin, timeout):
        conn = Connection("a" if not conns else "b", log); conns.append(conn); return conn
    pool = transactions.Pool({"postgres-source": {"admin": object()}}, {},
        source_sql=lambda f: "INSERT INTO source VALUES (1)", connect=connect)
    pool.run_phase(steps(), "post_start")
    assert len(conns) == 2 and conns[0].closed is False and conns[1].closed is True
    assert ("b", "commit") in log and ("a", "commit") not in log
    pool.run_phase(steps(), "post_recover")
    assert log.index(("b", "commit")) < log.index(("a", "commit"))
    assert any(sql == 'ROLLBACK TO SAVEPOINT "keep"' for _, sql in log)
    assert all(c.closed for c in conns)


@pytest.mark.parametrize("control", ["COMMIT", "ABORT"])
def test_error_closes_and_rolls_back_every_owned_session(control):
    conn = Connection("a", [])
    pool = transactions.Pool({"postgres-source": {"admin": object()}}, {},
        source_sql=lambda f: control + ";", connect=lambda *a: conn)
    with pytest.raises(ValueError, match="transaction-control-in-sql"):
        pool.run_phase(steps()[:1] + [steps()[2]], "post_start")
    assert conn.closed and ("a", "rollback") in conn.log


def test_every_step_has_evidence_and_log_including_commit_after_recovery():
    from livetest.resultschema import _validate_assertion
    log, conns, records, messages = [], [], [], []
    def connect(admin, timeout):
        conn = Connection(str(len(conns)), log); conns.append(conn); return conn
    pool = transactions.Pool({"postgres-source": {"admin": object()}}, {},
        source_sql=lambda f: "INSERT INTO source VALUES (1)", connect=connect,
        trace=records.append, report=messages.append)
    pool.run_phase(steps(), "post_start")
    assert len(records) == 7
    pool.run_phase(steps(), "post_recover")
    assert len(records) == len(steps())
    for rec, step in zip(records, steps()):
        _validate_assertion(rec, "transaction")
        event = rec["actual"]["rows"][0]
        assert (event["session"], event["op"], event["phase"]) == (step["session"], step["op"], step["when"])
        assert event["startedAt"] <= event["endedAt"] and event["durationS"] >= 0
        assert event["outcome"] == "succeeded" and rec["status"] == "passed"
    assert records[-1]["actual"]["rows"][0]["phase"] == "post_recover"
    assert any("session=a op=commit phase=post_recover" in msg and "succeeded" in msg for msg in messages)


def test_failed_transaction_step_retains_failed_outcome_and_rolls_back():
    records, messages = [], []
    class Rejected(Connection):
        def commit(self): raise RuntimeError("commit rejected")
    conn = Rejected("a", [])
    pool = transactions.Pool({"postgres-source": {"admin": object()}}, {},
        source_sql=lambda f: "", connect=lambda *args: conn,
        trace=records.append, report=messages.append)
    with pytest.raises(RuntimeError, match="commit rejected"):
        pool.run_phase([steps()[0], {"session": "a", "op": "commit", "when": "post_start"}], "post_start")
    event = records[-1]["actual"]["rows"][0]
    assert event["op"] == "commit" and event["outcome"] == "failed"
    assert records[-1]["status"] == "failed" and conn.closed
    assert any("commit rejected" in msg for msg in messages)


@pytest.mark.parametrize("change", [lambda s: s[0].update(db="mysql-source"),
    lambda s: s[0].update(unknown=1), lambda s: s[0].update(op="sql"),
    lambda s: s[5].update(name="missing"), lambda s: s.pop(),
    lambda s: s[2].update(file="../escape.sql")])
def test_invalid_transaction_schedule(change, tmp_path):
    s = steps(); change(s)
    with pytest.raises(ValueError, match="invalid-transaction-seed"):
        transactions.normalize(s, tmp_path / "test.yaml", recover=True, lifecycle=True)
