"""Owned PostgreSQL seed sessions with explicit transaction boundaries."""
import math
import json
import re
import time
from pathlib import Path
from datetime import datetime, timezone

from livetest import lifecycle as _lifecycle
from livetest.resultschema import build_assertion_result
from livetest.sqlutil import split_sql_statements
from livetest.substitute import render

OPS = {"begin", "sql", "commit", "rollback", "savepoint", "rollback_to", "release"}
PHASES = {"pre_deploy": 0, "post_start": 1, "post_recover": 2}
IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,62}\Z")


def normalize_observe(raw):
    def refuse(message):
        raise ValueError("invalid-transaction-observe: " + message)
    if not isinstance(raw, dict) or set(raw) - {"tables", "slot"}:
        refuse("only tables and optional slot are allowed")
    tables = raw.get("tables")
    if not isinstance(tables, list) or not 1 <= len(tables) <= 16:
        refuse("declare 1..16 source table/key pairs")
    for ref in tables:
        if not isinstance(ref, dict) or set(ref) != {"table", "key"}:
            refuse("each source table needs exactly table and key")
        if not isinstance(ref["table"], str) or "${TID}" not in ref["table"]:
            refuse("table must contain ${TID}")
        if not isinstance(ref["key"], str) or not IDENT.fullmatch(ref["key"]):
            refuse("key must be a plain column identifier")
    if "slot" in raw and (not isinstance(raw["slot"], str) or
        (raw["slot"] != "${PG_SLOT}" and "${TID}" not in raw["slot"])):
        refuse("slot must use the owned slot token or ${TID}")
    return raw


def normalize(raw, path, *, recover=False, lifecycle=False):
    if raw is None:
        return []
    def refuse(message):
        raise ValueError(f"invalid-transaction-seed: {path}: {message}")
    if not isinstance(raw, list) or not raw:
        refuse("transactions must be a non-empty ordered list")
    active, out, phase = {}, [], -1
    for i, step in enumerate(raw):
        if not isinstance(step, dict) or set(step) - {"session", "op", "db", "file", "name", "when", "after", "observe"}:
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
        allowed = {"session", "op", "when", "after"} | ({"db", "observe"} if op == "begin" else {"file"} if op == "sql"
                       else {"name"} if op in ("savepoint", "rollback_to", "release") else set())
        if set(step) - allowed:
            refuse(f"step {i} has keys not used by {op}")
        if op == "begin":
            if session in active or step.get("db") not in ("postgres-source", "postgres-target"):
                refuse("begin needs a closed session and a Postgres db route")
            if "observe" in step:
                normalize_observe(step["observe"])
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
    def __init__(self, admins, tokens, *, source_sql, timeout=120, connect=None, record=None, trace=None, report=None, owned=None, probe=None):
        self.admins, self.tokens, self.source_sql = admins, tokens, source_sql
        self.timeout, self.connect, self.record = timeout, connect or connect_session, record
        self.sessions, self.routes = {}, {}
        self.trace, self.report = trace, report
        self.owned, self.probe = owned, probe or _lifecycle.make_probe
        self.observers = {}
        self.active_observers = set()

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
                self.routes.pop(name, None)
        if problems:
            raise ValueError("transaction-cleanup-error: " + "; ".join(problems))

    def _prepare_observe(self, route, raw):
        normalize_observe(raw)
        spec = {"tables": [{"table": render(r["table"], self.tokens), "key": r["key"]} for r in raw["tables"]]}
        for ref in spec["tables"]:
            _lifecycle._table(ref["table"])
            if self.owned is None or not self.owned("pg-table", route, ref["table"]):
                raise ValueError("transaction-observe-not-owned: " + ref["table"])
        if "slot" in raw:
            slot = render(raw["slot"], self.tokens)
            if not IDENT.fullmatch(slot) or self.owned is None or not self.owned("pg-slot", route, slot):
                raise ValueError("transaction-observe-not-owned: slot")
            spec["slot"] = slot
        return spec

    def _read_observe(self, route, spec, remaining=2.0):
        from livetest.diagnostics import read_keys
        deadline = time.monotonic() + remaining
        def left():
            seconds = deadline - time.monotonic()
            if seconds <= 0:
                raise ValueError("transaction-observe-deadline")
            return seconds
        probe = self.probe(self.admins[route]["admin"])
        result = {"tables": [read_keys(probe, ref["table"], ref["key"], left()) for ref in spec["tables"]]}
        if "slot" in spec:
            rows = probe.query("SELECT confirmed_flush_lsn::text, restart_lsn::text FROM pg_replication_slots WHERE slot_name = %s",
                               (spec["slot"],), left())
            if len(rows) != 1:
                raise ValueError("transaction-observe-slot-missing")
            result["slot"] = {"name": spec["slot"], "confirmedFlushLsn": rows[0][0], "restartLsn": rows[0][1]}
        left()
        result["observedUtc"] = datetime.now(timezone.utc).isoformat()
        return result

    def observe_recovery(self, phase, remaining=2.0):
        seen = set()
        for route, spec in self.observers.values():
            identity = (route, json.dumps(spec, sort_keys=True))
            if identity in seen:
                continue
            seen.add(identity)
            event, ok, detail = {"phase": phase}, False, "ok"
            try:
                event.update(self._read_observe(route, spec, remaining))
                ok = True
            except Exception as e:
                detail = f"transaction-observe-error: {e}"
                raise
            finally:
                if self.trace:
                    self.trace(build_assertion_result(type="transaction", target=route, db=route,
                        spec={"phase": phase}, status="passed" if ok else "failed", detail=detail,
                        expected={"kind": "count", "count": 1}, actual={"kind": "rows", "rows": [event]}))

    def _step(self, step, event):
        name, op = step["session"], step["op"]
        if op == "begin":
            self.active_observers.discard(name)
            if "observe" in step:
                self.observers[name] = (step["db"], self._prepare_observe(step["db"], step["observe"]))
                self.active_observers.add(name)
            self.sessions[name] = self.connect(self.admins[step["db"]]["admin"], self.timeout)
            self.routes[name] = step["db"]
            return
        conn = self.sessions[name]
        if op in ("commit", "rollback"):
            observation = self.observers.get(name) if op == "commit" and name in self.active_observers else None
            if observation:
                cur = conn.cursor()
                try:
                    cur.execute("SELECT txid_current()")
                    event["txid"] = int(cur.fetchone()[0])
                finally:
                    cur.close()
            getattr(conn, op)()
            if op == "commit":
                event["commitSucceeded"] = True
            conn.close()
            del self.sessions[name]
            self.routes.pop(name, None)
            self.active_observers.discard(name)
            if observation:
                route, spec = observation
                probe = self.probe(self.admins[route]["admin"])
                event["commitStatus"] = probe.query("SELECT txid_status(%s)", (event["txid"],), 2.0)[0][0]
                if event["commitStatus"] != "committed":
                    raise ValueError("transaction-commit-not-confirmed")
                event["source"] = self._read_observe(route, spec)
            return
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
            if cur is not conn:
                cur.close()

    def _say(self, text):
        if self.report:
            from livetest.evidence import known_secrets, redact_text
            self.report(redact_text(text, known_secrets()))

    def run_phase(self, steps, when):
        from livetest.resultschema import build_assertion_result
        started = time.monotonic()
        try:
            for step in steps:
                if step.get("when", "post_start") != when:
                    continue
                left = started + step.get("after", 0) - time.monotonic()
                if left > 0:
                    time.sleep(left)
                name, op = step["session"], step["op"]
                route = step.get("db", self.routes.get(name))
                began = time.monotonic()
                event = {"session": name, "op": op, "phase": when,
                         "startedAt": datetime.now(timezone.utc).isoformat(), "outcome": "failed"}
                self._say(f"transactions: session={name} op={op} phase={when} startedAt={event['startedAt']}")
                detail = "ok"
                try:
                    self._step(step, event)
                    event["outcome"] = "succeeded"
                except BaseException as e:
                    detail = f"transaction-step-error: {type(e).__name__}: {e}"
                    raise
                finally:
                    event["endedAt"] = datetime.now(timezone.utc).isoformat()
                    event["durationS"] = time.monotonic() - began
                    self._say(f"transactions: session={name} op={op} phase={when} "
                              f"startedAt={event['startedAt']} endedAt={event['endedAt']} "
                              f"outcome={event['outcome']} detail={detail}")
                    if self.trace:
                        self.trace(build_assertion_result(type="transaction", target=name, db=route,
                            spec=dict(step), status="passed" if event["outcome"] == "succeeded" else "failed",
                            detail=detail, expected={"kind": "rows", "rows": [{"outcome": "succeeded"}]},
                            actual={"kind": "rows", "rows": [event]}))
        except BaseException:
            self.close()
            raise
