"""The machine-wide lock dir is shared by every user who runs striim-test on the host.

A second user on a shared Linux host died at collection with ``PermissionError`` on
``/tmp/slt-locks/.slt-endpoint-9080.registry.lock``: the first user's umask had made the dir 0775 and
its files 0664. The dir is now made like /tmp (1777) and its files 0666, and an existing file is
opened without O_CREAT, which Linux's fs.protected_regular refuses on another user's file in a sticky
world-writable dir even when the mode allows the write. These tests simulate that refusal."""
import builtins
import errno
import io
import json
import os
import re
import stat
import sys
from pathlib import Path

import pytest

from livetest import infra, opregistry, stack

SHARED_ENV = {"SLT_INFRA_OWNERSHIP": "shared", "SLT_KEEP_SERVICES": "1"}


def _mode(p) -> int:
    return stat.S_IMODE(os.stat(p).st_mode)


@pytest.fixture
def umask022():
    old = os.umask(0o022)
    yield
    os.umask(old)


@pytest.fixture
def lock_root(tmp_path, monkeypatch):
    d = tmp_path / "slt-locks"
    monkeypatch.delenv("SLT_LOCK_DIR", raising=False)     # the shared default, not a private dir
    monkeypatch.setattr(stack, "_LOCK_DIR", d)
    mod = sys.modules.get("livetest.lockdir")
    if mod is not None:                               # a fresh process-wide widening pass per test
        monkeypatch.setattr(mod, "_widened", set())
    return d


@pytest.fixture
def protected_regular(monkeypatch):
    """Refuse every O_CREAT open of a path that already exists, as the kernel does for a second user
    opening the first user's file in a sticky world-writable dir."""
    real = os.open

    def guarded(path, flags, mode=0o777, *a, **k):
        if flags & os.O_CREAT and os.path.exists(path):
            raise PermissionError(errno.EACCES, "Permission denied (protected_regular)", str(path))
        return real(path, flags, mode, *a, **k)

    monkeypatch.setattr(os, "open", guarded)
    real_io = io.open

    def guarded_io(file, mode="r", *a, **k):      # "w" and "a" open with O_CREAT
        if isinstance(file, (str, os.PathLike)) and any(c in mode for c in "wa") and os.path.exists(file):
            raise PermissionError(errno.EACCES, "Permission denied (protected_regular)", str(file))
        return real_io(file, mode, *a, **k)

    monkeypatch.setattr(io, "open", guarded_io)
    monkeypatch.setattr(builtins, "open", guarded_io)


def test_lock_dir_is_made_sticky_and_world_writable(lock_root, umask022):
    stack.lock_path(".slt-provision.lock")
    assert _mode(lock_root) == 0o1777
    infra._lock_dir()
    assert _mode(lock_root) == 0o1777


def test_shared_declaration_leaves_only_world_writable_entries(lock_root, umask022):
    decl = infra._declare_shared(dict(SHARED_ENV), 9080, infra._lock_dir())
    try:
        assert _mode(infra._registry_path(lock_root, 9080)) == 0o666
        assert _mode(infra._shared_dir(lock_root, 9080)) == 0o1777
        assert _mode(decl._marker) == 0o666
    finally:
        decl.release()


def test_provision_lock_is_world_writable(lock_root, umask022, tmp_path):
    from livetest import services
    services.forget_provisioned([], state_dir=tmp_path / "state")
    locks = [p for p in lock_root.iterdir() if "provision" in p.name and p.name.endswith(".lock")]
    assert locks and all(_mode(p) == 0o666 for p in locks)


def test_an_older_versions_dir_and_own_files_are_widened(lock_root, umask022):
    lock_root.mkdir(mode=0o775)
    os.chmod(lock_root, 0o775)
    old = lock_root / ".slt-endpoint-9080.registry.lock"
    old.write_text("")
    os.chmod(old, 0o664)
    sub = lock_root / ".slt-endpoint-9080.shared"
    sub.mkdir()
    os.chmod(sub, 0o775)
    stack.lock_path(".slt-provision.lock")
    assert (_mode(lock_root), _mode(old), _mode(sub)) == (0o1777, 0o666, 0o1777)


def test_a_dir_another_user_owns_and_nobody_may_write_names_the_fixes(lock_root, monkeypatch):
    lock_root.mkdir()
    monkeypatch.setattr(os, "geteuid", lambda: os.stat(lock_root).st_uid + 1)
    monkeypatch.setattr(os, "access", lambda p, m, **k: False)
    with pytest.raises(PermissionError) as ei:
        stack.lock_path(".slt-provision.lock")
    msg = str(ei.value)
    assert str(lock_root) in msg and f"chmod 1777 {lock_root}" in msg and "SLT_LOCK_DIR" in msg


def test_a_second_user_joins_the_first_users_endpoint_files(lock_root, protected_regular):
    first = infra._declare_shared(dict(SHARED_ENV), 9080, infra._lock_dir())
    first.release()
    assert infra._registry_path(lock_root, 9080).exists()
    second = infra._declare_shared(dict(SHARED_ENV), 9080, infra._lock_dir())
    second.release()


def test_a_second_user_rewrites_the_first_users_op_registry(lock_root, protected_regular):
    opregistry.record("a.jar", "f1")      # the files do not exist yet: created
    opregistry.record("b.jar", "f2")      # they exist now: rewritten without O_CREAT
    with opregistry.in_use():
        pass
    assert json.loads(opregistry.registry_path().read_text()) == {"a.jar": "f1", "b.jar": "f2"}


def test_clearing_another_users_op_registry_empties_it(lock_root, monkeypatch):
    opregistry.record("a.jar", "f1")
    reg = opregistry.registry_path()
    real_unlink = type(reg).unlink

    def refused(self, *a, **k):
        if self == reg:        # a sticky dir: only the file's owner may remove it
            raise PermissionError(errno.EPERM, "Operation not permitted", str(self))
        return real_unlink(self, *a, **k)

    monkeypatch.setattr(type(reg), "unlink", refused)
    opregistry.clear()
    opregistry.record("b.jar", "f2")
    assert json.loads(reg.read_text()) == {"b.jar": "f2"}


# ---- review findings ------------------------------------------------------------

def test_only_the_frameworks_own_entries_are_widened(lock_root, umask022):
    # M2: the widening pass touched everything the user owned under the dir
    lock_root.mkdir()
    ours = lock_root / ".lift-slt-op-inuse.lock"
    ours.write_text("")
    os.chmod(ours, 0o664)
    markers = lock_root / ".slt-endpoint-9080.shared"
    markers.mkdir()
    os.chmod(markers, 0o775)
    marker = markers / "123"
    marker.write_text("{}")
    os.chmod(marker, 0o644)
    key = lock_root / "id_ed25519"
    key.write_text("k")
    os.chmod(key, 0o600)
    private = lock_root / "bin"
    private.mkdir()
    os.chmod(private, 0o700)
    tool = private / ".slt-tool"
    tool.write_text("")
    os.chmod(tool, 0o755)
    stack.lock_path(".slt-provision.lock")
    assert (_mode(ours), _mode(markers), _mode(marker)) == (0o666, 0o1777, 0o666)
    assert (_mode(key), _mode(private), _mode(tool)) == (0o600, 0o700, 0o755)


def test_an_explicit_slt_lock_dir_is_left_private(tmp_path, monkeypatch, umask022):
    # M2: SLT_LOCK_DIR is how a user asks for a dir of their own; it is not widened, and its files
    # follow the umask
    from livetest import services
    d = tmp_path / "home"
    d.mkdir()
    os.chmod(d, 0o750)
    key = d / "id_ed25519"
    key.write_text("k")
    os.chmod(key, 0o600)
    monkeypatch.setenv("SLT_LOCK_DIR", str(d))
    monkeypatch.setattr(stack, "_LOCK_DIR", d)
    services.forget_provisioned([], state_dir=tmp_path / "state")
    opregistry.record("a.jar", "f1")
    assert (_mode(d), _mode(key)) == (0o750, 0o600)
    made = [p for p in d.iterdir() if p.name.startswith(".")]
    assert made and all(_mode(p) == 0o644 for p in made), [(p.name, oct(_mode(p))) for p in made]


def test_a_planted_symlink_is_not_followed(lock_root, tmp_path):
    # L1: a writable dir another user may own; a JSON record is never written through a symlink
    stack.lock_path(".slt-provision.lock")
    victim = tmp_path / "victim"
    victim.write_text("precious")
    opregistry.registry_path().symlink_to(victim)
    with pytest.raises(OSError):
        opregistry.record("a.jar", "f1")
    assert victim.read_text() == "precious"


def test_release_empties_another_users_leftover_exclusive_records(lock_root, monkeypatch):
    # L2: a killed exclusive run of another user left the owner and verdict records; this user may not
    # remove them from the sticky dir, and release() must not raise over it
    d = infra._lock_dir()
    lease = infra._exclusive_lease_path(d, 9080)
    extras = [Path(str(lease) + ".owner"), infra._verdict_path(lease)]
    for p in extras:
        p.write_text(json.dumps({"runId": "theirs", "pid": 1}))
    real_unlink = Path.unlink

    def refused(self, *a, **k):
        if self in extras:
            raise PermissionError(errno.EPERM, "Operation not permitted", str(self))
        return real_unlink(self, *a, **k)

    monkeypatch.setattr(Path, "unlink", refused)
    infra.Infra("exclusive", 9080, d, run_id="mine", lease_path=lease).release()
    assert [json.loads(p.read_text()) for p in extras] == [{}, {}]


def test_a_file_mid_creation_by_another_user_is_retried(lock_root, monkeypatch):
    # L3: between another user's create and its fchmod the file is not yet 0666
    stack.lock_path(".slt-provision.lock")
    p = opregistry.registry_path()
    p.write_text("{}")
    real, seen = os.open, []

    def flaky(path, flags, *a, **k):
        if str(path) == str(p) and len(seen) < 2:
            seen.append(path)
            raise PermissionError(errno.EACCES, "Permission denied", str(path))
        return real(path, flags, *a, **k)

    monkeypatch.setattr(os, "open", flaky)
    opregistry.record("a.jar", "f1")
    assert len(seen) == 2 and json.loads(p.read_text()) == {"a.jar": "f1"}


def test_a_dir_mid_creation_by_another_user_is_retried(lock_root, monkeypatch):
    # L3: between another user's mkdir and its chmod the dir is not yet 1777
    lock_root.mkdir()
    answers = iter([False, False, True])
    monkeypatch.setattr(os, "geteuid", lambda: os.stat(lock_root).st_uid + 1)
    monkeypatch.setattr(os, "access", lambda p, m, **k: next(answers))
    assert stack.lock_path(".slt-provision.lock").parent == lock_root


def test_every_install_path_pins_the_filelock_the_shared_dir_needs():
    # L5: FileLock(mode=) needs >= 3.10, and the no-O_CREAT fallback on an existing file >= 3.20.4
    live = Path(stack.__file__).resolve().parents[1]
    for f in (live.parents[1] / "pyproject.toml", live / "pyproject.toml", live / "requirements.txt"):
        pins = re.findall(r"filelock\s*([<>=!~][^\"',\s]*)", f.read_text())
        assert pins == [">=3.20.4"], (f, pins)
