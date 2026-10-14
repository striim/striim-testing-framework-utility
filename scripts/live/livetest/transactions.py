"""Owned PostgreSQL seed sessions with explicit transaction boundaries."""
import math
import re
import time
from pathlib import Path

from livetest import lifecycle as _lifecycle
from livetest.sqlutil import split_sql_statements
from livetest.substitute import render

OPS = {"begin", "sql", "commit", "rollback", "savepoint", "rollback_to", "release"}
PHASES = {"pre_deploy": 0, "post_start": 1, "post_recover": 2}
IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,62}\Z")


def normalize(raw, path, *, recover=False, lifecycle=False):
    if raw is None:
        return []
    def refuse(message):
        raise ValueError(f"invalid-transaction-seed: {path}: {message}")
    if not isinstance(raw, list) or not raw:
        refuse("transactions must be a non-empty ordered list")
    active, out, phase = {}, [], -1
    for i, step in enumerate(raw):
        if not isinstance(step, dict) or set(step) - {"session", "op", "db", "file", "name", "when", "after"}:
            refuse(f"step {i} has unknown keys or is not a mapping")
        session, op, when = step.get("session"), step.get("op"), step.get("when", "post_start")
        if not isinstance(session, str) or not IDENT.fullmatch(session) or op not in OPS or when not in PHASES:
            refuse(f"step {i} needs a valid session, op and when")
        if PHASES[when] < phase:
            refuse("steps must be in pre_deploy, post_start, post_recover order")
        phase = PHASES[when]
        if when == "post_recover" and not recover:
            refuse("post_recover requires recover")
        if when != "pre_deploy" and not lifecycle:
            refuse("post-start transactions require lifecycle readiness")
        try:
            after = _lifecycle.parse_duration(step.get("after", 0), "transactions.after", path)
        except ValueError as e:
            refuse(str(e))
        if when == "pre_deploy" and after:
            refuse("pre_deploy has no after clock")
        allowed = {"session", "op", "when", "after"} | ({"db"} if op == "begin" else {"file"} if op == "sql"
                       else {"name"} if op in ("savepoint", "rollback_to", "release") else set())
        if set(step) - allowed:
            refuse(f"step {i} has keys not used by {op}")
        if op == "begin":
            if session in active or step.get("db") not in ("postgres-source", "postgres-target"):
                refuse("begin needs a closed session and a Postgres db route")
            active[session] = []
        else:
            if session not in active:
                refuse(f"session {session} is not open")
            if op == "sql":
                file = step.get("file")
                if not isinstance(file, str) or not file or Path(file).is_absolute() or ".." in Path(file).parts:
                    refuse("sql needs a case-relative file")
            elif op in ("savepoint", "rollback_to", "release"):
                name = step.get("name")
                if not isinstance(name, str) or not IDENT.fullmatch(name):
                    refuse("savepoint name must be a plain identifier")
                if op == "savepoint":
                    if name in active[session]:
                        refuse("savepoint names must be unique within a transaction")
                    active[session].append(name)
                else:
                    if name not in active[session]:
                        refuse("savepoint does not exist")
                    index = active[session].index(name)
                    active[session] = active[session][:index + (op == "rollback_to")]
            else:
                del active[session]
        out.append({**step, "when": when, "after": after})
    if active:
        refuse(f"sessions never closed: {sorted(active)}")
    return out


def connect_session(admin, timeout):
    probe = _lifecycle.PgProbe(admin)
    conn = admin._connect(**probe.params, connect_timeout=max(1, math.ceil(timeout)))
    conn.autocommit = False
    cur = conn.cursor()
    try:
        cur.execute("SET LOCAL statement_timeout = %s", (max(1, int(timeout * 1000)),))
        cur.execute("SET LOCAL lock_timeout = %s", (max(1, int(timeout * 1000)),))
        cur.execute(f'SET LOCAL search_path TO "{admin._schema()}"')
    except BaseException:
        conn.close()
        raise
    finally:
        cur.close()
    return conn


class Pool:
    def __init__(self, admins, tokens, *, source_sql, timeout=120, connect=None, record=None):
        self.admins, self.tokens, self.source_sql = admins, tokens, source_sql
        self.timeout, self.connect, self.record = timeout, connect or connect_session, record
        self.sessions = {}

    def close(self):
        problems = []
        for name, conn in list(self.sessions.items()):
            try:
                conn.rollback()
            except Exception as e:
                problems.append(f"{name}: rollback failed: {e}")
            finally:
                try:
                    conn.close()
                except Exception as e:
                    problems.append(f"{name}: close failed: {e}")
                del self.sessions[name]
        if problems:
            raise ValueError("transaction-cleanup-error: " + "; ".join(problems))

    def run_phase(self, steps, when):
        started = time.monotonic()
        try:
            for step in steps:
                if step.get("when", "post_start") != when:
                    continue
                left = started + step.get("after", 0) - time.monotonic()
                if left > 0:
                    time.sleep(left)
                name, op = step["session"], step["op"]
                if op == "begin":
                    self.sessions[name] = self.connect(self.admins[step["db"]]["admin"], self.timeout)
                    continue
                conn = self.sessions[name]
                if op in ("commit", "rollback"):
                    getattr(conn, op)()
                    conn.close()
                    del self.sessions[name]
                    continue
                cur = conn.cursor()
                try:
                    if op == "sql":
                        template = self.source_sql(step["file"])
                        sql = render(template, self.tokens)
                        statements = split_sql_statements(sql)
                        if any(s.split(None, 1)[0].upper() in ("BEGIN", "START", "COMMIT", "END", "ROLLBACK", "SAVEPOINT", "RELEASE", "PREPARE") for s in statements):
                            raise ValueError("transaction-control-in-sql: use explicit transaction steps")
                        for statement in statements:
                            cur.execute(statement)
                        if self.record:
                            self.record("seed", step["file"], sql, template=template)
                    else:
                        command = {"savepoint": "SAVEPOINT", "rollback_to": "ROLLBACK TO SAVEPOINT", "release": "RELEASE SAVEPOINT"}[op]
                        cur.execute(f'{command} "{step["name"]}"')
                finally:
                    # Real cursors have independent lifetime; a fake may be its connection.
                    if cur is not conn:
                        cur.close()
        except BaseException:
            self.close()
            raise
