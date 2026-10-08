"""The ownership ledger: acquired per object, exact-name reset and cleanup, cleanup as part of the
result (contract set 1.7.0, C7.6).

A run deletes only what its persisted ledger records as ``confirmed`` for its identity, by exact name,
on the exact target it was created on. Nothing is ever deleted because of a prefix, a glob, a
computed name or an unresolved intent:

* **Acquire before create.** Each DDL file is rendered and scanned for the Postgres tables, views
  (plain and materialized) and replication slots it creates. Before it runs, the catalog is read; an
  object that already exists and is not in this identity's ledger is a ``collision`` and the file is
  refused, leaving the object untouched. After it runs (or fails), the catalog is read back and only
  objects that now exist are ``confirmed``. ``CREATE PUBLICATION`` / ``CREATE SCHEMA`` are refused
  before execution (unsupported resource type).
* **Never CASCADE.** Owned views drop before owned tables, each kind in reverse creation order, so a
  case's own views and foreign keys are gone before what they read. Anything else that still depends
  on an owned relation makes Postgres refuse the drop: the relation is left in place and the cleanup
  fails as ``not-owned-dependent``, naming the dependent.
* **Unsupported kinds are never deleted.** Tables of other engines, Kafka topics, GCS buckets and OP
  uploads cannot be acquired by the ledger (unsupported resource type): they are left in place and recorded as
  verification gaps, so such a run never qualifies.
* **Identity and target binding.** A ledger file is imported only for the same full identity and
  ledger version. Every attempt records its targets (Postgres endpoint and system identifier, the
  Striim URL, the docker app-node containers and their ids). A prior attempt's entry is reset or
  replayed only when its recorded target is verified to be the one being deleted from.
* **Files.** A lifecycle case writes only inside the framework-allocated ``${OWNED_DIR}``, created
  with an exclusive ``mkdir`` on each node (refused if it exists or a node cannot be read) and
  removed as one unit from the nodes it was created on. A legacy case's output and server files are
  claimed by exact rendered path; any other match of the same glob (rolled parts, siblings) is
  reported. A native Striim's file is claimed, deleted and verified on the local filesystem when the
  server is on this host (its URL is the loopback or this host); a remote native server's file is a
  named ``native-remote-file`` gap and is never deleted. OP checkpoints are listed and deleted by exact name only when ``<ns>.`` is a delimited
  component. An inventory that cannot be read is never taken as absence.
* **Cleanup** deletes confirmed entries in order under a cleanup deadline, re-reads each one, and
  returns a ``CleanupResult`` (``ok|failed|skipped``, verified or with named verification gaps).
  It never raises: an unexpected error becomes a failed result. Foreign lookalikes are reported and
  never touched. ``SLT_LIFECYCLE_FAULT=cleanup:<kind>`` fails that kind's first owned delete (the
  cleanup-failure control; such a run never qualifies). ``python -m livetest.ownership replay
  <ledger.json>`` repeats the cleanup for a kept or fault-injected run.

``livetest.plugin`` reaches this module through ``lifecycle-hooks@1``.
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import os
import posixpath
import re
import socket
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse

from livetest import lifecycle as _lifecycle
from livetest import runident, stack

LEDGER_VERSION = 2
PG_ROUTES = ("postgres-source", "postgres-target")
FAULT_ENV = "SLT_LIFECYCLE_FAULT"
OWNED_ROOT = "/opt/striim/slt-runs/"
DOCKER_HOME = "/opt/striim"
CHECKPOINT_SUFFIXES = (".position.json", ".position.bk", ".cursors.json", ".cursors.bk")
# An OP that cannot resolve its app or component name falls back to a bare letters-and-digits prefix.
_BARE_CHECKPOINT = re.compile(r"^[A-Za-z0-9]+\.")
# Only an object this run is known to have created is deletion authority; an ``intended`` entry
# (the process stopped between intent and confirmation) is never deleted.
DELETABLE_STATES = ("confirmed", "delete-failed")
UNSUPPORTED_KINDS = ("engine-tables", "topic", "bucket", "upload")   # not acquirable in 4.1 (F2): never deleted
FILE_KINDS = ("file-output", "server-file", "owned-dir")
# Views before tables, and within each kind the reverse of creation order: a view or foreign key can
# only name a relation that already existed, so every owned dependent is dropped before what it reads.
CLEANUP_ORDER = ("namespace", "pg-view", "pg-table", "pg-slot", "engine-tables", "topic", "bucket", "file-output",
                 "server-file", "owned-dir", "upload")
FAULT_ALIASES = {"table": "pg-table", "slot": "pg-slot", "dir": "owned-dir", "file": "file-output"}
DOCKER_TIMEOUT_S = 60.0         # every docker command is killed after this
CLEANUP_OP_S = 60.0             # one delete or verification read
CLEANUP_DEADLINE_S = 600.0      # the whole cleanup of one case

_CREATE_TABLE = re.compile(r'\bCREATE\s+(?:UNLOGGED\s+)?TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?'
                           r'((?:"?[A-Za-z_][A-Za-z0-9_]*"?\.)?"?[A-Za-z_][A-Za-z0-9_]*"?)', re.I)
_CREATE_VIEW = re.compile(r'\bCREATE\s+(?:OR\s+REPLACE\s+)?(?:RECURSIVE\s+)?(MATERIALIZED\s+)?VIEW\s+'
                          r'(?:IF\s+NOT\s+EXISTS\s+)?((?:"?[A-Za-z_][A-Za-z0-9_]*"?\.)?"?[A-Za-z_][A-Za-z0-9_]*"?)', re.I)
_CREATE_SLOT = re.compile(r"pg_create_logical_replication_slot\s*\(\s*'([A-Za-z_][A-Za-z0-9_]*)'", re.I)
_UNSUPPORTED = re.compile(r"\bCREATE\s+(PUBLICATION|SCHEMA)\b", re.I)
_SAFE_PATH = re.compile(r"^[A-Za-z0-9_./-]+\Z")
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\Z")
_EXISTS_SH = 'if [ -e "$1" ]; then echo slt-present; else echo slt-absent; fi'
_LS_DONE = "slt-ls-done"


class OwnershipError(AssertionError):
    """A refused acquisition (collision, unsupported resource, unparseable catalog, unreadable
    inventory, foreign ledger). The object is left untouched and the case fails before creating it."""


class CleanupError(AssertionError):
    """Raised by the plugin after the ``finally`` when cleanup failed and nothing else was in flight."""


class Unsupported(RuntimeError):
    """A kind this ledger cannot delete in 4.1: a verification gap, never a delete."""


class InventoryError(RuntimeError):
    """A node's listing or existence check could not be read (never treated as absence)."""


def _docker(argv):
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=DOCKER_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return SimpleNamespace(returncode=124, stdout="", stderr=f"timed out after {DOCKER_TIMEOUT_S:.0f}s")


def _now() -> str:
    return _lifecycle.now_iso()


def _safe_path(path: str) -> str:
    if not _SAFE_PATH.match(path or "") or ".." in path.split("/"):
        raise OwnershipError(f"unsafe server path {path!r} (allowed: letters, digits, _ . / -)")
    return path


def native_local(url) -> bool:
    """A native Striim on this host: its URL names the loopback or this host, so its FileWriter writes the
    local filesystem, which is where native mode reads and places server files (``striimfile``)."""
    host = (urlparse(url or "").hostname or "").lower()
    return host in ("localhost", "::1") or host.startswith("127.") \
        or host in (socket.gethostname().lower(), socket.getfqdn().lower())


def local_ls(path: str) -> list:
    """The local matches of ``<path>*`` (the native counterpart of ``docker_ls``)."""
    parent, base = os.path.split(path)
    try:
        names = os.listdir(parent or ".")
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise InventoryError(f"listing {parent} could not be read: {exc}") from None
    return sorted(os.path.join(parent, n) for n in names if n.startswith(base))


def local_rm(path: str) -> None:
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass


def make_catalog(admin):
    """A bounded, cancellable admin connection for catalog reads and exact-name drops."""
    return _lifecycle.PgProbe(admin, which="admin")


def _schema(admin) -> str:
    role = getattr(admin, "role", "source")
    return admin.dsn.get(f"{role}_schema", "qasource" if role == "source" else "qatarget")


def _table_name(admin, raw: str) -> str:
    quoted = '"' in raw
    name = raw.replace('"', "")
    schema, table = name.split(".", 1) if "." in name else (_schema(admin), name)
    if not quoted:
        schema, table = schema.lower(), table.lower()
    if not (_IDENT.match(schema) and _IDENT.match(table)):
        raise OwnershipError(f"unsafe table name {raw!r}")
    return f"{schema}.{table}"


def _quoted(name: str) -> str:
    schema, table = name.split(".", 1)
    return f'"{schema}"."{table}"'


def pg_tables_present(catalog, names) -> set:
    names = sorted(set(names))
    if not names:
        return set()
    rows = catalog.query("SELECT schemaname, tablename FROM pg_tables WHERE (schemaname || '.' || tablename) = ANY(%s)",
                         (names,), 30.0)
    return {f"{s}.{t}" for s, t in rows}


def pg_views_present(catalog, names) -> dict:
    """{name: materialized} of the views and materialized views among ``names`` that exist."""
    names = sorted(set(names))
    if not names:
        return {}
    rows = catalog.query("SELECT schemaname, viewname, false FROM pg_views WHERE (schemaname || '.' || viewname) = ANY(%s) "
                         "UNION ALL SELECT schemaname, matviewname, true FROM pg_matviews "
                         "WHERE (schemaname || '.' || matviewname) = ANY(%s)", (names, names), 30.0)
    return {f"{s}.{v}": bool(m) for s, v, m in rows}


def pg_tables_like(catalog, schema: str, pattern: str) -> set:
    rows = catalog.query("SELECT schemaname, tablename FROM pg_tables WHERE schemaname = %s AND tablename LIKE %s ESCAPE '\\'",
                         (schema, pattern), 30.0)
    return {f"{s}.{t}" for s, t in rows}


def pg_slot_present(catalog, name: str) -> bool:
    rows = catalog.query("SELECT count(*) FROM pg_replication_slots WHERE slot_name = %s", (name,), 30.0)
    return int(rows[0][0]) > 0


def pg_system_identifier(catalog):
    """The backend's immutable ``system_identifier`` (a new initdb has a new one), or None if unreadable."""
    try:
        rows = catalog.query("SELECT system_identifier::text FROM pg_control_system()", None, 30.0)
        return str(rows[0][0]) if rows and rows[0][0] is not None else None
    except Exception:                   # noqa: BLE001 - unreadable identity refuses resets, never guesses
        return None


def _like_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def namespace_present(client, ns: str):
    """True/False from ``LIST NAMESPACES``, or None when the reply cannot be read (never assumed absent).
    The live response shape is UNVERIFIED -- confirm on a live cluster."""
    try:
        resp = client.api.post_tungsten_line("LIST NAMESPACES;")
    except Exception:                   # noqa: BLE001
        return None
    if not isinstance(resp, list) or not resp:
        return None
    names = set()

    def walk(value):
        if isinstance(value, str):
            names.add(value.upper())
        elif isinstance(value, dict):
            for v in value.values():
                walk(v)
        elif isinstance(value, list):
            for v in value:
                walk(v)
    for entry in resp:
        if not isinstance(entry, dict) or entry.get("executionStatus") == "Failure" or "output" not in entry:
            return None
        walk(entry["output"])
    return ns.upper() in names


def _nodes():
    return stack.app_nodes()


def container_ids(nodes) -> dict:
    """{node container name: container id, or None when it cannot be inspected}."""
    ids = {}
    for n in nodes:
        r = _docker(["docker", "inspect", "-f", "{{.Id}}", n])
        out = (getattr(r, "stdout", "") or "").strip()
        ids[n] = out if getattr(r, "returncode", 1) == 0 and out else None
    return ids


def docker_exists(path: str, nodes=None):
    """True (present on a node), False (absent on every node) or None (a node could not be read)."""
    unreadable = False
    for n in (nodes if nodes is not None else _nodes()):
        r = _docker(["docker", "exec", n, "sh", "-c", _EXISTS_SH, "sh", path])
        out = (getattr(r, "stdout", "") or "").strip()
        ok = getattr(r, "returncode", 1) == 0
        if ok and out == "slt-present":
            return True
        if not (ok and out == "slt-absent"):
            unreadable = True
    return None if unreadable else False


def docker_ls(pattern: str, nodes=None) -> list:
    """The listed matches on every node; ``InventoryError`` when any node's listing cannot be read."""
    found = set()
    for n in (nodes if nodes is not None else _nodes()):
        r = _docker(["docker", "exec", n, "sh", "-c", f"ls -1d {pattern} 2>/dev/null; echo {_LS_DONE}"])
        lines = [ln.strip() for ln in (getattr(r, "stdout", "") or "").splitlines() if ln.strip()]
        if getattr(r, "returncode", 1) != 0 or not lines or lines[-1] != _LS_DONE:
            raise InventoryError(f"listing {pattern} on {n} could not be read (rc {getattr(r, 'returncode', None)}): "
                                 f"{(getattr(r, 'stderr', '') or '').strip()}")
        found.update(lines[:-1])
    return sorted(found)


def docker_rm(path: str, recursive: bool = False, nodes=None) -> None:
    flag = "-rf" if recursive else "-f"
    for n in (nodes if nodes is not None else _nodes()):
        r = _docker(["docker", "exec", n, "rm", flag, "--", path])
        if getattr(r, "returncode", 0):
            raise RuntimeError(f"rm {flag} {path} on {n} failed: {(getattr(r, 'stderr', '') or '').strip()}")


def needs_owned_dir(m) -> bool:
    spec = getattr(m, "lifecycle", None)
    return spec is not None and (spec.sink == "file" or bool(m.assert_.get("file"))
                                 or any(not load for _f, _d, _w, load in m.server_files))


def snapshot_targets(admins, ctx, nodes=None, env=None) -> dict:
    """The targets a delete would reach right now: Postgres routes (endpoint + system identifier),
    the Striim URL, the docker app nodes (names + container ids)."""
    routes = {}
    for route, e in (admins or {}).items():
        admin = (e or {}).get("admin")
        dsn = getattr(admin, "dsn", None)
        if route in PG_ROUTES and isinstance(dsn, dict):
            rec = {k: v for k, v in dsn.items() if "password" not in k}
            rec["role"] = getattr(admin, "role", "source")
            rec["systemIdentifier"] = pg_system_identifier(make_catalog(admin))
            routes[route] = rec
    striim = None
    if ctx is not None:
        striim = {"url": getattr(ctx, "url", None), "user": getattr(ctx, "user", None), "mode": getattr(ctx, "mode", None)}
    docker = None
    if getattr(ctx, "mode", None) == "docker":
        names = tuple(nodes if nodes is not None else _nodes())
        docker = {"stackPrefix": stack.prefix(env) if env is not None else None, "nodes": container_ids(names)}
    return {"routes": routes, "striim": striim, "docker": docker}


# ---------------------------------------------------------------------------------------------
# Cleanup result
# ---------------------------------------------------------------------------------------------

@dataclass
class CleanupResult:
    status: str                         # ok | failed | skipped
    detail: str | None = None
    owned: list = field(default_factory=list)
    foreign: list = field(default_factory=list)
    failures: list = field(default_factory=list)
    verified: bool = False
    gaps: list = field(default_factory=list)
    fault: dict | None = None
    ledger_path: str | None = None
    stopped_apps: list = field(default_factory=list)

    def record(self) -> dict:
        return {"status": self.status, "detail": self.detail}

    def resources(self) -> dict:
        return {"owned": self.owned, "reused": [], "foreign": self.foreign, "cleanupVerified": self.verified,
                "verificationGaps": self.gaps, "ledger": self.ledger_path, "faultInjected": self.fault}


def skipped_without_ledger(reason: str) -> CleanupResult:
    return CleanupResult("skipped", reason, gaps=[f"no ownership ledger: {reason}"])


# ---------------------------------------------------------------------------------------------
# The ledger
# ---------------------------------------------------------------------------------------------

class Ledger:
    def __init__(self, path: Path, ident, doc: dict, env=None):
        self.path, self.ident, self.doc = path, ident, doc
        self.env = os.environ if env is None else env
        self.foreign: list = []
        self.gaps: list = []
        self.current: dict | None = None
        self._fault_fired = False

    # -- persistence ------------------------------------------------------------------------------
    @classmethod
    def open(cls, ident, state_dir=None, env=None) -> "Ledger":
        """Open (or continue) this identity's ledger and persist it before any side effect. A prior
        file is imported only for the same ledger version and the same full identity."""
        if state_dir is None:
            from livetest import layout
            state_dir = layout.state_dir(env)
        path = Path(state_dir) / "lifecycle" / "ledgers" / f"{ident.per_test}.json"
        prior = None
        if path.exists():
            try:
                prior = json.loads(path.read_text())
            except ValueError as e:
                raise OwnershipError(f"ownership ledger {path} is unreadable ({e}); refusing to guess what "
                                     f"this identity owns") from None
            if not isinstance(prior, dict) or prior.get("ledgerVersion") != LEDGER_VERSION:
                raise OwnershipError(f"ledger-version-mismatch: {path} is not ledger version {LEDGER_VERSION}; "
                                     f"its entries are not imported and nothing it lists is deleted")
            was = {k: v for k, v in (prior.get("identity") or {}).items() if k != "attempt"}
            now = {k: v for k, v in runident.record(ident).items() if k != "attempt"}
            if was != now:
                raise OwnershipError(f"ledger-identity-mismatch: {path} was written for {was}, not {now} (a "
                                     f"per-test hash collision or a foreign ledger); it is never deletion authority")
        doc = {"ledgerVersion": LEDGER_VERSION, "identity": runident.record(ident),
               "attempts": list((prior or {}).get("attempts", [])) + [ident.attempt],
               "entries": list((prior or {}).get("entries", [])),
               "bindings": dict((prior or {}).get("bindings", {})),
               "keep": None, "faultInjected": None,
               # the process that writes this attempt: a ledger whose writer is alive is in progress
               "writer": {"pid": os.getpid(), "host": socket.gethostname()}}
        ledger = cls(path, ident, doc, env)
        ledger.persist()
        return ledger

    def persist(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.doc, indent=2, sort_keys=True) + "\n")
        os.replace(tmp, self.path)

    # -- entries ----------------------------------------------------------------------------------
    @property
    def entries(self) -> list:
        return self.doc["entries"]

    def find(self, kind: str, name: str, db=None, attempt=None, states=None) -> list:
        return [e for e in self.entries if e["kind"] == kind and e["name"] == name and e.get("db") == db
                and (attempt is None or e["attempt"] == attempt) and (states is None or e["state"] in states)]

    def add(self, kind: str, name: str, db=None, state="intended", note=None, **extra) -> dict:
        existing = self.find(kind, name, db, attempt=self.ident.attempt)
        if existing:
            existing[0]["state"] = state
            entry = existing[0]
        else:
            entry = {"kind": kind, "name": name, "db": db, "state": state, "attempt": self.ident.attempt, "at": _now()}
            self.entries.append(entry)
        entry.update(extra)
        if note:
            entry["note"] = note
        self.persist()
        return entry

    def _set(self, entry: dict, state: str, detail=None) -> None:
        entry["state"] = state
        entry["at"] = _now()
        if detail:
            entry["detail"] = detail
        self.persist()

    def _drop_entry(self, entry: dict) -> None:
        self.entries.remove(entry)
        self.persist()

    def owns(self, kind: str, db, name: str) -> bool:
        """Witness ownership (C7.2): a confirmed table of this attempt on ``db``, or a normalized path
        inside a confirmed owned directory of this attempt."""
        mine = self.ident.attempt
        if kind == "pg-table":
            return any(e["kind"] == "pg-table" and e["name"] == name and e.get("db") == db and e["attempt"] == mine
                       and e["state"] == "confirmed" for e in self.entries)
        if kind == "owned-file":
            return posixpath.normpath(name) == name and any(
                e["kind"] == "owned-dir" and e["attempt"] == mine and e["state"] == "confirmed"
                and name.startswith(e["name"].rstrip("/") + "/") for e in self.entries)
        return False

    # -- targets ----------------------------------------------------------------------------------
    def bind(self, admins, ctx) -> dict:
        """Record this attempt's targets once, before anything of a prior attempt is deleted. A prior
        attempt's binding is never overwritten."""
        if self.current is None:
            self.current = snapshot_targets(admins, ctx, env=self.env)
            self.doc["bindings"][self.ident.attempt] = self.current
            self.persist()
        return self.current

    def _binding(self, entry) -> dict | None:
        return self.doc.get("bindings", {}).get(entry["attempt"])

    def _nodes_for(self, entry):
        if entry.get("nodes"):
            return tuple(entry["nodes"])
        rec = self._binding(entry)
        if rec and (rec.get("docker") or {}).get("nodes"):
            return tuple(rec["docker"]["nodes"])
        return tuple(_nodes())

    def target_problem(self, entry, now: dict) -> str | None:
        """Why ``entry`` must not be deleted through the targets ``now`` (None: same target, verified)."""
        rec = self._binding(entry)
        if rec is None:
            return f"attempt {entry['attempt']} recorded no target binding"
        kind = entry["kind"]
        if kind in ("pg-table", "pg-view", "pg-slot"):
            was, cur = (rec.get("routes") or {}).get(entry.get("db")), (now.get("routes") or {}).get(entry.get("db"))
            if not was or not cur:
                return f"route {entry.get('db')} is not bound on both sides"
            for k in ("host", "port", "dbname"):
                if str(was.get(k)) != str(cur.get(k)):
                    return f"{entry.get('db')} {k} changed from {was.get(k)!r} to {cur.get(k)!r}"
            if not was.get("systemIdentifier") or was.get("systemIdentifier") != cur.get("systemIdentifier"):
                return (f"{entry.get('db')} backend identity changed or unreadable ({was.get('systemIdentifier')!r} "
                        f"-> {cur.get('systemIdentifier')!r})")
        elif kind == "namespace":
            was, cur = (rec.get("striim") or {}).get("url"), (now.get("striim") or {}).get("url")
            if not was or was != cur:
                return f"Striim endpoint changed from {was!r} to {cur!r}"
        elif kind in FILE_KINDS and (rec.get("striim") or {}).get("mode") not in (None, "docker"):
            return self._native_problem(entry, remote_ok=True)
        elif kind in FILE_KINDS:
            was = (rec.get("docker") or {}).get("nodes") or {}
            cur = (now.get("docker") or {}).get("nodes") or {}
            for n in (entry.get("nodes") or list(was)):
                if not was.get(n) or was.get(n) != cur.get(n):
                    return f"docker node {n} changed or cannot be inspected ({was.get(n)!r} -> {cur.get(n)!r})"
            if not was:
                return "no docker nodes were recorded"
        return None

    def _native_problem(self, entry, remote_ok=False) -> str | None:
        """Why a native-mode file entry cannot be deleted from this process's filesystem (None: it can).
        A remote native server's file is never reachable here; with ``remote_ok`` that is left to
        ``_delete``, which records it as a named gap rather than a refusal."""
        url = ((self._binding(entry) or {}).get("striim") or {}).get("url")
        if not native_local(url):
            return None if remote_ok else (f"native-remote-file: {entry['name']} is on the Striim host of "
                                           f"{url!r}, not this host's filesystem; not deleted")
        host = entry.get("host") or (self.doc.get("writer") or {}).get("host")
        if host != socket.gethostname():
            return f"native file written on host {host!r}, not this host {socket.gethostname()!r}"
        return None

    def _refuse_target(self, entry, why: str) -> None:
        self.gaps.append(f"{entry['kind']} {entry['name']}: not deleted, {why}")
        self._foreign(entry["kind"], entry["name"], f"recorded by attempt {entry['attempt']} on another target "
                                                    f"({why}); not deleted")

    def _unresolved_intents(self, entries) -> None:
        for e in entries:
            if e["state"] == "intended":
                self._set(e, "unresolved-intent", "the attempt stopped before confirming it; never deleted")
                self.gaps.append(f"{e['kind']} {e['name']}: an unresolved intent of attempt {e['attempt']} "
                                 f"is not deletion authority")

    # -- acquisition --------------------------------------------------------------------------------
    def reset(self, *, client, admins: dict, ctx, tokens=None) -> list:
        """Delete, by exact name, what a PRIOR attempt of this identity confirmed and did not remove,
        only where its recorded target is verified to be the current one."""
        now = self.bind(admins, ctx)
        mine = self.ident.attempt
        self._unresolved_intents([e for e in self.entries if e["attempt"] != mine])
        prior = [e for e in self.entries if e["attempt"] != mine and e["state"] in DELETABLE_STATES]
        done = []
        self._delete_all(prior, None, lambda e: {"client": client, "admins": admins, "ctx": ctx}, tokens or {},
                         check=lambda e: self.target_problem(e, now), fault=False,
                         detail="reset by a later attempt", done=done)
        return done

    def acquire_namespace(self, client) -> dict:
        ns = self.ident.ns
        present = namespace_present(client, ns)
        if present is None:
            raise OwnershipError(f"namespace-catalog-unparseable: LIST NAMESPACES could not be read, so "
                                 f"{ns} cannot be acquired (refusing rather than assuming it is absent)")
        if present:
            raise OwnershipError(f"collision:namespace:{ns} already exists and is not an object this run "
                                 f"created; it is left untouched")
        return self.add("namespace", ns, None, "confirmed", "absent before this attempt; created by its TQL")

    def run_ddl_files(self, files, *, admins: dict, tokens: dict, render_file, run_one) -> None:
        """DDL one file at a time: refuse unsupported resources, acquire (pre-existence check), run,
        confirm from the catalog. The original exception of a failed file always propagates."""
        for db, fname in files:
            sql = render_file(fname)
            bad = _UNSUPPORTED.search(sql)
            if bad:
                raise OwnershipError(f"unsupported-resource: {fname} runs CREATE {bad.group(1).upper()}, which the "
                                     f"ownership ledger does not support; refused before "
                                     f"execution")
            if db not in PG_ROUTES:
                self.gaps.append(f"engine-tables {db}: the tables of {fname} are not acquired and never deleted "
                                 f"for this resource type (no bounded catalog for this engine); left in place")
                run_one(db, fname)
                continue
            admin = _lifecycle._admin(admins, db)
            catalog = make_catalog(admin)
            # in the file's own order, which cleanup reverses (CLEANUP_ORDER)
            tables = list(dict.fromkeys(_table_name(admin, raw) for raw in _CREATE_TABLE.findall(sql)))
            views = list(dict.fromkeys(_table_name(admin, raw) for _m, raw in _CREATE_VIEW.findall(sql)))
            slots = sorted(set(_CREATE_SLOT.findall(sql)))
            present = pg_tables_present(catalog, tables)
            for t in tables:
                if t in present:
                    raise OwnershipError(f"collision:pg-table:{t} already exists before {fname} runs and is not "
                                         f"recorded in this identity's ledger; refused, the table is left untouched")
            for v in pg_views_present(catalog, views):
                # CREATE OR REPLACE of a view an earlier file of this attempt created is still ours
                if not self.find("pg-view", v, db, attempt=self.ident.attempt, states=("confirmed",)):
                    raise OwnershipError(f"collision:pg-view:{v} already exists before {fname} runs and is not "
                                         f"recorded in this identity's ledger; refused, the view is left untouched")
            for s in slots:
                if pg_slot_present(catalog, s):
                    raise OwnershipError(f"collision:pg-slot:{s} already exists before {fname} runs; refused, the "
                                         f"slot is left untouched")
            intents = ([self.add("pg-table", t, db) for t in tables] + [self.add("pg-view", v, db) for v in views]
                       + [self.add("pg-slot", s, db) for s in slots])
            try:
                run_one(db, fname)
            finally:
                try:
                    now = pg_tables_present(catalog, tables)
                    now_views = pg_views_present(catalog, views)
                    for e in intents:
                        if e["kind"] == "pg-view":
                            exists = e["name"] in now_views
                            e["materialized"] = now_views.get(e["name"], False)
                        else:
                            exists = (e["name"] in now) if e["kind"] == "pg-table" else pg_slot_present(catalog, e["name"])
                        if exists:
                            self._set(e, "confirmed")
                        else:
                            self._drop_entry(e)
                except Exception as exc:    # noqa: BLE001 - never mask the file's own exception
                    self.gaps.append(f"{db}: could not confirm objects of {fname} from the catalog: {exc}")

    def allocate_owned_dir(self, ctx) -> dict:
        path = _safe_path(self.ident.owned_dir)
        if getattr(ctx, "mode", None) != "docker":
            raise OwnershipError(f"owned-dir-unsupported-native: {path} cannot be allocated on a native Striim in "
                                 f"this mode (native owned-directory allocation is unsupported)")
        nodes = tuple(_nodes())
        present = docker_exists(path, nodes)
        if present is None:
            raise OwnershipError(f"owned-dir-inventory-unreadable: {path} could not be checked on every node; "
                                 f"refusing to allocate rather than assuming it is absent")
        if present:
            raise OwnershipError(f"collision:owned-dir:{path} already exists; the framework allocates it fresh "
                                 f"and refuses an existing one (left untouched)")
        entry = None
        for n in nodes:
            parent = _docker(["docker", "exec", n, "mkdir", "-p", "--", OWNED_ROOT.rstrip("/")])
            r = parent if getattr(parent, "returncode", 0) else _docker(["docker", "exec", n, "mkdir", "--", path])
            if getattr(r, "returncode", 0):
                if entry is not None:
                    self._set(entry, "confirmed", f"partial allocation: created on {entry['nodes']} only")
                raise OwnershipError(f"owned-dir: exclusive mkdir {path} on {n} failed: "
                                     f"{(getattr(r, 'stderr', '') or '').strip()}"
                                     + (f"; the nodes it was created on ({entry['nodes']}) stay in the ledger"
                                        if entry is not None else ""))
            if entry is None:
                entry = self.add("owned-dir", path, None, "confirmed", "allocated by the framework (exclusive mkdir)",
                                 nodes=[n])
            else:
                entry["nodes"].append(n)
                self.persist()
        return entry

    def claim_exact_file(self, ctx, path: str, kind: str) -> dict | None:
        """A legacy case's output or server file: owned by exact rendered path only. Any other match of
        ``<path>*`` is reported, never removed."""
        path = _safe_path(path)
        native = getattr(ctx, "mode", None) != "docker"
        if native and not native_local(getattr(ctx, "url", None)):
            why = (f"native-remote-file: {kind} {path} is written on the Striim host of {getattr(ctx, 'url', None)!r}, "
                   f"not this host's filesystem; it is not claimed or deleted")
            self.gaps.append(why)
            return self.add(kind, path, None, "not-deleted", why)
        try:
            matches = local_ls(path) if native else docker_ls(f"{path}*")
        except InventoryError as exc:
            raise OwnershipError(f"file-inventory-unreadable: {path} cannot be claimed ({exc}); refusing rather "
                                 f"than assuming it is absent") from None
        for other in matches:
            if other != path:
                self._foreign(kind, other, "matches the output glob but is not this run's exact path; preserved")
        if path in matches:
            prior = [e for e in self.find(kind, path) if e["attempt"] != self.ident.attempt
                     and e["state"] in DELETABLE_STATES and self.current is not None
                     and self.target_problem(e, self.current) is None]
            if prior:
                local_rm(path) if native else docker_rm(path, nodes=self._nodes_for(prior[-1]))
                self._set(prior[-1], "deleted", "reset by a later attempt")
            else:
                self._foreign(kind, path, "exists before this attempt and is not in this identity's ledger; preserved")
                return None
        if native:
            return self.add(kind, path, None, "confirmed", "absent before this attempt (native, this host)",
                            host=socket.gethostname())
        return self.add(kind, path, None, "confirmed", "absent before this attempt")

    def _foreign(self, kind, name, note) -> None:
        item = {"kind": kind, "name": name, "note": note}
        if item not in self.foreign:
            self.foreign.append(item)

    # -- deletion -----------------------------------------------------------------------------------
    def _fault(self, entry) -> None:
        spec = (self.env.get(FAULT_ENV) or "").strip()
        if not spec or self._fault_fired or not spec.startswith("cleanup:"):
            return
        kind = FAULT_ALIASES.get(spec.split(":", 1)[1], spec.split(":", 1)[1])
        if entry["kind"] == kind:
            self._fault_fired = True
            self.doc["faultInjected"] = {"spec": spec, "kind": kind, "name": entry["name"], "at": _now()}
            self.persist()
            raise RuntimeError(f"injected-fault: {FAULT_ENV}={spec} failed the delete of {kind} {entry['name']}")

    def _delete(self, entry, *, client, admins, ctx, tokens, fault=True, slot_on_active=None) -> None:
        kind, name, db = entry["kind"], entry["name"], entry.get("db")
        if kind in UNSUPPORTED_KINDS:
            raise Unsupported(f"{kind} {name}: ownership is not acquired for this resource type; "
                              f"never deleted")
        if fault:
            self._fault(entry)
        if kind == "namespace":
            if client is None:
                raise RuntimeError("no Striim client to drop the namespace")
            client.teardown_namespace(name)
        elif kind in ("pg-table", "pg-view"):
            what = "TABLE" if kind == "pg-table" else "MATERIALIZED VIEW" if entry.get("materialized") else "VIEW"
            try:
                make_catalog(_lifecycle._admin(admins, db)).run(f"DROP {what} IF EXISTS {_quoted(name)}", 30.0)
            except Exception as exc:
                if getattr(exc, "pgcode", None) != "2BP01":     # dependent_objects_still_exist
                    raise
                # Never CASCADE: what still depends on it here is not this run's to drop.
                raise RuntimeError(f"not-owned-dependent: {name} is left in place; an object that depends on it "
                                   f"is not in this run's ledger or was not dropped before it, and nothing is "
                                   f"dropped with CASCADE ({str(exc).strip()})") from None
        elif kind == "pg-slot":
            admin = _lifecycle._admin(admins, db)
            if slot_on_active is None:
                admin.drop_replication_slot(name)
            else:
                admin.drop_replication_slot(name, on_active=slot_on_active)
        elif kind in ("file-output", "server-file") and getattr(ctx, "mode", None) not in (None, "docker"):
            why = self._native_problem(entry)
            if why:
                raise Unsupported(f"{kind} {name}: {why}")
            local_rm(_safe_path(name))              # native mode wrote it on this host's filesystem
        elif kind in ("file-output", "server-file"):
            docker_rm(_safe_path(name), nodes=self._nodes_for(entry))
        elif kind == "owned-dir":
            if not name.startswith(OWNED_ROOT):
                raise RuntimeError(f"refusing to remove {name}: not under {OWNED_ROOT}")
            docker_rm(_safe_path(name), recursive=True, nodes=self._nodes_for(entry))
        else:
            raise RuntimeError(f"unknown ledger kind {kind}")

    def _verify(self, entry, *, client, admins, ctx):
        """True (absent), False (still present) or None (no read available: a verification gap). An
        unreadable inventory raises, so it is recorded as a failed read, never as absence."""
        kind, name, db = entry["kind"], entry["name"], entry.get("db")
        if kind == "namespace":
            present = namespace_present(client, name) if client is not None else None
            return None if present is None else not present
        if kind == "pg-table":
            return name not in pg_tables_present(make_catalog(_lifecycle._admin(admins, db)), [name])
        if kind == "pg-view":
            return name not in pg_views_present(make_catalog(_lifecycle._admin(admins, db)), [name])
        if kind == "pg-slot":
            return not pg_slot_present(make_catalog(_lifecycle._admin(admins, db)), name)
        if kind in FILE_KINDS + ("op-checkpoint",):
            if getattr(ctx, "mode", None) != "docker":
                if kind in ("file-output", "server-file") and self._native_problem(entry) is None:
                    return not os.path.lexists(name)
                return None
            present = docker_exists(name, self._nodes_for(entry))
            if present is None:
                raise InventoryError(f"{name}: a node's inventory could not be read")
            return not present
        return None

    def _delete_all(self, entries, result, targets, tokens, *, check=None, fault=True, detail=None, done=None,
                    deadline=None, slot_on_active=None) -> None:
        for kind in CLEANUP_ORDER:
            of_kind = [x for x in entries if x["kind"] == kind]
            for e in (of_kind[::-1] if kind in ("pg-view", "pg-table") else of_kind):
                if deadline is not None and deadline.expired():
                    self._set(e, "delete-failed", "cleanup-deadline: not attempted")
                    if result is not None:
                        result.failures.append({"kind": kind, "name": e["name"], "error": "cleanup-deadline: not attempted"})
                    continue
                why = check(e) if check is not None else None
                if why:
                    self._refuse_target(e, why)
                    continue
                t = targets(e)
                bound = CLEANUP_OP_S if deadline is None else max(0.05, min(CLEANUP_OP_S, deadline.remaining()))
                try:
                    _lifecycle.bounded(lambda e=e, t=t: self._delete(
                        e, tokens=tokens, fault=fault, slot_on_active=slot_on_active, **t), bound)
                    self._set(e, "deleted", detail)
                    if done is not None:
                        done.append(e)
                except Unsupported as exc:
                    self._set(e, "not-deleted", str(exc))
                    self.gaps.append(str(exc))
                except Exception as exc:    # noqa: BLE001 - recorded; the result carries it
                    self._set(e, "delete-failed", f"reset: {exc}" if result is None else str(exc))
                    if result is not None:
                        result.failures.append({"kind": kind, "name": e["name"], "error": str(exc)})

    def _verify_all(self, entries, result, targets, deadline=None) -> None:
        for e in entries:
            bound = CLEANUP_OP_S if deadline is None else max(0.05, min(CLEANUP_OP_S, deadline.remaining()))
            try:
                t = targets(e)
                absent = _lifecycle.bounded(lambda e=e, t=t: self._verify(e, **t), bound)
            except Exception as exc:    # noqa: BLE001
                absent = "error"
                self.gaps.append(f"{e['kind']} {e['name']}: verification read failed: {exc}")
            if absent is True:
                self._set(e, "verified-absent")
            elif absent is False:
                self._set(e, "delete-failed", "still present after delete")
                result.failures.append({"kind": e["kind"], "name": e["name"], "error": "still present after delete"})
            elif absent is None:
                self.gaps.append(f"{e['kind']} {e['name']}: no catalog read for this resource type")

    def _foreign_report(self, admins) -> None:
        tid = self.ident.tid
        for route in PG_ROUTES:
            entry = admins.get(route)
            if not entry:
                continue
            try:
                admin = entry["admin"]
                for other in sorted(pg_tables_like(make_catalog(admin), _schema(admin), _like_escape(tid) + "%")):
                    if not self.find("pg-table", other, route):
                        self._foreign("pg-table", other, "carries this run's prefix but is not in its ledger "
                                                         "(never deleted on a prefix alone); preserved")
            except Exception as exc:    # noqa: BLE001
                self.gaps.append(f"foreign report for {route}: {exc}")
        for e in self.entries:
            if e["kind"] != "pg-table" or e["attempt"] != self.ident.attempt:
                continue
            try:
                catalog = make_catalog(_lifecycle._admin(admins, e["db"]))
                schema, table = e["name"].split(".", 1)
                logical = table[len(tid):] if table.startswith(tid) else table
                for other in sorted(pg_tables_like(catalog, schema, "%" + _like_escape(logical))):
                    if not self.find("pg-table", other, e["db"]):
                        self._foreign("pg-table", other, "a lookalike of an owned table (another run or a legacy "
                                                         "name); preserved")
            except Exception as exc:    # noqa: BLE001 - a report, never a reason to fail cleanup
                self.gaps.append(f"foreign report for {e['name']}: {exc}")

    def _not_acquired(self, kind: str, name: str) -> None:
        self.gaps.append(f"{kind} {name}: a framework-derived name is not acquired for this resource type, so it is never "
                         f"deleted (unsupported resource type)")

    def cleanup(self, *, client, admins, ctx, tokens, gcs_cleanup=(), kafka_cleanup=(), upload_names=(),
                file_globs=(), checkpoint_ns=None, keep_reason=None, keep_slots=False, deadline_s=None) -> CleanupResult:
        mine = self.ident.attempt
        for _admin, bucket in gcs_cleanup:
            self._not_acquired("bucket", bucket)
        for _admin, topic in kafka_cleanup:
            self._not_acquired("topic", topic)
        for name in upload_names:
            self._not_acquired("upload", name)
        result = CleanupResult("ok", ledger_path=str(self.path))
        try:
            self.bind(admins or {}, ctx)
        except Exception as exc:        # noqa: BLE001 - recorded; the attempt's own entries need no rebinding
            self.gaps.append(f"target binding could not be recorded: {exc}")
        self._foreign_report(admins or {})
        if keep_reason:
            self.doc["keep"] = {"reason": keep_reason, "at": _now()}
            self.persist()
            result.status, result.detail = "skipped", f"resources kept: {keep_reason}; remove them with " \
                                                      f"python -m livetest.ownership replay {self.path}"
            result.gaps = [f"cleanup skipped: {keep_reason}"]
            # Deliberate exploration keeps slots; failure-only retention releases them.
            slots = [e for e in self.entries if e["attempt"] == mine
                     and e["kind"] == "pg-slot" and e["state"] == "confirmed" and not keep_slots]
            deadline = _lifecycle.Deadline(deadline_s or CLEANUP_DEADLINE_S)
            targets = lambda e: {"client": client, "admins": admins, "ctx": ctx}  # noqa: E731
            stop_attempted = False

            def stop_owned_apps(remaining_s):
                nonlocal stop_attempted
                if stop_attempted or client is None or not any(
                        e["attempt"] == mine and e["kind"] == "namespace" and e["state"] == "confirmed"
                        for e in self.entries):
                    return
                stop_attempted = True
                stop_deadline = _lifecycle.Deadline(min(remaining_s, deadline.remaining()))
                from livetest.striim import StriimClient
                try:
                    response = _lifecycle.bounded(lambda: client.api.post_tungsten_line("LIST APPLICATIONS;"),
                                                  max(0.05, stop_deadline.remaining()))
                    apps = StriimClient._apps_in_namespace(response, self.ident.ns)
                except Exception as exc:
                    apps = [self.ident.app]
                    self.gaps.append(f"kept app inventory: {exc}")
                for app in apps:
                    if stop_deadline.expired():
                        self.gaps.append(f"kept app stop {app}: slot release deadline expired")
                        break
                    try:
                        _lifecycle.bounded(lambda app=app: client.stop_app(app),
                                           max(0.05, stop_deadline.remaining()))
                        result.stopped_apps.append(app)
                    except Exception as exc:
                        self.gaps.append(f"kept app stop {app}: {exc}")
                        print(f"[slt] slot cleanup: STOP failed for {app}: {exc}")

            self._delete_all(slots, result, targets, tokens, deadline=deadline, slot_on_active=stop_owned_apps)
            self._verify_all([e for e in slots if e["state"] == "deleted"], result, targets, deadline)
            result.gaps.extend(self.gaps)
        else:
            deadline = _lifecycle.Deadline(deadline_s or CLEANUP_DEADLINE_S)
            targets = lambda e: {"client": client, "admins": admins, "ctx": ctx}   # noqa: E731
            self._unresolved_intents([x for x in self.entries if x["attempt"] == mine])
            self._delete_all([x for x in self.entries if x["attempt"] == mine and x["state"] == "confirmed"],
                             result, targets, tokens, deadline=deadline)
            if checkpoint_ns and getattr(ctx, "mode", None) == "docker":
                try:
                    clear_own_checkpoints(ctx, checkpoint_ns, self)
                except Exception as exc:    # noqa: BLE001
                    self.gaps.append(f"op-checkpoint cleanup: {exc}")
            self._verify_all([x for x in self.entries if x["attempt"] == mine and x["state"] == "deleted"],
                             result, targets, deadline)
            for pattern in file_globs:
                if getattr(ctx, "mode", None) == "docker":
                    try:
                        listed = docker_ls(_safe_path(pattern) + "*")
                    except Exception as exc:    # noqa: BLE001
                        self.gaps.append(f"foreign report for {pattern}*: {exc}")
                        continue
                    for other in listed:
                        if not self.find("file-output", other) and not self.find("server-file", other):
                            self._foreign("file-output", other, "matches the output glob but is not this run's "
                                                                "exact path; preserved")
            result.gaps = list(self.gaps)
        if result.failures:
            result.status = "failed"
            result.detail = "; ".join(f"{f['kind']} {f['name']}: {f['error']}" for f in result.failures) + (f"; remove them with python -m livetest.ownership replay {self.path}" if keep_reason else "")
        result.owned = [{k: e.get(k) for k in ("kind", "name", "db", "state")} for e in self.entries if e["attempt"] == mine]
        result.foreign = list(self.foreign)
        result.fault = self.doc.get("faultInjected")
        result.verified = (result.status == "ok" and not result.gaps
                           and all(e["state"] == "verified-absent" for e in result.owned))
        self.persist()
        return result

    def crashed(self, exc: BaseException) -> CleanupResult:
        """A cleanup that raised (for example, the ledger could not be persisted): a failed result
        built from memory only, so the case's own exception and the in-use release are unaffected."""
        mine = self.ident.attempt
        detail = f"cleanup-error: {type(exc).__name__}: {exc}"
        return CleanupResult("failed", detail,
                             owned=[{k: e.get(k) for k in ("kind", "name", "db", "state")}
                                    for e in self.entries if e["attempt"] == mine],
                             foreign=list(self.foreign), failures=[{"kind": "cleanup", "name": str(self.path),
                                                                    "error": detail}],
                             verified=False, gaps=list(self.gaps) + [detail], fault=self.doc.get("faultInjected"),
                             ledger_path=str(self.path))


def clear_own_checkpoints(ctx, ns: str, ledger=None) -> list:
    """List the OP checkpoint files a namespace-scoped or fallback glob matches and delete only the
    listed names in which ``<ns>.`` is a whole component (preceded by the start, ``.`` or ``_``).
    Every other listed name -- a lookalike namespace or a fallback base -- is reported, never removed."""
    if getattr(ctx, "mode", None) != "docker":
        return []
    if not re.match(r"^[A-Za-z0-9_-]+\Z", ns):
        raise OwnershipError(f"unsafe namespace {ns!r} for checkpoint cleanup")
    scoped = [f"{DOCKER_HOME}/*{ns}.*{s}*" for s in CHECKPOINT_SUFFIXES]
    patterns = scoped + [f"{DOCKER_HOME}/*{s}*" for s in CHECKPOINT_SUFFIXES]
    own = re.compile(rf"(^|[._]){re.escape(ns)}\.")
    deleted = []
    for path in docker_ls(" ".join(patterns)):
        base = path.rsplit("/", 1)[-1]
        if not (_BARE_CHECKPOINT.match(base) or any(fnmatch.fnmatchcase(path, g) for g in scoped)):
            continue   # another namespace's checkpoint: neither ours nor a fallback, so not reported
        if path.startswith(DOCKER_HOME + "/") and "/" not in base and own.search(base):
            docker_rm(_safe_path(path))
            deleted.append(path)
            if ledger is not None:
                ledger.add("op-checkpoint", path, None, "deleted", "listed and matched <ns>. exactly")
        elif ledger is not None:
            ledger._foreign("op-checkpoint", path, "listed by a checkpoint glob without this namespace as a "
                                                   "component (another namespace or a fallback base); preserved")
    return deleted


def run_cleanup(ledger, **kw) -> CleanupResult:
    """H8: the one cleanup call of a case. A case that stopped before its ledger opened has nothing owned.
    Never raises: an unexpected error is a failed cleanup result."""
    if ledger is None:
        return skipped_without_ledger("the case stopped before its ownership ledger was opened")
    try:
        return ledger.cleanup(**kw)
    except Exception as exc:            # noqa: BLE001 - the case's own exception must survive
        return ledger.crashed(exc)


# ---------------------------------------------------------------------------------------------
# replay
# ---------------------------------------------------------------------------------------------

def replay_command(ledger_path) -> str:
    return f"python -m livetest.ownership replay {ledger_path}"


IN_PROGRESS = "do not replay"


def leftover_notice(path, why) -> str:
    """One leftover line: the reason, and the reclaim command unless the run is still in progress."""
    return why if why.endswith(IN_PROGRESS) else f"{why}; reclaim it with {replay_command(path)}"


def writer_alive(doc, pid_alive=None) -> bool:
    """True when the ledger's writer is another live process on this host: its run is still in flight,
    so its objects are not leftovers and must not be replayed. A writer on another host cannot be
    checked here and counts as not alive; this process itself may replay what it wrote."""
    w = doc.get("writer") or {}
    if w.get("host") != socket.gethostname() or not isinstance(w.get("pid"), int) or w["pid"] == os.getpid():
        return False
    if pid_alive is None:
        from livetest.infra import _pid_alive as pid_alive
    return bool(pid_alive(w["pid"]))


def leftover_ledgers(state_dir, current_run=None, pid_alive=None) -> list:
    """Ledgers of earlier runs that still list objects to delete (an entry ``confirmed`` or
    ``delete-failed``): a run that was killed or timed out before its cleanup, a failed cleanup, or a
    kept run. Nothing reclaims them by itself, since a new run's identity never matches an old one;
    each is removed with ``replay_command``. ``[(path, why)]``, oldest first; ``current_run``'s own
    ledgers are left out."""
    d = Path(state_dir) / "lifecycle" / "ledgers"
    out = []
    for path in sorted(d.glob("*.json"), key=lambda p: p.stat().st_mtime) if d.is_dir() else ():
        try:
            doc = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if not isinstance(doc, dict):
            continue
        rec = doc.get("identity") or {}
        if current_run and rec.get("runId") == current_run:
            continue
        pending = [e for e in doc.get("entries", []) if e.get("state") in DELETABLE_STATES]
        if not pending:
            continue
        keep = (doc.get("keep") or {}).get("reason")
        if writer_alive(doc, pid_alive):
            why = f"in progress (writer pid {doc['writer']['pid']} is alive), {IN_PROGRESS}"
        elif keep:
            why = f"kept ({keep})"
        else:
            why = "not cleaned up (killed, timed out or a failed cleanup)"
        kinds = ", ".join(sorted({e.get("kind", "?") for e in pending}))
        out.append((path, f"case {rec.get('case')!r} run {rec.get('runId')!r}: {len(pending)} object(s) "
                          f"left ({kinds}), {why}"))
    return out


def _latest_binding(doc, pick) -> dict | None:
    for attempt in reversed(doc.get("attempts", [])):
        value = pick(doc.get("bindings", {}).get(attempt) or {})
        if value:
            return value
    return None


def _service_setting(key, env):
    """A service setting as striim-test hands it to a run (``dispatch.service_env``): the shell,
    then the .env of ``env``'s project root, then the clone's .env."""
    from livetest import paths
    value = (env.get(key) or "").strip()
    if value:
        return value
    clone_env = {k: v for k, v in env.items() if k != "SLT_PROJECT_ROOT"}
    return paths.setting(key, env) or paths.setting(key, clone_env)


class _LazyClient:
    """The replay's Striim client, built on first use. ``StriimApi`` logs in when it is constructed, and
    only a namespace entry needs Striim, so a ledger holding only files replays with the server stopped;
    a namespace entry whose server cannot be reached fails that entry, not the whole replay."""

    def __init__(self, make):
        self._make, self._client = make, None

    def __getattr__(self, name):
        if self._client is None:
            self._client = self._make()
        return getattr(self._client, name)


def replay(ledger_path, *, admins=None, client=None, ctx=None, env=None, pid_alive=None) -> CleanupResult:
    """Repeat the exact-name cleanup a kept or fault-injected run could not finish, from its ledger file.
    Targets are rebuilt from the recorded bindings (never from this shell's stack prefix) and each
    entry is deleted only after its recorded target is verified."""
    env = os.environ if env is None else env
    path = Path(ledger_path)
    doc = json.loads(path.read_text())
    if doc.get("ledgerVersion") != LEDGER_VERSION:
        raise OwnershipError(f"ledger-version-mismatch: {path} is not ledger version {LEDGER_VERSION}; not replayed")
    if writer_alive(doc, pid_alive):
        raise OwnershipError(f"ledger-in-progress: {path} is being written by the live process "
                             f"{doc['writer']['pid']} on this host; its run is in flight, not replayed")
    rec = doc["identity"]
    ident = runident.Identity(run_id=rec["runId"], worker=rec["worker"], case=rec["case"], attempt="replay",
                              slug="", per_test=rec["perTest"], tid=rec["perTest"] + "_",
                              tid_upper=rec["perTest"].upper() + "_", tid_oracle=rec["perTest"].upper() + "_",
                              ns=rec["namespace"], ns_truncated=rec["namespaceTruncated"], app=rec["app"],
                              app_bare=rec["app"].split(".", 1)[-1], pg_slot=rec["pgSlot"], owned_dir=rec["ownedDir"])
    if admins is None:
        from livetest.pgclient import PgAdmin
        from livetest.registry import load_service
        defaults = load_service("postgres").docker_defaults
        admins = {}
        for route in PG_ROUTES:
            dsn = _latest_binding(doc, lambda b, r=route: (b.get("routes") or {}).get(r))
            if not dsn:
                continue
            full = {k: v for k, v in dsn.items() if k != "systemIdentifier"}
            for which in ("admin", "source", "target"):
                full[f"{which}_password"] = (_service_setting(f"SLT_PG_{which.upper()}_PASSWORD", env)
                                             or defaults.get(f"{which}_password"))
            admins[route] = {"admin": PgAdmin(full, role=full.pop("role", "source")), "schema": None}
    striim = _latest_binding(doc, lambda b: b.get("striim")) or {}
    if ctx is None:
        ctx = SimpleNamespace(url=striim.get("url"), user=striim.get("user"), mode=striim.get("mode"))
    if client is None and striim.get("url"):
        from livetest.striim import StriimClient
        # The recorded server and user are the ones the run used; the settings fill what the binding
        # lacks, and the password always comes from them, as striim-test hands STRIIM_* to a run
        # (shell, then the project .env, then the clone's; STRIIM_PASSWORD is STRIIM_PASS's alias).
        user = striim.get("user") or _service_setting("STRIIM_USER", env) or "admin"
        client = _LazyClient(lambda: StriimClient.from_url(striim["url"], user,
                                                           _service_setting("STRIIM_PASS", env) or "striim"))
    recorded_nodes = []
    for b in doc.get("bindings", {}).values():
        for n in ((b or {}).get("docker") or {}).get("nodes") or {}:
            if n not in recorded_nodes:
                recorded_nodes.append(n)
    ledger = Ledger(path, ident, doc, env)
    now = snapshot_targets(admins, ctx, nodes=recorded_nodes)
    ledger._unresolved_intents(list(ledger.entries))
    result = CleanupResult("ok", ledger_path=str(path))
    targets = lambda e: {"client": client, "admins": admins, "ctx": ctx}   # noqa: E731
    replayed = []
    ledger._delete_all([e for e in ledger.entries if e["state"] in DELETABLE_STATES], result, targets,
                       {"TID": ident.tid, "TID_ORACLE": ident.tid_oracle, "TID_UPPER": ident.tid_upper},
                       check=lambda e: ledger.target_problem(e, now), fault=False, detail="replayed", done=replayed,
                       deadline=_lifecycle.Deadline(CLEANUP_DEADLINE_S))
    ledger._verify_all(replayed, result, targets)
    if result.failures:
        result.status = "failed"
        result.detail = "; ".join(f"{f['kind']} {f['name']}: {f['error']}" for f in result.failures)
    result.gaps = list(ledger.gaps)
    result.owned = [{k: e.get(k) for k in ("kind", "name", "db", "state")} for e in ledger.entries]
    result.foreign = list(ledger.foreign)
    result.verified = result.status == "ok" and not result.gaps and all(e["state"] == "verified-absent" for e in result.owned)
    ledger.persist()
    return result


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="python -m livetest.ownership")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("replay", help="repeat the exact-name cleanup recorded in a ledger file")
    r.add_argument("ledger")
    args = p.parse_args(argv)
    result = replay(args.ledger)
    print(json.dumps({**result.record(), **result.resources()}, indent=2, sort_keys=True))
    return 0 if result.status == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
