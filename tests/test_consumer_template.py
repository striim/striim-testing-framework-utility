"""The starter template (templates/consumer-repo) works as docs/SET-UP-YOUR-OWN-REPO.md says.

The sync script runs against a local bare repository standing in for the framework (two tags, no
network): it clones, is idempotent, checks, refuses a dirty checkout, a branch pin and a foreign
folder, and moves to a new pin. The template's manifest and case load, and `striim-test list`
selects the case.
"""
from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "cli"))
from _clikit import framework_env, run_cli  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
TEMPLATE = REPO / "templates" / "consumer-repo"
GIT_ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
           "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}


def _git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True,
                          text=True, env=GIT_ENV).stdout.strip()


@pytest.fixture
def upstream(tmp_path):
    """A bare 'framework' repository with tags v1 and v2 and a branch named `feature`."""
    work = tmp_path / "upstream-work"
    work.mkdir()
    _git(work, "init", "-q", "-b", "main")
    (work / "VERSION").write_text("1\n")
    _git(work, "add", ".")
    _git(work, "commit", "-q", "-m", "one")
    _git(work, "tag", "v1")
    (work / "VERSION").write_text("2\n")
    _git(work, "commit", "-q", "-am", "two")
    _git(work, "tag", "v2")
    _git(work, "branch", "feature")
    bare = tmp_path / "upstream.git"
    _git(tmp_path, "clone", "-q", "--bare", str(work), str(bare))
    return SimpleNamespace(url=str(bare), v1=_git(work, "rev-parse", "v1"), v2=_git(work, "rev-parse", "v2"))


@pytest.fixture
def consumer(tmp_path):
    repo = tmp_path / "my-tests"
    shutil.copytree(TEMPLATE, repo)
    return repo


def _sync(consumer, upstream, *args):
    home = consumer.parent / "framework"
    r = subprocess.run([sys.executable, str(consumer / "scripts" / "sync-framework.py"), "--home", str(home),
                        "--remote", upstream.url, "--no-install", *args],
                       capture_output=True, text=True, env=GIT_ENV)
    return r, home


def _pin(consumer, value):
    (consumer / "framework.pin").write_text(value + "\n")


def test_template_ships_its_files():
    names = {p.relative_to(TEMPLATE).as_posix() for p in TEMPLATE.rglob("*") if p.is_file()}
    assert {"README.md", "framework.pin", "scripts/sync-framework.py", "gold-targets.yaml",
            ".env.example", ".gitignore", "tests/live/orders-cdc/test.yaml"} <= names
    pin = (TEMPLATE / "framework.pin").read_text().split()[0]
    assert len(pin) == 40 or pin.startswith("v"), pin


def test_template_pin_names_a_commit_of_this_repository():
    # A tag is cut by tools/release.py; a commit must be in this history. Either way a new
    # consumer's first sync can find it.
    pin = (TEMPLATE / "framework.pin").read_text().split()[0]
    ref = pin if len(pin) == 40 else f"refs/tags/{pin}"
    r = subprocess.run(["git", "-C", str(TEMPLATE), "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"],
                       capture_output=True, text=True)
    assert r.returncode == 0, f"framework.pin {pin!r} does not resolve in this repository"


def test_sync_clones_at_a_tag_detached_and_is_idempotent(consumer, upstream):
    _pin(consumer, "v1")
    r, home = _sync(consumer, upstream)
    assert r.returncode == 0, r.stderr
    assert f"at v1 ({upstream.v1[:12]})" in r.stdout
    assert _git(home, "rev-parse", "HEAD") == upstream.v1
    assert subprocess.run(["git", "-C", str(home), "symbolic-ref", "-q", "HEAD"]).returncode != 0  # detached
    assert (home / "VERSION").read_text() == "1\n"
    r, _ = _sync(consumer, upstream)
    assert r.returncode == 0, r.stderr
    # no branch of its own: only the default branch `git clone` makes, never checked out
    assert [b for b in _git(home, "branch", "--format=%(refname:short)").splitlines() if "detached" not in b] == ["main"]
    assert _git(home, "rev-parse", "HEAD") == upstream.v1


def test_check_passes_at_the_pin_and_fails_after_a_bump(consumer, upstream):
    _pin(consumer, "v1")
    _sync(consumer, upstream)
    r, _ = _sync(consumer, upstream, "--check")
    assert r.returncode == 0, r.stderr
    _pin(consumer, "v2")
    r, _ = _sync(consumer, upstream, "--check")
    assert r.returncode == 1 and "is not at v2" in r.stderr


def test_a_bump_moves_the_checkout(consumer, upstream):
    _pin(consumer, "v1")
    _sync(consumer, upstream)
    _pin(consumer, "v2")
    r, home = _sync(consumer, upstream)
    assert r.returncode == 0, r.stderr
    assert _git(home, "rev-parse", "HEAD") == upstream.v2


def test_a_full_commit_pin_works(consumer, upstream):
    _pin(consumer, upstream.v1)
    r, home = _sync(consumer, upstream)
    assert r.returncode == 0, r.stderr
    assert _git(home, "rev-parse", "HEAD") == upstream.v1


def test_local_changes_are_refused_not_overwritten(consumer, upstream):
    _pin(consumer, "v1")
    _, home = _sync(consumer, upstream)
    (home / "VERSION").write_text("patched\n")
    _pin(consumer, "v2")
    r, _ = _sync(consumer, upstream)
    assert r.returncode == 3 and "local changes" in r.stderr and "issue" in r.stderr
    assert (home / "VERSION").read_text() == "patched\n"


@pytest.mark.parametrize("pin,code,words", [("feature", 2, "is a branch"), ("v9", 2, "no tag"),
                                            ("abc1234", 2, "short commit")])
def test_bad_pins_are_refused(consumer, upstream, pin, code, words):
    _pin(consumer, pin)
    r, _ = _sync(consumer, upstream)
    assert r.returncode == code and words in r.stderr, r.stderr


def test_a_folder_that_is_not_a_checkout_is_refused(consumer, upstream):
    _pin(consumer, "v1")
    home = consumer.parent / "framework"
    home.mkdir()
    (home / "mine.txt").write_text("x")
    r, _ = _sync(consumer, upstream)
    assert r.returncode == 3 and "not a git checkout" in r.stderr


def test_dry_run_changes_nothing(consumer, upstream):
    _pin(consumer, "v1")
    r, home = _sync(consumer, upstream, "--dry-run")
    assert r.returncode == 0 and "would clone" in r.stdout
    assert not home.exists()


def _load_script():
    spec = importlib.util.spec_from_file_location("sync_framework", TEMPLATE / "scripts" / "sync-framework.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _venv(tmp_path, monkeypatch, mod, name="venv", cli=True):
    """Point the script at a fake venv: sys.prefix in tmp_path, optionally with its striim-test."""
    prefix = tmp_path / name
    (prefix / "bin").mkdir(parents=True, exist_ok=True)
    if cli:
        (prefix / "bin" / "striim-test").write_text("#!/bin/sh\n")
    monkeypatch.setattr(mod.sys, "prefix", str(prefix))
    monkeypatch.setattr(mod.sys, "base_prefix", "/usr")
    return prefix


def _pip(calls):
    return lambda argv: calls.append(argv) or SimpleNamespace(returncode=0)


def test_install_runs_pip_once_per_commit(consumer, upstream, monkeypatch, tmp_path):
    _pin(consumer, "v1")
    _, home = _sync(consumer, upstream)
    mod = _load_script()
    _venv(tmp_path, monkeypatch, mod)
    calls = []
    assert mod.install(home, upstream.v1, run=_pip(calls)) is True
    assert calls[0][-2:] == ["-e", str(home)] and calls[0][1:4] == ["-m", "pip", "install"]
    assert "--no-index" not in calls[0]
    assert mod.install(home, upstream.v1, run=_pip(calls)) is False and len(calls) == 1
    assert mod.install(home, upstream.v2, run=_pip(calls)) is True and len(calls) == 2


def test_a_recreated_venv_is_installed_again(consumer, upstream, monkeypatch, tmp_path):
    """The record lives in the venv: a venv deleted and made again at the same path has none."""
    _pin(consumer, "v1")
    _, home = _sync(consumer, upstream)
    mod = _load_script()
    prefix = _venv(tmp_path, monkeypatch, mod)
    calls = []
    mod.install(home, upstream.v1, run=_pip(calls))
    shutil.rmtree(prefix)
    _venv(tmp_path, monkeypatch, mod)
    assert mod.install(home, upstream.v1, run=_pip(calls)) is True and len(calls) == 2


def test_a_missing_striim_test_is_installed_again(consumer, upstream, monkeypatch, tmp_path):
    _pin(consumer, "v1")
    _, home = _sync(consumer, upstream)
    mod = _load_script()
    prefix = _venv(tmp_path, monkeypatch, mod)
    calls = []
    mod.install(home, upstream.v1, run=_pip(calls))
    (prefix / "bin" / "striim-test").unlink()
    assert mod.install(home, upstream.v1, run=_pip(calls)) is True and len(calls) == 2


def test_offline_installs_use_no_index(consumer, upstream, monkeypatch, tmp_path):
    _pin(consumer, "v1")
    _, home = _sync(consumer, upstream)
    mod = _load_script()
    _venv(tmp_path, monkeypatch, mod, name="a")
    calls = []
    mod.install(home, upstream.v1, run=_pip(calls), find_links=tmp_path / "wheels")
    assert calls[-1][-5:] == ["--no-index", "--find-links", str(tmp_path / "wheels"), "-e", str(home)]
    _venv(tmp_path, monkeypatch, mod, name="b")
    mod.install(home, upstream.v1, run=_pip(calls), offline=True)
    assert "--no-index" in calls[-1] and "--no-build-isolation" in calls[-1]


def test_install_outside_a_venv_is_refused(consumer, upstream, monkeypatch):
    _pin(consumer, "v1")
    _, home = _sync(consumer, upstream)
    mod = _load_script()
    monkeypatch.setattr(mod.sys, "prefix", "/usr")
    monkeypatch.setattr(mod.sys, "base_prefix", "/usr")
    with pytest.raises(mod.SyncError) as e:
        mod.install(home, upstream.v1)
    assert e.value.code == 2


def test_check_fails_on_a_changed_tracked_file(consumer, upstream):
    _pin(consumer, "v1")
    _, home = _sync(consumer, upstream)
    (home / "VERSION").write_text("patched\n")
    r, _ = _sync(consumer, upstream, "--check")
    assert r.returncode == 1 and "local changes" in r.stderr and "VERSION" in r.stderr


def test_an_untracked_file_is_a_local_change(consumer, upstream):
    _pin(consumer, "v1")
    _, home = _sync(consumer, upstream)
    (home / "extra_test.py").write_text("x = 1\n")
    r, _ = _sync(consumer, upstream, "--check")
    assert r.returncode == 1 and "extra_test.py" in r.stderr
    r, _ = _sync(consumer, upstream)
    assert r.returncode == 3 and "extra_test.py" in r.stderr


def test_a_checkout_on_a_branch_at_the_pin_is_detached(consumer, upstream):
    _pin(consumer, "v2")
    home = consumer.parent / "framework"
    _git(consumer.parent, "clone", "-q", upstream.url, str(home))      # on main, which is v2
    assert _git(home, "rev-parse", "HEAD") == upstream.v2
    r, _ = _sync(consumer, upstream, "--check")
    assert r.returncode == 1 and "branch" in r.stderr
    r, _ = _sync(consumer, upstream)
    assert r.returncode == 0, r.stderr
    assert subprocess.run(["git", "-C", str(home), "symbolic-ref", "-q", "HEAD"]).returncode != 0
    r, _ = _sync(consumer, upstream, "--check")
    assert r.returncode == 0, r.stderr


def test_the_template_manifest_and_case_load(consumer):
    sys.path.insert(0, str(REPO / "scripts" / "cli"))
    from striim_test import doctor
    from livetest import project
    p = project.load_project(consumer / "gold-targets.yaml")
    assert p.suites["live"] == (consumer / "tests" / "live").resolve()
    check, = doctor.check_manifests([consumer / "tests/live/orders-cdc/test.yaml"])
    assert check.status == doctor.OK_, check.line()


def test_list_selects_the_template_case(consumer, tmp_path):
    r = run_cli(["list", "--tier", "live", "--targets", consumer / "gold-targets.yaml"], cwd=tmp_path,
                env=framework_env())
    assert r.rc == 0, r.stderr
    assert r.ids() == ["live:tests/live/orders-cdc::orders-cdc"], r.stdout


def test_sync_makes_env_from_the_example_once(consumer, upstream):
    _pin(consumer, "v1")
    r, _ = _sync(consumer, upstream, "--dry-run")
    assert "would copy .env.example to .env" in r.stdout and not (consumer / ".env").exists()
    r, _ = _sync(consumer, upstream)
    assert r.returncode == 0, r.stderr
    assert "made" in r.stdout and ".env" in r.stdout
    env = consumer / ".env"
    assert env.read_text() == (consumer / ".env.example").read_text()
    assert env.stat().st_mode & 0o777 == 0o600
    env.write_text("STRIIM_URL=http://mine:9080\n")
    r, _ = _sync(consumer, upstream)
    assert r.returncode == 0 and env.read_text() == "STRIIM_URL=http://mine:9080\n"
    _sync(consumer, upstream, "--check")
    assert env.read_text() == "STRIIM_URL=http://mine:9080\n"


@pytest.fixture
def upstream_needs_new_python(tmp_path):
    """A bare 'framework' repository whose tag v1 has a pyproject.toml that needs Python 3.99."""
    work = tmp_path / "upstream-py-work"
    work.mkdir()
    _git(work, "init", "-q", "-b", "main")
    (work / "pyproject.toml").write_text('[project]\nname = "x"\nrequires-python = ">=3.99"\n')
    _git(work, "add", ".")
    _git(work, "commit", "-q", "-m", "one")
    _git(work, "tag", "v1")
    bare = tmp_path / "upstream-py.git"
    _git(tmp_path, "clone", "-q", "--bare", str(work), str(bare))
    return SimpleNamespace(url=str(bare))


def _sync_with(python, consumer, upstream, *args):
    return subprocess.run([python, str(consumer / "scripts" / "sync-framework.py"),
                           "--home", str(consumer.parent / "framework"), "--remote", upstream.url, *args],
                          capture_output=True, text=True, env=GIT_ENV)


def test_a_venv_with_too_old_a_python_is_refused_before_installing(consumer, upstream_needs_new_python):
    _pin(consumer, "v1")
    r = _sync_with(sys.executable, consumer, upstream_needs_new_python)
    assert r.returncode == 2, r.stderr
    assert "needs Python 3.99 or later" in r.stderr and "pip" not in r.stderr


def test_outside_a_venv_with_no_new_enough_python_nothing_is_made(consumer, upstream_needs_new_python):
    base = getattr(sys, "_base_executable", None)
    if not base or base == sys.executable:
        pytest.skip("needs the base interpreter of this venv")
    _pin(consumer, "v1")
    r = _sync_with(base, consumer, upstream_needs_new_python)
    assert r.returncode == 2, r.stderr
    assert "needs Python 3.99 or later" in r.stderr and "--python" in r.stderr
    assert not (consumer / ".venv").exists()


def test_requires_python_reads_the_lower_bound(tmp_path):
    mod = _load_script()
    assert mod.requires_python(tmp_path) is None
    for spec, want in ((">=3.12", (3, 12)), (">= 3.12.1, <4", (3, 12, 1)), (">=3", (3, 0)), ("~=3.12", None)):
        (tmp_path / "pyproject.toml").write_text(f'[project]\nrequires-python = "{spec}"\n')
        assert mod.requires_python(tmp_path) == want, spec
    assert mod.requires_python(REPO) == (3, 12)


def _fake_python(folder, name, version):
    folder.mkdir(parents=True, exist_ok=True)
    exe = folder / name
    exe.write_text(f"#!/bin/sh\necho {version}\n")
    exe.chmod(0o755)
    return exe


def _running(monkeypatch, mod, version, prefix="/usr"):
    """Make the script believe it runs on a Python of ``version`` that is not in a venv."""
    monkeypatch.setattr(mod, "sys", SimpleNamespace(version_info=version, executable="/usr/bin/python3",
                                                    prefix=prefix, base_prefix="/usr"))


def test_find_python_skips_an_old_python3_for_a_new_enough_python3_n(tmp_path, monkeypatch):
    mod = _load_script()
    _running(monkeypatch, mod, (3, 11, 9))
    bin_dir = tmp_path / "bin"
    _fake_python(bin_dir, "python3.12", "3 12 7")
    _fake_python(bin_dir, "python3.13", "3 13 1")
    monkeypatch.setenv("PATH", str(bin_dir))
    assert mod.find_python((3, 12), None) == str(bin_dir / "python3.12")
    assert mod.find_python((3, 13), None) == str(bin_dir / "python3.13")
    with pytest.raises(mod.SyncError) as e:
        mod.find_python((3, 14), None)
    assert e.value.code == 2 and "3.11.9" in str(e.value) and "--python" in str(e.value)


def test_find_python_uses_the_running_python_when_it_is_new_enough(monkeypatch, tmp_path):
    mod = _load_script()
    _running(monkeypatch, mod, (3, 13, 0))
    monkeypatch.setenv("PATH", str(tmp_path))
    assert mod.find_python((3, 12), None) == "/usr/bin/python3"


def test_an_explicit_python_must_be_new_enough(tmp_path):
    mod = _load_script()
    old = _fake_python(tmp_path, "python-old", "3 11 9")
    with pytest.raises(mod.SyncError) as e:
        mod.find_python((3, 12), str(old))
    assert e.value.code == 2 and "3.11.9" in str(e.value)
    new = _fake_python(tmp_path, "python-new", "3 12 0")
    assert mod.find_python((3, 12), str(new)) == str(new)


def test_an_existing_repo_venv_with_an_old_python_is_refused_not_replaced(tmp_path, monkeypatch):
    mod = _load_script()
    _running(monkeypatch, mod, (3, 12, 0))
    monkeypatch.setattr(mod, "REPO", tmp_path)
    venv = tmp_path / ".venv"
    _fake_python(venv / "bin", "python", "3 11 9")
    with pytest.raises(mod.SyncError) as e:
        mod.prepare_venv(venv, (3, 12), None, "v1")
    assert e.value.code == 2 and "3.11.9" in str(e.value) and "rm -rf" in str(e.value)
    assert (venv / "bin" / "python").is_file()
    _fake_python(venv / "bin", "python", "3 12 3")
    assert mod.prepare_venv(venv, (3, 12), None, "v1") is None


def test_a_missing_repo_venv_is_made_with_a_python_that_qualifies(tmp_path):
    mod = _load_script()
    venv = tmp_path / ".venv"
    need = tuple(sys.version_info[:2])
    assert mod.prepare_venv(venv, need, None, "v1") == sys.executable
    assert mod.python_version(mod.venv_python(venv))[:2] == need
    assert mod.prepare_venv(venv, need, None, "v1") is None
