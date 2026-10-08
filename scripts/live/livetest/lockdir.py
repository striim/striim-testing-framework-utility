"""The machine-wide lock dir (``SLT_LOCK_DIR``, default ``/tmp/slt-locks``), shared by every user who
runs striim-test on this host.

Every invocation on a host must coordinate in ONE dir (docs/internals/design/lifecycle.md), and what it guards
(the Docker containers and host ports) is host-wide, so a second user cannot simply take a dir of their
own. So the dir is made like /tmp, 1777 (anyone may add a file, only its owner may remove it), and
every file in it 0666, whatever the umask. An existing file is always opened without O_CREAT: in a
sticky world-writable dir, Linux's fs.protected_regular refuses O_CREAT on a file another user owns,
even when its mode allows the write.

Older versions made the dir and its files with the umask (typically 0775/0664), which locks every
other user out. ``ensure_dir`` widens the framework's own entries that the calling user owns, once per
process; what another user owns is theirs to widen, and a dir nobody lets us write to fails with the
fixes named.

An explicit ``SLT_LOCK_DIR`` is a dir of the user's own (README, "When a run cannot start"): it is
never widened, and its files follow the umask.
"""
import os
import pwd
import re
import stat
import time
from pathlib import Path

DIR_MODE = 0o1777
FILE_MODE = 0o666
# What the framework makes in the dir: `.slt-...` or `.<prefix>-slt-...` (stack.state_name), and the
# marker and member files inside such a dir. Nothing else is widened.
_OURS = re.compile(r"^\.(?:[a-z0-9][a-z0-9-]*-)?slt-")

_widened: set = set()

# Another user's process creates a file or the dir with its umask and widens it a moment later
# (fchmod, chmod): a permission refusal inside that window is retried this often, this far apart.
_RACE_TRIES, _RACE_WAIT_S = 5, 0.05


def _retry_denied(fn):
    for _ in range(_RACE_TRIES - 1):
        try:
            return fn()
        except PermissionError:
            time.sleep(_RACE_WAIT_S)
    return fn()


def shared() -> bool:
    """False when SLT_LOCK_DIR names a dir of the user's own."""
    return not (os.environ.get("SLT_LOCK_DIR") or "").strip()


def file_mode() -> int:
    """The mode for a new file in the lock dir. Pass as FileLock(..., mode=file_mode()): filelock >=
    3.20.4 then creates the file with it whatever the umask, opens an existing one without O_CREAT when
    O_CREAT is refused, and ignores a non-owner's failed fchmod."""
    if shared():
        return FILE_MODE
    umask = os.umask(0)
    os.umask(umask)
    return FILE_MODE & ~umask


def _owner(uid: int) -> str:
    try:
        return pwd.getpwuid(uid).pw_name
    except KeyError:
        return f"uid {uid}"


def ensure_dir(d) -> Path:
    """Create ``d`` (and its parents) if needed, and make it a shared lock dir."""
    d = Path(d)
    d.mkdir(parents=True, exist_ok=True)
    st = d.stat()
    if not shared():
        return d
    if st.st_uid == os.geteuid():
        if stat.S_IMODE(st.st_mode) != DIR_MODE:
            os.chmod(d, DIR_MODE)
        if d not in _widened:
            _widen_own_entries(d)
            _widened.add(d)
    elif not _writable(d):
        who = _owner(st.st_uid)
        raise PermissionError(
            f"the shared lock dir {d} belongs to {who} (mode {stat.S_IMODE(st.st_mode):o}) and you cannot "
            f"write to it; an older striim-test made it. Any run of this version as {who} widens it, or "
            f"{who} can run `chmod 1777 {d}`. Or use a dir of your own: export SLT_LOCK_DIR=$HOME/.slt-locks "
            f"(README, \"When a run cannot start\")")
    return d


def _writable(d: Path) -> bool:
    for _ in range(_RACE_TRIES - 1):
        if os.access(d, os.W_OK | os.X_OK):
            return True
        time.sleep(_RACE_WAIT_S)
    return os.access(d, os.W_OK | os.X_OK)


def _widen_own_entries(d: Path) -> None:
    uid = os.geteuid()

    def widen(p: str) -> bool:
        """Widen ``p`` if it is ours and a plain file or dir; True for a dir."""
        try:
            st = os.lstat(p)
            is_dir = stat.S_ISDIR(st.st_mode)
            mode = DIR_MODE if is_dir else FILE_MODE
            if st.st_uid == uid and (is_dir or stat.S_ISREG(st.st_mode)) and stat.S_IMODE(st.st_mode) != mode:
                os.chmod(p, mode)
            return is_dir
        except OSError:
            return False    # gone, or not ours to change: best effort

    try:
        names = os.listdir(d)
    except OSError:
        return
    for name in names:
        if not _OURS.match(name):
            continue
        p = os.path.join(d, name)
        if widen(p):                    # a marker or member dir: its entries are pid files
            try:
                for child in os.listdir(p):
                    widen(os.path.join(p, child))
            except OSError:
                pass


def _open(path, flags: int) -> int:
    """Open an existing file without O_CREAT, or create it with ``file_mode()``. Never through a
    symlink: the dir may belong to another user, who could plant one to a file of ours."""
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        return _retry_denied(lambda: os.open(path, flags))
    except FileNotFoundError:
        pass
    try:
        fd = os.open(path, flags | os.O_CREAT | os.O_EXCL, file_mode())
    except FileExistsError:            # created between the two opens
        return _retry_denied(lambda: os.open(path, flags))
    os.fchmod(fd, file_mode())
    return fd


def write_text(path, text: str) -> None:
    """``Path.write_text`` for a file in the shared dir."""
    with os.fdopen(_open(path, os.O_WRONLY | os.O_TRUNC), "w") as f:
        f.write(text)


def open_append(path):
    """``open(path, "a+")`` for a file in the shared dir."""
    return os.fdopen(_open(path, os.O_RDWR | os.O_APPEND), "a+")
