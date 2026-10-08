"""The running Striim's version is observed from the runtime by one bounded ``docker exec``
of a fixed script (the ``/opt/striim/lib`` listing, then dpkg's ``striim-node`` version). Exactly one
``Platform-<v>.jar`` decides, dpkg corroborates when present; anything else is unreadable or unobserved, and an
unobserved or unreadable version never qualifies."""
from __future__ import annotations

import subprocess
from types import SimpleNamespace

import pytest

from livetest import canon, evidence, infra, releases

C = "slt-striim-w2b1"
EXPECTED = {"STRIIM_VERSION": "5.4.2", "STRIIM_SERIES": "5.4", "JAVA_RELEASE": "17"}
LIB = ["StriimParser-5.4.2.jar", "avro-striim11-1.12.0.jar", "jna-platform-5.12.1.jar"]


def _listing(jars, dpkg=None):
    return "".join(f"{n}\n" for n in sorted(LIB + jars)) + f"{infra.RUNTIME_PROBE_MARK}\n" + (f"{dpkg}\n" if dpkg else "")


@pytest.fixture
def probe(monkeypatch):
    """The bounded runner answers the probe with ``answer`` and records each argv."""
    calls, answer = [], {}

    def runner(argv):
        calls.append(argv)
        return SimpleNamespace(returncode=answer.get("rc", 0), stdout=answer.get("stdout", ""), stderr=answer.get("stderr", ""))
    monkeypatch.setattr(infra, "_docker_inspect", runner)

    def run(stdout="", rc=0, stderr="", container=C):
        answer.update(stdout=stdout, rc=rc, stderr=stderr)
        return infra.runtime_version(None, SimpleNamespace(mode="docker"), container)
    run.calls = calls
    return run


def _qualify_reason(version):
    observed = {"version": version}
    view = {"integrity": {}, "assertions": [], "blocked": evidence.forms({"runtime": {"striim": {"observed": observed}}}),
            "striim": {"expected": EXPECTED, "observed": observed},
            "data": {"comparisons": [{"profile": canon.PROFILE, "equal": True, "actual": {"owned": True}}]}}
    return evidence._exact_reason(view)


def test_jar_and_dpkg_agree_version_qualifies(probe):
    version = probe(_listing(["Platform-5.4.2.jar"], "5.4.2"))
    assert version == "5.4.2"
    assert _qualify_reason(version) is None


def test_jar_alone_decides_without_dpkg(probe):
    assert probe(_listing(["Platform-5.4.2.jar", "Platform-5.4.2-sources.jar", "Platform-5.4.2-javadoc.jar"])) == "5.4.2"


def test_jar_and_dpkg_disagree_is_unreadable(probe):
    version = probe(_listing(["Platform-5.4.2.jar"], "5.4.3"))
    assert set(version) == {"unreadable"} and "5.4.3" in version["unreadable"]
    assert _qualify_reason(version) == "not observed or unreadable: runtime.striim.observed.version"


@pytest.mark.parametrize("jars", [[], ["Platform-5.4.2.jar", "Platform-5.4.3.jar"]], ids=["none", "two"])
def test_zero_or_two_platform_jars_is_unreadable(probe, jars):
    version = probe(_listing(jars, "5.4.2"))
    assert set(version) == {"unreadable"} and f"{len(jars)} Platform-*.jar" in version["unreadable"]


@pytest.mark.parametrize("rc", [1, 124, 127])
def test_exec_failure_timeout_or_no_start_is_unobserved(monkeypatch, rc):
    """Through the real bounded runner: rc 1, a timeout (124) and an exec that cannot start (127)."""
    calls = []

    def run(argv, **kw):
        calls.append((argv, kw.get("timeout")))
        if rc == 124:
            raise subprocess.TimeoutExpired(argv, kw["timeout"])
        if rc == 127:
            raise FileNotFoundError(2, "No such file or directory", "docker")
        return SimpleNamespace(returncode=1, stdout="", stderr="Error: No such container")
    monkeypatch.setattr(subprocess, "run", run)
    version = infra.runtime_version(None, SimpleNamespace(mode="docker"), C)
    assert set(version) == {"unobserved"} and f"rc {rc}" in version["unobserved"]
    [(argv, timeout)] = calls
    assert argv[:3] == ["docker", "exec", C] and timeout == 10


def test_native_endpoint_is_unobserved(probe):
    assert probe(container=None) == {"unobserved": "native endpoint: no runtime installation is observable from the harness"}
    assert probe.calls == []


def test_malformed_version_is_unreadable(probe):
    version = probe(_listing(["Platform-5.4.2;x.jar"]))
    assert set(version) == {"unreadable"} and "5.4.2;x" in version["unreadable"]


def test_exactly_one_exec_with_a_fixed_script_and_the_container_as_argv(probe):
    probe(_listing(["Platform-5.4.2.jar"], "5.4.2"), container="c; rm -rf /")
    [argv] = probe.calls
    assert argv == ["docker", "exec", "c; rm -rf /", "sh", "-c", infra.RUNTIME_PROBE_SCRIPT]
    assert "ls -1 /opt/striim/lib" in argv[-1] and "dpkg-query -W -f='${Version}\\n' striim-node" in argv[-1]
    assert "c; rm" not in argv[-1]


@pytest.mark.parametrize("names", [["Platform-5.4.2.jar"], ["Platform-5.4.2.jar", "Platform-5.4.2-sources.jar",
                                   "Platform-5.4.2-javadoc.jar", "PlatformX-1.jar"], [], ["Platform-5.4.2.jar", "Platform-5.4.3.jar"]],
                         ids=["one", "skips", "none", "two"])
def test_platform_jar_rule_is_detect_releases_rule(tmp_path, names):
    """``releases.detect_release`` is a synced file, so ``infra.platform_jars`` carries its rule; the two agree."""
    (tmp_path / "lib").mkdir()
    for n in names + ["StriimParser-5.4.2.jar"]:
        (tmp_path / "lib" / n).write_bytes(b"")
    jars = infra.platform_jars(p.name for p in (tmp_path / "lib").iterdir())
    if len(jars) == 1:
        assert releases.detect_release(tmp_path)["STRIIM_VERSION"] == jars[0][len("Platform-"):-len(".jar")]
    else:
        with pytest.raises(releases.ReleaseError):
            releases.detect_release(tmp_path)
