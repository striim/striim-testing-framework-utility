"""A reused /shared volume must never hand a node or agent an earlier start's keystore.

/shared outlives the cluster (compose down without -v). A node or agent starts with the primary,
but the primary writes the agent keystore only after its own node answers, so the consumer used
to find an EARLIER start's aks.*/sks.* already there and take it. That happened across releases
(5.4.2 left aks.p12, then 5.4.0.8 wrote aks.jks; the other way, jks is preferred and wins) and on
a plain restart of the same release. The agent then never joined, and the run failed as
"CLUSTER-READY deadline expired" (seen 2026-09-26).

The fix: the primary stamps /shared/.keystore-epoch and removes the old keystores before it
generates new ones, and consumers accept only a keystore newer than the stamp. These tests run
the entrypoint's own helper functions in bash and pin the order of the primary's steps. The
race itself is proven at container level in the task evidence (aksrace/race.sh).
"""
import os
import subprocess
import time
from pathlib import Path

import pytest

_IMAGES = Path(__file__).resolve().parents[1] / "services" / "striim" / "images"
_ENTRYPOINT = _IMAGES / "striim" / "files" / "entrypoint.sh"


def _code(text: str) -> str:
    return "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("#"))


@pytest.fixture(scope="module")
def entrypoint() -> str:
    return _code(_ENTRYPOINT.read_text())


def _branch(text: str, role: str) -> str:
    start = text.index(f'"${{ROLE}}" == "{role}" ]; then')
    nxt = text.find('elif [ "${ROLE}"', start + 1)
    return text[start:nxt if nxt > 0 else None]


def _helpers() -> str:
    text = _ENTRYPOINT.read_text()
    return text[text.index("KEYSTORE_EPOCH="):text.index('if [ "${ROLE}" == "primary" ]')]


def _keystore_path(tmp_path, *args) -> str:
    script = _helpers() + '\nkeystore_path "$@"\n'
    r = subprocess.run(["bash", "-c", script, "bash", *args], capture_output=True, text=True)
    return r.stdout.strip()


def _age(path: Path, seconds: int) -> None:
    t = time.time() - seconds
    os.utime(path, (t, t))


def test_a_keystore_older_than_the_stamp_is_not_this_clusters(tmp_path):
    (tmp_path / "aks.p12").write_text("earlier start")
    _age(tmp_path / "aks.p12", 3600)
    (tmp_path / ".keystore-epoch").touch()
    # macOS /bin/bash 3.2 compares -nt in whole seconds, so a keystore written in the same
    # second as the stamp is not "newer" there.
    _age(tmp_path / ".keystore-epoch", 60)
    epoch = str(tmp_path / ".keystore-epoch")
    assert _keystore_path(tmp_path, str(tmp_path), "aks", epoch) == ""
    (tmp_path / "aks.jks").write_text("this start")
    assert _keystore_path(tmp_path, str(tmp_path), "aks", epoch) == str(tmp_path / "aks.jks")


def test_the_preferred_extension_does_not_win_over_freshness(tmp_path):
    # The 5.4.0.x -> 5.4.2 direction: jks is tried first, and a stale jks used to win.
    (tmp_path / "sks.jks").write_text("earlier start")
    _age(tmp_path / "sks.jks", 3600)
    (tmp_path / ".keystore-epoch").touch()
    _age(tmp_path / ".keystore-epoch", 60)
    (tmp_path / "sks.p12").write_text("this start")
    assert _keystore_path(tmp_path, str(tmp_path), "sks", str(tmp_path / ".keystore-epoch")) \
        == str(tmp_path / "sks.p12")


def test_without_a_stamp_any_keystore_is_accepted(tmp_path):
    # The primary's own conf dir, and a /shared written by an older primary image.
    (tmp_path / "aks.jks").write_text("x")
    assert _keystore_path(tmp_path, str(tmp_path), "aks") == str(tmp_path / "aks.jks")
    assert _keystore_path(tmp_path, str(tmp_path), "aks", str(tmp_path / "no-stamp")) \
        == str(tmp_path / "aks.jks")


def test_the_primary_stamps_and_clears_before_it_generates(entrypoint):
    primary = _branch(entrypoint, "primary")
    stamp = primary.index('touch "$KEYSTORE_EPOCH"')
    clear = primary.index("clear_shared /shared")
    fn = entrypoint[entrypoint.index("clear_shared() {"):]
    fn = fn[:fn.index("\n}")]
    for name in ("sks.jks", "sks.p12", "sksKey.pwd", "aks.jks", "aks.p12", "aksKey.pwd"):
        assert f'"$1"/{name}' in fn
    assert stamp < clear < primary.index("sksConfig.sh") < primary.index("aksConfig.sh")
    assert primary.count("share_keystore ") == 2


def test_node_and_agent_take_only_this_starts_keystore(entrypoint):
    for role, base in (("node", "sks"), ("agent", "aks")):
        branch = _branch(entrypoint, role)
        assert branch.index("9080") < branch.index(f"wait_keystore /shared {base}")
        assert f'keystore_path /shared {base} "$KEYSTORE_EPOCH"' in branch
        assert f"keystore_path /shared {base})" not in branch


def test_the_password_is_published_before_the_keystore(entrypoint):
    fn = entrypoint[entrypoint.index("share_keystore() {"):]
    fn = fn[:fn.index("\n}")]
    assert fn.index('cp "$2"') < fn.index('mv "/shared/.')

