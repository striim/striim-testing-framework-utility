"""Parallel-stack prefix resolution (SLT_STACK_PREFIX — audit B4).

The prefix goes IN FRONT of the `slt` family token (`alt` -> `alt-slt-striim`), applied
uniformly to container names, compose project names (interpolated in the compose files
themselves) and the per-stack coordination filenames. Unset/empty must be a byte-identical
no-op everywhere — that is the whole backward-compatibility contract.
"""
import pytest

from livetest import stack
from livetest.registry import load_service
from livetest.services import _provision_state_paths, ensure_provisioned_once


def test_no_prefix_is_identity(monkeypatch):
    monkeypatch.delenv("SLT_STACK_PREFIX", raising=False)
    assert stack.prefix() == ""
    assert stack.prefixed("slt-striim") == "slt-striim"
    assert stack.state_name(".slt-provision.lock") == ".slt-provision.lock"
    assert stack.striim_container() == "slt-striim"
    assert stack.app_nodes() == ("slt-striim", "slt-node")
    assert stack.cluster_containers() == ("slt-striim", "slt-node", "slt-agent")


def test_empty_prefix_is_identity(monkeypatch):
    monkeypatch.setenv("SLT_STACK_PREFIX", "")
    assert stack.prefix() == ""
    assert stack.prefixed("slt-striim") == "slt-striim"


def test_prefix_goes_in_front_of_the_slt_token(monkeypatch):
    monkeypatch.setenv("SLT_STACK_PREFIX", "alt")
    assert stack.striim_container() == "alt-slt-striim"
    assert stack.app_nodes() == ("alt-slt-striim", "alt-slt-node")
    assert stack.cluster_containers() == (
        "alt-slt-striim", "alt-slt-node", "alt-slt-agent")


def test_state_name_keeps_the_dotfile_leading_dot(monkeypatch):
    monkeypatch.setenv("SLT_STACK_PREFIX", "alt")
    assert stack.state_name(".slt-provision-registry.json") == ".alt-slt-provision-registry.json"
    assert stack.state_name(".slt-op-registry.lock") == ".alt-slt-op-registry.lock"


def test_explicit_env_mapping_beats_os_environ(monkeypatch):
    # The console resolves with ITS configured prefix (console.env), which may differ from
    # the process environment — the env seam must win in both directions.
    monkeypatch.setenv("SLT_STACK_PREFIX", "alt")
    assert stack.prefixed("slt-x", env={}) == "slt-x"
    assert stack.prefixed("slt-x", env={"SLT_STACK_PREFIX": "b2"}) == "b2-slt-x"


@pytest.mark.parametrize("bad", ["Alt", "alt stack", "-alt", "alt_x", " alt", "alt/x"])
def test_invalid_prefix_fails_loud(monkeypatch, bad):
    # A bad prefix would otherwise surface as a cryptic docker-compose name error mid-run.
    monkeypatch.setenv("SLT_STACK_PREFIX", bad)
    with pytest.raises(stack.StackPrefixError):
        stack.prefix()


def test_registry_container_resolves_under_prefix():
    # registry.load_service is the ONE place every consumer reads the container name from
    # (docker exec targets, provision keys, teardown bookkeeping, console allowlists).
    assert load_service("postgres", env={"SLT_STACK_PREFIX": "alt"}).container == "alt-slt-postgres"
    assert load_service("postgres", env={}).container == "slt-postgres"


def test_provision_state_paths_are_prefix_scoped(tmp_path):
    reg, lock = _provision_state_paths(tmp_path, env={"SLT_STACK_PREFIX": "alt"})
    assert reg.name == ".alt-slt-provision-registry.json"
    assert lock.name == ".alt-slt-provision-registry.lock"
    reg0, lock0 = _provision_state_paths(tmp_path, env={})
    assert reg0.name == ".slt-provision-registry.json"
    assert lock0.name == ".slt-provision-registry.lock"


def test_two_stacks_do_not_share_provision_records(tmp_path, monkeypatch):
    # The same checkout hosts both stacks' registries: a record written under one prefix
    # must not make the OTHER stack skip its own bring-up.
    calls = []
    monkeypatch.setenv("SLT_STACK_PREFIX", "alt")
    assert ensure_provisioned_once("slt-postgres",
                                   lambda: calls.append("alt"), state_dir=tmp_path) is True
    monkeypatch.delenv("SLT_STACK_PREFIX")
    assert ensure_provisioned_once("slt-postgres",
                                   lambda: calls.append("default"), state_dir=tmp_path) is True
    assert calls == ["alt", "default"]
    assert (tmp_path / ".alt-slt-provision-registry.json").exists()
    assert (tmp_path / ".slt-provision-registry.json").exists()


def test_opregistry_files_are_prefix_scoped(tmp_path, monkeypatch):
    from livetest.opregistry import ensure_registered
    monkeypatch.setenv("SLT_STACK_PREFIX", "alt")
    assert ensure_registered(tmp_path, "A.jar", "fp", lambda: None) is True
    assert (tmp_path / ".alt-slt-op-registry.json").exists()
    assert not (tmp_path / ".slt-op-registry.json").exists()
    monkeypatch.delenv("SLT_STACK_PREFIX")
    # The default stack starts from ITS OWN empty registry — the alt record is invisible.
    calls = []
    assert ensure_registered(tmp_path, "A.jar", "fp", lambda: calls.append(1)) is True
    assert calls == [1]


def test_cluster_provision_lock_is_prefix_scoped(monkeypatch):
    from livetest.plugin import _cluster_provision_lock
    monkeypatch.delenv("SLT_STACK_PREFIX", raising=False)
    assert _cluster_provision_lock().name == ".slt-provision.lock"
    monkeypatch.setenv("SLT_STACK_PREFIX", "alt")
    assert _cluster_provision_lock().name == ".alt-slt-provision.lock"


def test_docker_targets_follow_the_prefix(monkeypatch, tmp_path):
    # striimfile/opartifacts/striim_provision exec/cp/restart against the PREFIXED nodes.
    monkeypatch.setenv("SLT_STACK_PREFIX", "alt")
    from livetest import stack as _stack, striim_provision as sp
    # restart_app_nodes clears the OP registry (a restart invalidates every registration), and
    # the default registry is MACHINE-WIDE -- so a hermetic test must redirect it or it deletes
    # the real one out from under a live run.
    monkeypatch.setattr(_stack, "_LOCK_DIR", tmp_path / "locks")
    seen = []
    sp.restart_app_nodes(None, run=lambda argv: seen.append(argv))
    assert seen == [["docker", "restart", "alt-slt-striim", "alt-slt-node"]]

    from livetest.striimfile import read_server_files

    class _Ctx:
        mode = "docker"

    execs = []

    def fake_run(argv):
        execs.append(argv)

        class R:
            stdout = ""
        return R()

    read_server_files(_Ctx(), "/tmp/out", run=fake_run)
    assert [a[2] for a in execs] == ["alt-slt-striim", "alt-slt-node"]


# --------------------------------------------------------------- compose: PRIMARY_HOSTNAME

def _compose_text():
    from pathlib import Path
    return (Path(__file__).resolve().parents[1]
            / "services" / "striim" / "compose.yaml").read_text()


def test_primary_hostname_carries_the_stack_prefix():
    """PRIMARY_HOSTNAME is what the node and agent are told to connect to -- the entrypoint
    writes it into striim.node.servernode.address and MetaDataRepositoryLocation.

    A bare `slt-striim` still worked, but only because compose adds the SERVICE name as a
    network alias, so the name resolved to whichever primary shared the caller's network.
    Two stacks up meant two containers answering to the same name, which is correct in
    practice and indistinguishable from a cross-stack leak when read in a config file.
    """
    text = _compose_text()
    assert "PRIMARY_HOSTNAME: slt-striim" not in text, \
        "unprefixed PRIMARY_HOSTNAME: it names another stack's primary as readily as this one"
    assert text.count("PRIMARY_HOSTNAME: ${SLT_STACK_PREFIX:+${SLT_STACK_PREFIX}-}slt-striim") == 3, \
        "every service that connects to the primary must name it the same prefixed way " \
        "(primary, node, agent)"


def test_primary_hostname_uses_the_same_prefix_form_as_container_name():
    """If the two ever diverge, PRIMARY_HOSTNAME names a container that does not exist."""
    text = _compose_text()
    form = "${SLT_STACK_PREFIX:+${SLT_STACK_PREFIX}-}"
    assert f"container_name: {form}slt-striim" in text
    assert f"PRIMARY_HOSTNAME: {form}slt-striim" in text


def test_primary_hostname_is_a_no_op_when_no_prefix_is_set():
    """The backward-compatibility contract: unset prefix must render the old bare name."""
    import os
    import subprocess
    form = "${SLT_STACK_PREFIX:+${SLT_STACK_PREFIX}-}slt-striim"
    for prefix, expected in (("", "slt-striim"), ("alt", "alt-slt-striim")):
        rendered = subprocess.run(
            ["bash", "-c", f'SLT_STACK_PREFIX="{prefix}"; echo "{form}"'],
            capture_output=True, text=True, env={**os.environ},
        ).stdout.strip()
        assert rendered == expected, f"prefix {prefix!r} rendered {rendered!r}"
