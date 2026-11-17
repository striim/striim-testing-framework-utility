"""Switches: settings that turn something on or off.

One rule in both engines and the CLI (livetest.paths.flag, byte-identical in inttest.paths): `1` is
on, `0` or unset is off (SLT_PRE_UP and INT_VARIANT_ITEMS are on when unset), and any other value
stops the run with a usage error naming the setting. Before, most switches were on for any
non-empty value, so `SLT_SKIP_VERIFY=0` skipped every assertion, and `SLT_RUN_DISABLED=0` ran
disabled live cases while the integration tier skipped them.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import types
from pathlib import Path

import pytest

from inttest import paths as int_paths
from livetest import infra, paths, plugin, prestart, registry, striim_provision
from striim_test import pytest_guard

SCRIPTS = Path(__file__).resolve().parents[2]


# --- the rule --------------------------------------------------------------------------------

@pytest.mark.parametrize("mod", [paths, int_paths], ids=["live", "integration"])
def test_one_is_on_zero_and_unset_are_off(mod):
    assert mod.flag("SLT_RUN_DISABLED", {"SLT_RUN_DISABLED": "1"}) is True
    assert mod.flag("SLT_RUN_DISABLED", {"SLT_RUN_DISABLED": "0"}) is False
    assert mod.flag("SLT_RUN_DISABLED", {"SLT_RUN_DISABLED": ""}) is False
    assert mod.flag("SLT_RUN_DISABLED", {"SLT_RUN_DISABLED": " "}) is False
    assert mod.flag("SLT_RUN_DISABLED", {}) is False


@pytest.mark.parametrize("mod", [paths, int_paths], ids=["live", "integration"])
@pytest.mark.parametrize("value", ["true", "yes", "on", "false", "no", "off", "2", "01", " 1", "0 ", "x"])
def test_any_other_value_is_an_error_naming_the_setting(mod, value):
    with pytest.raises(mod.FlagError, match=rf"SLT_KEEP_RESOURCES={value!r}.*1 \(on\) or 0 \(off\)"):
        mod.flag("SLT_KEEP_RESOURCES", {"SLT_KEEP_RESOURCES": value})


def test_default_on_switches_are_on_when_unset():
    assert paths.flag("SLT_PRE_UP", {}) is True
    assert paths.flag("INT_VARIANT_ITEMS", {}) is True
    assert paths.flag("SLT_PRE_UP", {"SLT_PRE_UP": "0"}) is False
    with pytest.raises(paths.FlagError, match="unset is on"):
        paths.flag("SLT_PRE_UP", {"SLT_PRE_UP": "off"})


def test_flag_problems_lists_every_bad_switch():
    env = {"SLT_KEEP_RESOURCES": "yes", "INT_KEEP_SERVICES": "true", "SLT_PARALLEL": "1",
           "SLT_RUN_DISABLED": "0", "SLT_STACK_PREFIX": "not-a-switch"}
    problems = paths.flag_problems(env)
    assert len(problems) == 2
    assert any("SLT_KEEP_RESOURCES='yes'" in p for p in problems)
    assert any("INT_KEEP_SERVICES='true'" in p for p in problems)
    assert paths.flag_problems({}) == []


def test_every_documented_switch_is_checked():
    """Each setting the page documents as "`1`: ..." or "`0`: ..." is in SWITCHES."""
    page = (SCRIPTS.parent / "docs" / "SETTINGS.md").read_text()
    documented = set(re.findall(r"^\| `((?:SLT|INT)_[A-Z0-9_]+)` \|[^|]*\|[^|]*\| `[01]`: ", page, re.M))
    assert documented, "the settings page changed shape"
    assert documented <= set(paths.SWITCHES), sorted(documented - set(paths.SWITCHES))


# --- the call sites --------------------------------------------------------------------------

def test_zero_is_off_at_every_live_reader():
    assert plugin._parallel({"SLT_PARALLEL": "0"}) is False
    assert plugin._skip_shared_teardown({"SLT_KEEP_SERVICES": "0"}) is False
    assert plugin.should_keep_resources({"SLT_KEEP_RESOURCES": "0"}, True, True) is False
    assert plugin.should_keep_resources({"SLT_KEEP_RESOURCES_ON_ERROR": "0"}, True, False) is False
    assert plugin.should_keep_resources({"SLT_KEEP_RESOURCES_ON_ERROR": "1"}, True, False) is True
    assert registry.opted_in(None, {"SLT_EMULATORS": "0"}) is False
    assert registry.opted_in("SLT_SPANNER", {"SLT_SPANNER": "1"}) is True
    assert striim_provision.teradata_jdbc_opted_in({"SLT_TERADATA_JDBC": "0"}) is False
    assert prestart.enabled({"SLT_PRE_UP": "1"}) is True
    assert prestart.enabled({"SLT_PRE_UP": "0"}) is False


def test_a_bad_value_is_refused_at_every_live_reader():
    for call in (lambda: plugin._parallel({"SLT_PARALLEL": "true"}),
                 lambda: plugin._skip_shared_teardown({"SLT_KEEP_SERVICES": "yes"}),
                 lambda: plugin.should_keep_resources({"SLT_KEEP_RESOURCES": "on"}, True, True),
                 lambda: registry.opted_in(None, {"SLT_EMULATORS": "false"}),
                 lambda: striim_provision.teradata_jdbc_opted_in({"SLT_TERADATA_JDBC": "true"}),
                 lambda: prestart.enabled({"SLT_PRE_UP": "no"})):
        with pytest.raises(paths.FlagError):
            call()


def test_shared_ownership_needs_keep_services_one_not_zero():
    with pytest.raises(infra.InfraOwnershipError, match="requires SLT_KEEP_SERVICES=1"):
        infra.declare({"SLT_INFRA_OWNERSHIP": "shared", "SLT_KEEP_SERVICES": "0"})


def test_allow_no_cluster_zero_keeps_the_all_skipped_guard_on(monkeypatch):
    monkeypatch.setenv("SLT_ALLOW_NO_CLUSTER", "0")
    rep = types.SimpleNamespace(nodeid="c/test.yaml::c", longrepr=("f", 1, "Skipped: no Striim"),
                                _slt_live=True, _slt_infra_skip="cluster")
    tr = types.SimpleNamespace(stats={"skipped": [rep]})
    session = types.SimpleNamespace(config=types.SimpleNamespace(
        pluginmanager=types.SimpleNamespace(get_plugin=lambda name: tr)))
    assert plugin._slt_infra_guard(session, 0) == 1


@pytest.mark.parametrize("tier", ["live", "integration"])
def test_run_disabled_means_the_same_in_both_tiers(monkeypatch, tier):
    raw = {"disabled": "known bug"}
    monkeypatch.delenv("SLT_RUN_DISABLED", raising=False)
    assert pytest_guard._disabled_reason(tier, raw) == "disabled: known bug"
    monkeypatch.setenv("SLT_RUN_DISABLED", "0")
    assert pytest_guard._disabled_reason(tier, raw) == "disabled: known bug"
    monkeypatch.setenv("SLT_RUN_DISABLED", "1")
    assert pytest_guard._disabled_reason(tier, raw) is None
    monkeypatch.setenv("SLT_RUN_DISABLED", "true")
    with pytest.raises(paths.FlagError, match="SLT_RUN_DISABLED='true'"):
        pytest_guard._disabled_reason(tier, raw)


def test_disabled_parallel_follows_slt_parallel_one_not_zero(monkeypatch):
    raw = {"disabled_parallel": "restarts a node"}
    monkeypatch.delenv("SLT_RUN_DISABLED", raising=False)
    monkeypatch.setenv("SLT_PARALLEL", "0")
    assert pytest_guard._disabled_reason("live", raw) is None
    monkeypatch.setenv("SLT_PARALLEL", "1")
    assert pytest_guard._disabled_reason("live", raw) == "disabled_parallel: restarts a node"


def test_no_engine_reads_a_switch_except_through_the_helper():
    """A literal read of a switch (``env.get("SLT_KEEP_SERVICES")``, ``os.getenv(...)``) is the
    truthiness bug this closes; every read goes through paths.flag or paths.parse_flag."""
    names = "|".join(paths.SWITCHES)
    read = re.compile(rf"""(?:\.get|getenv|setting)\(\s*["']({names})["']""")
    hits = []
    for root in ("live/livetest", "integration/inttest", "cli/striim_test"):
        for src in sorted((SCRIPTS / root).rglob("*.py")):
            if src.name == "paths.py":
                continue
            for n, line in enumerate(src.read_text().splitlines(), 1):
                if read.search(line) and "parse_flag(" not in line:
                    hits.append(f"{src.relative_to(SCRIPTS)}:{n}: {line.strip()}")
    assert hits == []


# --- the gates, through the real plugin (FW-68: no test executed these lines) ------------------

MANIFEST = """\
name: {name}
purpose: a gate pin
tql: app.tql
depth: regression
requires: [postgres]
{extra}
assert:
  smoke: true
"""
# Nothing listens at STRIIM_URL, and SLT_STRIIM_NATIVE_ONLY forbids building a Docker cluster: a
# case that gets past its gate ends here.
PAST_THE_GATE = "SLT_STRIIM_NATIVE_ONLY=1: existing native Striim did not authenticate"


def _run(tmp_path, extra, **env_extra):
    case = tmp_path / "cases" / "gate"
    case.mkdir(parents=True)
    (case / "test.yaml").write_text(MANIFEST.format(name="gate", extra=extra))
    (case / "app.tql").write_text("CREATE APPLICATION ${APP};\nEND APPLICATION ${APP};\n")
    (tmp_path / "state").mkdir(exist_ok=True)
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("SLT_", "INT_", "PYTEST_", "COVERAGE_", "STRIIM"))}
    env.update(SLT_INFRA_OWNERSHIP="shared", SLT_KEEP_SERVICES="1", SLT_LOCK_DIR=str(tmp_path / "locks"),
               SLT_STATE_DIR=str(tmp_path / "state"), SLT_PROJECT_ROOT=str(tmp_path),
               SLT_CASE_RECORDS="off", SLT_PRE_UP="0", DOCKER_HOST="unix:///nonexistent",
               STRIIM_URL="http://127.0.0.1:9", SLT_STRIIM_NATIVE_ONLY="1", **env_extra)
    r = subprocess.run([sys.executable, "-m", "pytest", "-p", "livetest.plugin", "-p", "no:cacheprovider",
                        "--import-mode=importlib", "-rs", "-q", str(tmp_path / "cases")],
                       cwd=tmp_path, env=env, capture_output=True, text=True, timeout=300)
    return r.returncode, r.stdout + r.stderr


def test_a_disabled_case_skips_with_its_reason(tmp_path):
    rc, out = _run(tmp_path, 'disabled: "PLAT-1 known bug"')
    assert rc == 0 and "1 skipped" in out, out[-1500:]
    assert "disabled: PLAT-1 known bug (set SLT_RUN_DISABLED=1 to force-run)" in out
    assert "LIVE TIER PROVED NOTHING" not in out, "a declared skip is not an infrastructure skip"


def test_run_disabled_zero_still_skips_a_disabled_case(tmp_path):
    rc, out = _run(tmp_path, 'disabled: "PLAT-1 known bug"', SLT_RUN_DISABLED="0")
    assert rc == 0 and "1 skipped" in out, out[-1500:]


def test_run_disabled_one_forces_a_disabled_case_past_the_gate(tmp_path):
    rc, out = _run(tmp_path, 'disabled: "PLAT-1 known bug"', SLT_RUN_DISABLED="1")
    assert "disabled: PLAT-1" not in out, out[-1500:]
    assert rc == 1 and PAST_THE_GATE in out, out[-1500:]


def test_disabled_parallel_skips_only_a_parallel_run(tmp_path):
    rc, out = _run(tmp_path, 'disabled_parallel: "restarts a node"', SLT_PARALLEL="1")
    assert rc == 0 and "1 skipped" in out, out[-1500:]
    assert "disabled_parallel: restarts a node (set SLT_RUN_DISABLED=1 to force-run)" in out


def test_a_bad_switch_stops_the_run_before_anything_runs(tmp_path):
    rc, out = _run(tmp_path, 'disabled: "PLAT-1 known bug"', SLT_RUN_DISABLED="true")
    assert rc == pytest.ExitCode.USAGE_ERROR, out[-1500:]
    assert "SLT_RUN_DISABLED='true'" in out
    assert "skipped" not in out


# --- the entry points outside pytest ---------------------------------------------------------

def test_livetest_cli_refuses_a_bad_switch_before_any_command(monkeypatch, capsys):
    from livetest import cli, project
    ran = []
    monkeypatch.setattr(project, "load_and_activate", lambda *a, **k: None)
    monkeypatch.setattr(cli, "cmd_down", lambda targets, **k: ran.append(targets) or 0)
    monkeypatch.setattr(sys, "argv", ["livetest.cli", "stop", "all"])
    monkeypatch.setenv("SLT_KEEP_SERVICES", "yes")
    assert cli.main() == 2
    assert ran == [] and "SLT_KEEP_SERVICES='yes'" in capsys.readouterr().err
    monkeypatch.setenv("SLT_KEEP_SERVICES", "1")
    assert cli.main() == 0 and ran == [["all"]]


def test_preflight_refuses_a_bad_switch_before_any_step(monkeypatch):
    from livetest import preflight
    ran = []
    monkeypatch.setattr(preflight, "teardown", lambda tests: ran.append(tests) or 0)
    monkeypatch.delenv("GOLD_TARGETS", raising=False)
    monkeypatch.setenv("SLT_KEEP_RESOURCES_ON_ERROR", "true")
    assert preflight.main(["--teardown"]) == 2 and ran == []
    monkeypatch.setenv("SLT_KEEP_RESOURCES_ON_ERROR", "0")
    assert preflight.main(["--teardown"]) == 0 and ran == [[]]


# --- a service's declared opt-in gate is a switch, checked with the others ----------------------

def _consumer_gate(tmp_path, value="true"):
    """A consumer service declaring `opt_in_env: SLT_MY_EMULATOR`, with `value` set for it in the
    project's .env. Returns the environment that makes both visible."""
    svc = tmp_path / "svc" / "myemu"
    svc.mkdir(parents=True)
    (svc / "service.yaml").write_text("name: myemu\nopt_in_env: SLT_MY_EMULATOR\n")
    (tmp_path / ".env").write_text(f"SLT_MY_EMULATOR={value}\n")
    return {"SLT_PROJECT_ROOT": str(tmp_path), "SLT_SERVICES_DIR": str(tmp_path / "svc")}


def test_a_bad_declared_opt_in_gate_stops_the_run_before_anything_runs(tmp_path):
    env = _consumer_gate(tmp_path)          # _run's SLT_PROJECT_ROOT is tmp_path too
    rc, out = _run(tmp_path, 'disabled: "PLAT-1 known bug"', SLT_SERVICES_DIR=env["SLT_SERVICES_DIR"])
    assert rc == pytest.ExitCode.USAGE_ERROR, out[-1500:]
    assert "SLT_MY_EMULATOR='true'" in out and "skipped" not in out


def test_livetest_cli_and_preflight_refuse_a_bad_declared_opt_in_gate(tmp_path, monkeypatch, capsys):
    # In-process, the hermetic fixture reads no .env: the gate comes from the process environment.
    from livetest import cli, preflight, project
    for key, value in _consumer_gate(tmp_path).items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("SLT_MY_EMULATOR", "true")
    monkeypatch.delenv("GOLD_TARGETS", raising=False)
    ran = []
    monkeypatch.setattr(project, "load_and_activate", lambda *a, **k: None)
    monkeypatch.setattr(cli, "cmd_down", lambda targets, **k: ran.append(targets) or 0)
    monkeypatch.setattr(preflight, "teardown", lambda tests: ran.append(tests) or 0)
    monkeypatch.setattr(sys, "argv", ["livetest.cli", "stop", "all"])
    assert cli.main() == 2
    assert "SLT_MY_EMULATOR='true'" in capsys.readouterr().err
    assert preflight.main(["--teardown"]) == 2
    assert ran == []
    monkeypatch.setenv("SLT_MY_EMULATOR", "1")
    assert cli.main() == 0 and preflight.main(["--teardown"]) == 0 and ran == [["all"], []]


def test_a_declared_opt_in_gate_set_to_1_or_0_passes(tmp_path):
    from livetest import service_env
    for n, value in enumerate(("1", "0", "")):
        env = _consumer_gate(tmp_path / str(n), value)
        assert service_env.switch_problems(paths.effective_env(env)) == []
