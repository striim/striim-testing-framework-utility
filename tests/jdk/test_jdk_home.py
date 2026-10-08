"""``resolve_build_java_home`` off macOS: the JDK comes only from ``SLT_JDK<release>_HOME``.

Both engines' ``opartifacts.py`` carry the same function; the root conftest puts both on the path.
Nothing here starts java: ``run`` is a fake.
"""
import types

import pytest

from inttest import opartifacts as integration
from livetest import opartifacts as live

ENGINES = pytest.mark.parametrize("engine", [live, integration], ids=["live", "integration"])


def _run(major="17", java_home_out="", java_home_rc=0, calls=None):
    def run(argv):
        if calls is not None:
            calls.append(list(argv))
        if argv[0] == "/usr/libexec/java_home":
            return types.SimpleNamespace(returncode=java_home_rc, stdout=java_home_out, stderr="")
        return types.SimpleNamespace(returncode=0, stdout="", stderr=f'openjdk version "{major}.0.9" 2023-10-17')
    return run


def _jdk(tmp_path, release="11"):
    home = tmp_path / f"jdk{release}"
    (home / "bin").mkdir(parents=True)
    (home / "bin" / "java").write_text("#!/bin/sh\n")
    return home


@pytest.fixture(autouse=True)
def _no_ambient_jdk(monkeypatch):
    for name in ("SLT_JDK8_HOME", "SLT_JDK11_HOME", "SLT_JDK17_HOME", "JAVA_HOME"):
        monkeypatch.delenv(name, raising=False)


@ENGINES
def test_linux_unset_variable_named_error(engine):
    calls = []
    with pytest.raises(engine.OpArtifactError, match="SLT_JDK11_HOME is unset") as ei:
        engine.resolve_build_java_home("11", run=_run("17", calls=calls), platform="Linux")
    assert "/usr/lib/jvm guess" in str(ei.value)
    assert not any(argv[0] == "/usr/libexec/java_home" for argv in calls)


@ENGINES
def test_linux_variable_without_bin_java_refused(engine, tmp_path, monkeypatch):
    monkeypatch.setenv("SLT_JDK11_HOME", str(tmp_path))
    with pytest.raises(engine.OpArtifactError, match=r"SLT_JDK11_HOME=.* has no bin/java"):
        engine.resolve_build_java_home("11", run=_run("17"), platform="Linux")


@ENGINES
def test_linux_variable_accepted(engine, tmp_path, monkeypatch):
    home = _jdk(tmp_path)
    monkeypatch.setenv("SLT_JDK11_HOME", str(home))
    calls = []
    assert engine.resolve_build_java_home("11", run=_run("17", calls=calls), platform="Linux") == str(home)
    assert not any(argv[0] == "/usr/libexec/java_home" for argv in calls)


@ENGINES
def test_darwin_branch_unchanged_with_fake_run(engine, tmp_path, monkeypatch):
    monkeypatch.setenv("SLT_JDK11_HOME", str(_jdk(tmp_path)))       # never read on macOS
    calls = []
    mac_home = "/Library/Java/JavaVirtualMachines/11.jdk/Contents/Home"
    assert engine.resolve_build_java_home("11", run=_run("17", mac_home + "\n", calls=calls), platform="Darwin") == mac_home
    assert ["/usr/libexec/java_home", "-v", "11"] in calls
    with pytest.raises(engine.OpArtifactError, match="'/usr/libexec/java_home -v 11'"):
        engine.resolve_build_java_home("11", run=_run("17", "", java_home_rc=1), platform="Darwin")


@ENGINES
def test_noop_when_running_major_matches(engine):
    for platform in ("Linux", "Darwin"):
        assert engine.resolve_build_java_home("17", run=_run("17"), platform=platform) is None


def test_both_engines_agree(tmp_path, monkeypatch):
    home = _jdk(tmp_path)

    def outcome(engine, release, platform, env):
        for name in ("SLT_JDK11_HOME", "SLT_JDK17_HOME"):
            monkeypatch.delenv(name, raising=False)
        for name, value in env.items():
            monkeypatch.setenv(name, value)
        try:
            return "ok", engine.resolve_build_java_home(release, run=_run("17", "/mac/jdk\n"), platform=platform)
        except engine.OpArtifactError as e:
            return "error", str(e)
    cases = [("11", "Linux", {}), ("11", "Linux", {"SLT_JDK11_HOME": str(tmp_path)}),
             ("11", "Linux", {"SLT_JDK11_HOME": str(home)}), ("11", "Darwin", {}), ("17", "Linux", {})]
    for release, platform, env in cases:
        assert outcome(live, release, platform, env) == outcome(integration, release, platform, env), (release, platform, env)


@ENGINES
def test_linux_relative_and_tilde_homes_are_made_absolute(engine, tmp_path, monkeypatch):
    # mvn runs in the module dir, so a relative JAVA_HOME would resolve elsewhere; a quoted ~
    # is never expanded by a shell.
    home = _jdk(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SLT_JDK11_HOME", home.name)
    assert engine.resolve_build_java_home("11", run=_run("17"), platform="Linux") == str(home.resolve())
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("SLT_JDK11_HOME", f"~/{home.name}")
    assert engine.resolve_build_java_home("11", run=_run("17"), platform="Linux") == str(home.resolve())
