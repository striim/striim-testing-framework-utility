"""striim-test doctor: the checks that predict, before a run starts, what would make it skip or
refuse its cases later -- the running cluster's release, the license's CPU count, the jar builds,
the per-case gates, the integration services and session -- and ``--strict``, which fails on them.
Docker, the JDK lookup and every probe are injected; nothing here reaches Docker or a network."""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace

import pytest

from _clikit import REPO

sys.path.insert(0, str(REPO / "scripts" / "cli"))

from striim_test import doctor  # noqa: E402
from striim_test.errors import CONFIG, INFRA  # noqa: E402


def install(tmp_path, version="5.4.0.6"):
    home = tmp_path / f"Striim_{version}"
    (home / "lib").mkdir(parents=True)
    (home / "lib" / f"Platform-{version}.jar").write_text("")
    return str(home)


SHARED = {"SLT_INFRA_OWNERSHIP": "shared", "SLT_KEEP_SERVICES": "1"}


# --- release -----------------------------------------------------------------------------------

def test_release_from_striim_home(tmp_path):
    c, = doctor.check_release({"STRIIM_HOME": install(tmp_path)})
    assert c.status == "ok" and "Striim 5.4.0.6 (JDK 17)" in c.message


def test_release_without_striim_home_warns_of_the_default():
    c, = doctor.check_release({})
    assert c.status == "warn" and "assumes Striim 5.4.2" in c.message


def test_release_from_a_home_with_no_platform_jar_fails(tmp_path):
    c, = doctor.check_release({"STRIIM_HOME": str(tmp_path)})
    assert c.status == doctor.FAIL and c.code == CONFIG and "no Platform-*.jar" in c.message


# --- the running cluster's identity (5.4.2 running, 5.4.0.6 required: every live case skipped) ---

def test_a_shared_cluster_of_another_release_fails_and_says_both_fixes(tmp_path):
    env = {**SHARED, "STRIIM_HOME": install(tmp_path)}
    c, = doctor.check_cluster_identity(env, {}, running=lambda e: "5.4.2")
    assert c.status == doctor.FAIL and c.code == CONFIG
    assert "running version 5.4.2 != required 5.4.0.6" in c.message
    assert "Point STRIIM_HOME at the 5.4.2 install, or stop the shared stack" in c.message


def test_a_shared_cluster_of_the_required_release_passes(tmp_path):
    env = {**SHARED, "STRIIM_HOME": install(tmp_path)}
    c, = doctor.check_cluster_identity(env, {}, running=lambda e: "5.4.0.6")
    assert c.status == "ok"


def test_an_exclusive_run_with_a_cluster_already_answering_fails():
    env = {"SLT_INFRA_OWNERSHIP": "exclusive"}
    c, = doctor.check_cluster_identity(env, {}, running=lambda e: "5.4.2")
    assert c.status == doctor.FAIL and "never adopts" in c.message


def test_identity_is_left_to_the_ownership_check_when_ownership_is_unset():
    assert doctor.check_cluster_identity({}, {}, running=lambda e: "5.4.2") == []


def test_the_striim_check_runs_identity_only_when_a_cluster_answers():
    seen = []
    identity = lambda env, dotenv: seen.append(1) or []        # noqa: E731
    doctor.check_striim({}, {}, probe=lambda *a: (True, "ok"), identity=identity)
    doctor.check_striim({}, {}, probe=lambda *a: (False, "connection error"),
                        docker=lambda: (False, "down"), identity=identity)
    assert seen == [1]


# --- sizing: the license's 24 CPUs and Docker's memory -----------------------------------------

def sizing(env=None, ncpu=14, mem=24 * 2**30, running=None):
    running = running or {}
    return doctor.check_sizing(env or {}, {}, info=lambda e: (ncpu, mem),
                               cpus=lambda name: running.get(name))


def test_uncapped_nodes_over_the_license_fail_and_say_the_caps():
    c = [c for c in sizing() if c.status == doctor.FAIL][0]
    assert "14 + 14 CPUs" in c.message and "SLT_STRIIM_PRIMARY_CPUS=12 SLT_STRIIM_NODE_CPUS=12" in c.message


def test_capped_nodes_within_the_license_pass():
    c, = sizing({"SLT_STRIIM_PRIMARY_CPUS": "4", "SLT_STRIIM_NODE_CPUS": "4", "SLT_STRIIM_MEM_LIMIT": "4g"})
    assert c.status == "ok" and "4 + 4 CPUs" in c.message


def test_a_running_cluster_is_judged_by_its_containers_limits():
    c, = sizing(running={"slt-striim": 4.0, "slt-node": 4.0, "slt-agent": 0.0})
    assert c.status == "ok" and "running containers" in c.message


def test_a_running_cluster_without_its_node_predicts_skipped_cluster_cases():
    c, = sizing(running={"slt-striim": 0.0, "slt-agent": 0.0})
    assert c.skips and "without slt-node" in c.message


def test_three_heaps_beyond_dockers_memory_with_no_limit_warn():
    checks = sizing({"SLT_STRIIM_PRIMARY_CPUS": "4", "SLT_STRIIM_NODE_CPUS": "4"}, mem=23 * 2**30)
    w, = [c for c in checks if c.status == "warn"]
    assert "48.0 GB" in w.message and "SLT_STRIIM_MEM_LIMIT" in w.message


def test_sizing_is_not_checked_against_your_own_striim():
    assert sizing({"STRIIM_URL": "http://s:9080"}) == []


# --- jar builds --------------------------------------------------------------------------------

def case(tmp_path, name, text="name: x\n"):
    d = tmp_path / name
    d.mkdir(exist_ok=True)
    (d / "test.yaml").write_text(text)
    return d / "test.yaml"


def build(env, files, tier="integration", mvn=True, jdk=None):
    def java_home(release):
        if jdk:
            raise doctor_opartifacts().OpArtifactError(jdk)
        return None
    return doctor.check_build(env, files, lambda f: tier, java_home=java_home,
                              which=lambda tool: "/bin/mvn" if mvn else None)


def doctor_opartifacts():
    from livetest import opartifacts
    return opartifacts


def test_builds_with_everything_present_pass(tmp_path):
    c, = build({"STRIIM_HOME": install(tmp_path)}, [case(tmp_path, "a")])
    assert c.status == "ok" and "1 selected case(s) build a jar" in c.message


def test_no_mvn_predicts_skips(tmp_path):
    c, = build({"STRIIM_HOME": install(tmp_path)}, [case(tmp_path, "a")], mvn=False)
    assert c.skips and "mvn is not on PATH" in c.message


def test_a_missing_jdk_predicts_skips(tmp_path):
    c, = build({"STRIIM_HOME": install(tmp_path)}, [case(tmp_path, "a")], jdk="build needs JDK 17")
    assert c.skips and "build needs JDK 17" in c.message


def test_integration_cases_without_striim_home_predict_skips(tmp_path):
    c, = build({}, [case(tmp_path, "a")])
    assert c.skips and "STRIIM_HOME is unset" in c.message


def test_a_live_case_that_loads_nothing_builds_nothing(tmp_path):
    assert build({}, [case(tmp_path, "a")], tier="live", mvn=False) == []


# --- per-case gates (livetest.plugin LiveItem._runtest) -----------------------------------------

def gates(monkeypatch, tmp_path, manifest, env=None, openssl=True, outage=None):
    from livetest import manifest as live_manifest
    monkeypatch.setattr(live_manifest, "load_manifest", lambda f: manifest)
    return doctor.check_case_gates([case(tmp_path, "c")], env or {}, {}, None,
                                   which=lambda tool: "/bin/openssl" if openssl else None,
                                   outage=outage or (lambda svc, env: ("slt-x", "")))


def m(**kw):
    return SimpleNamespace(**{"recover": None, "topology": "single", "action_specs": [],
                              "requires": [], **kw})


def test_node_faults_without_the_opt_in_predict_a_skip(monkeypatch, tmp_path):
    c, = gates(monkeypatch, tmp_path, m(recover={"node": "slt-node"}))
    assert c.skips and "SLT_ENABLE_NODE_FAULTS=1" in c.message
    assert gates(monkeypatch, tmp_path, m(recover={"node": "slt-node"}),
                 env={"SLT_ENABLE_NODE_FAULTS": "1"}) == []


def test_the_cluster_topology_on_your_own_server_predicts_a_skip(monkeypatch, tmp_path):
    c, = gates(monkeypatch, tmp_path, m(topology="cluster"), env={"STRIIM_URL": "http://s:9080"})
    assert c.skips and "topology: cluster" in c.message


def test_an_outage_target_without_a_container_predicts_a_skip(monkeypatch, tmp_path):
    c, = gates(monkeypatch, tmp_path, m(action_specs=[{"type": "service_outage", "service": "pg"}]),
               outage=lambda svc, env: (None, "service_outage: service pg has no container"))
    assert c.skips and "has no container" in c.message


def test_spanner_without_openssl_fails(monkeypatch, tmp_path):
    c, = gates(monkeypatch, tmp_path, m(requires=["spanner"]), openssl=False)
    assert c.status == doctor.FAIL and c.code == INFRA and "openssl" in c.subject


# --- integration services and session ---------------------------------------------------------

UP = lambda: (True, "Docker 29.0")                        # noqa: E731


def test_an_integration_service_the_tier_does_not_provision_predicts_a_skip():
    c, = doctor.check_int_services({"nosuch": ["a"]}, {}, docker=UP)
    assert c.skips and "does not provision it" in c.message


def test_incomplete_real_spanner_settings_fail(tmp_path):
    c, = doctor.check_int_services({"spanner": ["a"]}, {"INT_SPANNER_CREDENTIALS": str(tmp_path / "k.json")},
                                   docker=UP)
    assert c.status == doctor.FAIL and "INT_SPANNER_PROJECT" in c.message


def test_the_spanner_emulator_with_docker_passes():
    c, = doctor.check_int_services({"spanner": ["a"]}, {}, docker=UP)
    assert c.status == "ok"


def test_the_spanner_emulator_without_docker_predicts_a_skip():
    c, = doctor.check_int_services({"spanner": ["a"]}, {}, docker=lambda: (False, "down"))
    assert c.skips


def test_a_held_integration_session_fails(monkeypatch, tmp_path):
    from filelock import FileLock

    from inttest import plugin as int_plugin
    path = tmp_path / ".int-session.lock"
    monkeypatch.setattr(int_plugin, "_session_lock_path", lambda: path)
    assert doctor.check_int_session({})[0].status == "ok"
    held = FileLock(str(path))
    held.acquire()
    try:
        c, = doctor.check_int_session({})
        assert c.status == doctor.FAIL and c.code == INFRA
    finally:
        held.release()
    assert doctor.check_int_session({"INT_ALLOW_CONCURRENT_SESSIONS": "1"}) == []


# --- lock dir ----------------------------------------------------------------------------------

@pytest.mark.skipif(os.geteuid() == 0, reason="root writes anywhere")
def test_an_unwritable_lock_dir_fails(tmp_path):
    d = tmp_path / "locks"
    d.mkdir()
    d.chmod(0o500)
    try:
        c, = doctor.check_lock_dir({"SLT_LOCK_DIR": str(d)})
        assert c.status == doctor.FAIL and "SLT_LOCK_DIR=$HOME/.slt-locks" in c.message
    finally:
        d.chmod(0o700)


def test_a_lock_dir_not_yet_made_is_judged_by_its_parent(tmp_path):
    c, = doctor.check_lock_dir({"SLT_LOCK_DIR": str(tmp_path / "new" / "locks")})
    assert c.status == "ok"


# --- --strict ----------------------------------------------------------------------------------

def test_strict_fails_only_the_warnings_that_predict_skips():
    checks = [doctor._skip("build", "mvn is not on PATH"), doctor._warn("release", "assumes 5.4.2"),
              doctor._ok("striim", "ok")]
    out = doctor.strict(checks)
    assert [c.status for c in out] == [doctor.FAIL, "warn", "ok"]
    assert out[0].code == INFRA and doctor.exit_code(out) == INFRA
    assert doctor.exit_code(checks) == 0


# --- scope: integration and perf cases need no cluster ------------------------------------------

def subjects(tmp_path, tier):
    f = case(tmp_path, "c", "name: c\n")
    checks = doctor.run_checks({"SLT_PROJECT_ROOT": str(tmp_path)}, REPO, cases=[str(f)],
                               tier_of=lambda p: tier, probe=lambda *a: (False, "refused"),
                               docker=lambda: (False, "down"), java_home=lambda r: None,
                               which=lambda tool: "/bin/" + tool, outage=lambda s, e: ("x", ""))
    return {c.subject for c in checks}


def test_integration_cases_alone_skip_the_cluster_checks(tmp_path):
    seen = subjects(tmp_path, "integration")
    assert not {"striim", "ownership", "lock dir", "ledgers"} & seen
    assert {"release", "build"} <= seen


def test_a_live_case_keeps_the_cluster_checks(tmp_path):
    assert {"striim", "ownership", "lock dir"} <= subjects(tmp_path, "live")


def test_a_list_of_live_case_roots_is_checked_entry_by_entry(tmp_path):
    a, b = tmp_path / "a" / "regression", tmp_path / "b" / "hello"
    a.mkdir(parents=True)
    b.mkdir(parents=True)
    env = {"SLT_PROJECT_ROOT": str(tmp_path), "SLT_LIVE_CASES": os.pathsep.join([str(a), str(b)])}
    assert not [c for c in doctor.check_env(env, REPO)[0] if c.status == doctor.FAIL]
    env["SLT_LIVE_CASES"] += os.pathsep + str(tmp_path / "missing")
    c, = [c for c in doctor.check_env(env, REPO)[0] if c.status == doctor.FAIL]
    assert "missing" in c.message


def test_a_tryout_mismatch_at_the_same_release_says_to_restart_not_repoint(tmp_path, monkeypatch):
    from livetest import plugin
    monkeypatch.setattr(plugin, "_tryout_redeploy_reason", lambda have, want: "the tryout jars differ")
    env = {**SHARED, "STRIIM_HOME": install(tmp_path)}
    c, = doctor.check_cluster_identity(env, {}, running=lambda e: "5.4.0.6")
    assert c.status == doctor.FAIL and "tryout jars differ" in c.message
    assert "Stop the shared stack" in c.message and "Point STRIIM_HOME" not in c.message
