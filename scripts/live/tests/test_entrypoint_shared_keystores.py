"""The primary clears the previous cluster's files out of /shared before publishing its own.

/shared is the slt-striim-shared volume. `stop` removes it with `down -v`, but Docker keeps a
volume another container still uses: on a Mac an old agent container held it, so
it still had sks.jks and aks.jks from a 5.4.0.6 cluster. 5.4.2 writes sks.p12, keystore_path
prefers .jks, and the node copied the stale keystore with the new password: "Keystore could not
be opened: integrity check failed ... Server Beginning Shutdown", and the cluster never formed.
"""
import re
import subprocess
from pathlib import Path

_ENTRYPOINT = (Path(__file__).resolve().parents[1]
               / "services" / "striim" / "images" / "striim" / "files" / "entrypoint.sh")

_PUBLISHED = ("sks.jks", "sks.p12", "sksKey.pwd", "aks.jks", "aks.p12", "aksKey.pwd",
              "startUp.properties", "server.sh")


def _function(name) -> str:
    m = re.search(rf"^{name}\(\) \{{\n.*?^\}}\n", _ENTRYPOINT.read_text(), re.S | re.M)
    assert m, f"entrypoint.sh has no {name}() helper"
    return m.group(0)


def _primary_branch() -> list:
    text = _ENTRYPOINT.read_text()
    start = text.index('if [ "${ROLE}" == "primary" ]; then')
    end = text.index('elif [ "${ROLE}" == "node" ]; then')
    return [ln.strip() for ln in text[start:end].splitlines()
            if ln.strip() and not ln.lstrip().startswith("#")]


def test_clear_shared_removes_everything_the_primary_publishes_and_nothing_else(tmp_path):
    for name in _PUBLISHED + ("unrelated.txt",):
        (tmp_path / name).write_text("stale")
    subprocess.run(["bash", "-c", _function("clear_shared") + 'clear_shared "$1"', "_",
                    str(tmp_path)], check=True)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["unrelated.txt"]


def test_clear_shared_on_an_empty_volume_succeeds(tmp_path):
    subprocess.run(["bash", "-c", _function("clear_shared") + 'clear_shared "$1"', "_",
                    str(tmp_path)], check=True)


def test_after_clearing_the_node_finds_the_new_keystore(tmp_path):
    (tmp_path / "sks.jks").write_text("stale 5.4.0.6")
    script = (_function("clear_shared") + _function("keystore_path")
              + 'clear_shared "$1"; echo new > "$1/sks.p12"; keystore_path "$1" sks')
    out = subprocess.run(["bash", "-c", script, "_", str(tmp_path)], capture_output=True,
                         text=True, check=True).stdout.strip()
    assert out == f"{tmp_path}/sks.p12"


def test_the_primary_clears_shared_before_it_generates_or_publishes_anything():
    lines = _primary_branch()
    clear = [i for i, ln in enumerate(lines) if ln == "clear_shared /shared"]
    assert clear, "the primary never clears /shared"
    first_keystore = next(i for i, ln in enumerate(lines) if "sksConfig.sh" in ln)
    first_publish = next(i for i, ln in enumerate(lines) if ln.startswith("cp ") and "/shared" in ln)
    assert clear[0] < first_keystore and clear[0] < first_publish
