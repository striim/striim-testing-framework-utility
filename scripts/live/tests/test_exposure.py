"""What the stack exposes on the host: the primary's JDWP debugger and the service ports' bind address.

JDWP is opt-in (SLT_STRIIM_DEBUG=1) and published on 127.0.0.1 only. Every other published port is
bound to ${SLT_BIND_ADDRESS} (all interfaces when compose runs with it unset, as before), with a
second entry for the address containers reach the host on (${SLT_BIND_GATEWAY}); the harness sets
both for its own bring-ups so a run is reachable from this machine and its containers only.
"""
import os
import re
import types
from pathlib import Path

import pytest
import yaml

import livetest
from livetest import bindaddr
from livetest import paths
from livetest import striim_provision as sp

_DETECT = bindaddr._cached_gateway       # the real one; the suite's conftest stubs it per test

REPO = Path(livetest.__file__).resolve().parents[3]
LIVE = REPO / "scripts" / "live"
INTEG = REPO / "scripts" / "integration"
STRIIM = LIVE / "services" / "striim"
ENTRYPOINT = STRIIM / "images" / "striim" / "files" / "entrypoint.sh"
DEBUG_OVERLAY = "compose.debug.yaml"
JDWP = "127.0.0.1:${SLT_STRIIM_DEBUG_HOST_PORT:-8787}:8787"

BIND = "${SLT_BIND_ADDRESS:-0.0.0.0}"
GATEWAY = "${SLT_BIND_GATEWAY:-${SLT_BIND_ADDRESS:-0.0.0.0}}"


# ------------------------------------------------------------------ JDWP (opt-in, loopback only)
def test_jdwp_is_written_only_inside_the_debug_branch():
    lines = ENTRYPOINT.read_text().splitlines()
    jdwp = [i for i, line in enumerate(lines) if "Xrunjdwp" in line and not line.lstrip().startswith("#")]
    assert len(jdwp) == 1, jdwp
    opens = [i for i, line in enumerate(lines[:jdwp[0]])
             if re.match(r'\s*if \[ "\$\{STRIIM_DEBUG:-0\}" = "1" \]; then\s*$', line)]
    assert opens, "the JDWP line must sit inside `if [ \"${STRIIM_DEBUG:-0}\" = \"1\" ]; then`"
    between = lines[opens[-1] + 1:jdwp[0]]
    assert not [line for line in between if re.match(r"\s*(fi|else|elif)\b", line)], between
    close = next(i for i in range(jdwp[0] + 1, len(lines)) if re.match(r"\s*fi\s*$", lines[i]))
    # The anchor rewrite that appends the JMX agent stays unconditional: it runs with debug off too.
    anchor = next(i for i, line in enumerate(lines)
                  if "$JVM_DEBUG_OPTS $JAVA_AGENT \\\\|g'" in line and "$JAXP_OPTS" not in line)
    assert anchor > close


def test_the_base_compose_publishes_no_debugger_port():
    compose = yaml.safe_load((STRIIM / "compose.yaml").read_text())
    for name, svc in compose["services"].items():
        assert not [p for p in svc.get("ports") or [] if str(p).endswith(":8787")], name
        assert "STRIIM_DEBUG" not in (svc.get("environment") or {}), name


@pytest.mark.parametrize("value", [None, "", "0"])
def test_the_debug_overlay_is_left_out_unless_the_switch_is_1(monkeypatch, value):
    if value is None:
        monkeypatch.delenv("SLT_STRIIM_DEBUG", raising=False)
    else:
        monkeypatch.setenv("SLT_STRIIM_DEBUG", value)
    assert DEBUG_OVERLAY not in sp._compose_files()


@pytest.mark.parametrize("value", ["true", "yes", " 1"])
def test_a_debug_switch_that_is_not_1_or_0_stops_the_run(monkeypatch, value):
    # SLT_STRIIM_DEBUG is a switch: anything but 1 or 0 is refused, not read as off.
    monkeypatch.setenv("SLT_STRIIM_DEBUG", value)
    assert "SLT_STRIIM_DEBUG" in paths.SWITCHES
    with pytest.raises(paths.FlagError, match="SLT_STRIIM_DEBUG"):
        sp._compose_files()


def test_slt_striim_debug_1_adds_the_overlay_publishing_jdwp_on_loopback(monkeypatch):
    monkeypatch.setenv("SLT_STRIIM_DEBUG", "1")
    files = sp._compose_files()
    assert files[files.index(DEBUG_OVERLAY) - 1] == "-f"
    overlay = yaml.safe_load((STRIIM / DEBUG_OVERLAY).read_text())
    assert set(overlay["services"]) == {"slt-striim"}
    primary = overlay["services"]["slt-striim"]
    assert primary["ports"] == [JDWP]
    assert primary["environment"] == {"STRIIM_DEBUG": "1"}


def test_slt_striim_debug_from_dotenv_is_honoured(monkeypatch):
    monkeypatch.delenv("SLT_STRIIM_DEBUG", raising=False)
    from livetest import paths
    monkeypatch.setattr(paths, "effective_env",
                        lambda env=None, *a, **k: {**(env or {}), "SLT_STRIIM_DEBUG": "1"})
    assert DEBUG_OVERLAY in sp._compose_files()


def test_debug_switch_is_a_known_setting():
    from livetest import paths
    assert "SLT_STRIIM_DEBUG" in paths.SERVICE_KEYS


# ------------------------------------------------------------------ bind address: compose shape
def _port_entries():
    out = []
    for root in (LIVE, INTEG):
        for path in sorted(root.glob("services/*/compose*.yaml")):
            if path.name == DEBUG_OVERLAY:
                continue
            doc = yaml.safe_load(path.read_text()) or {}
            for name, svc in (doc.get("services") or {}).items():
                ports = [str(p) for p in (svc or {}).get("ports") or []]
                if ports:
                    out.append((f"{path.relative_to(REPO)}:{name}", ports))
    return out


def test_every_published_port_is_bound_to_the_bind_address_and_the_gateway():
    mappings = 0
    for where, ports in _port_entries():
        assert len(ports) % 2 == 0, (where, ports)
        for first, second in zip(ports[0::2], ports[1::2]):
            assert first.startswith(BIND + ":"), (where, first)
            assert second.startswith(GATEWAY + ":"), (where, second)
            assert first[len(BIND):] == second[len(GATEWAY):], (where, first, second)
            mappings += 1
    assert mappings == 32, mappings          # every service port of both tiers, JDWP excluded


def test_no_port_entry_carries_a_second_address():
    for where, ports in _port_entries():
        for p in ports:
            rest = p[len(BIND):] if p.startswith(BIND) else p[len(GATEWAY):]
            bare = re.sub(r"\$\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}", "X", rest)
            assert bare.count(":") == 2, (where, p)          # ":<host port>:<container port>"


def test_bind_address_is_a_known_setting():
    from livetest import paths
    assert "SLT_BIND_ADDRESS" in paths.SERVICE_KEYS


# ------------------------------------------------------------------ bind address: the harness value
def _run(outputs):
    calls = []

    def run(argv, **kw):
        calls.append((argv, kw))
        code, out, err = outputs[argv[1]]
        return types.SimpleNamespace(returncode=code, stdout=out, stderr=err)
    return run, calls


def test_an_explicit_bind_address_is_the_only_address(monkeypatch):
    monkeypatch.setattr(bindaddr, "_cached_gateway", lambda run=None: pytest.fail("detected"))
    assert bindaddr.compose_env({"SLT_BIND_ADDRESS": " 10.1.2.3 "}) == {
        "SLT_BIND_ADDRESS": "10.1.2.3", "SLT_BIND_GATEWAY": "10.1.2.3"}
    assert bindaddr.compose_env({"SLT_BIND_ADDRESS": "0.0.0.0", "SLT_BIND_GATEWAY": "172.17.0.1"}) == {
        "SLT_BIND_ADDRESS": "0.0.0.0", "SLT_BIND_GATEWAY": "0.0.0.0"}


@pytest.mark.parametrize("bad", ["localhost", "::1", "10.0.0", "1.2.3.4:80", "0.0.0.0 1.2.3.4"])
def test_a_bind_address_that_is_not_an_ipv4_address_is_refused(bad):
    with pytest.raises(bindaddr.BindAddressError, match="SLT_BIND_ADDRESS"):
        bindaddr.compose_env({"SLT_BIND_ADDRESS": bad})


def test_unset_bind_address_publishes_on_loopback_and_the_detected_gateway(monkeypatch):
    monkeypatch.setattr(bindaddr, "_cached_gateway", lambda run=None: "172.17.0.1")
    assert bindaddr.compose_env({}) == {"SLT_BIND_ADDRESS": "127.0.0.1", "SLT_BIND_GATEWAY": "172.17.0.1"}
    monkeypatch.setattr(bindaddr, "_cached_gateway", lambda run=None: None)
    assert bindaddr.compose_env({"SLT_BIND_ADDRESS": "", "SLT_BIND_GATEWAY": "0.0.0.0"}) == {
        "SLT_BIND_ADDRESS": "127.0.0.1", "SLT_BIND_GATEWAY": "127.0.0.1"}


def test_docker_desktop_needs_no_gateway():
    run, calls = _run({"info": (0, "Docker Desktop\n", "")})
    assert bindaddr.detect_gateway(run=run) is None
    assert [c[0][:2] for c in calls] == [["docker", "info"]]


def test_a_linux_engine_publishes_on_the_default_bridge_gateway():
    run, calls = _run({"info": (0, "Ubuntu 24.04.1 LTS\n", ""),
                       "network": (0, "fd00::1 172.17.0.1 \n", "")})
    assert bindaddr.detect_gateway(run=run) == "172.17.0.1"
    assert calls[1][0][:4] == ["docker", "network", "inspect", "bridge"]
    for _argv, kw in calls:
        assert kw.get("timeout"), "every docker query is bounded"


@pytest.mark.parametrize("outputs, needle", [
    ({"info": (0, "Ubuntu\n", ""), "network": (1, "", "network bridge not found")}, "network bridge not found"),
    ({"info": (0, "Ubuntu\n", ""), "network": (124, "", "")}, "timed out"),
    ({"info": (0, "Ubuntu\n", ""), "network": (0, "fd00::1\n", "")}, "no IPv4 gateway"),
])
def test_an_undeterminable_gateway_stops_the_bring_up_and_names_the_setting(outputs, needle):
    run, _ = _run(outputs)
    with pytest.raises(bindaddr.BindAddressError) as e:
        bindaddr.detect_gateway(run=run)
    assert needle in str(e.value) and "SLT_BIND_ADDRESS" in str(e.value)


@pytest.mark.parametrize("info", [
    (1, "", "Cannot connect to the Docker daemon"),         # no daemon
    (124, "", "timed out"),                                 # dockercmd's bound expired
    (0, "", ""),                                            # answered nothing
    FileNotFoundError("docker"),                            # no docker binary
])
def test_docker_unreachable_falls_back_to_loopback_without_raising(monkeypatch, capsys, info):
    monkeypatch.setattr(bindaddr, "_cached_gateway", _DETECT)
    monkeypatch.setattr(bindaddr, "_CACHE", {})
    calls = []

    def run(argv, **kw):
        calls.append(argv)
        if isinstance(info, Exception):
            raise info
        return types.SimpleNamespace(returncode=info[0], stdout=info[1], stderr=info[2])
    for _ in range(2):
        assert bindaddr.compose_env({}, run=run) == {
            "SLT_BIND_ADDRESS": "127.0.0.1", "SLT_BIND_GATEWAY": "127.0.0.1"}
    # never asked beyond `docker info`, and not kept: the next bring-up asks again
    assert calls == [["docker", "info", "--format", "{{.OperatingSystem}}"]] * 2
    assert "SLT_BIND_ADDRESS" in capsys.readouterr().err


def test_a_determined_gateway_is_asked_once_per_process(monkeypatch):
    monkeypatch.setattr(bindaddr, "_cached_gateway", _DETECT)
    monkeypatch.setattr(bindaddr, "_CACHE", {})
    run, calls = _run({"info": (0, "Ubuntu\n", ""), "network": (0, "172.17.0.1\n", "")})
    for _ in range(3):
        assert bindaddr.compose_env({}, run=run)["SLT_BIND_GATEWAY"] == "172.17.0.1"
    assert len(calls) == 2


def test_a_missing_docker_binary_still_surfaces_as_docker_unavailable(monkeypatch):
    """The fallback does not hide a missing binary: the bring-up's own `compose up` reports it."""
    monkeypatch.setattr(bindaddr, "_cached_gateway", _DETECT)
    monkeypatch.setattr(bindaddr, "_CACHE", {})
    monkeypatch.setenv("SLT_KEEP_SERVICES", "1")
    monkeypatch.delenv("SLT_BIND_ADDRESS", raising=False)
    import livetest.services as services_mod
    from livetest.registry import load_service

    def run(argv, **kw):
        raise FileNotFoundError("docker")
    monkeypatch.setattr(services_mod.dockercmd, "run", run)
    with pytest.raises(services_mod.DockerUnavailable):
        services_mod._default_compose_up(load_service("mysql"))


def test_detection_never_runs_when_the_bind_address_is_set(monkeypatch, tmp_path):
    """SLT_BIND_ADDRESS set: no bring-up asks Docker where to bind (service, then cluster)."""
    monkeypatch.setattr(bindaddr, "_cached_gateway", _DETECT)
    monkeypatch.setattr(bindaddr, "_CACHE", {})
    monkeypatch.setenv("SLT_BIND_ADDRESS", "0.0.0.0")
    monkeypatch.setenv("SLT_KEEP_SERVICES", "1")
    monkeypatch.delenv("SLT_STRIIM_TRYOUT_JARS", raising=False)
    import livetest.services as services_mod
    from livetest.registry import load_service
    seen = []

    def fake_run(argv, **kw):
        seen.append(list(argv))
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")
    monkeypatch.setattr(services_mod.dockercmd, "run", fake_run)
    services_mod._default_compose_up(load_service("mysql"))
    sp.cluster_up(tmp_path, {"STRIIM_VERSION": "5.4.2"}, run=lambda argv, cwd=None: seen.append(list(argv)),
                  query_run=lambda argv, **kw: pytest.fail(f"queried Docker: {argv}"))
    assert [a[:3] for a in seen if "up" in a] == [["docker", "compose", "-f"]] * 2
    assert not [a for a in seen if a[1:2] in (["info"], ["network"])]


# ------------------------------------------------------------------ the harness passes it to compose up
def test_a_service_bring_up_runs_compose_with_the_bind_env(monkeypatch):
    monkeypatch.setenv("SLT_KEEP_SERVICES", "1")          # a single `up` call, no reset
    monkeypatch.delenv("SLT_BIND_ADDRESS", raising=False)
    monkeypatch.setattr(bindaddr, "_cached_gateway", lambda run=None: "172.17.0.1")
    import livetest.services as services_mod
    from livetest.registry import load_service
    seen = []

    def fake_run(argv, **kw):
        seen.append((argv, kw.get("env")))
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")
    monkeypatch.setattr(services_mod.dockercmd, "run", fake_run)
    services_mod._default_compose_up(load_service("mysql"))
    ups = [env for argv, env in seen if "up" in argv]
    assert ups and ups[0]["SLT_BIND_ADDRESS"] == "127.0.0.1"
    assert ups[0]["SLT_BIND_GATEWAY"] == "172.17.0.1"


def test_the_cluster_bring_up_runs_compose_with_the_bind_env(monkeypatch, tmp_path):
    monkeypatch.setenv("SLT_BIND_ADDRESS", "10.9.8.7")
    monkeypatch.delenv("SLT_BIND_GATEWAY", raising=False)
    monkeypatch.delenv("SLT_STRIIM_TRYOUT_JARS", raising=False)
    seen = {}

    def run(argv, cwd=None):
        seen["argv"] = argv
        seen["env"] = {k: os.environ.get(k) for k in ("SLT_BIND_ADDRESS", "SLT_BIND_GATEWAY")}
    sp.cluster_up(tmp_path, {"STRIIM_VERSION": "5.4.2"}, run=run)
    assert "up" in seen["argv"]
    assert seen["env"] == {"SLT_BIND_ADDRESS": "10.9.8.7", "SLT_BIND_GATEWAY": "10.9.8.7"}
    assert os.environ.get("SLT_BIND_GATEWAY") is None          # restored after the call


def test_the_cluster_asks_docker_through_its_query_runner(monkeypatch, tmp_path):
    monkeypatch.setattr(bindaddr, "_cached_gateway", _DETECT)
    monkeypatch.setattr(bindaddr, "_CACHE", {})
    monkeypatch.delenv("SLT_BIND_ADDRESS", raising=False)
    monkeypatch.delenv("SLT_STRIIM_TRYOUT_JARS", raising=False)
    query, calls = _run({"info": (0, "Ubuntu\n", ""), "network": (0, "172.17.0.1\n", "")})
    seen = {}

    def run(argv, cwd=None):
        seen.update({k: os.environ.get(k) for k in ("SLT_BIND_ADDRESS", "SLT_BIND_GATEWAY")})
    sp.cluster_up(tmp_path, {"STRIIM_VERSION": "5.4.2"}, run=run, query_run=query)
    assert seen == {"SLT_BIND_ADDRESS": "127.0.0.1", "SLT_BIND_GATEWAY": "172.17.0.1"}
    assert [c[0][1] for c in calls] == ["info", "network"]


def test_a_prefixed_stack_reports_each_unset_port_variable_once():
    from livetest import preflight
    missing = preflight.missing_port_overrides(["kafka"], env={"SLT_STACK_PREFIX": "alt"})
    names = [var for _svc, var, _default in missing]
    assert names == sorted(set(names), key=names.index)               # no duplicates
    assert set(names) == {"SLT_ZOOKEEPER_CLIENT_PORT", "SLT_KAFKA_HOST_PORT",
                          "SLT_KAFKA_DOCKER_HOST_PORT", "SLT_SCHEMA_REGISTRY_HOST_PORT"}
