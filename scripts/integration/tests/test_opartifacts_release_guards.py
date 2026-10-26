"""The integration tier refuses a STRIIM_HOME without STRIIM_VERSION and a jar stamped for another release."""
from __future__ import annotations

import os

import pytest

from inttest import opartifacts
from tests.test_opartifacts import MODULE, RELEASE_54, _fp, _hermetic_build_env, _jar, _mk


def test_verify_striim_home_refuses_an_install_without_the_version(tmp_path):
    home = tmp_path / "Striim_5.4.0.6"
    _mk(home / "lib" / "Platform-5.4.0.6.jar", 1000)
    with pytest.raises(opartifacts.OpArtifactError, match="does not contain STRIIM_VERSION=5.4.0.6C"):
        opartifacts._verify_striim_home({"STRIIM_HOME": str(home)}, {"STRIIM_VERSION": "5.4.0.6C"})


def test_verify_striim_home_accepts_the_matching_install_and_skips_when_unset(tmp_path):
    home = tmp_path / "Striim_5.4.0.6C"
    _mk(home / "lib" / "Platform-5.4.0.6C.jar", 1000)
    opartifacts._verify_striim_home({"STRIIM_HOME": str(home)}, {"STRIIM_VERSION": "5.4.0.6C"})
    opartifacts._verify_striim_home({}, {"STRIIM_VERSION": "5.4.0.6C"})


def test_build_jar_runs_the_striim_home_guard_before_mvn(tmp_path, monkeypatch):
    # _hermetic_build_env minus its stub of the guard under test.
    monkeypatch.setattr(opartifacts, "_root", lambda: tmp_path)
    monkeypatch.setattr(opartifacts, "_common_dir", lambda: tmp_path / "Common")
    monkeypatch.setattr(opartifacts, "resolve_build_java_home", lambda *a, **k: None)
    monkeypatch.setenv("STRIIM_HOME", str(tmp_path / "Striim_other"))
    _mk(tmp_path / MODULE / "src" / "main" / "java" / "X.java", 1000)
    with pytest.raises(opartifacts.OpArtifactError, match="does not contain STRIIM_VERSION"):
        opartifacts.build_jar(MODULE, RELEASE_54, run=lambda *a: pytest.fail("must not build"))


def test_build_jar_refuses_a_jar_stamped_for_another_release(tmp_path, monkeypatch):
    _hermetic_build_env(tmp_path, monkeypatch)
    mod = tmp_path / MODULE
    _mk(mod / "src" / "main" / "java" / "X.java", 1000)
    jar = mod / "target" / "ReferenceOpV1-5.4.jar"
    _jar(jar, fp={**_fp(RELEASE_54), "STRIIM_VERSION": "5.4.0.5"}, major=61)
    os.utime(jar, (2000, 2000))
    with pytest.raises(opartifacts.OpArtifactError, match="Refusing to reuse a jar from a different"):
        opartifacts.build_jar(MODULE, RELEASE_54, run=lambda *a: pytest.fail("must not build"))
