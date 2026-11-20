"""Checkout-mode Striim build dependencies are verified, not just present.

Third-party files (JDBC drivers, the JMX agent, Oracle Instant Client) must match the SHA-256 list
the framework ships (services/striim/deps.sha256). Striim publishes no checksums, so a Striim
`.deb` is pinned the first time this machine sees it and must match that pin on every reuse.

download-dependencies.sh runs for real against a fake `curl` on PATH: no network.
"""
import hashlib
import json
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

import livetest
from livetest import striim_provision as sp

REPO = Path(livetest.__file__).resolve().parents[3]
STRIIM = REPO / "scripts" / "live" / "services" / "striim"
VERSION = "5.4.2"
THIRD_PARTY = ["sqljdbc_6.0.8112.200_enu.tar.gz", "mysql-connector-java-8.0.30.zip",
               "vertica-jdbc-25.3.0-0.jar", "terajdbc-20.00.00.46.jar",
               "jmx_prometheus_javaagent-0.16.1.jar", "instantclient-basic-linux.x64-21.6.0.0.0dbru.zip"]


@pytest.fixture(autouse=True)
def _checkout_mode(monkeypatch):
    monkeypatch.delenv(sp.DEPS_MANIFEST_ENV, raising=False)
    monkeypatch.delenv(sp.TERADATA_JDBC_ENV, raising=False)
    monkeypatch.setattr(sp, "_in_checkout", lambda: True)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _content(name: str) -> bytes:
    return f"bytes of {name}\n".encode()


def _shipped(tmp_path, monkeypatch, names):
    """Point the shipped digest list at one naming ``names`` with their _content digests."""
    path = tmp_path / "shipped.sha256"
    path.write_text("# test list\n" + "".join(f"{_sha(_content(n))}  {n}\n" for n in names))
    monkeypatch.setattr(sp, "SHIPPED_DEP_DIGESTS", path)
    return path


def _fill(ctx: Path, overrides=None):
    d = sp._deps_dir(ctx)
    d.mkdir(parents=True, exist_ok=True)
    for n in sp._required_deps(VERSION):
        (d / n).write_bytes((overrides or {}).get(n, _content(n)))
    return d


def _third_party_required():
    return [n for n in sp._required_deps(VERSION) if not n.startswith("striim-")]


# ------------------------------------------------------------------ the shipped list
def test_the_shipped_list_names_every_third_party_download_and_no_striim_file():
    digests = sp.shipped_dep_digests()
    assert sorted(digests) == sorted(THIRD_PARTY)
    assert all(len(v) == 64 and int(v, 16) >= 0 for v in digests.values())
    script = (STRIIM / "download-dependencies.sh").read_text()
    for name in THIRD_PARTY:
        assert name in script, name
    assert not [n for n in digests if n.startswith("striim-")]


def test_the_shipped_list_is_the_one_the_script_reads():
    assert sp.SHIPPED_DEP_DIGESTS == STRIIM / "deps.sha256"
    assert "deps.sha256" in (STRIIM / "download-dependencies.sh").read_text()


# ------------------------------------------------------------------ ensure_deps (checkout mode)
def test_present_third_party_junk_is_refused_against_the_real_shipped_list(tmp_path):
    _fill(tmp_path, {n: b"not the vendor's file" for n in _third_party_required()})
    with pytest.raises(sp.StriimDepsError, match="digest mismatch.*sqljdbc_6.0.8112.200_enu.tar.gz"):
        sp.ensure_deps(tmp_path, {"STRIIM_VERSION": VERSION}, run=lambda *a, **k: pytest.fail("downloaded"))


def test_a_download_of_junk_bytes_is_refused(tmp_path):
    ran = []

    def run(argv, cwd=None):          # the download: any non-empty bytes under the right names
        ran.append(argv)
        _fill(tmp_path, {n: b"not the vendor's file" for n in sp._required_deps(VERSION)})
    with pytest.raises(sp.StriimDepsError, match="digest mismatch"):
        sp.ensure_deps(tmp_path, {"STRIIM_VERSION": VERSION}, run=run)
    assert ran == [["./download-dependencies.sh"]]


def test_matching_files_pass_and_only_the_striim_debs_are_pinned(tmp_path, monkeypatch):
    _shipped(tmp_path, monkeypatch, THIRD_PARTY)
    d = _fill(tmp_path)
    sp.ensure_deps(tmp_path, {"STRIIM_VERSION": VERSION}, run=lambda *a, **k: pytest.fail("downloaded"))
    pins = json.loads((d / sp.DEPS_PINS_FILE).read_text())
    debs = [n for n in sp._required_deps(VERSION) if n.startswith("striim-")]
    assert pins == {"schemaVersion": 1, "sha256": {n: _sha(_content(n)) for n in debs}}


def test_a_striim_deb_that_changed_since_it_was_pinned_is_refused(tmp_path, monkeypatch):
    _shipped(tmp_path, monkeypatch, THIRD_PARTY)
    d = _fill(tmp_path)
    sp.ensure_deps(tmp_path, {"STRIIM_VERSION": VERSION}, run=lambda *a, **k: None)
    node = f"striim-node-{VERSION}-Linux.deb"
    (d / node).write_bytes(b"a different build under the same name")
    with pytest.raises(sp.StriimDepsError, match=f"{node}.*first") as e:
        sp.ensure_deps(tmp_path, {"STRIIM_VERSION": VERSION}, run=lambda *a, **k: None)
    assert sp.DEPS_PINS_FILE in str(e.value)


def _count_hashes(monkeypatch):
    hashed = []
    real = sp._sha256_of
    monkeypatch.setattr(sp, "_sha256_of", lambda p: hashed.append(Path(p).name) or real(p))
    return hashed


def test_unchanged_files_pass_again_without_being_hashed_again(tmp_path, monkeypatch):
    # The striim-node .deb alone is ~3.3 GB: a file whose size, mtime and ctime are what they were
    # when it last matched its digest is not read again.
    _shipped(tmp_path, monkeypatch, THIRD_PARTY)
    d = _fill(tmp_path)
    hashed = _count_hashes(monkeypatch)
    sp.ensure_deps(tmp_path, {"STRIIM_VERSION": VERSION}, run=lambda *a, **k: None)
    assert sorted(hashed) == sorted(sp._required_deps(VERSION))       # first sight: every file
    before = (d / sp.DEPS_PINS_FILE).read_bytes()
    hashed.clear()
    sp.ensure_deps(tmp_path, {"STRIIM_VERSION": VERSION}, run=lambda *a, **k: None)
    assert hashed == []
    assert (d / sp.DEPS_PINS_FILE).read_bytes() == before


def test_a_same_size_replacement_with_its_mtime_restored_is_hashed_and_refused(tmp_path, monkeypatch):
    # A copy that preserves timestamps (cp -p, touch -r, an archive extract) can keep size and
    # mtime; the replacement still changes the file's ctime.
    _shipped(tmp_path, monkeypatch, THIRD_PARTY)
    d = _fill(tmp_path)
    sp.ensure_deps(tmp_path, {"STRIIM_VERSION": VERSION}, run=lambda *a, **k: None)
    node = d / f"striim-node-{VERSION}-Linux.deb"
    st = node.stat()
    node.write_bytes(b"X" * st.st_size)
    os.utime(node, ns=(st.st_atime_ns, st.st_mtime_ns))
    assert (node.stat().st_size, node.stat().st_mtime_ns) == (st.st_size, st.st_mtime_ns)
    with pytest.raises(sp.StriimDepsError, match=f"{node.name}.*first"):
        sp.ensure_deps(tmp_path, {"STRIIM_VERSION": VERSION}, run=lambda *a, **k: None)


def test_a_new_shipped_digest_is_checked_although_the_file_is_unchanged(tmp_path, monkeypatch):
    _shipped(tmp_path, monkeypatch, THIRD_PARTY)
    d = _fill(tmp_path)
    sp.ensure_deps(tmp_path, {"STRIIM_VERSION": VERSION}, run=lambda *a, **k: None)
    jar = "vertica-jdbc-25.3.0-0.jar"
    listed = sp.SHIPPED_DEP_DIGESTS.read_text().replace(_sha(_content(jar)), _sha(b"the new release"))
    sp.SHIPPED_DEP_DIGESTS.write_text(listed)
    with pytest.raises(sp.StriimDepsError, match=f"digest mismatch.*{jar}"):
        sp.ensure_deps(tmp_path, {"STRIIM_VERSION": VERSION}, run=lambda *a, **k: None)


@pytest.mark.parametrize("text", ["not json", "[]", '{"schemaVersion": 2, "files": {}}',
                                  '{"schemaVersion": 1, "files": {"x": 1}}'])
def test_an_unreadable_verified_record_means_hashing_again(tmp_path, monkeypatch, text):
    _shipped(tmp_path, monkeypatch, THIRD_PARTY)
    d = _fill(tmp_path)
    sp.ensure_deps(tmp_path, {"STRIIM_VERSION": VERSION}, run=lambda *a, **k: None)
    (tmp_path / sp.DEPS_VERIFIED_FILE).write_text(text)
    hashed = _count_hashes(monkeypatch)
    sp.ensure_deps(tmp_path, {"STRIIM_VERSION": VERSION}, run=lambda *a, **k: None)
    assert sorted(hashed) == sorted(sp._required_deps(VERSION))
    hashed.clear()
    sp.ensure_deps(tmp_path, {"STRIIM_VERSION": VERSION}, run=lambda *a, **k: None)
    assert hashed == []                                                 # rewritten, usable again


def test_an_unwritable_verified_record_does_not_fail_the_check(tmp_path, monkeypatch):
    # Once every file is pinned, a check used to write nothing; a deps cache it cannot write must
    # not turn that into a failure.
    _shipped(tmp_path, monkeypatch, THIRD_PARTY)
    _fill(tmp_path)
    (tmp_path / sp.DEPS_VERIFIED_FILE).mkdir()
    for _ in range(2):
        sp.ensure_deps(tmp_path, {"STRIIM_VERSION": VERSION}, run=lambda *a, **k: None)


def test_the_verified_record_is_outside_the_image_build_context(tmp_path, monkeypatch):
    # The Dockerfile bind-mounts deps/ into its install steps, so a file that changes there
    # invalidates their cached layers.
    _shipped(tmp_path, monkeypatch, THIRD_PARTY)
    d = _fill(tmp_path)
    sp.ensure_deps(tmp_path, {"STRIIM_VERSION": VERSION}, run=lambda *a, **k: None)
    assert (tmp_path / sp.DEPS_VERIFIED_FILE).is_file()
    assert not list((tmp_path / "images").rglob(sp.DEPS_VERIFIED_FILE))
    assert sp.DEPS_VERIFIED_FILE in (STRIIM / ".gitignore").read_text()


def test_a_failed_check_records_nothing_as_verified(tmp_path, monkeypatch):
    _shipped(tmp_path, monkeypatch, THIRD_PARTY)
    d = _fill(tmp_path, {"vertica-jdbc-25.3.0-0.jar": b"tampered"})
    with pytest.raises(sp.StriimDepsError):
        sp.ensure_deps(tmp_path, {"STRIIM_VERSION": VERSION}, run=lambda *a, **k: None)
    assert not (tmp_path / sp.DEPS_VERIFIED_FILE).exists()


@pytest.mark.parametrize("text", ["not json", "[]", '{"schemaVersion": 2, "sha256": {}}',
                                  '{"schemaVersion": 1, "sha256": {"x": "nothex"}}'])
def test_an_unreadable_pin_record_is_refused_not_rewritten(tmp_path, monkeypatch, text):
    _shipped(tmp_path, monkeypatch, THIRD_PARTY)
    d = _fill(tmp_path)
    (d / sp.DEPS_PINS_FILE).write_text(text)
    with pytest.raises(sp.StriimDepsError, match=sp.DEPS_PINS_FILE):
        sp.ensure_deps(tmp_path, {"STRIIM_VERSION": VERSION}, run=lambda *a, **k: None)
    assert (d / sp.DEPS_PINS_FILE).read_text() == text


def test_the_pin_record_is_never_written_through_a_symlink(tmp_path, monkeypatch):
    _shipped(tmp_path, monkeypatch, THIRD_PARTY)
    d = _fill(tmp_path)
    target = tmp_path / "elsewhere.json"
    (d / sp.DEPS_PINS_FILE).symlink_to(target)
    with pytest.raises(sp.StriimDepsError, match="symlink"):
        sp.ensure_deps(tmp_path, {"STRIIM_VERSION": VERSION}, run=lambda *a, **k: None)
    assert not target.exists()


def test_a_present_teradata_driver_is_verified_too(tmp_path, monkeypatch):
    _shipped(tmp_path, monkeypatch, THIRD_PARTY)
    d = _fill(tmp_path)
    (d / sp.TERADATA_JDBC_JAR).write_bytes(b"tampered")
    with pytest.raises(sp.StriimDepsError, match=sp.TERADATA_JDBC_JAR):
        sp.ensure_deps(tmp_path, {"STRIIM_VERSION": VERSION}, run=lambda *a, **k: None)


def test_a_third_party_file_without_a_shipped_digest_is_pinned_like_a_deb(tmp_path, monkeypatch):
    names = [n for n in THIRD_PARTY if not n.startswith("instantclient")]
    _shipped(tmp_path, monkeypatch, names)
    d = _fill(tmp_path)
    sp.ensure_deps(tmp_path, {"STRIIM_VERSION": VERSION}, run=lambda *a, **k: None)
    pins = json.loads((d / sp.DEPS_PINS_FILE).read_text())["sha256"]
    assert "instantclient-basic-linux.x64-21.6.0.0.0dbru.zip" in pins


def test_manifest_mode_does_not_read_or_write_pins(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setenv(sp.DEPS_MANIFEST_ENV, str(tmp_path / "m.json"))
    monkeypatch.setattr(sp, "stage_striim_deps", lambda *a, **k: calls.append("staged"))
    sp.ensure_deps(tmp_path, {"STRIIM_VERSION": VERSION}, run=lambda *a, **k: pytest.fail("downloaded"))
    assert calls == ["staged"]
    assert not (sp._deps_dir(tmp_path) / sp.DEPS_PINS_FILE).exists()


# ------------------------------------------------------------------ download-dependencies.sh
FAKE_CURL = r"""#!/usr/bin/env bash
# Fake curl: `-w` probes answer 200; `-o <dest> <url>` writes $FAKE_SRC/<basename of url>.
out=""; url=""; w=""
while [ $# -gt 0 ]; do
  case "$1" in
    -o) out="$2"; shift 2;;
    -w) w="$2"; shift 2;;
    --retry|--max-time) shift 2;;
    -*) shift;;
    *) url="$1"; shift;;
  esac
done
if [ -n "$w" ]; then printf 200; exit 0; fi
src="$FAKE_SRC/$(basename "$url")"
[ -f "$src" ] || { echo "fake curl: no $src" >&2; exit 22; }
cp "$src" "$out"
"""


def _recipe(tmp_path, junk=()):
    """A copy of the striim recipe's script with a digest list for _content bytes, a fake curl,
    and a source dir serving each file's _content (or junk for names in ``junk``)."""
    recipe = tmp_path / "striim"
    recipe.mkdir()
    shutil.copy(STRIIM / "download-dependencies.sh", recipe / "download-dependencies.sh")
    (recipe / "deps.sha256").write_text("".join(f"{_sha(_content(n))}  {n}\n" for n in THIRD_PARTY))
    src = tmp_path / "src"
    src.mkdir()
    for n in sp._required_deps(VERSION) + [sp.TERADATA_JDBC_JAR]:
        (src / n).write_bytes(b"junk" if n in junk else _content(n))
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "curl").write_text(FAKE_CURL)
    (bindir / "curl").chmod(0o755)
    env = {k: v for k, v in os.environ.items() if not k.startswith(("STRIIM_", "INSTANTCLIENT_", "SLT_"))}
    env.update(PATH=f"{bindir}{os.pathsep}{env['PATH']}", FAKE_SRC=str(src), STRIIM_VERSION=VERSION)
    return recipe, env


def _script(recipe, env):
    return subprocess.run(["bash", str(recipe / "download-dependencies.sh")], env=env, cwd=recipe,
                          capture_output=True, text=True, timeout=120)


def test_the_script_downloads_and_accepts_files_matching_the_list(tmp_path):
    recipe, env = _recipe(tmp_path)
    r = _script(recipe, env)
    assert r.returncode == 0, r.stdout + r.stderr
    deps = recipe / "images" / "striim" / "deps"
    for n in sp._required_deps(VERSION):
        assert (deps / n).read_bytes() == _content(n)
    assert not list(deps.glob("*.part"))


def test_the_script_refuses_a_download_that_does_not_match_and_keeps_nothing(tmp_path):
    bad = "mysql-connector-java-8.0.30.zip"
    recipe, env = _recipe(tmp_path, junk={bad})
    r = _script(recipe, env)
    assert r.returncode != 0
    assert bad in r.stderr and "SHA-256" in r.stderr
    deps = recipe / "images" / "striim" / "deps"
    assert not (deps / bad).exists() and not (deps / f"{bad}.part").exists()


def test_the_script_refuses_a_present_file_that_does_not_match(tmp_path):
    bad = "vertica-jdbc-25.3.0-0.jar"
    recipe, env = _recipe(tmp_path)
    deps = recipe / "images" / "striim" / "deps"
    deps.mkdir(parents=True)
    (deps / bad).write_bytes(b"left over from somewhere")
    r = _script(recipe, env)
    assert r.returncode != 0
    assert bad in r.stderr and "SHA-256" in r.stderr


def test_the_script_verifies_the_opt_in_teradata_driver(tmp_path):
    recipe, env = _recipe(tmp_path, junk={sp.TERADATA_JDBC_JAR})
    env["SLT_TERADATA_JDBC"] = "1"
    r = _script(recipe, env)
    assert r.returncode != 0 and sp.TERADATA_JDBC_JAR in r.stderr
