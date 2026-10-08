"""The executed-plugin harness shared by tests/lifecycle/test_plugin_exec.py and
tests/evidence/test_exec_exact.py: a pytester run of one fixture case through the real
``livetest.plugin`` with fakes at the infrastructure edges only (moved out of test_plugin_exec.py,
test ids unchanged; the fake store gained typed rows with ``cursor.description``
names behind the exact read: the server-side measurement and the named-cursor fetch).
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

# At the repo root, outside scripts/live's testpaths: the live plugin collects every test.yaml it meets.
FIXTURES = Path(__file__).resolve().parents[4] / "tests" / "fixtures" / "lifecycle"
# The engine sources, for the pytest child (and the guard's imports) when the framework is not installed.
SOURCES = [str(Path(__file__).resolve().parents[4] / "scripts" / d) for d in ("live", "integration", "cli")]
from tests import _hermetic_child  # noqa: E402
RUN = "20260914T230000Z-abcd1234"

CONFTEST = r'''
import fnmatch, hashlib, json, os, re, threading
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import livetest.infra as I
import livetest.lifecycle as L
import livetest.ownership as O
import livetest.plugin as P
from livetest import runident

SC = json.loads(os.environ["XTR_EXEC"])
# Services the case resolves through the real registry, resolver and token builder (a connection-only
# definition: nothing is brought up); every other service is faked below.
REAL_SERVICES = set(SC.get("real_services", []))
_real = SimpleNamespace(load_service=P.load_service, build_service_tokens=P.build_service_tokens,
                        resolve_service=I.resolve_service, rendered_tql=P._rendered_tql,
                        deploy_retrying=P._deploy_tql_once_retrying)
LOG = os.environ["XTR_LOG"]
TID = runident.derive(os.environ["XTR_CASE_NAME"], os.environ).tid
TABLES = {k.replace("<TID>", TID): list(v) for k, v in SC.get("tables", {}).items()}
SLOTS = set()


def typed(value):
    """Scenario JSON cannot carry Decimal/datetime/bytes: {"$decimal": "1.50"}, {"$tstz": iso}, {"$bytes": hex}."""
    import datetime as _dt
    from decimal import Decimal as _D
    if isinstance(value, dict) and len(value) == 1:
        (k, v), = value.items()
        if k == "$decimal":
            return _D(v)
        if k == "$tstz":
            return _dt.datetime.fromisoformat(v)
        if k == "$bytes":
            return memoryview(bytes.fromhex(v))
    return value


# typed row dicts behind the exact read; a table without explicit rows reads as {"id": <id>}
ROWS = {k.replace("<TID>", TID): [{c: typed(v) for c, v in r.items()} for r in rows]
        for k, rows in SC.get("rows", {}).items()}
NAMESPACES = set()
FILES, DIRS = {}, set()
STATE = {"running": False, "loaded": False, "mirror_left": SC.get("mirror_ops"), "owned_dir": None,
         "poisoned_once": False}


def log(event, **kw):
    with open(LOG, "a") as f:
        f.write(json.dumps({"event": event, **kw}) + "\n")


def qualify(name, role):
    name = name.replace('"', "").strip()
    return name if "." in name else f"qa{role}.{name}"


def mirror_of(table):
    schema, t = table.split(".")
    return f"qatarget.{t[:-3]}tgt" if schema == "qasource" and t.endswith("src") else None


def mirroring():
    mode = SC.get("mirror", "none")
    if mode == "all" or (mode == "live" and STATE["running"]):
        if STATE["mirror_left"] is None:
            return True
        if STATE["mirror_left"] > 0:
            STATE["mirror_left"] -= 1
            return True
    return False


def capture(table, ids):
    if mirror_of(table) and mirroring():
        TABLES.setdefault(mirror_of(table), []).extend(ids)
        if STATE["owned_dir"]:
            out = STATE["owned_dir"] + "/out.json"          # FileWriter JSON events, one per line
            FILES[out] = FILES.get(out, "") + "".join(json.dumps({"data": {"id": i}}) + "\n" for i in ids)


def exec_sql(sql, role):
    for stmt in [s.strip() for s in sql.split(";") if s.strip()]:
        m = re.match(r"CREATE TABLE (\S+)", stmt, re.I)
        if m:
            q = qualify(m.group(1), role)
            if SC.get("ddl_fail") or (SC.get("ddl_fail_tgt") and q.endswith("tgt")) or q in TABLES:
                raise RuntimeError(f'relation "{m.group(1)}" already exists')
            TABLES[q] = []
            log("create", table=q)
            continue
        m = re.match(r"INSERT INTO (\S+)\s*\(id\)\s*VALUES\s*(.+)$", stmt, re.I | re.S)
        if m:
            q = qualify(m.group(1), role)
            ids = [int(x) for x in re.findall(r"\((\d+)\)", m.group(2))]
            TABLES.setdefault(q, []).extend(ids)
            capture(q, ids)
            log("insert", table=q, ids=ids)
            continue
        m = re.match(r"DELETE FROM (\S+) WHERE id = (\d+)", stmt, re.I)
        if m:
            q, i = qualify(m.group(1), role), int(m.group(2))
            TABLES[q] = [x for x in TABLES.get(q, []) if x != i]
            if mirror_of(q) and mirroring():
                TABLES[mirror_of(q)] = [x for x in TABLES.get(mirror_of(q), []) if x != i]
            log("delete", table=q, id=i)


def like(pattern):
    out, i = "", 0
    while i < len(pattern):
        c = pattern[i]
        if c == "\\" and i + 1 < len(pattern):
            out += re.escape(pattern[i + 1]); i += 2; continue
        out += ".*" if c == "%" else re.escape(c)
        i += 1
    return re.compile(out)


class FakePgAdmin:
    def __init__(self, dsn, connect=None, role="source"):
        self.dsn, self.role = dsn, role

    def ensure_setup(self):
        log("ensure_setup")
        if SC.get("postgres_connect_fail"):
            raise RuntimeError(SC["postgres_connect_fail"])

    def reset_schemas(self):
        log("reset_schemas")

    def reset_test_objects(self, tid):
        log("reset_test_objects", tid=tid)

    def drop_replication_slot(self, name):
        SLOTS.discard(name)
        log("drop_slot", name=name)

    def run_sql(self, sql):
        exec_sql(sql, self.role)


class FakeConn:
    def __init__(self, user):
        self.role = {"qasource": "source", "qatarget": "target"}.get(user, "admin")
        self.cancelled = threading.Event()
        self.autocommit = False

    def cursor(self, name=None):
        conn = self

        class Cur:
            description = None
            rows = []

            def execute(self, sql, params=None):
                if SC.get("hang_probe") and conn.role != "admin":
                    log("probe_hang")
                    conn.cancelled.wait(60)
                    raise RuntimeError("canceling statement due to user request")
                self.description = [("c",)]
                if sql.startswith("SET TRANSACTION"):
                    self.description = None
                    return
                # the exact read measures the rows first, then fetches them from a server-side cursor
                measure = re.match(r'SELECT count\(\*\), .* FROM \(SELECT \* FROM "([^"]+)"\."([^"]+)" LIMIT %s\) _slt_r$', sql)
                star = re.match(r'SELECT octet_length\(ROW\(_slt_r\.\*\)::text\), _slt_r\.\* FROM "([^"]+)"\."([^"]+)" _slt_r'
                                r'(?: ORDER BY (.+))? LIMIT %s$', sql)
                if measure or star:
                    table = f"{(measure or star).group(1)}.{(measure or star).group(2)}"
                    if name_ is None and star or name_ is not None and measure:
                        raise RuntimeError(f"exact read statement on the wrong cursor: {sql}")
                    if table not in TABLES:
                        raise RuntimeError(f'relation "{table}" does not exist')
                    rows = ROWS.get(table, [{"id": i} for i in TABLES[table]])
                    cols = list(rows[0]) if rows else ["id"]
                    sizes = [len(repr(tuple(r.get(c) for c in cols))) for r in rows][:params[0]]
                    if measure:
                        self.description = [("count",), ("coalesce",), ("coalesce",)]
                        self.rows = [(len(sizes), sum(sizes), max(sizes, default=0))]
                        return
                    order_by = star.group(3) and ", ".join(c.strip()[len("_slt_r."):] for c in star.group(3).split(","))
                    log("select", table=table, limit=params[0], order_by=order_by)
                    if order_by:
                        keys = [c.strip().strip('"') for c in order_by.split(",")]
                        rows = sorted(rows, key=lambda r: tuple(r[k] for k in keys))
                    self.description = [("octet_length",)] + [(c,) for c in cols]
                    self.rows = [(len(repr(tuple(r.get(c) for c in cols))),) + tuple(r.get(c) for c in cols)
                                 for r in rows][:params[0]]
                    return
                if "pg_control_system" in sql:
                    self.rows = [("7000000000000000001",)]
                elif sql.startswith("SELECT schemaname, tablename FROM pg_tables WHERE (schemaname"):
                    self.rows = [tuple(n.split(".", 1)) for n in sorted(TABLES) if n in params[0]]
                elif "tablename LIKE %s" in sql:
                    rx = like(params[1])
                    self.rows = [tuple(n.split(".", 1)) for n in sorted(TABLES)
                                 if n.split(".", 1)[0] == params[0] and rx.fullmatch(n.split(".", 1)[1])]
                elif sql.startswith("SELECT count(*) FROM pg_replication_slots"):
                    self.rows = [(1 if params[0] in SLOTS else 0,)]
                elif sql.startswith("SELECT active FROM pg_replication_slots"):
                    self.rows = [(bool(SC.get("slot_active", True)),)]
                elif sql.startswith("DROP TABLE IF EXISTS"):
                    self.description = None
                    name = sql[len("DROP TABLE IF EXISTS "):].replace('"', "")
                    TABLES.pop(name, None)
                    log("drop_table", table=name)
                else:
                    m = re.match(r"SELECT count\(\*\) FROM (\S+)(?: WHERE CAST\(id AS text\) = %s)?$", sql)
                    if m:
                        ids = TABLES.get(qualify(m.group(1), conn.role), [])
                        n = len(ids) if params is None else sum(1 for x in ids if str(x) == params[0])
                        if params is not None:
                            log("observe", table=qualify(m.group(1), conn.role), id=params[0], rows=list(ids), match=n)
                        self.rows = [(n,)]
                    else:
                        self.description = None
                        exec_sql(sql, conn.role)

            def fetchall(self):
                return self.rows

            def fetchone(self):
                return self.rows[0] if self.rows else None

            def fetchmany(self, size):
                out, self.rows = self.rows[:size], self.rows[size:]
                return out

            def close(self):
                pass
        name_ = name
        return Cur()

    def cancel(self):
        log("probe_cancel")
        self.cancelled.set()

    def close(self):
        pass


class Api:
    def post_tungsten_line(self, line, timeout=None):
        assert line == "LIST NAMESPACES;", line
        return [{"command": line, "executionStatus": "Success",
                 "output": [{f"namespace{i}": {"name": n}} for i, n in enumerate(sorted(NAMESPACES | {"Global"}))]}]


class FakeClient:
    api = Api()

    def teardown_namespace(self, ns):
        NAMESPACES.discard(ns)
        log("teardown_namespace", ns=ns)

    def current_status(self, app):
        if SC.get("initial_load") and STATE["running"] and not STATE["loaded"]:
            STATE["loaded"] = True
            for t, ids in list(TABLES.items()):
                if mirror_of(t) and TID in t:
                    TABLES.setdefault(mirror_of(t), []).extend(ids)
                    if STATE["owned_dir"]:                   # an initial load into a FileWriter output
                        out = STATE["owned_dir"] + "/out.json"
                        FILES[out] = FILES.get(out, "") + "".join(json.dumps({"data": {"id": i}}) + "\n" for i in ids)
        return "RUNNING" if STATE["running"] else "CREATED"

    def await_running(self, app, timeout, poll=2.0, progress=None):
        if not STATE["running"]:
            raise RuntimeError("never RUNNING")

    def deploy_tql(self, tql):
        log("deploy_retry")
        STATE["running"] = True

    def load_jar(self, name):                              # A server_files load: udf jar
        log("load_jar", name=name)

    def load_open_processor_idempotent(self, name, tag=None, before_unload=None):
        log("load_open_processor", name=name)


def docker(argv):
    if argv[1] == "inspect":
        name = argv[-1]
        if name in SC.get("inspect_fail", []):
            return SimpleNamespace(returncode=1, stdout="", stderr=f"Error: No such object: {name}")
        if "-f" in argv and "Config.Image" in argv[argv.index("-f") + 1]:     # image id | image reference
            digest = hashlib.sha256(name.encode()).hexdigest()
            return SimpleNamespace(returncode=0, stdout=f"sha256:{digest}|{name}:5.4.0.6\n", stderr="")
        return SimpleNamespace(returncode=0, stdout=f"id-{name}\n", stderr="")
    cmd = argv[3:]
    ok = SimpleNamespace(returncode=0, stdout="", stderr="")
    if argv[1] == "exec" and cmd[:2] == ["sh", "-c"] and cmd[2] == I.RUNTIME_PROBE_SCRIPT:   # runtime_probe: real
        log("runtime_probe", container=argv[2])
        return SimpleNamespace(returncode=0, stdout=SC["runtime_listing"], stderr="")

    def hit(path):
        return path in FILES or path in DIRS or any(f.startswith(path + "/") for f in FILES)
    if cmd[0] == "head":                                   # bounded exact file reads
        path = cmd[-1]
        return SimpleNamespace(returncode=0 if path in FILES else 1, stdout=FILES.get(path, ""), stderr="")
    if cmd[0] == "test":
        return SimpleNamespace(returncode=0 if hit(cmd[2]) else 1, stdout="", stderr="")
    if cmd[0] == "sh" and len(cmd) == 5:
        return SimpleNamespace(returncode=0, stdout="slt-present\n" if hit(cmd[4]) else "slt-absent\n", stderr="")
    if cmd[0] == "mkdir":
        if "-p" in cmd and cmd[-1] == O.OWNED_ROOT.rstrip("/"):
            return ok
        DIRS.add(cmd[-1])
        STATE["owned_dir"] = cmd[-1]
        log("mkdir", path=cmd[-1])
        return ok
    if cmd[0] == "rm":
        if SC.get("rm_fail"):
            log("rm_failed", path=cmd[-1])
            return SimpleNamespace(returncode=1, stdout="", stderr="rm: cannot remove: Read-only file system")
        path = cmd[-1]
        if cmd[1] == "-rf":
            DIRS.discard(path)
            for f in [f for f in FILES if f.startswith(path + "/")]:
                FILES.pop(f)
        else:
            FILES.pop(path, None)
        log("rm", flag=cmd[1], path=path)
        return ok
    if cmd[0] == "sh":
        script, done = cmd[2], ""
        if script.endswith("; echo slt-ls-done"):
            script, done = script[:-len("; echo slt-ls-done")], "slt-ls-done\n"
        pats = script[len("ls -1d "):-len(" 2>/dev/null")].split()
        found = sorted({f for f in list(FILES) + list(DIRS) for p in pats if fnmatch.fnmatch(f, p)})
        return SimpleNamespace(returncode=0, stdout="".join(f"{f}\n" for f in found) + done, stderr="")
    raise AssertionError(argv)


def resolve_striim(config):
    log("resolve_striim")
    if SC.get("no_cluster"):                              # no reachable Striim: the case skips
        config._slt_striim_reason = "no reachable Striim (hermetic no_cluster)"
        return None
    ctx = SimpleNamespace(url="http://localhost:9080", user="admin", password="striim", mode="docker",
                          topology=P.Topology(), view_host="localhost", groups={"app": "default", "source": "Agents"})
    config._slt_striim, config._slt_striim_provisioned = ctx, bool(SC.get("provisioned"))
    return ctx


def resolve_service(infra, svc, env, started, resolve_fn, progress=None):
    log("resolve", svc=svc, ownership=getattr(infra, "ownership", None))
    if svc in REAL_SERVICES:
        return _real.resolve_service(infra, svc, env, started, resolve_fn, progress=progress)
    if SC.get("service_fail"):
        raise RuntimeError("service setup failed: slt-postgres exited before accepting connections")
    for rel in SC.get("rewrite_on_resolve", []):         # A case file changed after the input snapshot
        Path(rel).write_text("id\n9\n")
    started.add(f"slt-{svc}")
    if infra is not None:                                  # the declared service, for runtime.services
        infra.services.append({"name": svc, "container": f"slt-{svc}", "status": "reused"})
    return SimpleNamespace(name=svc, mode="docker", started=True, base={
        "host": "localhost", "port": 5432, "dbname": "sltdb", "admin_user": "postgres", "admin_password": "striim",
        "source_user": "qasource", "source_password": "striim", "target_user": "qatarget", "target_password": "striim"})


def deploy(client, tql, ns, name):
    log("deploy", ns=ns)
    if SC.get("deploy_fail_echo"):                         # a server error that quotes the statement it refused
        raise RuntimeError(f"deploy failed: Invalid property in statement: {tql}")
    for rel in SC.get("rewrite_on_deploy", []):          # a golden changed while the case runs
        Path(rel).write_text("id\n9\n")
    NAMESPACES.add(ns)
    if SC.get("op_poison") and not STATE["poisoned_once"]:
        STATE["poisoned_once"] = True
        log("deploy_poison")
        raise RuntimeError("deploy failed after ZipException")
    STATE["running"] = True
    for table, ids in SC.get("deliver_on_deploy", {}).items():   # a delayed delivery of an earlier attempt's rows
        TABLES.setdefault(table.replace("<TID>", TID), []).extend(ids)
        log("delivered", table=table.replace("<TID>", TID), ids=ids)


def read_files(ctx, path, run=None):
    return "".join(v for k, v in sorted(FILES.items()) if k.startswith(path))


def pytest_runtest_teardown(item):
    if hasattr(item, "_slt_data"):      # the comparison records the real runtest collected
        Path(os.environ["XTR_DUMP"]).with_name("data.json").write_text(json.dumps(item._slt_data, default=str))


def pytest_sessionfinish(session, exitstatus):
    xml = getattr(session.config.option, "xmlpath", None)
    if SC.get("foreign_envelope") and xml:                 # another invocation's envelope in this run
        stale = Path(xml).parent / "evidence" / "foreign" / os.environ.get("SLT_RUN_EPOCH", "r") / "evidence.json"
        stale.parent.mkdir(parents=True, exist_ok=True)
        stale.write_text(json.dumps({"evidenceVersion": 2, "kind": "case", "run": {"invocation": "0" * 32}}))
    Path(os.environ["XTR_DUMP"]).write_text(json.dumps(
        {"tables": TABLES, "slots": sorted(SLOTS), "namespaces": sorted(NAMESPACES), "files": sorted(FILES),
         "dirs": sorted(DIRS), "tid": TID}))


for name, path in SC.get("files", {}).items():
    FILES[name] = path
_real_smoke = P.assert_smoke
P._STATE_DIR = Path(os.environ["XTR_STATE"])
P._resolve_striim = resolve_striim
# like the real resolver, cached on the config (the envelope's runtime.striim.expected reads it)
P._resolve_release = lambda config: config.__dict__.setdefault(
    "_slt_release", {"STRIIM_VERSION": "5.4.0.6", "STRIIM_SERIES": "5.4", "JAVA_RELEASE": "17"})
P.load_service = lambda svc: _real.load_service(svc) if svc in REAL_SERVICES else SimpleNamespace(
    name=svc, container=f"slt-{svc}", compose=SC.get("compose"), provides={}, live_override_env=None,
    required_files=[], dir=Path("."))
P.build_service_tokens = lambda defn, resolved, schema: (
    _real.build_service_tokens(defn, resolved, schema) if defn.name in REAL_SERVICES
    else {"PG_SOURCE_SCHEMA": "qasource", "PG_TARGET_SCHEMA": "qatarget", "PG_SLOT": schema})
P.topology_satisfies = lambda required, topology: (True, "")
P.PgAdmin = FakePgAdmin
P.StriimClient = SimpleNamespace(from_url=lambda *a, **k: FakeClient())
P._deploy_tql_once_retrying = deploy
if SC.get("real_client"):
    # The real StriimClient.deploy_tql, the real StriimApi and the real retry wrapper; only the HTTP
    # call is fake. Each deploy attempt answers the next of real_client: Success, Failure, or NPE (the
    # transient import failure the wrapper retries once). Like Striim, every answer quotes the command.
    import striim_api
    from livetest.striim import StriimClient as _RealStriimClient
    _replies = list(SC["real_client"])
    _messages = {"Success": None, "Failure": "Invalid property in statement",
                 "NPE": 'Cannot invoke "com.x.Y.z()" because "w" is null'}

    def _tungsten_post(url, headers=None, data=None, **kw):
        status = _replies.pop(0) if len(_replies) > 1 else _replies[0]
        reply = [{"command": data, "executionStatus": "Success" if status == "Success" else "Failure",
                  "failureMessage": _messages[status]}]
        return SimpleNamespace(status_code=200, json=lambda: reply)
    striim_api.requests.post = _tungsten_post
    _api = striim_api.StriimApi.__new__(striim_api.StriimApi)
    _api.url_base, _api.getHeader = "http://striim.invalid", (lambda content_type: {})

    def _real_deploy_tql(self, tql):
        _RealStriimClient(_api).deploy_tql(tql)
        log("deploy", ns=None)
        STATE["running"] = True
    FakeClient.deploy_tql = _real_deploy_tql
    P._deploy_tql_once_retrying = _real.deploy_retrying
if not SC.get("real_tql"):                                # real_tql: the case's app.tql, rendered with its tokens
    P._rendered_tql = lambda m, tokens, renames: "tql"
P._node_log_marks = lambda ctx, run=None: {}
P.read_server_files = read_files
P.ensure_server_dir = lambda *a, **k: None


def _sha_of(path):
    return "sha256:" + hashlib.sha256(Path(path).read_bytes()).hexdigest()


def place_server_file(ctx, src, dest, run=None):           # The edge hashes the bytes it receives
    log("placed", dest=str(dest), sha=_sha_of(src))


P.place_server_file = place_server_file
P.opartifacts.upload_artifacts = lambda ctx, files, run=None, keep_existing=False: log("uploaded", sha=[_sha_of(f) for f in files])
P.assert_smoke = lambda client, apps, timeout, progress=None: _real_smoke(client, apps, timeout, settle_seconds=0, progress=progress)
def compose_down(*a, **k):
    log("compose_down")
    if SC.get("compose_down_fail"):
        raise RuntimeError("docker compose down failed: network slt-net has active endpoints")


P.compose_down = compose_down
_real_cleanup = O.Ledger.cleanup


def ledger_cleanup(self, **kw):
    if SC.get("persist_fail_in_cleanup"):
        def disk_full(*a, **k):
            raise OSError(28, "No space left on device")
        self.persist = disk_full
    return _real_cleanup(self, **kw)


O.Ledger.cleanup = ledger_cleanup
I._docker_ps = lambda argv: SimpleNamespace(stdout="", returncode=0)
I._docker_inspect = docker
# The running Striim's version is observed from the runtime (a fake runtime here), never the image tag
# (``runtime_probe: "real"`` runs the real probe instead; the docker fake answers it from ``runtime_listing``)
if SC.get("runtime_probe") != "real":
    I.runtime_version = lambda config, ctx, container: SC.get("runtime_version", "5.4.0.6")
import livetest.exactdata as X
X._run = lambda argv, timeout: (lambda r: SimpleNamespace(returncode=r.returncode, stdout=(r.stdout or "").encode(),
                                                          stderr=(r.stderr or "").encode()))(docker(argv))
if SC.get("op_stub"):                                      # an op module whose jar build is stubbed
    _stub_op = Path("stub-op.jar")
    if SC.get("op_poison"):
        _stub_op.write_bytes(b"stub-op")
    P.build_modules = lambda m, release, report=None: [
        (mod, SimpleNamespace(name="stub-op.jar", op_name="StubOp", path=_stub_op, content_tag="",
                              sha256="sha256:" + "0" * 64))
        for mod in m.modules]
if SC.get("op_poison"):
    P._node_log_tail = lambda ctx: "java.util.zip.ZipException: invalid LOC header (bad signature)"
    P._sp.restart_app_nodes = lambda client: log("restart_app_nodes")
    _real_in_use = P.opregistry.in_use
    _real_exclusive_reload = P.opregistry.exclusive_reload

    @contextmanager
    def traced_lock(kind, lock):
        log("lock_enter", kind=kind)
        try:
            with lock:
                yield
        finally:
            log("lock_exit", kind=kind)

    P.opregistry.in_use = lambda: traced_lock("shared", _real_in_use())
    P.opregistry.exclusive_reload = lambda: traced_lock("exclusive", _real_exclusive_reload())
P._sp.cluster_down = lambda *a, **k: log("cluster_down")
I.resolve_service = resolve_service
L._pg_connect = lambda **kw: FakeConn(kw["user"])
L.status_bounded = lambda client, app, remaining: client.current_status(app)
O._docker = docker
O._nodes = lambda: ("slt-striim",)
'''


class Run:
    def __init__(self, result, root, started, elapsed):
        self.result, self.root, self.started, self.elapsed = result, root, started, elapsed
        self.ret = result.ret
        self.text = result.stdout.str() + "\n" + result.stderr.str()

    def events(self):
        log = self.root / "fake.log"
        return [json.loads(ln) for ln in log.read_text().splitlines()] if log.exists() else []

    def names(self):
        return [e["event"] for e in self.events()]

    def dump(self):
        return json.loads((self.root / "dump.json").read_text())

    def junit(self):
        cases = list(ET.parse(self.root / "live" / "junit.xml").getroot().iter("testcase"))
        assert len(cases) == 1, self.text
        c = cases[0]
        status = "failed" if c.find("failure") is not None else "error" if c.find("error") is not None \
            else "skipped" if c.find("skipped") is not None else "passed"
        props = {p.get("name"): p.get("value") for p in c.iter("property")}
        return status, props, ET.tostring(c, encoding="unicode")

    def data(self):
        path = self.root / "data.json"
        return json.loads(path.read_text()) if path.exists() else None

    def sidecar(self):
        return json.loads((self.root / "live" / "junit.slt.json").read_text())["tests"][0]

    def envelope(self):
        found = list((self.root / "live" / "evidence").glob("*/*/evidence.json"))
        assert len(found) == 1, (found, self.text)
        assert found[0].stat().st_mtime >= self.started - 1          # fresh: written by this run
        return json.loads(found[0].read_text())


@pytest.fixture
def run_case(pytester, monkeypatch, tmp_path):
    def run(case, scenario=None, *, declared=True, edit=None, args=(), env=None, fixtures=None, prepare=None,
            guard=False):
        root = pytester.path
        dest = root / "cases" / case
        shutil.copytree((fixtures or FIXTURES) / case, dest)
        if edit:
            (dest / "test.yaml").write_text(edit((dest / "test.yaml").read_text()))
        name = next(ln.split(":", 1)[1].strip() for ln in (dest / "test.yaml").read_text().splitlines()
                    if ln.startswith("name:"))
        pytester.makeconftest(CONFTEST)
        (root / "state").mkdir(exist_ok=True)
        # The child runs with what a Python process needs plus what the test set, not the host's environment
        # (tests/_hermetic_child.py); the harness's own variables follow. A case that needs anything else passes it
        # in env.
        _hermetic_child.apply(monkeypatch, tmp_path)
        monkeypatch.setenv("XTR_EXEC", json.dumps(scenario or {}))
        monkeypatch.setenv("XTR_LOG", str(root / "fake.log"))
        monkeypatch.setenv("XTR_DUMP", str(root / "dump.json"))
        monkeypatch.setenv("XTR_CASE_NAME", name)
        monkeypatch.setenv("XTR_STATE", str(root / "state"))
        monkeypatch.setenv("SLT_STATE_DIR", str(root / "state"))
        monkeypatch.setenv("SLT_LOCK_DIR", str(tmp_path / "locks"))
        monkeypatch.setenv("SLT_RUN_EPOCH", RUN)
        monkeypatch.setenv("PYTHONPATH", os.pathsep.join(SOURCES + [os.environ.get("PYTHONPATH", "")]))
        if declared:
            monkeypatch.setenv("SLT_INFRA_OWNERSHIP", "shared")
            monkeypatch.setenv("SLT_KEEP_SERVICES", "1")
        for k, v in (env or {}).items():
            monkeypatch.setenv(k, v)
        guard_args = ()
        if guard:                                          # striim-test's real guard writes selection/results
            for src in SOURCES:
                if src not in sys.path:
                    monkeypatch.syspath_prepend(src)
            import livetest, inttest, striim_test
            (root / "live").mkdir(exist_ok=True)
            (root / "gold-targets.yaml").write_text("schemaVersion: 1\ntargets: []\nsuites:\n  live: cases\nstateDir: state\n")
            (root / "guard.json").write_text(json.dumps({
                "tier": "live", "suiteRoot": str(root / "cases"), "consumerRoot": str(root),
                "manifest": str(root / "gold-targets.yaml"), "cases": [],
                "packages": {m.__name__: str(Path(m.__file__).resolve().parent) for m in (livetest, inttest, striim_test)},
                "selectionOut": str(root / "live" / "selection.json"), "resultsOut": str(root / "live" / "results.json")}))
            monkeypatch.setenv("STRIIM_TEST_GUARD", str(root / "guard.json"))
            guard_args = ("-p", "striim_test.pytest_guard")
        if prepare:
            prepare(root, name)
        started = time.time()
        result = pytester.runpytest_subprocess(
            *guard_args, "-p", "livetest.plugin", "-o", "addopts=", "-p", "no:cacheprovider", "--import-mode=importlib",
            f"--junitxml={root / 'live' / 'junit.xml'}", *args, "cases", timeout=180)
        return Run(result, root, started, time.time() - started)
    return run


def _agree(run, status, qualifies=False):
    junit_status, props, xml = run.junit()
    envelope = run.envelope()
    assert junit_status == status, xml
    assert run.sidecar()["status"] == status
    assert envelope["run"]["status"] == status
    assert envelope["run"]["qualifies"] is qualifies, envelope["run"]["qualifiesReason"]
    assert props["slt_evidence_json"].endswith("evidence.json")
    assert props["slt_qualifies"] == ("true" if qualifies else "false")
    assert run.ret == (0 if status == "passed" else 1), run.text
    return envelope


