import os
import types
from pathlib import Path
import pytest
from livetest import striim_provision as sp
from livetest.topology import parse_deployment_groups  # noqa

def test_deps_present_false_when_missing(tmp_path):
    assert sp.deps_present(tmp_path) is False

def test_deps_present_true_when_all_there(tmp_path):
    for n in sp.REQUIRED_DEPS:
        (tmp_path / n).write_text("x")
    assert sp.deps_present(tmp_path) is True

def test_ensure_deps_runs_script_when_missing(tmp_path):
    calls = []
    sp.ensure_deps(tmp_path, run=lambda argv, cwd=None: calls.append((argv, cwd)))
    assert calls and "download-dependencies.sh" in " ".join(calls[0][0])

def test_ensure_deps_skips_when_present(tmp_path):
    for n in sp.REQUIRED_DEPS:
        (tmp_path / "images" / "striim" / "deps").mkdir(parents=True, exist_ok=True)
        (tmp_path / "images" / "striim" / "deps" / n).write_text("x")
    calls = []
    sp.ensure_deps(tmp_path, run=lambda *a, **k: calls.append(a))
    assert not calls

class FakeClient:
    def __init__(self, seq): self.seq = seq
    def list_deployment_groups(self): return self.seq.pop(0)

def _groups(agent):
    servers = [{"uuid": "u1"}, {"uuid": "u2"}]
    ags = [{"uuid": "a1"}] if agent else []
    return [{"output": [{"g1": {"name": "default", "actualServers": servers}},
                        {"g2": {"name": "Agents", "actualServers": ags}}]}]

def test_wait_agent_registered_succeeds():
    c = FakeClient([_groups(False), _groups(True)])
    sp.wait_agent_registered(c, timeout=30, poll=0)

def test_wait_agent_registered_times_out():
    c = FakeClient([_groups(False)] * 3)
    with pytest.raises(TimeoutError):
        sp.wait_agent_registered(c, timeout=0, poll=0)

class FlakyThenOkClient:
    """First call raises (simulating a transient probe error), then succeeds."""
    def __init__(self, seq):
        self.seq = seq
        self.calls = 0

    def list_deployment_groups(self):
        self.calls += 1
        item = self.seq.pop(0)
        if item is None:
            raise RuntimeError("transient probe error")
        return item

def test_wait_agent_registered_tolerates_transient_errors():
    c = FlakyThenOkClient([None, _groups(True)])
    sp.wait_agent_registered(c, timeout=300, poll=0)
    assert c.calls == 2

def test_cluster_up_self_provisions_spanner_emulator(tmp_path):
    calls = []
    sp.cluster_up(tmp_path, run=lambda argv, cwd=None: calls.append((argv, cwd)))
    argv = calls[0][0]
    # brings up the slt- named cluster with the Spanner emulator redirect baked in
    assert "compose.spanner-emulator.yaml" in argv
    assert {"slt-striim", "slt-node", "slt-agent"}.issubset(set(argv))

def test_cluster_down_invokes_compose_down(tmp_path):
    calls = []
    sp.cluster_down(tmp_path, run=lambda argv, cwd=None: calls.append((argv, cwd)))
    argv = calls[0][0]
    assert argv[:2] == ["docker", "compose"]
    assert "down" in argv and "-v" in argv   # applies the emulator redirect and volume cleanup
    assert "compose.spanner-emulator.yaml" in argv   # applies the emulator redirect on down too


# ---- release-awareness: version-tagged image + per-release deps ------------

RELEASE_50 = {"STRIIM_RELEASE": "5.0.6.2F", "STRIIM_VERSION": "5.0.6.2F",
              "STRIIM_SERIES": "5.0", "JAVA_RELEASE": "11"}

def test_required_deps_are_version_specific():
    default = sp._required_deps("5.4.0.6")
    other = sp._required_deps("5.0.6.2F")
    assert "striim-dbms-5.4.0.6-Linux.deb" in default
    assert "striim-dbms-5.0.6.2F-Linux.deb" in other
    assert "striim-dbms-5.4.0.6-Linux.deb" not in other

def test_deps_present_checks_the_release_specific_filenames(tmp_path):
    for n in sp._required_deps("5.0.6.2F"):
        (tmp_path / n).write_text("x")
    assert sp.deps_present(tmp_path, "5.0.6.2F") is True
    assert sp.deps_present(tmp_path, "5.4.0.6") is False   # different version's debs absent

def test_ensure_deps_passes_release_version_through_env(tmp_path, monkeypatch):
    seen = {}
    def fake_run(argv, cwd=None):
        seen["STRIIM_VERSION"] = os.environ.get("STRIIM_VERSION")
    sp.ensure_deps(tmp_path, RELEASE_50, run=fake_run)
    assert seen["STRIIM_VERSION"] == "5.0.6.2F"
    # the override is undone afterwards (no env leak across tests)
    assert os.environ.get("STRIIM_VERSION") != "5.0.6.2F" or "STRIIM_VERSION" not in os.environ

def test_image_ref_carries_the_stack_prefix():
    """The image tag is per-stack, so two checkouts cannot overwrite each other's image.

    It used to be `slt-striim:<version>` for every stack. Two working copies of this repo build
    their own Dockerfile and files/ into that one tag, so whichever ran last won and the other
    correctly detected "built from different sources" and rebuilt -- about 20 minutes, on
    alternate runs, indefinitely. Measured 2026-09-23 between two checkouts, which
    differ deliberately (one installs a Teradata JDBC driver).
    """
    assert sp.image_ref("5.4.0.6C", env={"SLT_STACK_PREFIX": "ec"}) == "ec-slt-striim:5.4.0.6C"
    # unset prefix is unchanged, so a stack that never set one keeps its existing image
    assert sp.image_ref("5.4.0.6C", env={}) == "slt-striim:5.4.0.6C"


def test_image_present_and_probe_use_the_prefixed_tag(monkeypatch):
    """Both docker call sites must agree with compose, or the probe inspects the wrong image.

    conftest strips every SLT_* var, so WITHOUT setting one here both sides of the assert collapse
    to the unprefixed name and this passes even against a hard-coded literal -- which is how the
    first version of it was wrong. Set a prefix and assert the literal.
    """
    monkeypatch.setenv("SLT_STACK_PREFIX", "zz")
    seen = []
    def run(argv):
        seen.append(argv)
        return types.SimpleNamespace(returncode=0, stdout="deadbeef", stderr="")
    sp.image_present("5.4.0.6C", run=run)
    assert seen[0] == ["docker", "images", "-q", "zz-slt-striim:5.4.0.6C"]


def test_image_present_checks_the_release_specific_tag():
    calls = []
    def run(argv):
        calls.append(argv)
        return types.SimpleNamespace(stdout="abc123\n" if argv[-1] == "slt-striim:5.0.6.2F" else "")
    assert sp.image_present("5.0.6.2F", run=run) is True
    assert sp.image_present("5.4.0.6", run=run) is False
    assert calls[0][-1] == "slt-striim:5.0.6.2F"

def test_ensure_image_builds_only_when_release_tag_missing(tmp_path):
    seen = {}
    def build_run(argv, cwd=None):
        seen["argv"] = argv
        seen["STRIIM_VERSION"] = os.environ.get("STRIIM_VERSION")
        seen["JDK_VERSION"] = os.environ.get("JDK_VERSION")
    # query_run is injected, NOT left to fall through to the real subprocess: on a machine that
    # happens to have slt-striim:5.0.6.2F this reached the real daemon (docker images, then
    # create/cp/rm), skipped the build, and failed with KeyError('argv'). Empty stdout =>
    # image_present False => a build is attempted, which is what this asserts.
    sp.ensure_image(tmp_path, RELEASE_50, run=build_run,
                    query_run=lambda argv: types.SimpleNamespace(stdout="", returncode=0))
    assert seen["argv"][:3] == ["docker", "compose", "build"]
    assert seen["STRIIM_VERSION"] == "5.0.6.2F"
    assert seen["JDK_VERSION"] == "11"

def test_cluster_up_and_down_target_the_release_version(tmp_path):
    seen = {}
    def run(argv, cwd=None):
        seen["up_version"] = os.environ.get("STRIIM_VERSION")
    sp.cluster_up(tmp_path, RELEASE_50, run=run)
    assert seen["up_version"] == "5.0.6.2F"
    def run_down(argv, cwd=None):
        seen["down_version"] = os.environ.get("STRIIM_VERSION")
    sp.cluster_down(tmp_path, RELEASE_50, run=run_down)
    assert seen["down_version"] == "5.0.6.2F"

def test_wait_cluster_ready_needs_both_cluster_and_agent():
    # only-agent (1 node) should NOT be "ready"; 2 nodes + agent should be
    only_agent = [{"output": [{"g1": {"name": "default", "actualServers": [{"uuid": "u1"}]}},
                               {"g2": {"name": "Agents", "actualServers": [{"uuid": "a1"}]}}]}]
    full = _groups(True)  # 2 servers + agent (helper from earlier tests)
    c = FakeClient([only_agent, full])
    sp.wait_cluster_ready(c, timeout=30, poll=0)   # first poll not ready, second ready
    with pytest.raises(TimeoutError):
        sp.wait_cluster_ready(FakeClient([only_agent]), timeout=0, poll=0)

def test_wait_cluster_ready_reports_progress_while_waiting():
    # The wait phase must surface a status message (not go silent for minutes on a cold cluster).
    # Use a not-ready-first poll (nodes up, agent not yet registered) so the loop actually waits and
    # emits progress before the second (ready) poll. The message carries an "(elapsed/timeout)" suffix.
    msgs = []
    c = FakeClient([_groups(False), _groups(True)])
    sp.wait_cluster_ready(c, timeout=30, poll=0, progress=lambda label, phase: msgs.append((label, phase)))
    assert any(label == "cluster" and phase.startswith("waiting for node + agent to register")
               for label, phase in msgs)


# ---- OP-loader poisoning recovery (review #5) -------------------------------

def test_op_loader_poisoned_matches_known_signatures():
    assert sp.op_loader_poisoned("... java.util.zip.ZipException: invalid LOC header (bad signature) ...")
    assert sp.op_loader_poisoned("File copying failed during dependency verification")
    assert sp.op_loader_poisoned("error in opening zip file")

def test_op_loader_poisoned_ignores_ordinary_failures():
    # a genuine bad-TQL / app failure must NOT be mistaken for loader poisoning
    assert not sp.op_loader_poisoned("Application FOO reached terminal status DEPLOY_FAILED")
    assert not sp.op_loader_poisoned("column ORDER_DATE does not exist")
    assert not sp.op_loader_poisoned("")

def test_restart_app_nodes_restarts_both_and_skips_wait_without_client(monkeypatch, tmp_path):
    # _LOCK_DIR is redirected because restart_app_nodes clears the OP registry, whose default
    # home is machine-wide: without this a hermetic `pytest` run silently deletes the real
    # registrations (and races a live run in flight).
    from livetest import stack as _stack
    monkeypatch.setattr(_stack, "_LOCK_DIR", tmp_path / "locks")
    calls = []
    sp.restart_app_nodes(client=None, run=lambda argv: calls.append(argv))
    assert calls == [["docker", "restart", "slt-striim", "slt-node"]]  # both nodes, no wait


def test_cluster_down_removes_orphans():
    # A container whose compose SERVICE is gone survives a plain `down` -- it is only removed
    # by --remove-orphans. Without this, upgrading across the slt-striim-node -> slt-node
    # rename left two 40 GB-heap JVMs running while `stop live` reported a clean teardown.
    calls = []
    sp.cluster_down(Path("/x"), {"STRIIM_VERSION": "5.4.0.6"},
                    run=lambda argv, cwd=None: calls.append(argv))
    assert calls[0][-3:] == ["-v", "--remove-orphans"] or "--remove-orphans" in calls[0]
    assert "down" in calls[0] and "-v" in calls[0]


def test_cluster_up_and_down_agree_on_orphan_removal():
    # The two halves must not drift: a rename strands containers on BOTH paths, and a
    # teardown that misses them is the worse half (it reports success over running JVMs).
    up, down = [], []
    sp.cluster_up(Path("/x"), {"STRIIM_VERSION": "5.4.0.6"},
                  run=lambda argv, cwd=None: up.append(argv))
    sp.cluster_down(Path("/x"), {"STRIIM_VERSION": "5.4.0.6"},
                    run=lambda argv, cwd=None: down.append(argv))
    assert "--remove-orphans" in up[0] and "--remove-orphans" in down[0]


# --- staleness is answered by the image's own build inputs, not by its name ------------------
#
# The tag stays `slt-striim:<version>`. An earlier attempt encoded a digest in the tag instead;
# that broke every consumer that parsed or constructed the name (plugin.py, stack-doctor twice,
# the console, the docs) and needed the same digest implemented in Python AND shell. Bytes need
# no agreement, so nothing outside this module knows this check exists.

def _fake_docker(inputs: dict, create_rc=0, cp_rc=0):
    """A docker stub whose `cp` materialises `inputs` ({relpath: bytes}) into the dest dir."""
    calls = []

    def run(argv):
        calls.append(argv)
        if argv[:3] == ["docker", "images", "-q"]:
            return types.SimpleNamespace(stdout="abc123\n", returncode=0)   # image present
        if argv[:2] == ["docker", "create"]:
            return types.SimpleNamespace(stdout="deadbeef\n", returncode=create_rc)
        if argv[:2] == ["docker", "cp"]:
            if cp_rc == 0:
                dest = Path(argv[-1])
                for rel, data in inputs.items():
                    (dest / rel).parent.mkdir(parents=True, exist_ok=True)
                    (dest / rel).write_bytes(data)
            return types.SimpleNamespace(stdout="", returncode=cp_rc)
        return types.SimpleNamespace(stdout="", returncode=0)

    return run, calls


def _checkout(tmp_path, entrypoint=b"echo hi\n", dockerfile=b"FROM scratch\n"):
    root = tmp_path / "images" / "striim"
    (root / "files").mkdir(parents=True)
    (root / "Dockerfile").write_bytes(dockerfile)
    (root / "files" / "entrypoint.sh").write_bytes(entrypoint)
    return tmp_path


def test_matching_inputs_mean_the_image_is_current(tmp_path):
    d = _checkout(tmp_path)
    run, _ = _fake_docker(sp._local_build_inputs(d))
    assert sp.image_inputs_match("5.4.0.6", d, run=run) is True


def test_an_edited_entrypoint_makes_the_image_stale(tmp_path):
    # The failure that started all of this: entrypoint.sh's baked cluster address changed and
    # every machine holding the old image kept using it, so the cluster silently never formed.
    d = _checkout(tmp_path)
    stale = dict(sp._local_build_inputs(d))
    stale["files/entrypoint.sh"] = b"echo OLD\n"
    run, _ = _fake_docker(stale)
    assert sp.image_inputs_match("5.4.0.6", d, run=run) is False


def test_an_edited_dockerfile_makes_the_image_stale(tmp_path):
    # The case a digest-of-the-image-contents could not see, and the reason the inputs are
    # copied IN rather than inferred from what the build produced.
    d = _checkout(tmp_path)
    stale = dict(sp._local_build_inputs(d))
    stale["Dockerfile"] = b"FROM scratch\nRUN apt-get install -y something\n"
    run, _ = _fake_docker(stale)
    assert sp.image_inputs_match("5.4.0.6", d, run=run) is False


def test_an_image_without_embedded_inputs_is_stale(tmp_path):
    # Built before the inputs were copied in: its provenance is unknowable, so rebuild.
    d = _checkout(tmp_path)
    run, _ = _fake_docker({}, cp_rc=1)
    assert sp.image_inputs_match("5.4.0.6", d, run=run) is False


def test_docker_failing_to_answer_does_not_force_a_rebuild(tmp_path):
    # A 20-minute rebuild is the wrong answer to "docker hiccupped".
    d = _checkout(tmp_path)
    run, _ = _fake_docker({}, create_rc=1)
    assert sp.image_inputs_match("5.4.0.6", d, run=run) is True

    def boom(argv):
        raise OSError("docker vanished")
    assert sp.image_inputs_match("5.4.0.6", d, run=boom) is True


def test_the_probe_container_is_always_removed(tmp_path):
    d = _checkout(tmp_path)
    run, calls = _fake_docker(sp._local_build_inputs(d))
    sp.image_inputs_match("5.4.0.6", d, run=run)
    # Removed by NAME, not by the id create returned: a probe leaked by a SIGINT is invisible to
    # `docker ps` and to --remove-orphans, so it is named to be greppable and self-clearing.
    rms = [a for a in calls if a[:3] == ["docker", "rm", "-f"]]
    assert rms and all(a[3].startswith("slt-image-probe-") for a in rms), rms
    # Cleared BEFORE create as well, so an interrupted run does not block the next one.
    assert calls.index(rms[0]) < next(i for i, a in enumerate(calls) if a[:2] == ["docker", "create"])
    # ...and it is CREATED, never started: no amd64 emulation cost.
    assert not any(a[:2] == ["docker", "start"] for a in calls)


def test_ensure_image_rebuilds_a_present_but_stale_image(tmp_path, monkeypatch):
    d = _checkout(tmp_path)
    built = []
    monkeypatch.setattr(sp, "image_present", lambda v, run=None: True)
    monkeypatch.setattr(sp, "image_inputs_match", lambda v, sd, run=None: False)
    sp.ensure_image(d, {"STRIIM_VERSION": "5.4.0.6", "JAVA_RELEASE": "17"},
                    run=lambda argv, cwd=None: built.append(argv))
    assert built and built[0][:3] == ["docker", "compose", "build"]


def test_ensure_image_skips_when_present_and_current(tmp_path, monkeypatch):
    d = _checkout(tmp_path)
    monkeypatch.setattr(sp, "image_present", lambda v, run=None: True)
    monkeypatch.setattr(sp, "image_inputs_match", lambda v, sd, run=None: True)
    sp.ensure_image(d, {"STRIIM_VERSION": "5.4.0.6"},
                    run=lambda argv, cwd=None: pytest.fail("rebuilt a current image"))


def test_ensure_image_touches_no_real_docker_when_runners_are_injected(tmp_path, monkeypatch):
    # The whole suite must stay hermetic: an earlier version reached the real daemon through an
    # un-forwarded seam and could untag a developer's actual 21 GB image.
    d = _checkout(tmp_path)
    escaped = []
    monkeypatch.setattr(sp.subprocess, "run",
                        lambda argv, **k: escaped.append(argv) or types.SimpleNamespace(
                            stdout="", returncode=0, stderr=""))
    sp.ensure_image(d, {"STRIIM_VERSION": "5.4.0.6", "JAVA_RELEASE": "17"},
                    run=lambda argv, cwd=None: None,
                    query_run=lambda argv: types.SimpleNamespace(stdout="", returncode=1))
    assert escaped == [], f"these reached the real docker: {escaped}"


# --- a REUSED cluster must also notice a stale image -----------------------------------------
#
# This is the flow the hashed-tag design covered only by accident (its version comparison always
# mismatched, forcing a redeploy every session). With the tag back to `slt-striim:<version>` the
# version always matches, so without an explicit check a cluster left running -- the dominant
# developer flow, and where entrypoint.sh is most likely to have just been edited -- keeps
# serving containers built from the old image, and ensure_image is never reached at all.

def test_a_reused_cluster_on_stale_sources_is_redeployed(monkeypatch):
    # Exercises the real predicate. The previous version of this test monkeypatched both inputs
    # and then asserted the mocks returned what they were set to -- deleting the whole feature
    # left it green.
    from livetest import plugin
    monkeypatch.setattr(plugin._sp, "image_inputs_match", lambda v, d: False)
    reason = plugin._redeploy_reason("5.4.0.6C", "5.4.0.6C", Path("/svc"))
    assert reason and "built from different sources" in reason


def test_a_reused_cluster_on_current_sources_is_left_alone(monkeypatch):
    from livetest import plugin
    monkeypatch.setattr(plugin._sp, "image_inputs_match", lambda v, d: True)
    assert plugin._redeploy_reason("5.4.0.6C", "5.4.0.6C", Path("/svc")) is None


def test_a_version_mismatch_still_wins(monkeypatch):
    # And it must not even ASK about sources: the cluster is going down either way, and the
    # probe costs a docker round-trip.
    from livetest import plugin
    monkeypatch.setattr(plugin._sp, "image_inputs_match",
                        lambda v, d: pytest.fail("probed the image for a version mismatch"))
    reason = plugin._redeploy_reason("5.2.0.1", "5.4.0.6C", Path("/svc"))
    assert reason and "5.2.0.1" in reason and "5.4.0.6C" in reason


def test_an_uninspectable_cluster_is_not_redeployed_on_a_guess(monkeypatch):
    from livetest import plugin
    monkeypatch.setattr(plugin._sp, "image_inputs_match", lambda v, d: True)
    assert plugin._redeploy_reason(None, "5.4.0.6C", Path("/svc")) is None


def test_image_inputs_match_pins_the_platform():
    # compose pins linux/amd64; a containerd image store refuses to create a container from a
    # foreign-platform image without it, `create` fails, and the check then reads "current"
    # forever -- the original bug, restored, with nothing to notice.
    seen = []

    def run(argv):
        seen.append(argv)
        if argv[:3] == ["docker", "images", "-q"]:
            return types.SimpleNamespace(stdout="abc123\n", returncode=0)
        return types.SimpleNamespace(stdout="", returncode=1)

    sp.image_inputs_match("5.4.0.6", Path("services/striim"), run=run)
    create = next(a for a in seen if a[:2] == ["docker", "create"])
    assert "--platform" in create and "linux/amd64" in create, create


# ---- re-authentication after a node restart (DEFERRED_ISSUES: live-test framework) -------
#
# StriimApi authenticates ONCE in __init__ and getHeader() signs every later call with that
# cached token. `docker restart` kills the JVM that issued it, so without a refresh every
# subsequent call is 401 -- and wait_cluster_ready swallows those and reports a timeout, so
# the operator is sent to inspect nodes that are perfectly healthy. Observed 2026-08-25 on an
# OP sweep: ~22 consecutive 401s, then "cluster not ready within 300s".

class _ExpiringClient:
    """A client whose token dies with the restart: every call 401s until re-auth."""

    def __init__(self, groups_after_reauth):
        self.api = types.SimpleNamespace(getAuthToken=self._reauth)
        self._valid = False
        self._groups = groups_after_reauth
        self.reauth_calls = 0
        self.polls = 0

    def _reauth(self, timeout=None):
        self.reauth_calls += 1
        self._valid = True

    def list_deployment_groups(self):
        self.polls += 1
        if not self._valid:
            raise sp_striim_error("LIST DEPLOYMENTGROUPS failed: 401 — Please check if valid "
                                  "authorization header/token is passed")
        return self._groups


def sp_striim_error(msg):
    from livetest.striim import StriimError
    return StriimError(msg)


def test_restart_app_nodes_reauthenticates_before_waiting(monkeypatch, tmp_path):
    """The whole point of the recovery is to observe the cluster it just restarted."""
    from livetest import stack as _stack
    monkeypatch.setattr(_stack, "_LOCK_DIR", tmp_path / "locks")
    monkeypatch.setenv("SLT_CLUSTER_SETTLE", "0")
    client = _ExpiringClient(_groups(True))
    sp.restart_app_nodes(client, run=lambda argv: None, timeout=5)
    assert client.reauth_calls == 1, "the stale token was never refreshed"


def test_restart_app_nodes_reauth_happens_before_the_first_poll(monkeypatch, tmp_path):
    """Ordering is the fix: re-auth AFTER the wait would still time out."""
    from livetest import stack as _stack
    monkeypatch.setattr(_stack, "_LOCK_DIR", tmp_path / "locks")
    monkeypatch.setenv("SLT_CLUSTER_SETTLE", "0")
    order = []
    client = _ExpiringClient(_groups(True))
    real_reauth, real_list = client._reauth, client.list_deployment_groups
    client.api.getAuthToken = lambda timeout=None: (order.append("reauth"), real_reauth())
    client.list_deployment_groups = lambda: (order.append("poll"), real_list())[1]
    sp.restart_app_nodes(client, run=lambda argv: None, timeout=5)
    assert order[0] == "reauth", f"polled before re-authenticating: {order}"


def test_restart_app_nodes_tolerates_a_client_that_cannot_reauthenticate(monkeypatch, tmp_path):
    """Re-auth is a repair; failing to repair must not be louder than the original fault."""
    from livetest import stack as _stack
    monkeypatch.setattr(_stack, "_LOCK_DIR", tmp_path / "locks")
    monkeypatch.setenv("SLT_CLUSTER_SETTLE", "0")
    sp.restart_app_nodes(FakeClient([_groups(True)]), run=lambda argv: None, timeout=5)


def test_restart_app_nodes_reauth_deadline_scales_with_the_callers_timeout(monkeypatch, tmp_path):
    """A 120s cap expired ~10s after a hard kill on the emulated Mac cluster, whose
    API first answers authenticated ~171s after container start."""
    from livetest import stack as _stack
    monkeypatch.setattr(_stack, "_LOCK_DIR", tmp_path / "locks")
    monkeypatch.setenv("SLT_CLUSTER_SETTLE", "0")
    seen = []
    monkeypatch.setattr(sp, "_reauthenticate",
                        lambda client, timeout: seen.append(timeout))
    sp.restart_app_nodes(FakeClient([_groups(True)]), run=lambda argv: None, timeout=300)
    assert seen == [300.0]


def test_restart_app_nodes_names_the_reauth_deadline_when_it_expired(monkeypatch, tmp_path):
    """The next failure must say WHICH deadline expired, not be another opaque timeout."""
    from livetest import stack as _stack
    monkeypatch.setattr(_stack, "_LOCK_DIR", tmp_path / "locks")
    monkeypatch.setenv("SLT_CLUSTER_SETTLE", "0")
    monkeypatch.setattr(sp.time, "sleep", lambda _s: None)

    class _NeverBack:
        api = _TokenApi([ConnectionRefusedError(61, "Connection refused")] * 50)

        def list_deployment_groups(self):
            raise sp_striim_error("LIST DEPLOYMENTGROUPS failed: 401 — token")

    with pytest.raises(TimeoutError) as e:
        sp.restart_app_nodes(_NeverBack(), run=lambda argv: None, timeout=0)
    msg = str(e.value)
    assert "CLUSTER-READY deadline (0s) expired" in msg
    assert "RE-AUTHENTICATION deadline (0s) expired first" in msg
    assert "Connection refused" in msg


def test_restart_app_nodes_says_reauth_succeeded_when_only_the_cluster_wait_expired(monkeypatch, tmp_path):
    from livetest import stack as _stack
    monkeypatch.setattr(_stack, "_LOCK_DIR", tmp_path / "locks")
    monkeypatch.setenv("SLT_CLUSTER_SETTLE", "0")
    client = _ExpiringClient(_groups(False))
    with pytest.raises(TimeoutError) as e:
        sp.restart_app_nodes(client, run=lambda argv: None, timeout=0)
    msg = str(e.value)
    assert "CLUSTER-READY deadline (0s) expired" in msg and "not ready" in msg
    assert "Re-authentication after the restart succeeded" in msg


def test_wait_cluster_ready_says_unauthenticated_when_no_poll_ever_answered():
    """Defect (b): reporting "cluster not ready" when the nodes were never observed at all
    is a real failure wearing the wrong label -- it cost a debugging session."""
    class _AlwaysUnauthorized:
        def list_deployment_groups(self):
            raise sp_striim_error("LIST DEPLOYMENTGROUPS failed: 401 — token")

    with pytest.raises(TimeoutError) as e:
        sp.wait_cluster_ready(_AlwaysUnauthorized(), timeout=0, poll=0)
    msg = str(e.value)
    assert "no authenticated reply" in msg
    assert "401" in msg, "the operator needs the cause, not just the symptom"
    assert "not ready" not in msg, "must not blame the cluster it never reached"


def test_wait_cluster_ready_still_blames_the_cluster_when_it_did_answer():
    """The new message must not swallow the genuine not-ready case."""
    with pytest.raises(TimeoutError) as e:
        sp.wait_cluster_ready(FakeClient([_groups(False)]), timeout=0, poll=0)
    assert "cluster (>=2 nodes + agent) not ready" in str(e.value)

# --- _reauthenticate retry behaviour (independent review) --------------------------

class _TokenApi:
    """Stands in for tools/python/striim_api.StriimApi's getAuthToken."""
    def __init__(self, failures):
        self.failures = list(failures)   # exceptions to raise, in order; then succeed
        self.calls = 0

    def getAuthToken(self, timeout=None):
        self.calls += 1
        if self.failures:
            raise self.failures.pop(0)
        return "token"


class _TokenClient:
    def __init__(self, api):
        self.api = api


def test_reauthenticate_retries_a_connection_reset_then_succeeds(monkeypatch):
    """The transient this exists for: the node is mid-`docker restart`."""
    monkeypatch.setattr(sp.time, "sleep", lambda _s: None)
    api = _TokenApi([ConnectionResetError(54, "Connection reset by peer")])
    sp._reauthenticate(_TokenClient(api), timeout=10, poll=0)
    assert api.calls == 2, "should have retried once and then succeeded"


def test_reauthenticate_gives_up_quietly_when_the_node_never_comes_back(monkeypatch):
    """Exhaustion returns rather than raising: wait_cluster_ready is the thing that reports a
    genuinely dead cluster, and a repair must not be louder than what it repairs."""
    monkeypatch.setattr(sp.time, "sleep", lambda _s: None)
    api = _TokenApi([ConnectionResetError(54, "reset")] * 50)
    sp._reauthenticate(_TokenClient(api), timeout=0, poll=0)   # deadline already passed
    assert api.calls == 1


def test_reauthenticate_does_NOT_retry_a_permanent_auth_failure(monkeypatch):
    """The regression an earlier `except Exception` introduced, pinned against the real client.

    A wrong password is an HTTP 401. Retrying it turned a one-second correct error into ~7
    minutes ending in a message saying the token merely expired. requests.HTTPError IS an
    OSError, so getAuthToken must convert it (to RuntimeError) for this to fail fast.
    """
    import requests
    from livetest.striim import striim_api
    calls = []
    def post(url, **kw):
        calls.append(url)
        r = requests.Response()
        r.status_code, r.url = 401, url
        return r
    monkeypatch.setattr(striim_api.requests, "post", post)
    monkeypatch.setattr(sp.time, "sleep", lambda _s: None)
    api = striim_api.StriimApi.__new__(striim_api.StriimApi)
    api.url_base, api.username, api.password = "http://striim:9080/api/v2", "u", "p"
    with pytest.raises(RuntimeError, match="authentication failed"):
        sp._reauthenticate(_TokenClient(api), timeout=120, poll=0)
    assert len(calls) == 1, "a permanent failure must fail fast, not burn the deadline"




def test_restart_app_nodes_default_runner_has_finite_timeout_and_reports_failure(monkeypatch, tmp_path):
    from livetest import stack

    monkeypatch.setattr(stack, "_LOCK_DIR", tmp_path / "locks")
    monkeypatch.setenv("SLT_CLUSTER_SETTLE", "0")
    calls = []

    def run(argv, **kw):
        calls.append((argv, kw))
        return types.SimpleNamespace(returncode=1, stderr="cannot restart worker")

    monkeypatch.setattr(sp.subprocess, "run", run)
    with pytest.raises(RuntimeError, match="cannot restart worker"):
        sp.restart_app_nodes(FakeClient([_groups(True)]), timeout=17)
    assert len(calls) == 1
    assert calls[0][1]["timeout"] == 17


def test_reauthenticate_bounds_each_attempt_by_the_deadline(monkeypatch):
    """Each attempt's request timeout is what is left of the deadline, connect capped at 10 s."""
    seen = []
    class _Api(_TokenApi):
        def getAuthToken(self, timeout=None):
            seen.append(timeout)
            return super().getAuthToken(timeout)
    clock = iter([100.0, 100.0, 150.0, 150.0])
    monkeypatch.setattr(sp.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(sp.time, "sleep", lambda _s: None)
    sp._reauthenticate(_TokenClient(_Api([ConnectionResetError(54, "reset")])), timeout=60, poll=0)
    assert seen == [(10.0, 60.0), (10.0, 10.0)]


def test_reauthenticate_still_works_with_a_getauthtoken_without_timeout(monkeypatch):
    """A getAuthToken of another shape (a test double, another transport) must not turn the
    repair into a TypeError."""
    calls = []
    api = types.SimpleNamespace(getAuthToken=lambda: calls.append(1))
    assert sp._reauthenticate(_TokenClient(api), timeout=10, poll=0) is None
    assert calls == [1]
