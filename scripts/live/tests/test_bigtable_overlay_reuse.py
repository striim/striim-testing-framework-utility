"""A reused Docker cluster must carry the Bigtable emulator redirect this run asks for.

compose.bigtable-emulator.yaml is applied only when a run opts in (SLT_BIGTABLE_SERVICE or
SLT_EMULATORS), but reusing a running cluster never runs compose. So before a warm or booting cluster
is accepted, every member's BIGTABLE_EMULATOR_HOST is compared with the run's choice and endpoint.
A mismatch is a redeploy reason: where the framework may recreate the cluster it does, and a shared
or exclusive run refuses with how to resolve it. Member environments come from a simulated
`docker container inspect`; nothing reaches Docker or Striim.
"""
import json
import subprocess
from types import SimpleNamespace

import pytest

from livetest import paths, plugin, stack, striim_provision as sp
from tests.test_cluster_resolution import groups, resolve

OPT_INS = ("SLT_BIGTABLE_SERVICE", "SLT_EMULATORS", "SLT_BIGTABLE_GRPC_HOST_PORT")
DOTENV_VALUES = paths.dotenv_values     # the real .env layer; the root conftest stubs it out per test
REDIRECT_9470 = "host.docker.internal:9470"


class Cluster:
    """The running members' BIGTABLE_EMULATOR_HOST, as `docker container inspect` reports it;
    cluster_up replaces it with what its compose files would give."""

    def __init__(self, redirect):
        self.redirect = {c: redirect for c in stack.cluster_containers()}
        self.events = []

    def inspect(self, argv):
        assert argv[:4] == ["docker", "container", "inspect", "-f"]
        value = self.redirect[argv[-1]]
        env = ["PATH=/usr/bin"] + ([f"BIGTABLE_EMULATOR_HOST={value}"] if value else [])
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(env), stderr="")

    def up(self, *a, **k):
        files = sp._compose_files()
        self.events.append("up")
        value = sp._interpolate("host.docker.internal:${SLT_BIGTABLE_GRPC_HOST_PORT:-9470}", __import__("os").environ)
        self.redirect = {c: (value if sp._BIGTABLE_OVERLAY in files else None)
                         for c in stack.cluster_containers()}


def _run(monkeypatch, tmp_path, cluster, *, requested=None, port=None, ownership=None, booting=False):
    for key in OPT_INS:
        monkeypatch.delenv(key, raising=False)
    if requested:
        monkeypatch.setenv(requested, "1")
    if port:
        monkeypatch.setenv("SLT_BIGTABLE_GRPC_HOST_PORT", port)
    monkeypatch.delenv(plugin._tryout.TRYOUT_ENV, raising=False)
    monkeypatch.setattr(sp, "candidate_mode", lambda: False)
    real_reason = plugin._redeploy_reason

    def wire():
        monkeypatch.setattr(plugin, "_redeploy_reason", real_reason)    # the real reuse decision
        monkeypatch.setattr(plugin, "_STRIIM_DIR", sp.Path(sp.__file__).resolve().parents[1] / "services" / "striim")
        monkeypatch.setattr(sp, "_inspect", cluster.inspect)
        monkeypatch.setattr(sp, "image_inputs_match", lambda *a, **k: True)
        monkeypatch.setattr(sp, "tryout_stock_check", lambda: (None, []))
        monkeypatch.setattr(sp, "ensure_deps", lambda *a, **k: None)
        monkeypatch.setattr(sp, "ensure_image", lambda *a, **k: None)
        monkeypatch.setattr(sp, "cluster_up", cluster.up)
        monkeypatch.setattr(sp, "cluster_down", lambda *a, **k: cluster.events.append("down"))
        if ownership:
            monkeypatch.setattr(plugin._slt_infra, "of", lambda config: infra)
    infra = SimpleNamespace(ownership=ownership) if ownership else None
    probes = [False, False, False, True] if booting else None
    ctx, config, *_ = resolve(monkeypatch, tmp_path, [groups(True)], probes=probes, before=wire)
    return ctx, config


@pytest.mark.parametrize("booting", [False, True], ids=["warm", "booting"])
@pytest.mark.parametrize("initial, requested", [(REDIRECT_9470, None), (None, "SLT_BIGTABLE_SERVICE"),
                                                (None, "SLT_EMULATORS")],
                         ids=["opted-in-cluster-opted-out-run", "plain-cluster-opted-in-run",
                              "plain-cluster-emulators-run"])
def test_a_shared_cluster_with_the_other_redirect_state_is_refused(monkeypatch, tmp_path, booting,
                                                                    initial, requested):
    cluster = Cluster(initial)
    ctx, config = _run(monkeypatch, tmp_path, cluster, requested=requested, ownership="shared",
                       booting=booting)
    assert ctx is None and cluster.events == []                  # never recreated
    assert "shared mode never redeploys" in config._slt_striim_reason
    assert "Bigtable emulator redirect" in config._slt_striim_reason or "Bigtable clients" in config._slt_striim_reason
    assert ("SLT_BIGTABLE_SERVICE" in config._slt_striim_reason)


@pytest.mark.parametrize("booting", [False, True], ids=["warm", "booting"])
def test_a_shared_cluster_on_another_emulator_port_is_refused(monkeypatch, tmp_path, booting):
    cluster = Cluster(REDIRECT_9470)
    ctx, config = _run(monkeypatch, tmp_path, cluster, requested="SLT_BIGTABLE_SERVICE", port="9471",
                       ownership="shared", booting=booting)
    assert ctx is None and cluster.events == []
    assert "not host.docker.internal:9471" in config._slt_striim_reason


@pytest.mark.parametrize("booting", [False, True], ids=["warm", "booting"])
@pytest.mark.parametrize("initial, requested, port, after", [
    (REDIRECT_9470, None, None, None),
    (None, "SLT_BIGTABLE_SERVICE", None, REDIRECT_9470),
    (REDIRECT_9470, "SLT_BIGTABLE_SERVICE", "9471", "host.docker.internal:9471"),
], ids=["opted-in-cluster-opted-out-run", "plain-cluster-opted-in-run", "port-changed"])
def test_a_cluster_the_framework_may_recreate_is_redeployed_to_the_requested_redirect(
        monkeypatch, tmp_path, booting, initial, requested, port, after):
    # No ownership declared: the path that already redeploys a cluster on a version or tryout mismatch.
    cluster = Cluster(initial)
    ctx, config = _run(monkeypatch, tmp_path, cluster, requested=requested, port=port, booting=booting)
    assert cluster.events == ["down", "up"]
    assert ctx is not None and config._slt_striim_provisioned is True
    assert set(cluster.redirect.values()) == {after}


@pytest.mark.parametrize("booting", [False, True], ids=["warm", "booting"])
@pytest.mark.parametrize("initial, requested", [(None, None), (REDIRECT_9470, "SLT_BIGTABLE_SERVICE")],
                         ids=["both-off", "both-on"])
@pytest.mark.parametrize("ownership", [None, "shared"], ids=["undeclared", "shared"])
def test_a_cluster_with_the_requested_redirect_is_reused_unchanged(monkeypatch, tmp_path, booting,
                                                                  initial, requested, ownership):
    cluster = Cluster(initial)
    ctx, config = _run(monkeypatch, tmp_path, cluster, requested=requested, ownership=ownership,
                       booting=booting)
    assert ctx is not None and cluster.events == [] and config._slt_striim_provisioned is False


def test_one_member_out_of_step_is_enough(monkeypatch, tmp_path):
    cluster = Cluster(REDIRECT_9470)
    cluster.redirect[stack.cluster_containers()[2]] = None       # the agent was recreated without it
    ctx, config = _run(monkeypatch, tmp_path, cluster, requested="SLT_BIGTABLE_SERVICE", ownership="shared")
    assert ctx is None and stack.cluster_containers()[2] in config._slt_striim_reason


def test_a_member_docker_cannot_inspect_does_not_count_as_matching(tmp_path):
    def broken(argv):
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="no such container")
    why = sp.bigtable_redirect_reason(tmp_path, env={}, run=broken)
    assert "cannot be inspected" in why


@pytest.mark.parametrize("dotenv,on", [("SLT_BIGTABLE_SERVICE=1\n", True), ("SLT_BIGTABLE_SERVICE=0\n", False)])
def test_a_blank_process_opt_in_takes_the_dotenv_value_everywhere(monkeypatch, tmp_path, dotenv, on):
    # A blank process value counts as unset, so .env fills it in. The service gate reads the run's
    # settings that way; the cluster's redirect overlay and the reuse check must make the same choice.
    from livetest import preflight
    monkeypatch.setattr(paths, "dotenv_values", DOTENV_VALUES)
    (tmp_path / "clone.env").write_text(dotenv)
    for key in ("SLT_PROJECT_ROOT", "SLT_MACHINE_ENV", *OPT_INS):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("SLT_FRAMEWORK_DOTENV", str(tmp_path / "clone.env"))
    monkeypatch.setenv("SLT_BIGTABLE_SERVICE", " ")
    assert preflight._gated_out("bigtable", paths.effective_env()) is not on
    calls = []
    sp.cluster_up(tmp_path, run=lambda argv, cwd=None: calls.append(argv))
    assert (sp._BIGTABLE_OVERLAY in calls[0]) is on
    striim_dir = sp.Path(sp.__file__).resolve().parents[1] / "services" / "striim"
    redirected, plain = Cluster(REDIRECT_9470).inspect, Cluster(None).inspect
    assert sp.bigtable_redirect_reason(striim_dir, run=redirected if on else plain) is None
    assert "this run" in sp.bigtable_redirect_reason(striim_dir, run=plain if on else redirected)
