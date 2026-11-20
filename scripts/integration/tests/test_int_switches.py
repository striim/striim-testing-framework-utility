"""The integration engine's switches.

Every switch is read through paths.flag: `1` on, `0` or unset off, anything else a usage error
(scripts/live/tests/test_switches.py tests the helper itself). The `disabled:` gate at the top of
IntYamlItem.runtest is executed through the real plugin; no test ran it before.

INT_SHARED_SERVICES is retired: it dropped the session lock while the YAML path still started
containers and a serial session's ${TID} stayed empty, so two sessions addressed the same tables.
"""
from __future__ import annotations

import importlib.util
import json
import sys
import types
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
from filelock import FileLock

from inttest import plugin as P

ROOT = Path(__file__).resolve().parents[3]
_spec = importlib.util.spec_from_file_location(
    "int_switches_plugin_exec", Path(__file__).resolve().parent / "test_plugin_exec.py")
X = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(X)


class _Cfg:
    option = types.SimpleNamespace(collectonly=False)


@pytest.fixture
def held(monkeypatch, tmp_path):
    """Another session holds a session lock this test owns (the surrounding run's is set aside)."""
    monkeypatch.setattr(P, "_session_lock", None)
    monkeypatch.delenv("PYTEST_XDIST_WORKER", raising=False)
    foreign = tmp_path / ".int-session.lock"
    monkeypatch.setattr(P, "_session_lock_path", lambda: foreign)
    other = FileLock(str(foreign), timeout=0)
    other.acquire()
    try:
        yield
    finally:
        other.release()


# --- INT_SHARED_SERVICES ---------------------------------------------------------------------

def test_int_shared_services_no_longer_skips_the_session_lock(held, monkeypatch):
    monkeypatch.delenv("INT_ALLOW_CONCURRENT_SESSIONS", raising=False)
    monkeypatch.setenv("INT_SHARED_SERVICES", "1")
    with pytest.raises(pytest.UsageError, match="another integration-test session"):
        P._acquire_session_lock(_Cfg())


def test_int_shared_services_is_read_and_documented_nowhere():
    """Its readers, its two doc passages, the .env.example line and the settings row are gone. The
    release notes and the upgrade guide say it was removed; tests may name it to prove that."""
    hits = []
    for path in sorted(ROOT.rglob("*")):
        rel = path.relative_to(ROOT)
        if (not path.is_file() or rel.parts[0] in (".git", ".venv-test") or "tests" in rel.parts
                or rel.name in ("RELEASE-NOTES.md", "UPGRADING.md") or path.suffix in (".pyc", ".jar")):
            continue
        try:
            text = path.read_text()
        except (UnicodeDecodeError, OSError):
            continue
        if "INT_SHARED_SERVICES" in text or "_shared_services(" in text:
            hits.append(str(rel))
    assert hits == []


# --- switches --------------------------------------------------------------------------------

def test_allow_concurrent_sessions_zero_keeps_the_session_lock(held, monkeypatch):
    monkeypatch.setenv("INT_ALLOW_CONCURRENT_SESSIONS", "0")
    with pytest.raises(pytest.UsageError, match="another integration-test session"):
        P._acquire_session_lock(_Cfg())


def test_allow_concurrent_sessions_one_skips_it(held, monkeypatch):
    monkeypatch.setenv("INT_ALLOW_CONCURRENT_SESSIONS", "1")
    P._acquire_session_lock(_Cfg())
    assert P._session_lock is None


def _all_env_skipped():
    rep = types.SimpleNamespace(_int_case=True, _int_env_skip="env", nodeid="c::x",
                                longrepr=("f", 1, "Skipped: docker not available"))
    tr = types.SimpleNamespace(stats={"skipped": [rep]})
    return types.SimpleNamespace(config=types.SimpleNamespace(
        pluginmanager=types.SimpleNamespace(get_plugin=lambda name: tr)))


def test_allow_all_skipped_zero_keeps_the_guard_on(monkeypatch):
    monkeypatch.setenv("INT_ALLOW_ALL_SKIPPED", "0")
    assert P._int_all_skipped_guard(_all_env_skipped(), 0) == 1
    monkeypatch.setenv("INT_ALLOW_ALL_SKIPPED", "1")
    assert P._int_all_skipped_guard(_all_env_skipped(), 0) == 0


def test_variant_items_is_a_switch_that_is_on_when_unset():
    assert P._variant_items({}) is True
    assert P._variant_items({"INT_VARIANT_ITEMS": "0"}) is False
    with pytest.raises(ValueError, match="INT_VARIANT_ITEMS='no'"):
        P._variant_items({"INT_VARIANT_ITEMS": "no"})


def test_an_opt_in_gate_is_a_switch(monkeypatch):
    monkeypatch.setattr(P, "_load_service_yaml", lambda name: {"opt_in_env": "INT_DEMO"})
    monkeypatch.delenv("INT_EMULATORS", raising=False)
    monkeypatch.setenv("INT_DEMO", "0")
    with pytest.raises(pytest.skip.Exception, match="set INT_DEMO=1"):
        P._require_service_opt_in("demo")
    monkeypatch.setenv("INT_DEMO", "1")
    P._require_service_opt_in("demo")
    monkeypatch.setenv("INT_DEMO", "yes")
    with pytest.raises(ValueError, match="INT_DEMO='yes'"):
        P._require_service_opt_in("demo")


def test_a_bad_declared_opt_in_gate_is_one_usage_error_at_configure(pytester, monkeypatch, tmp_path):
    # Before, the run started and each case requiring the service raised its own FlagError.
    svc = tmp_path / "svc" / "demo"
    svc.mkdir(parents=True)
    (svc / "service.yaml").write_text("name: demo\nopt_in_env: INT_DEMO\n")
    (tmp_path / ".env").write_text("INT_DEMO=yes\n")
    monkeypatch.setenv("SLT_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("SLT_INT_SERVICES_DIR", str(tmp_path / "svc"))
    monkeypatch.delenv("INT_DEMO", raising=False)
    result = pytester.runpytest_subprocess("-p", "inttest.plugin", "-p", "no:cacheprovider")
    assert result.ret == pytest.ExitCode.USAGE_ERROR, result.stdout.str() + result.stderr.str()
    assert "INT_DEMO='yes'" in result.stderr.str()


# --- the disabled: gate, through the real plugin ---------------------------------------------

EVENT = json.dumps([X.EVENT])
FILES = {"in.json": EVENT, "out.json": EVENT}
DISABLED = X.DATA_CASE + 'disabled: "PLAT-9 known bug"\n'


def _run(pytester, monkeypatch, tmp_path, env=()):
    for var in ("SLT_RUN_DISABLED", "INT_ALLOW_ALL_SKIPPED", "INT_STACK_PREFIX"):
        monkeypatch.delenv(var, raising=False)
    for k, v in dict(env).items():
        monkeypatch.setenv(k, v)
    result, failure, events = X.run_case(pytester, monkeypatch, tmp_path, DISABLED,
                                         {"returns": [X.EVENT]}, FILES)
    return result, failure, [e["event"] for e in events]


def test_a_disabled_case_skips_before_any_build_or_drive(pytester, monkeypatch, tmp_path):
    result, failure, events = _run(pytester, monkeypatch, tmp_path)
    assert failure is None
    result.assert_outcomes(skipped=1)
    skipped = ET.parse(tmp_path / "junit.xml").find(".//testcase/skipped")
    assert "disabled (PLAT-9 known bug); set SLT_RUN_DISABLED=1 to force" in (
        skipped.get("message") + (skipped.text or ""))
    assert events == [], "nothing is driven for a disabled case"
    assert result.ret == 0, "a declared skip is not an environment skip: the all-skipped guard stays quiet"


def test_run_disabled_zero_still_skips_it(pytester, monkeypatch, tmp_path):
    result, _failure, events = _run(pytester, monkeypatch, tmp_path, env={"SLT_RUN_DISABLED": "0"})
    result.assert_outcomes(skipped=1)
    assert events == []


def test_run_disabled_one_runs_it(pytester, monkeypatch, tmp_path):
    result, failure, events = _run(pytester, monkeypatch, tmp_path, env={"SLT_RUN_DISABLED": "1"})
    assert failure is None, failure
    result.assert_outcomes(passed=1)
    assert "drive" in events


def test_a_bad_switch_stops_the_run_before_collection(pytester, monkeypatch, tmp_path):
    d = pytester.mkdir("case")
    (d / "test.yaml").write_text(DISABLED)
    for name, content in FILES.items():
        (d / name).write_text(content)
    (tmp_path / "state").mkdir()
    monkeypatch.setenv("SLT_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("SLT_RUN_DISABLED", "true")
    result = pytester.runpytest_subprocess("-p", "inttest.plugin", "-p", "no:cacheprovider", "case")
    assert result.ret == pytest.ExitCode.USAGE_ERROR, result.stdout.str()
    assert "SLT_RUN_DISABLED='true'" in result.stderr.str() + result.stdout.str()


def test_the_switch_check_at_configure_does_not_need_livetest(monkeypatch):
    # The plugin treats livetest-dependent modules as optional (services may be None); the switch
    # check at configure falls back to the switches alone where livetest.service_env cannot be
    # imported, rather than failing the session at startup (review N4).
    import livetest
    monkeypatch.setitem(sys.modules, "livetest.service_env", None)
    # An already-imported livetest holds service_env as an attribute, which `from livetest import
    # service_env` would return regardless of sys.modules; remove it so the import really fails.
    monkeypatch.delattr(livetest, "service_env", raising=False)
    calls = []
    real = P.paths_mod.flag_problems
    monkeypatch.setattr(P.paths_mod, "flag_problems", lambda env: calls.append(env) or real(env))
    assert P._switch_problems({"SLT_RUN_DISABLED": "1"}) == []
    problems = P._switch_problems({"SLT_RUN_DISABLED": "true"})
    assert len(problems) == 1 and "SLT_RUN_DISABLED='true'" in problems[0]
    assert len(calls) == 2                                    # the fallback answered, not service_env
