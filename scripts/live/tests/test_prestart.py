"""service.yaml `pre_up`: a host-side hook run before a service's bring-up.

It runs for a selected test that requires the service and for a start that names it, never for a
derived `start all`, never with the existing instance set, never with SLT_PRE_UP=0 (shell or
.env), and one at a time per name on the machine.
"""
import ast
import inspect
import os
import types

import pytest

from livetest import layout, paths, prestart, registry

_SERVICE = """name: tdx
compose: compose.yaml
container: slt-tdx
isolation: none
live_override_env: TDX_HOST
required_files: [deps/disk1]
pre_up: fetch.sh
docker_defaults: {port: 1}
live_env: {host: TDX_HOST}
"""


@pytest.fixture
def tdx(tmp_path, monkeypatch, real_prestart_run):
    """A consumer service whose pre_up creates its required file; hooks enabled, real runner."""
    from livetest import stack
    svc = tmp_path / "services" / "tdx"
    svc.mkdir(parents=True)
    (svc / "service.yaml").write_text(_SERVICE)
    (svc / "compose.yaml").write_text("services: {}\n")
    (svc / "fetch.sh").write_text("#!/bin/sh\nmkdir -p deps && : > deps/disk1 && echo \"$SLT_SERVICE_DIR\" > ran\n")
    (svc / "fetch.sh").chmod(0o755)
    monkeypatch.setattr(prestart, "run_hook", real_prestart_run)
    monkeypatch.setattr(stack, "_LOCK_DIR", tmp_path / "locks")
    monkeypatch.delenv("SLT_PRE_UP")
    monkeypatch.setattr(paths, "dotenv_values", lambda env=None: {})
    layout.set_roots(services=[tmp_path])
    yield svc
    layout._reset()


def _preflight(monkeypatch):
    from livetest import preflight
    monkeypatch.setattr(preflight, "_log", lambda m: None)
    monkeypatch.setattr(preflight._slt_infra, "resolve_service",
                        lambda *a, **k: types.SimpleNamespace(base={}))
    monkeypatch.setattr(preflight, "ensure_provisioned_once", lambda *a, **k: None)
    return preflight


def test_pre_up_loads_and_must_stay_inside_the_service_dir(tdx):
    assert registry.load_service("tdx").pre_up == "fetch.sh"
    for bad in ("/etc/x.sh", "../x.sh"):
        (tdx / "service.yaml").write_text(_SERVICE.replace("pre_up: fetch.sh", f"pre_up: {bad}"))
        with pytest.raises(registry.RegistryError, match="pre_up"):
            registry.load_service("tdx")


def test_start_all_never_runs_the_hook(tdx, monkeypatch):
    preflight = _preflight(monkeypatch)
    assert preflight._bring_up_services(["tdx"], {}, apply_gate=True) == {}   # skipped: files missing
    assert not (tdx / "ran").exists()


def test_a_named_start_runs_the_hook_then_brings_the_service_up(tdx, monkeypatch):
    preflight = _preflight(monkeypatch)
    assert list(preflight._bring_up_services(["tdx"], {}, apply_gate=False)) == ["tdx"]
    assert (tdx / "ran").read_text().strip() == str(tdx)       # cwd and SLT_SERVICE_DIR
    assert (tdx / "deps" / "disk1").is_file()


def test_a_test_selection_preflight_runs_the_hook():
    from livetest import preflight
    calls = [node for node in ast.walk(ast.parse(inspect.getsource(preflight.provision)))
             if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
             and node.func.id == "_bring_up_services"]
    [call] = calls
    assert [ast.unparse(arg) for arg in call.args] == ["services", "env"]
    keywords = {kw.arg: ast.unparse(kw.value) for kw in call.keywords}
    for key, value in {"infra": "_slt_decl", "pre_up": "True", "mode": "ctx.mode"}.items():
        assert keywords[key] == value


def _fake_driver(monkeypatch, **hooks):
    """A service driver (livetest.drivers) for the service named 'extdb', with these hooks."""
    from livetest import drivers
    fake = types.SimpleNamespace(**hooks)
    monkeypatch.setattr(drivers, "load", lambda defn: fake if getattr(defn, "name", None) == "extdb" else None)
    return fake


def _docker_only(mode):
    return None if mode == "docker" else "needs the Docker Striim cluster"


def test_a_selected_test_runs_the_hook_before_the_skip_check(tdx, monkeypatch):
    from livetest import plugin
    defn = registry.load_service("tdx")
    defn.name = "extdb"
    defn.unavailable_policy = "fail"
    _fake_driver(monkeypatch, unsupported_mode=_docker_only,
                 unavailable=lambda *a: None if (tdx / "ran").exists() else "no image",
                 compose_env=lambda env: {"SLT_EXTDB_FILES": "test-files"})
    monkeypatch.setenv("SLT_EXTDB_FILES", "restored-after-the-test")     # prepare_selected sets os.environ
    env = {}
    plugin._prepare_service(defn, env, "docker")
    assert (tdx / "ran").exists()
    assert env["SLT_EXTDB_FILES"] == "test-files"
    assert os.environ["SLT_EXTDB_FILES"] == "test-files"


def test_maybe_run_runs_for_a_selected_service(tdx):
    assert prestart.maybe_run(registry.load_service("tdx"), {}) is True
    assert registry.unavailable(registry.load_service("tdx"), {}) is None


@pytest.mark.parametrize("env", [{"SLT_PRE_UP": "0"}, {"TDX_HOST": "db.example"}])
def test_the_off_switch_and_an_existing_instance_stop_the_hook(tdx, env):
    assert prestart.maybe_run(registry.load_service("tdx"), env) is False
    assert not (tdx / "ran").exists()


def test_the_off_switch_is_read_from_env_file(tdx, monkeypatch):
    monkeypatch.setattr(paths, "dotenv_values", lambda env=None: {"SLT_PRE_UP": "0"})
    assert prestart.maybe_run(registry.load_service("tdx"), {}) is False
    assert "SLT_PRE_UP" in paths.SERVICE_KEYS


def test_the_hook_runs_under_the_machine_lock_for_its_name(tdx, tmp_path):
    from filelock import FileLock, Timeout
    held = FileLock(str(prestart.lock_path("tdx")))
    held.acquire()
    try:
        import threading
        t = threading.Thread(target=prestart.maybe_run, args=(registry.load_service("tdx"), {}))
        t.start()
        t.join(1.0)
        assert t.is_alive() and not (tdx / "ran").exists()    # waits for the lock
    finally:
        held.release()
    t.join(10)
    assert (tdx / "ran").exists()


def test_the_hook_is_skipped_when_every_required_file_is_present(tdx):
    # Review H1: once the files exist the hook never runs, so a dead download source cannot
    # hold up (or serialize) a run that already has what it needs.
    (tdx / "deps").mkdir()
    (tdx / "deps" / "disk1").write_text("")
    assert prestart.maybe_run(registry.load_service("tdx"), {}) is False
    assert not (tdx / "ran").exists()


def test_a_hook_that_outlives_its_timeout_is_killed_and_is_an_error(tdx):
    # Review H1: a stalled download must not hang the run, nor keep running behind it.
    (tdx / "fetch.sh").write_text("#!/bin/sh\necho $$ > pid\nsleep 60 &\necho $! > child\nwait\n")
    (tdx / "service.yaml").write_text(_SERVICE + "pre_up_timeout: 1\n")
    with pytest.raises(prestart.PreUpError, match="timed out after 1s"):
        prestart.maybe_run(registry.load_service("tdx"), {})
    import os, time
    child = int((tdx / "child").read_text())
    for _ in range(50):
        try:
            os.kill(child, 0)
        except ProcessLookupError:
            break
        time.sleep(0.1)
    else:
        pytest.fail("the hook's child process outlived the timeout")


def test_pre_up_timeout_must_be_a_positive_number(tdx):
    (tdx / "service.yaml").write_text(_SERVICE + "pre_up_timeout: 0\n")
    with pytest.raises(registry.RegistryError, match="pre_up_timeout"):
        registry.load_service("tdx")


def test_a_failing_hook_is_an_error_naming_its_output(tdx):
    # Review H2: not a quiet skip. The message carries the exit code and the output tail.
    (tdx / "fetch.sh").write_text("#!/bin/sh\necho 'HTTP 403 from the bucket'\nexit 7\n")
    with pytest.raises(prestart.PreUpError, match="exited 7.*HTTP 403 from the bucket"):
        prestart.maybe_run(registry.load_service("tdx"), {})


def test_a_hook_that_leaves_files_missing_is_an_error(tdx):
    (tdx / "fetch.sh").write_text("#!/bin/sh\nexit 0\n")
    with pytest.raises(prestart.PreUpError, match="left required files missing"):
        prestart.maybe_run(registry.load_service("tdx"), {})


def test_a_failed_hook_fails_the_selected_test_and_the_start(tdx, monkeypatch):
    from livetest import plugin
    src = inspect.getsource(plugin.LiveItem._runtest)
    assert 'pytest.fail(f"service {svc}: {e}", pytrace=False)' in src
    (tdx / "fetch.sh").write_text("#!/bin/sh\nexit 3\n")
    preflight = _preflight(monkeypatch)
    with pytest.raises(prestart.PreUpError):          # provision_services turns this into rc 1
        preflight._bring_up_services(["tdx"], {}, apply_gate=False)
    assert "except _prestart.PreUpError:\n        return 1" in inspect.getsource(preflight.provision)


def test_the_hermetic_suite_switches_hooks_off():
    import os
    assert os.environ.get("SLT_PRE_UP") == "0"


def test_always_check_and_resolved_hook_environment(tdx):
    (tdx / 'service.yaml').write_text(_SERVICE + 'pre_up_check: always\n')
    (tdx / 'deps').mkdir()
    (tdx / 'deps/disk1').touch()
    (tdx / 'fetch.sh').write_text('#!/bin/sh\nprintf "%s" "$CONSUMER_INPUT" > ran\n')
    defn = registry.load_service('tdx')
    assert prestart.maybe_run(defn, {'CONSUMER_INPUT': 'checkout-value'})
    assert (tdx / 'ran').read_text() == 'checkout-value'


@pytest.mark.parametrize('key,value', [('pre_up_check', 'sometimes'), ('unavailable_policy', 'ignore')])
def test_hook_policy_schema_rejects_unknown_values(tdx, key, value):
    (tdx / 'service.yaml').write_text(_SERVICE + f'{key}: {value}\n')
    with pytest.raises(registry.RegistryError, match=key):
        registry.load_service('tdx')


def test_strict_disabled_hook_fails_without_side_effects(tdx):
    (tdx / 'service.yaml').write_text(_SERVICE + 'unavailable_policy: fail\n')
    with pytest.raises(prestart.PreUpError, match='SLT_PRE_UP=0'):
        prestart.maybe_run(registry.load_service('tdx'), {'SLT_PRE_UP': '0'})
    assert not (tdx / 'ran').exists()


def test_driver_preparation_hook_before_resource_check_and_mode_before_hook(tdx, monkeypatch):
    defn = registry.load_service('tdx')
    defn.name = 'extdb'
    defn.unavailable_policy = 'fail'
    events = []
    monkeypatch.setattr(prestart, 'maybe_run', lambda *a, **k: events.append('hook'))
    _fake_driver(monkeypatch, unsupported_mode=_docker_only,
                 unavailable=lambda *a: events.append('resources'))
    monkeypatch.setattr(registry, 'unavailable', lambda *a: None)
    assert prestart.prepare(defn, {}, 'docker') is None
    assert events == ['hook', 'resources']
    events.clear()
    with pytest.raises(prestart.PreUpError, match='Docker Striim cluster'):
        prestart.prepare(defn, {}, 'native')
    assert events == []


def test_driver_preflight_reaches_hook_before_missing_image(tdx, monkeypatch):
    defn = registry.load_service('tdx')
    defn.name = 'extdb'
    defn.unavailable_policy = 'fail'
    preflight = _preflight(monkeypatch)
    monkeypatch.setattr(preflight, 'load_service', lambda *a: defn)
    _fake_driver(monkeypatch, unsupported_mode=_docker_only,
                 unavailable=lambda *a: None if (tdx / 'ran').exists() else 'no image')
    assert 'extdb' in preflight._bring_up_services(['extdb'], {}, apply_gate=False)
    assert (tdx / 'ran').exists()


def test_strict_postcondition_failure_is_not_a_skip(tdx, monkeypatch):
    defn = registry.load_service('tdx')
    defn.name = 'extdb'
    defn.unavailable_policy = 'fail'
    _fake_driver(monkeypatch, unavailable=lambda *a: 'agent image missing')
    with pytest.raises(prestart.PreUpError, match='agent image missing'):
        prestart.prepare(defn, {}, 'docker')


@pytest.mark.parametrize('mode', ['native', 'docker'])
@pytest.mark.parametrize('policy', ['fail', 'skip'])
def test_outer_pytest_topology_gate_obeys_strict_service_policy(monkeypatch, mode, policy):
    from livetest import plugin
    from livetest.topology import Topology
    manifest = types.SimpleNamespace(topology='cluster', requires=['extdb'], disabled=False,
                                     disabled_parallel=False, exact=None)
    monkeypatch.setattr(plugin, 'load_manifest', lambda path: manifest)
    monkeypatch.setattr(plugin._slt_inputs, 'snapshot', lambda *a, **kw: [])
    monkeypatch.setattr(plugin, '_resolve_striim', lambda cfg: types.SimpleNamespace(mode=mode, topology=Topology()))
    monkeypatch.setattr(plugin, 'load_service', lambda *a, **kw: types.SimpleNamespace(name='extdb', unavailable_policy=policy))
    monkeypatch.setattr(prestart, 'maybe_run', lambda *a, **kw: pytest.fail('hook ran before eligibility'))
    item = types.SimpleNamespace(manifest_path='fixture/test.yaml', config=object())
    try:
        plugin.LiveItem._runtest(item)
    except (pytest.fail.Exception, pytest.skip.Exception) as exc:
        assert isinstance(exc, pytest.fail.Exception if policy == 'fail' else pytest.skip.Exception)
    else:
        pytest.fail('eligibility unexpectedly passed')
