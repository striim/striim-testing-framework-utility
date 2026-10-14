"""Check whole target snapshots against declared committed transaction prefixes."""
import math
import threading
import time
from pathlib import Path
from datetime import datetime, timezone

from livetest import canon, diagnostics, exactdata, lifecycle as _lifecycle
from livetest.assertions import AssertionFailed
from livetest.resultschema import build_assertion_result
from livetest.substitute import render


def normalize(raw, path, *, lifecycle=False):
    if raw is None:
        return None
    def refuse(message):
        raise ValueError(f"invalid-snapshot-spec: {path}: {message}")
    if not isinstance(raw, dict) or set(raw) - {"db", "tables", "prefixes", "interval", "max_rows", "max_bytes", "max_snapshots"}:
        refuse("unknown snapshot keys or invalid mapping")
    if not lifecycle or raw.get("db") not in canon.DB_ROUTES:
        refuse("snapshot observer requires lifecycle and a supported exact database route")
    tables = raw.get("tables")
    if not isinstance(tables, dict) or not tables:
        refuse("tables must be a non-empty mapping")
    for alias, table in tables.items():
        if not isinstance(alias, str) or not alias or not isinstance(table, dict) or not {"target", "columns"} <= set(table) or set(table) - {"target", "columns", "keys"}:
            refuse("each table alias needs target and columns, with optional keys")
        if not isinstance(table["target"], str) or "${TID}" not in table["target"]:
            refuse("each target must contain ${TID}")
        if not isinstance(table["columns"], dict) or not table["columns"]:
            refuse("each table needs declared columns")
        try:
            canon.declaration({"columns": table["columns"]}, db_route=True)
        except ValueError as e:
            refuse(str(e))
        if "keys" in table:
            keys = table["keys"]
            if not isinstance(keys, list) or not keys or any(not isinstance(k, str) or k not in table["columns"] for k in keys) or len(set(keys)) != len(keys):
                refuse("table keys must be a non-empty list of unique declared columns")
    prefixes = raw.get("prefixes")
    if not isinstance(prefixes, list) or not prefixes:
        refuse("prefixes must be a non-empty list of complete states")
    for prefix in prefixes:
        if not isinstance(prefix, dict) or set(prefix) != set(tables):
            refuse("every prefix must name every table alias")
        for file in prefix.values():
            if not isinstance(file, str) or not file or Path(file).is_absolute() or ".." in Path(file).parts:
                refuse("prefix goldens must be case-relative paths")
    try:
        interval = _lifecycle.parse_duration(raw.get("interval", "100ms"), "snapshot.interval", path)
    except ValueError as e:
        refuse(str(e))
    if interval <= 0:
        refuse("interval must be positive")
    for key, default, maximum in (("max_rows", canon.DEFAULT_MAX_ROWS, canon.MAX_MAX_ROWS),
                                  ("max_bytes", canon.DEFAULT_MAX_BYTES, canon.MAX_MAX_BYTES),
                                  ("max_snapshots", 10000, 100000)):
        value = raw.get(key, default)
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
            refuse(f"{key} is outside its supported budget")
    return dict(raw)


def read_snapshot(spec, admins, tokens, remaining):
    route = spec["db"]
    admin = _lifecycle._admin(admins, route)
    mr, mb = spec.get("max_rows", canon.DEFAULT_MAX_ROWS), spec.get("max_bytes", canon.DEFAULT_MAX_BYTES)
    tables = {alias: render(table["target"], tokens) for alias, table in spec["tables"].items()}
    if len(set(tables.values())) != len(tables):
        raise ValueError("snapshot-target-ambiguous: two aliases name the same table")
    if route.startswith("postgres-"):
        def read(conn, cancelled):
            result = {}
            for index, (alias, table) in enumerate(tables.items()):
                names, values = _lifecycle.exact_read(conn, _lifecycle._table(table), None, mr,
                    exactdata.TRANSFER_FACTOR * mb, cancelled, start_snapshot=index == 0)
                if len(set(names)) != len(names):
                    raise ValueError("snapshot-duplicate-column")
                result[alias] = [dict(zip(names, row)) for row in values]
            return result
        result = _lifecycle.PgProbe(admin)._call(None, None, remaining, fetch=True, read=read)
    else:
        result, started = {}, time.monotonic()
        with admin._db().snapshot(multi_use=True) as snapshot:
            for alias, table in tables.items():
                result[alias] = exactdata.read_spanner(admin, table, None, mr, mb,
                    max(0.05, remaining - (time.monotonic() - started)), snapshot=snapshot)
    if sum(len(rows) for rows in result.values()) > mr:
        raise ValueError("snapshot-limit-exceeded: total rows exceed max_rows")
    return result


class Observer:
    def __init__(self, spec, *, admins, tokens, case_dir, inputs, read=None, owned=None, timeout=120, report=None):
        self.spec, self.admins, self.tokens, self.timeout = spec, admins, tokens, timeout
        self.read = read or read_snapshot
        self.history, self.error = [], None
        self.report, self.finalized = report, False
        self.stop_event, self.first_read = threading.Event(), threading.Event()
        self.thread = None
        self.declarations = {a: canon.declaration({"columns": t["columns"]}, db_route=True)
                             for a, t in spec["tables"].items()}
        self.prefixes = [{alias: canon.parse_golden(inputs.golden(Path(case_dir) / file),
                        db_route=True, tokens=tokens) for alias, file in prefix.items()}
                        for prefix in spec["prefixes"]]
        if owned is not None:
            for table in spec["tables"].values():
                _rendered, _sql, name, kind = exactdata._route_ref(tokens, table["target"], spec["db"])
                if not owned(kind, spec["db"], name):
                    raise ValueError(f"snapshot-target-not-owned: {name}")

    def record(self, ok, detail):
        return build_assertion_result(type="data", status="passed" if ok else "failed", spec=self.spec,
            target=f"snapshot:{self.spec['db']}", db=self.spec["db"], detail=detail,
            expected={"kind": "count", "count": len(self.prefixes)},
            actual={"kind": "rows", "rows": list(self.history)})

    def fail(self, detail):
        raise AssertionFailed(detail, [self.record(False, detail)])

    def sample(self):
        if len(self.history) >= self.spec.get("max_snapshots", 10000):
            self.fail("snapshot-limit-exceeded: max_snapshots reached")
        try:
            rows = self.read(self.spec, self.admins, self.tokens, self.timeout)
            if set(rows) != set(self.declarations):
                self.fail("snapshot-table-set-mismatch")
            for alias, values in rows.items():
                if any(set(r) - set(self.declarations[alias].columns) for r in values):
                    self.fail("snapshot-undeclared-column")
            mr, mb = self.spec.get("max_rows", canon.DEFAULT_MAX_ROWS), self.spec.get("max_bytes", canon.DEFAULT_MAX_BYTES)
            choices = []
            hashes = None
            for index, prefix in enumerate(self.prefixes):
                comps = [canon.compare(prefix[a], rows[a], d, max_rows=mr, max_bytes=mb)
                         for a, d in self.declarations.items()]
                hashes = [c["actual"]["sha256"] for c in comps]
                if all(c["equal"] for c in comps):
                    choices.append(index)
            entry = {"at": time.monotonic(), "observedUtc": datetime.now(timezone.utc).isoformat(), "prefix": max(choices) if choices else None,
                     "sha256": canon.aggregate(hashes), "rows": sum(len(v) for v in rows.values())}
            self.history.append(entry)
            if not choices:
                entry["tables"] = {}
                for alias, values in rows.items():
                    names = self.spec["tables"][alias].get("keys", list(self.declarations[alias].columns))
                    samples, clipped = [], False
                    for row in values[:diagnostics.MAX_KEYS]:
                        cells = canon.canon_row({k: row[k] for k in names if k in row}, self.declarations[alias], "actual")
                        text = {cell[0]: cell[-1] for cell in cells}
                        keys = [text.get(k) for k in names]
                        clipped = clipped or any(v is not None and len(v) > diagnostics.MAX_KEY_CHARS for v in keys)
                        samples.append([v[:diagnostics.MAX_KEY_CHARS] if v is not None else None for v in keys])
                    entry["tables"][alias] = {"keyColumns": names, "keys": samples, "rows": len(values),
                        "keysTruncated": len(values) > diagnostics.MAX_KEYS, "valuesTruncated": clipped}
                self.fail("snapshot-not-committed-prefix: related tables show an undeclared state")
        except AssertionFailed:
            raise
        except Exception as e:
            self.fail(f"snapshot-read-error: {e}")

    def start(self):
        self.sample()
        def work():
            try:
                while not self.stop_event.is_set():
                    self.sample()
                    self.first_read.set()
                    seconds = _lifecycle.parse_duration(self.spec.get("interval", "100ms"), "snapshot.interval", "observer")
                    if self.stop_event.wait(seconds):
                        break
            except BaseException as e:
                self.error = e
                if self.report:
                    try:
                        from livetest.striim import redact
                        self.report(redact(f"snapshot observer failed: {e}"))
                    except Exception:
                        pass
                self.first_read.set()
        self.thread = threading.Thread(target=work, daemon=True, name="snapshot-observer")
        self.thread.start()
        if not self.first_read.wait(self.timeout + _lifecycle.CANCEL_BOUND_S * 3):
            self.fail("snapshot-read-timeout: initial worker read did not finish")
        if self.error:
            raise self.error

    def stop(self):
        self.stop_event.set()
        if self.thread is not None:
            self.thread.join(self.timeout + _lifecycle.CANCEL_BOUND_S * 3)
            if self.thread.is_alive():
                self.fail("snapshot-read-timeout: worker did not finish")

    def abandon(self, reason):
        if self.finalized:
            return None
        try:
            self.stop()
        except Exception as e:
            self.error = self.error or e
        self.finalized = True
        return self.record(False, f"observer-incomplete: {self.error or reason}")

    def finish(self):
        try:
            self.stop()
            if self.error:
                raise self.error
            self.sample()
            if self.history[-1]["prefix"] != len(self.prefixes) - 1:
                self.fail("snapshot-final-prefix-missing")
        except AssertionFailed:
            self.finalized = True  # the raised failure already carries the history record
            raise
        self.finalized = True
        return [self.record(True, f"{len(self.history)} consistent snapshots match declared committed prefixes")]
