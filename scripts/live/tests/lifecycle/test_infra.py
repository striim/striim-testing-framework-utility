"""Infrastructure ownership (C7.1).

Declaration points, the exclusive allocation/lease/leftover/endpoint rules, the shared marker and
service adapter, and the enforcement that every live entry point declares before it can reach
``_resolve_striim``. Docker is always faked; cross-process rules use real subprocesses sharing a
temp lock dir.
"""
from __future__ import annotations

import ast
import hashlib
import json
import os
import subprocess
import sys
import threading
import time
from functools import partial
from pathlib import Path
from types import SimpleNamespace

import pytest

from livetest import infra, stack

LIVE = Path(__file__).resolve().parents[2]
REPO = LIVE.parents[1]


def _no_docker(argv):
    return SimpleNamespace(stdout="", returncode=0)


def _env(**kv):
    return {k: str(v) for k, v in kv.items()}


def _child_env(tmp_path, **extra):
    env = {k: v for k, v in os.environ.items()
           if not (k.startswith("SLT_") or k in ("PYTEST_XDIST_WORKER", "STRIIM_URL", "PYTHONPATH"))}
    env.update(PYTHONPATH=str(LIVE), PYTHONDONTWRITEBYTECODE="1", SLT_LOCK_DIR=str(tmp_path / "locks"))
    env.update({k: str(v) for k, v in extra.items()})
    return env


def _diag(procs) -> list:
    """Bounded failure diagnostics (4.1 fix r2): a live child is killed first, and every child's output is
    read with a timeout, so a failure message never waits on a child."""
    out = []
    for p in procs:
        if p.poll() is None:
            p.kill()
        try:
            out.append(p.communicate(timeout=10))
        except subprocess.TimeoutExpired:
            out.append(("<no output within 10s>", ""))
    return out


def _reap(procs) -> None:
    for p in procs:
        if p.poll() is None:
            p.kill()
            try:
                p.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                pass


def _wait_files(paths, procs, seconds: float, what: str) -> None:
    """Wait (bounded) until every path exists; a child that fails (non-zero exit, e.g. a refused declaration)
    fails the wait at once with its stderr. A clean exit is its normal end after writing its last file."""
    deadline = time.monotonic() + seconds
    while not all(p.exists() for p in paths):
        if any(proc.poll() not in (None, 0) for proc in procs):
            pytest.fail(f"{what}: a child failed: {_diag(procs)}")
        if time.monotonic() >= deadline:
            pytest.fail(f"{what}: not reached within {seconds:.0f}s: {_diag(procs)}")
        time.sleep(0.002)


def _finish(procs, seconds: float = 60) -> list:
    outs = []
    for p in procs:
        try:
            out, err = p.communicate(timeout=seconds)
        except subprocess.TimeoutExpired:
            pytest.fail(f"a child did not finish within {seconds:.0f}s: {_diag(procs)}")
        assert p.returncode == 0, err
        outs.append(out)
    return outs


_HOLDER = """\
import sys, time, pathlib
from types import SimpleNamespace
from livetest import infra
env = {"SLT_INFRA_OWNERSHIP": "exclusive", "SLT_RUN_EPOCH": sys.argv[1]}
i = infra.declare(env, run=lambda argv: SimpleNamespace(stdout="", returncode=0))
print("held", i.lease_kind, flush=True)
stop = pathlib.Path(sys.argv[2])
deadline = time.monotonic() + 60
while not stop.exists() and time.monotonic() < deadline:
    time.sleep(0.05)
i.release()
"""


class _Holder:
    """A second process holding the exclusive endpoint lease for run ``run_id``."""

    def __init__(self, tmp_path, run_id):
        self.stop = tmp_path / f"stop-{run_id}"
        self.proc = subprocess.Popen([sys.executable, "-c", _HOLDER, run_id, str(self.stop)],
                                     env=_child_env(tmp_path), stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, text=True)
        line = self.proc.stdout.readline()
        assert line.startswith("held held"), (line, self.proc.stderr.read() if self.proc.poll() is not None else "")

    def close(self):
        self.stop.write_text("")
        self.proc.wait(timeout=30)


# ---------------------------------------------------------------- declaration and the undeclared refusal

def test_undeclared_refused_naming_variable_exit_mapping(monkeypatch):
    with pytest.raises(infra.InfraOwnershipError) as ei:
        infra.declare({}, run=_no_docker)
    assert "SLT_INFRA_OWNERSHIP" in str(ei.value) and "exclusive" in str(ei.value) and "shared" in str(ei.value)
    with pytest.raises(infra.InfraOwnershipError, match="not an accepted value"):
        infra.declare(_env(SLT_INFRA_OWNERSHIP="mine"), run=_no_docker)
    assert infra.DEFAULT_INFRA_OWNERSHIP is None
    session = SimpleNamespace(items=[_Live()], config=SimpleNamespace(option=SimpleNamespace(collectonly=False)))
    with pytest.raises(pytest.UsageError, match="SLT_INFRA_OWNERSHIP"):
        infra.declare_if_live(session, _Live)
    # pytest maps a UsageError to exit 4; striim-test maps a tier's pytest exit 4 to C5 exit 2.
    monkeypatch.syspath_prepend(str(LIVE.parent / "cli"))   # scripts/cli, when the framework is not installed
    from striim_test import dispatch, errors
    assert int(pytest.ExitCode.USAGE_ERROR) == 4
    sel = {"selected": [{"id": "live:c::c", "nodeid": "c"}]}
    assert dispatch.map_exit(4, sel, None, False) == (errors.CONFIG, "pytest-exit:4") and errors.CONFIG == 2


class _Live:
    pass


def _session(items, collectonly=False):
    return SimpleNamespace(items=items, config=SimpleNamespace(option=SimpleNamespace(collectonly=collectonly)))


def test_declared_only_when_live_items_and_not_collect_only(monkeypatch):
    monkeypatch.setenv("SLT_INFRA_OWNERSHIP", "shared")
    monkeypatch.setenv("SLT_KEEP_SERVICES", "1")
    none = _session([object()])
    assert infra.declare_if_live(none, _Live) is None and not hasattr(none.config, "_slt_infra")
    collect = _session([_Live()], collectonly=True)
    assert infra.declare_if_live(collect, _Live) is None and not hasattr(collect.config, "_slt_infra")
    monkeypatch.delenv("SLT_INFRA_OWNERSHIP")            # neither of the above even reads it
    assert infra.declare_if_live(_session([_Live()], collectonly=True), _Live) is None
    monkeypatch.setenv("SLT_INFRA_OWNERSHIP", "shared")
    live = _session([object(), _Live()])
    decl = infra.declare_if_live(live, _Live)
    assert decl is live.config._slt_infra and decl.ownership == "shared"
    infra.release(live.config)


def _plugin():
    from livetest import plugin
    return plugin


def test_worker_env_declares_in_collection_finish(monkeypatch):
    plugin = _plugin()
    monkeypatch.setenv("PYTEST_XDIST_WORKER", "gw1")
    monkeypatch.setenv("SLT_INFRA_OWNERSHIP", "shared")
    monkeypatch.setenv("SLT_KEEP_SERVICES", "1")
    item = object.__new__(plugin.LiveItem)
    session = _session([item])
    plugin.pytest_collection_finish(session)
    decl = session.config._slt_infra
    assert decl.ownership == "shared" and decl.lease_path.is_file()
    plugin.pytest_unconfigure(session.config)
    assert not decl.lease_path.exists()
    monkeypatch.delenv("SLT_INFRA_OWNERSHIP")
    with pytest.raises(pytest.UsageError, match="SLT_INFRA_OWNERSHIP"):
        plugin.pytest_collection_finish(_session([item]))


def test_pytest_configure_unchanged_for_hermetic_callers(monkeypatch, tmp_path):
    plugin = _plugin()
    monkeypatch.setattr(plugin, "clear_provision_registry", lambda *a, **k: None)
    monkeypatch.setattr(plugin, "_STATE_DIR", tmp_path)
    config = SimpleNamespace(option=SimpleNamespace(numprocesses=None, dist=None))
    plugin.pytest_configure(config)                      # no SLT_INFRA_OWNERSHIP: no refusal
    assert not hasattr(config, "_slt_infra")
    with pytest.raises(pytest.UsageError, match="SLT_PARALLEL"):  # the serial guard still fires
        plugin.pytest_configure(SimpleNamespace(option=SimpleNamespace(numprocesses=4, dist=None)))


@pytest.mark.parametrize("entry", ["provision", "provision_cluster", "restart_app_nodes"])
def test_preflight_entrypoints_refuse_undeclared(entry, monkeypatch, tmp_path, capsys):
    plugin = _plugin()
    # preflight.py binds plugin._STATE_DIR/_STRIIM_DIR at import time;
    # stand both in before the fresh import so this hermetic run needs no state dir.
    monkeypatch.setattr(plugin, "_STATE_DIR", tmp_path)
    monkeypatch.setattr(plugin, "_STRIIM_DIR", tmp_path)
    monkeypatch.delitem(sys.modules, "livetest.preflight", raising=False)
    import importlib
    preflight = importlib.import_module("livetest.preflight")
    calls = []
    monkeypatch.setattr(preflight, "_resolve_striim", lambda cfg: calls.append("resolve"))
    monkeypatch.setattr(preflight, "_resolve_release", lambda cfg: calls.append("release") or {})
    monkeypatch.setattr(preflight, "clear_provision_registry", lambda *a, **k: calls.append("clear"))
    monkeypatch.setattr(preflight, "manifests_for", lambda ids: [SimpleNamespace()])
    monkeypatch.setattr(preflight, "compute_unions", lambda ms: ([], []))
    fn = getattr(preflight, entry)
    rc = fn(["some-test"]) if entry == "provision" else fn()
    assert rc == 2
    assert calls == []
    assert "SLT_INFRA_OWNERSHIP" in capsys.readouterr().out


_SCAN_ROOTS = ("scripts/live/livetest", "scripts/integration/inttest", "scripts/cli")
_DECLARERS = ("declare", "declare_or_log")


def _resolver_violations(source: str, rel: str):
    """(call sites, violations) of ``_resolve_striim`` in one module.

    A call must pass a plain config name, and the enclosing function must, unconditionally (a statement of
    the function body itself, before the call's statement): bind a name from ``declare``/``declare_or_log``,
    return when that name is None, and assign the name to ``<that config>._slt_infra``. Aliases (``import
    ... as``, ``x = _resolve_striim``) are followed, and any other reference to the resolver (passing it as a
    value) is a violation, so a caller cannot slip past the scanner. ``LiveItem._runtest`` is the live item
    path behind the H0 declaration."""
    tree = ast.parse(source)
    parents = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    names = {"_resolve_striim"}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            names |= {a.asname for a in node.names if a.name == "_resolve_striim" and a.asname}
        elif isinstance(node, ast.Assign) and isinstance(node.value, (ast.Name, ast.Attribute)) and \
                (getattr(node.value, "id", None) or getattr(node.value, "attr", None)) in names:
            names |= {t.id for t in node.targets if isinstance(t, ast.Name)}

    def enclosing(node, kinds):
        cur = parents.get(node)
        while cur is not None and not isinstance(cur, kinds):
            cur = parents.get(cur)
        return cur

    sites, bad = set(), []
    for node in ast.walk(tree):
        ref = (node.id if isinstance(node, ast.Name) else node.attr if isinstance(node, ast.Attribute) else None)
        if ref not in names:
            continue
        parent = parents.get(node)
        if isinstance(parent, ast.Assign) and parent.value is node:
            continue                                            # an alias binding, followed above
        fn = enclosing(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        cls = enclosing(fn, ast.ClassDef) if fn is not None else None
        label = f"{rel}:{cls.name + '.' if cls else ''}{fn.name if fn else '<module>'}"
        if not (isinstance(parent, ast.Call) and parent.func is node):
            bad.append(f"{label} (line {node.lineno}): the resolver is referenced without being called")
            continue
        sites.add(label)
        if label.endswith("plugin.py:LiveItem._runtest"):
            continue
        if fn is None or not parent.args or not isinstance(parent.args[0], ast.Name):
            bad.append(f"{label} (line {node.lineno}): not called with a plain config name")
            continue
        cfg = parent.args[0].id
        stmt = parent
        while parents.get(stmt) is not fn:
            stmt = parents[stmt]
        before = fn.body[:fn.body.index(stmt)]
        decls = {s.targets[0].id: i for i, s in enumerate(before)
                 if isinstance(s, ast.Assign) and len(s.targets) == 1 and isinstance(s.targets[0], ast.Name)
                 and isinstance(s.value, ast.Call)
                 and (getattr(s.value.func, "attr", None) or getattr(s.value.func, "id", None)) in _DECLARERS}
        guarded = {d for d, i in decls.items() if any(
            isinstance(s, ast.If) and isinstance(s.test, ast.Compare) and isinstance(s.test.left, ast.Name)
            and s.test.left.id == d and isinstance(s.test.ops[0], ast.Is)
            and isinstance(s.test.comparators[0], ast.Constant) and s.test.comparators[0].value is None
            and any(isinstance(b, ast.Return) for b in s.body) for s in before[i + 1:])}
        bound = any(isinstance(s, ast.Assign) and len(s.targets) == 1 and isinstance(s.targets[0], ast.Attribute)
                    and s.targets[0].attr == "_slt_infra" and isinstance(s.targets[0].value, ast.Name)
                    and s.targets[0].value.id == cfg and isinstance(s.value, ast.Name) and s.value.id in guarded
                    for s in before)
        if not bound:
            bad.append(f"{label} (line {node.lineno}): {cfg}._slt_infra is not unconditionally assigned a guarded "
                       f"declaration before the call")
    return sites, bad


def test_every_resolver_caller_is_behind_declaration():
    """Every non-test call of ``_resolve_striim`` sits behind an ownership declaration of the config it
    passes (the pre-flight entry points), or is ``LiveItem._runtest``, whose session declared in
    ``pytest_collection_finish`` (H0)."""
    plugin_src = (LIVE / "livetest" / "plugin.py").read_text()
    h0 = ast.parse(plugin_src)
    finish = [n for n in h0.body if isinstance(n, ast.FunctionDef) and n.name == "pytest_collection_finish"]
    assert finish and "declare_if_live" in ast.unparse(finish[0]), "H0 declaration hook is missing"
    sites, bad = set(), []
    for root in _SCAN_ROOTS:
        for path in sorted((REPO / root).rglob("*.py")):
            if "tests" in path.relative_to(REPO).parts:
                continue
            found, violations = _resolver_violations(path.read_text(), str(path.relative_to(REPO)))
            sites |= found
            bad += violations
    assert {"scripts/live/livetest/plugin.py:LiveItem._runtest",
            "scripts/live/livetest/preflight.py:provision",
            "scripts/live/livetest/preflight.py:provision_cluster",
            "scripts/live/livetest/preflight.py:restart_app_nodes"} <= sites, sites
    assert bad == [], f"_resolve_striim reachable without an ownership declaration: {bad}"


_GOOD_CALLER = """
def entry():
    decl = _slt_infra.declare_or_log(os.environ, _log)
    if decl is None:
        return 2
    cfg = _PreflightConfig()
    cfg._slt_infra = decl
    try:
        ctx = _resolve_striim(cfg)
    except Exception:
        return 1
"""

_BAD_CALLERS = {
    "conditional-declaration": """
def entry(cfg):
    if False:
        cfg._slt_infra = None
    _resolve_striim(cfg)
""",
    "alias": """
from livetest.plugin import _resolve_striim as resolve_cluster
def entry(cfg):
    resolve_cluster(cfg)
""",
    "declaration-on-another-config": """
def entry(cfg, other):
    decl = _slt_infra.declare_or_log(os.environ, _log)
    if decl is None:
        return 2
    other._slt_infra = decl
    _resolve_striim(cfg)
""",
    "unguarded-or-fake-declaration": """
def entry(cfg):
    cfg._slt_infra = object()
    _resolve_striim(cfg)
""",
    "declared-after-the-call": """
def entry(cfg):
    _resolve_striim(cfg)
    decl = _slt_infra.declare_or_log(os.environ, _log)
    if decl is None:
        return 2
    cfg._slt_infra = decl
""",
    "passed-as-a-value": """
def entry(cfg):
    run_later(_resolve_striim, cfg)
""",
}


@pytest.mark.parametrize("case", sorted(_BAD_CALLERS))
def test_resolver_scanner_rejects_callers_that_only_look_declared(case):
    assert _resolver_violations(_GOOD_CALLER, "good.py") == ({"good.py:entry"}, [])
    _sites, bad = _resolver_violations(_BAD_CALLERS[case], "bad.py")
    assert bad, f"the scanner accepted {case}"


def _preflight(monkeypatch, tmp_path):
    plugin = _plugin()
    monkeypatch.setattr(plugin, "_STATE_DIR", tmp_path)
    monkeypatch.setattr(plugin, "_STRIIM_DIR", tmp_path)
    monkeypatch.delitem(sys.modules, "livetest.preflight", raising=False)
    import importlib
    return importlib.import_module("livetest.preflight")


def test_provision_services_refuses_undeclared_before_any_bring_up(monkeypatch, tmp_path, capsys):
    preflight = _preflight(monkeypatch, tmp_path)
    calls = []
    monkeypatch.setattr(preflight, "resolve", lambda *a, **k: calls.append("resolve"))
    monkeypatch.setattr(preflight, "_prune_dead_records", lambda names: calls.append("prune"))
    monkeypatch.setattr(preflight, "_note_gcs_public_host", lambda: calls.append("gcs-host"))
    monkeypatch.setattr(preflight, "ensure_provisioned_once", lambda *a, **k: calls.append("setup"))
    assert preflight.provision_services(["postgres", "gcs"], env={}) == 2
    assert calls == []
    assert "SLT_INFRA_OWNERSHIP" in capsys.readouterr().out


class _StopAtBringUp(Exception):
    pass


def test_full_preflight_hands_its_declaration_to_the_service_adapter(monkeypatch, tmp_path):
    preflight = _preflight(monkeypatch, tmp_path)
    monkeypatch.setenv("SLT_INFRA_OWNERSHIP", "shared")
    monkeypatch.setenv("SLT_KEEP_SERVICES", "1")
    seen = {}
    monkeypatch.setattr(preflight, "clear_provision_registry", lambda *a, **k: None)
    monkeypatch.setattr(preflight, "manifests_for", lambda ids: [SimpleNamespace(requires=["postgres"])])
    monkeypatch.setattr(preflight, "compute_unions", lambda ms: (["postgres"], []))
    monkeypatch.setattr(preflight, "_resolve_release", lambda cfg: {})
    monkeypatch.setattr(preflight, "_resolve_striim", lambda cfg: SimpleNamespace(url="http://localhost:9080", user="admin",
                                                                                 password="striim", mode="docker"))

    def bring_up(services, env, apply_gate=True, **kw):
        seen.update(kw, services=services)
        assert kw["pre_up"] is True
        assert kw["mode"] == "docker"
        raise _StopAtBringUp()
    monkeypatch.setattr(preflight, "_bring_up_services", bring_up)
    with pytest.raises(_StopAtBringUp):
        preflight.provision(["some-test"])
    assert seen["services"] == ["postgres"]
    assert isinstance(seen.get("infra"), infra.Infra) and seen["infra"].ownership == "shared"
    seen["infra"].release()


_PREFLIGHT_WORKER = """\
import json, pathlib, sys, time
from types import SimpleNamespace
from livetest import plugin
plugin._STATE_DIR = plugin._STRIIM_DIR = pathlib.Path(sys.argv[3])
from livetest import infra, preflight, registry, services
work, tag = pathlib.Path(sys.argv[1]), sys.argv[2]
marker, ups = work / "container-up", work / "ups.log"
defn = SimpleNamespace(name="kafka", container="slt-kafka", live_override_env=None)
registry.load_service = lambda name: defn
services.container_running = lambda container: marker.exists()

def fake_resolve(name, env, started, progress=None):
    if defn.container not in started:
        time.sleep(1.0)
        with open(ups, "a") as f:
            f.write(tag + "\\n")
        marker.write_text("up")
        started.add(defn.container)
    return SimpleNamespace(name=name, base={})

preflight.resolve = fake_resolve
decl = infra.declare({"SLT_INFRA_OWNERSHIP": "shared", "SLT_KEEP_SERVICES": "1"})
(work / f"ready-{tag}").write_text("")
go_by = time.monotonic() + 60
while not (work / "go").exists():
    if time.monotonic() > go_by:
        sys.exit("no go within 60s")
    time.sleep(0.01)
preflight._bring_up_services(["kafka"], {}, apply_gate=False, infra=decl)
print(json.dumps(decl.services))
decl.release()
"""


def test_concurrent_shared_preflights_provision_a_service_once(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    for tag in ("a", "b"):   # a set SLT_STATE_DIR must exist (paths.state_dir, at plugin import)
        (tmp_path / f"state-{tag}").mkdir()
    procs = [subprocess.Popen([sys.executable, "-c", _PREFLIGHT_WORKER, str(work), tag, str(tmp_path)],
                              env={**_child_env(tmp_path, SLT_STATE_DIR=tmp_path / f"state-{tag}"),
                                   "PYTHONPATH": os.pathsep.join([str(LIVE), os.environ.get("PYTHONPATH", "")])},
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
             for tag in ("a", "b")]
    try:
        _wait_files([work / f"ready-{t}" for t in "ab"], procs, 60, "shared preflights ready")
        (work / "go").write_text("")
        outs = _finish(procs)
    finally:
        _reap(procs)
    statuses = [s["status"] for out in outs for s in json.loads(out.splitlines()[-1])]
    assert sorted(statuses) == ["provisioned-and-kept", "reused"]
    assert len((work / "ups.log").read_text().splitlines()) == 1


# ---------------------------------------------------------------- 4.1 fix r2: concurrent shared declarations

_SHARED_STRESS = """\
import json, pathlib, sys, time
from livetest import infra
work, tag, rounds, per_round = pathlib.Path(sys.argv[1]), sys.argv[2], int(sys.argv[3]), int(sys.argv[4])
env = {"SLT_INFRA_OWNERSHIP": "shared", "SLT_KEEP_SERVICES": "1", "SLT_RUN_EPOCH": "stress"}
declared, refused = 0, []
(work / f"ready-{tag}").write_text("")
for r in range(rounds):
    by = time.monotonic() + 30
    while not (work / f"go-{r}").exists():
        if time.monotonic() > by:
            sys.exit(f"no go-{r} within 30s")
        time.sleep(0.001)
    for _ in range(per_round):
        try:
            infra.declare(dict(env)).release()
            declared += 1
        except infra.InfraOwnershipError as e:
            refused.append(f"round {r}: {e}")
    (work / f"done-{r}-{tag}").write_text("")
print(json.dumps({"tag": tag, "declared": declared, "refused": refused}))
"""


def test_concurrent_shared_declarations_never_refuse_each_other(tmp_path):
    """C7.1: shared declarations on one port and lock dir always coexist. 8 processes x 20 synchronized rounds
    (5 declare/release each) must never see another shared probe as an exclusive holder (4.1 fix r2)."""
    procs_n, rounds, per_round = 8, 20, 5
    work = tmp_path / "work"
    work.mkdir()
    tags = [f"p{i}" for i in range(procs_n)]
    started = time.monotonic()
    procs = [subprocess.Popen([sys.executable, "-c", _SHARED_STRESS, str(work), t, str(rounds), str(per_round)],
                              env=_child_env(tmp_path), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
             for t in tags]
    try:
        _wait_files([work / f"ready-{t}" for t in tags], procs, 30, "stress children ready")
        for r in range(rounds):
            (work / f"go-{r}").write_text("")
            _wait_files([work / f"done-{r}-{t}" for t in tags], procs, 15, f"stress round {r}")
        outs = _finish(procs, 30)
    finally:
        _reap(procs)
    results = [json.loads(out.splitlines()[-1]) for out in outs]
    refused = [x for res in results for x in res["refused"]]
    total = procs_n * rounds * per_round
    assert refused == [], f"{len(refused)} of {total} shared declarations refused: {refused[:3]}"
    assert sum(res["declared"] for res in results) == total
    assert not list((tmp_path / "locks" / ".slt-endpoint-9080.shared").glob("*"))    # every marker released
    assert time.monotonic() - started < 30


# ---------------------------------------------------------------- exclusive

def test_exclusive_refuses_operator_prefix():
    with pytest.raises(infra.InfraOwnershipError, match="operator SLT_STACK_PREFIX"):
        infra.declare(_env(SLT_INFRA_OWNERSHIP="exclusive", SLT_RUN_EPOCH="r1", SLT_STACK_PREFIX="alt"),
                      run=_no_docker)


def test_exclusive_allocates_prefix_from_run_id_and_sets_env():
    env = _env(SLT_INFRA_OWNERSHIP="exclusive", SLT_RUN_EPOCH="20260914T230000Z-abcd1234")
    decl = infra.declare(env, run=_no_docker)
    want = "x" + hashlib.sha256(b"20260914T230000Z-abcd1234").hexdigest()[:8]
    try:
        assert decl.stack_prefix == want and env["SLT_STACK_PREFIX"] == want
        assert stack.prefix(env) == want and stack.striim_container(env) == f"{want}-slt-striim"
        assert decl.lease_kind == "held" and decl.lease_path.name == ".slt-endpoint-9080.exclusive"
        rec = decl.record()
        assert rec["ownership"] == "exclusive" and rec["stackPrefix"] == want and rec["lease"]["kind"] == "held"
    finally:
        decl.release()
    unset = {"SLT_INFRA_OWNERSHIP": "exclusive"}
    decl2 = infra.declare(unset, run=_no_docker)          # no run id yet: one is stamped first
    try:
        assert unset["SLT_RUN_EPOCH"] and decl2.stack_prefix == unset["SLT_STACK_PREFIX"]
    finally:
        decl2.release()


def test_exclusive_lease_conflict_second_exclusive_refused(tmp_path):
    holder = _Holder(tmp_path, "run-a")
    try:
        with pytest.raises(infra.InfraOwnershipError, match="another exclusive run holds the endpoint lease"):
            infra.declare(_env(SLT_INFRA_OWNERSHIP="exclusive", SLT_RUN_EPOCH="run-b"), run=_no_docker)
    finally:
        holder.close()
    decl = infra.declare(_env(SLT_INFRA_OWNERSHIP="exclusive", SLT_RUN_EPOCH="run-b"), run=_no_docker)
    assert decl.lease_kind == "held"                      # free again once the holder released it
    decl.release()


def test_exclusive_same_run_worker_joins_lease(tmp_path):
    holder = _Holder(tmp_path, "run-a")
    try:
        env = _env(SLT_INFRA_OWNERSHIP="exclusive", SLT_RUN_EPOCH="run-a", PYTEST_XDIST_WORKER="gw1")
        decl = infra.declare(env, run=lambda argv: pytest.fail("a joining worker must not re-run the leftover check"))
        assert decl.lease_kind == "joined"
        assert env["SLT_STACK_PREFIX"] == "x" + hashlib.sha256(b"run-a").hexdigest()[:8]
        decl.release()                                    # a joiner never releases the holder's lease
    finally:
        holder.close()


def test_exclusive_refused_while_live_shared_marker_exists(tmp_path):
    marker = tmp_path / "locks" / ".slt-endpoint-9080.shared" / str(os.getpid())
    marker.parent.mkdir(parents=True)
    marker.write_text("{}")
    with pytest.raises(infra.InfraOwnershipError, match="shared run"):
        infra.declare(_env(SLT_INFRA_OWNERSHIP="exclusive", SLT_RUN_EPOCH="r"), run=_no_docker)
    marker.unlink()
    (marker.parent / "999999999").write_text("{}")        # a dead pid is not a live registration
    decl = infra.declare(_env(SLT_INFRA_OWNERSHIP="exclusive", SLT_RUN_EPOCH="r"), run=_no_docker,
                         pid_alive=lambda pid: pid != 999999999)
    decl.release()


def test_shared_refused_while_exclusive_lease_held(tmp_path):
    holder = _Holder(tmp_path, "run-x")
    try:
        with pytest.raises(infra.InfraOwnershipError, match="exclusive run holds the endpoint lease"):
            infra.declare(_env(SLT_INFRA_OWNERSHIP="shared", SLT_KEEP_SERVICES="1"), run=_no_docker)
    finally:
        holder.close()


_JOINER = """\
import pathlib, sys, time
from types import SimpleNamespace
from livetest import infra
work = pathlib.Path(sys.argv[2])
real, paused = infra._read_owner, []


def gated(path):
    value = real(path)
    if value is not None and str(path).endswith(".owner") and not paused:
        paused.append(path)                 # the joiner has read the holder's owner record: stop here, once
        (work / "join-observed").write_text(value.get("runId", ""))
        deadline = time.monotonic() + 60
        while not (work / "join-go").exists():
            if time.monotonic() > deadline:
                sys.exit("no go within 60s")
            time.sleep(0.01)
    return value


infra._read_owner = gated
try:
    i = infra.declare({"SLT_INFRA_OWNERSHIP": "exclusive", "SLT_RUN_EPOCH": sys.argv[1]},
                      run=lambda argv: SimpleNamespace(stdout="", returncode=0))
except infra.InfraOwnershipError as e:
    (work / "join-result").write_text("refused: " + str(e))
else:
    (work / "join-result").write_text(i.lease_kind)
    i.release()
"""


@pytest.mark.parametrize("after_release", ["shared", "exclusive", "holder-kept"])
def test_exclusive_joiner_revalidates_after_last_holder_release(tmp_path, after_release):
    """r1 F4, the review's interleaving: a same-run joiner has read the holder's owner record and pauses before
    registering; the holder releases as the run's last member; a shared run or another exclusive run is admitted;
    then the joiner resumes. It is refused, never ``joined`` on the stale record. ``holder-kept``: with no
    release in between, the same pause still joins."""
    work = tmp_path / "work"
    work.mkdir()
    holder = _Holder(tmp_path, "run-a")
    joiner = subprocess.Popen([sys.executable, "-c", _JOINER, "run-a", str(work)], env=_child_env(tmp_path),
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    other = None
    try:
        _wait_files([work / "join-observed"], [joiner], 60, "the joiner read the holder's owner record")
        if after_release != "holder-kept":
            holder.close()                                # run-a's last registered member releases
            assert holder.proc.returncode == 0
            if after_release == "shared":
                other = infra.declare(_env(SLT_INFRA_OWNERSHIP="shared", SLT_KEEP_SERVICES="1"), run=_no_docker)
            else:
                other = infra.declare(_env(SLT_INFRA_OWNERSHIP="exclusive", SLT_RUN_EPOCH="run-b"), run=_no_docker)
        (work / "join-go").write_text("")
        _wait_files([work / "join-result"], [joiner], 60, "the joiner resumed and decided")
        _finish([joiner])
        result = (work / "join-result").read_text()
    finally:
        _reap([joiner])
        if other is not None:
            other.release()
        if holder.proc.poll() is None:
            holder.close()
    if after_release == "shared":
        assert other.lease_kind == "shared-marker"
        assert result.startswith("refused: ") and "shared run(s) are registered" in result, result
    elif after_release == "exclusive":
        assert other.lease_kind == "held"
        assert result.startswith("refused: ") and "another exclusive run holds" in result and "run-b" in result, result
    else:
        assert result == "joined"


_RELEASE_RACE = """\
import pathlib, sys, time
from types import SimpleNamespace
from livetest import infra
role, work = sys.argv[1], pathlib.Path(sys.argv[2])
infra._REGISTRY_WAIT_S = float(sys.argv[3])


def wait(name):
    deadline = time.monotonic() + 60
    while not (work / name).exists():
        if time.monotonic() > deadline:
            sys.exit(f"no {name} within 60s")
        time.sleep(0.01)


if role == "join":
    real = infra._add_member

    def paused(*args):                      # revalidated under the registry lock, not yet registered: stop here
        (work / "join-validated").write_text("")
        wait("join-go")
        return real(*args)

    infra._add_member = paused
i = infra.declare({"SLT_INFRA_OWNERSHIP": "exclusive", "SLT_RUN_EPOCH": "run-a"},
                  run=lambda argv: SimpleNamespace(stdout="", returncode=0))
(work / (role + "-ready")).write_text(i.lease_kind)
wait(role + "-stop")
i.release()
(work / (role + "-released")).write_text("")
"""


@pytest.mark.parametrize("registry", ["timeout", "locked"])
def test_holder_release_cannot_orphan_a_revalidated_joiner(tmp_path, registry):
    """A same-run joiner has revalidated under the registry lock and pauses before registering; the
    holder, the run's only member, releases. ``timeout``: its (shortened) registry wait expires and release returns
    without touching the run's records, reporting the deferral. ``locked``: release waits for the joiner's section.
    Either way the joiner completes ``joined``, a shared run and another exclusive run are refused while it is
    alive, and the endpoint is free once it releases."""
    work = tmp_path / "work"
    work.mkdir()

    def spawn(role, wait_s):
        return subprocess.Popen([sys.executable, "-c", _RELEASE_RACE, role, str(work), wait_s],
                                env=_child_env(tmp_path), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    holder = spawn("holder", "0.2" if registry == "timeout" else "60")
    procs = [holder]
    try:
        _wait_files([work / "holder-ready"], procs, 60, "the holder took the lease")
        joiner = spawn("join", "60")
        procs.append(joiner)
        _wait_files([work / "join-validated"], procs, 60, "the joiner revalidated under the registry lock")
        (work / "holder-stop").write_text("")
        if registry == "timeout":
            _wait_files([work / "holder-released"], procs, 60, "the holder's release gave up on the registry lock")
        else:
            time.sleep(1.0)
            assert not (work / "holder-released").exists(), "release did not wait for the joiner's locked section"
        (work / "join-go").write_text("")
        _wait_files([work / "join-ready", work / "holder-released"], procs, 60, "the joiner registered and the holder released")
        _, holder_err = holder.communicate(timeout=60)
        assert holder.returncode == 0, holder_err
        assert (work / "join-ready").read_text() == "joined"
        with pytest.raises(infra.InfraOwnershipError, match="exclusive run holds the endpoint lease.*run run-a"):
            infra.declare(_env(SLT_INFRA_OWNERSHIP="shared", SLT_KEEP_SERVICES="1"), run=_no_docker)
        with pytest.raises(infra.InfraOwnershipError, match="exclusive run run-a is still active"):
            infra.declare(_env(SLT_INFRA_OWNERSHIP="exclusive", SLT_RUN_EPOCH="run-b"), run=_no_docker)
        assert ("infrastructure release deferred" in holder_err) == (registry == "timeout"), holder_err
        (work / "join-stop").write_text("")
        _finish([joiner])
    finally:
        _reap(procs)
    shared = infra.declare(_env(SLT_INFRA_OWNERSHIP="shared", SLT_KEEP_SERVICES="1"), run=_no_docker)
    assert shared.lease_kind == "shared-marker"          # the run's last member cleaned up under the lock
    shared.release()


_REGISTRY_HOLDER = """\
import sys
from filelock import FileLock
lock = FileLock(sys.argv[1])
lock.acquire(timeout=30)
print("locked", flush=True)
sys.stdin.readline()
"""


def test_release_after_registry_timeout_keeps_the_claim_until_a_later_release(tmp_path, monkeypatch, capsys):
    """The timeout branch alone: another process holds the registry lock, so the holder's release
    changes nothing (member record, owner record, lease) and reports it; a later release with the lock free
    finishes the cleanup, and a shared run is admitted."""
    decl = infra.declare(_env(SLT_INFRA_OWNERSHIP="exclusive", SLT_RUN_EPOCH="run-a"), run=_no_docker)
    owner, member = Path(str(decl.lease_path) + ".owner"), decl._member
    locker = subprocess.Popen([sys.executable, "-c", _REGISTRY_HOLDER, str(infra._registry_path(decl.lock_dir, decl.port))],
                              env=_child_env(tmp_path), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True)
    try:
        assert locker.stdout.readline().startswith("locked"), _diag([locker])
        monkeypatch.setattr(infra, "_REGISTRY_WAIT_S", 0.2)
        decl.release()                                    # never raises
        assert owner.exists() and member is not None and member.exists()
        assert decl._member == member and decl._lease is not None and decl._lease.is_locked
        err = capsys.readouterr().err
        assert "infrastructure release deferred" in err and "run-a" in err, err
        _finish([locker])                                 # stdin closes: the registry lock is free again
    finally:
        _reap([locker])
    decl.release()
    assert not owner.exists() and not member.exists() and decl._lease is None and decl._member is None
    shared = infra.declare(_env(SLT_INFRA_OWNERSHIP="shared", SLT_KEEP_SERVICES="1"), run=_no_docker)
    assert shared.lease_kind == "shared-marker"
    shared.release()


def test_exclusive_refuses_leftover_containers_naming_stop_command():
    seen = []

    def docker(argv):
        seen.append(argv)
        return SimpleNamespace(stdout="0123abcd\n", returncode=0)

    prefix = "x" + hashlib.sha256(b"r9").hexdigest()[:8]
    with pytest.raises(infra.InfraOwnershipError) as ei:
        infra.declare(_env(SLT_INFRA_OWNERSHIP="exclusive", SLT_RUN_EPOCH="r9"), run=docker)
    assert f"SLT_STACK_PREFIX={prefix} python -m livetest.cli stop all" in str(ei.value)
    assert seen == [["docker", "ps", "-a", "--filter", f"name=^{prefix}-slt-", "-q"]]
    decl = infra.declare(_env(SLT_INFRA_OWNERSHIP="exclusive", SLT_RUN_EPOCH="r9"), run=_no_docker)
    decl.release()                                        # the refused attempt released its lease


def test_exclusive_refuses_reachable_or_open_endpoint_before_provisioning(tmp_path):
    cfg = SimpleNamespace(_slt_infra=infra.Infra("exclusive", 9080, tmp_path))
    url = "http://localhost:9080"
    why = infra.before_cluster_resolution(cfg, url, "localhost", 9080, lambda: True, lambda h, p: False)
    assert "already reachable" in why and "never adopts" in why
    why = infra.before_cluster_resolution(cfg, url, "localhost", 9080, lambda: False, lambda h, p: True)
    assert "already open" in why
    assert infra.before_cluster_resolution(cfg, url, "localhost", 9080, lambda: False, lambda h, p: False) is None
    shared = SimpleNamespace(_slt_infra=infra.Infra("shared", 9080, tmp_path))
    assert infra.before_cluster_resolution(shared, url, "localhost", 9080, lambda: True, lambda h, p: True) is None
    assert infra.before_cluster_resolution(SimpleNamespace(), url, "localhost", 9080,
                                           lambda: True, lambda h, p: True) is None


def test_exclusive_bind_striim_checks_published_port(tmp_path):
    ctx = SimpleNamespace(url="http://localhost:9080")
    seen = []

    def ports(host_port):
        def run(argv):
            seen.append(argv)
            return SimpleNamespace(stdout=json.dumps({"9080/tcp": [{"HostIp": "0.0.0.0", "HostPort": host_port}],
                                                      "9300/tcp": None}), returncode=0)
        return run

    cfg = SimpleNamespace(_slt_infra=infra.Infra("exclusive", 9080, tmp_path))
    assert infra.bind_striim(cfg, ctx, True, "xabc-slt-striim", run=ports("9080")) is None
    assert cfg._slt_infra.striim == {"status": "owned", "container": "xabc-slt-striim", "urlPort": 9080,
                                     "boundToAllocated": True, "provisionedBy": "this-process"}
    assert seen[0][:2] == ["docker", "inspect"] and seen[0][-1] == "xabc-slt-striim"
    why = infra.bind_striim(cfg, ctx, True, "xabc-slt-striim", run=ports("19080"))
    assert "not published by the allocated container" in why and cfg._slt_infra.striim["boundToAllocated"] is False
    assert "provisioned=False" in infra.bind_striim(cfg, ctx, False, "xabc-slt-striim", run=ports("9080"))
    shared = SimpleNamespace(_slt_infra=infra.Infra("shared", 9080, tmp_path))
    assert infra.bind_striim(shared, ctx, False, "slt-striim", run=lambda a: pytest.fail("no docker")) is None
    assert shared._slt_infra.striim["status"] == "reused"
    assert infra.bind_striim(SimpleNamespace(), ctx, True, "slt-striim", run=lambda a: pytest.fail("no docker")) is None


# ---------------------------------------------------------------- shared

def test_shared_requires_keep_services():
    with pytest.raises(infra.InfraOwnershipError, match="SLT_KEEP_SERVICES=1"):
        infra.declare(_env(SLT_INFRA_OWNERSHIP="shared"), run=_no_docker)


def test_shared_records_lock_dir_and_marker(tmp_path):
    decl = infra.declare(_env(SLT_INFRA_OWNERSHIP="shared", SLT_KEEP_SERVICES="1", SLT_RUN_EPOCH="r2"),
                         run=lambda argv: pytest.fail("shared runs no docker at declaration"))
    marker = tmp_path / "locks" / ".slt-endpoint-9080.shared" / str(os.getpid())
    assert decl.lease_path == marker and json.loads(marker.read_text())["pid"] == os.getpid()
    rec = decl.record()
    assert rec["lockDir"] == str(tmp_path / "locks") and rec["lease"] == {"kind": "shared-marker", "path": str(marker)}
    assert rec["ownership"] == "shared" and rec["services"] == [] and rec["stackPrefix"] == ""
    decl.release()
    assert not marker.exists()


# ---------------------------------------------------------------- the shared service adapter (N3)

_ADAPTER = """\
import json, pathlib, sys, time
from types import SimpleNamespace
from livetest import infra
work = pathlib.Path(sys.argv[1])
marker, ups = work / "container-up", work / "ups.log"
defn = SimpleNamespace(container="slt-postgres", live_override_env=None)

def fake_resolve(name, env, started, progress=None):
    if defn.container not in started:
        time.sleep(1.0)
        with open(ups, "a") as f:
            f.write(sys.argv[2] + "\\n")
        marker.write_text("up")
        started.add(defn.container)
    return SimpleNamespace(name=name)

i = infra.Infra("shared", 9080, infra._lock_dir())
(work / f"ready-{sys.argv[2]}").write_text("")
go_by = time.monotonic() + 60
while not (work / "go").exists():
    if time.monotonic() > go_by:
        sys.exit("no go within 60s")
    time.sleep(0.01)
infra.resolve_service(i, "postgres", {}, set(), fake_resolve, load=lambda n: defn,
                      container_running=lambda c: marker.exists())
print(json.dumps(i.services))
"""


def test_resolve_service_two_processes_different_state_dirs_provision_once(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    procs = [subprocess.Popen([sys.executable, "-c", _ADAPTER, str(work), tag],
                              env=_child_env(tmp_path, SLT_STATE_DIR=tmp_path / f"state-{tag}"),
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
             for tag in ("a", "b")]
    try:
        _wait_files([work / f"ready-{t}" for t in "ab"], procs, 60, "service adapters ready")
        (work / "go").write_text("")
        outs = _finish(procs)
    finally:
        _reap(procs)
    statuses = [s["status"] for out in outs for s in json.loads(out.splitlines()[-1])]
    assert sorted(statuses) == ["provisioned-and-kept", "reused"]
    assert len((work / "ups.log").read_text().splitlines()) == 1


def test_resolve_service_parallel_branch_no_recursive_lock(monkeypatch, tmp_path):
    from livetest import services
    assert services._PROVISION_LOCK != ".slt-shared-provision.lock"
    assert services._PROVISION_LOCK != infra.SHARED_PROVISION_LOCK
    monkeypatch.setenv("SLT_PARALLEL", "1")
    monkeypatch.setattr(services, "_state_dir", lambda: tmp_path / "state")
    (tmp_path / "state").mkdir()
    ups = []
    resolve_fn = partial(services.resolve, compose_up=lambda d: ups.append(d.name), post_up=lambda d: None)
    decl = infra.Infra("shared", 9080, tmp_path / "locks")
    done = {}

    def work():
        done["resolved"] = infra.resolve_service(decl, "postgres", dict(os.environ), set(), resolve_fn,
                                                 container_running=lambda c: False)

    t = threading.Thread(target=work, daemon=True)
    t.start()
    t.join(30)
    assert not t.is_alive(), "shared adapter + ensure_provisioned_once deadlocked"
    assert ups == ["postgres"] and done["resolved"].started is True
    assert decl.services[0]["status"] == "provisioned-and-kept"


def test_resolve_service_lock_timeout_is_bounded_failure(tmp_path):
    from filelock import FileLock
    holder = subprocess.Popen(
        [sys.executable, "-c",
         "import sys, time; from filelock import FileLock; l = FileLock(sys.argv[1]); l.acquire(); "
         "print('locked', flush=True); time.sleep(30)", str(tmp_path / "locks" / infra.SHARED_PROVISION_LOCK)],
        env=_child_env(tmp_path), stdout=subprocess.PIPE, text=True)
    (tmp_path / "locks").mkdir(exist_ok=True)
    try:
        assert holder.stdout.readline().strip() == "locked"
        decl = infra.Infra("shared", 9080, tmp_path / "locks")
        defn = SimpleNamespace(container="slt-postgres", live_override_env=None)
        started = time.monotonic()
        with pytest.raises(infra.InfraOwnershipError, match="lock-timeout"):
            infra.resolve_service(decl, "postgres", {}, set(), lambda *a, **k: pytest.fail("resolved"),
                                  load=lambda n: defn, container_running=lambda c: False, lock_timeout=0.3)
        assert time.monotonic() - started < 10
        assert decl.services == []
    finally:
        holder.kill()
        holder.wait()
    assert FileLock  # imported for the holder's module path parity


def test_resolve_service_exclusive_passes_through(tmp_path):
    calls = []
    decl = infra.Infra("exclusive", 9080, tmp_path / "locks")
    defn = SimpleNamespace(container="xabc-slt-postgres", live_override_env=None)
    out = infra.resolve_service(decl, "postgres", {}, set(),
                                lambda name, env, started, progress=None: calls.append(name) or "r",
                                load=lambda n: defn,
                                container_running=lambda c: pytest.fail("exclusive never probes containers"))
    assert out == "r" and calls == ["postgres"]
    assert decl.services == [{"name": "postgres", "container": "xabc-slt-postgres", "status": "owned"}]
    assert not (tmp_path / "locks" / infra.SHARED_PROVISION_LOCK).exists()
    legacy = []
    assert infra.resolve_service(None, "postgres", {}, set(),
                                 lambda name, env, started, progress=None: legacy.append(name) or "l") == "l"
    assert legacy == ["postgres"]
