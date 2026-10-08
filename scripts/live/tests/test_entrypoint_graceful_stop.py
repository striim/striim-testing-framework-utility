"""`docker stop` must stop Striim gracefully, not SIGKILL it after compose's timeout.

The entrypoint is PID 1. Its last command was a foreground `tail -f ... | redact_stream`, and
bash as PID 1 has no default action for SIGTERM and runs no trap while a foreground command
runs, so the signal was dropped: every node and agent was SIGKILLed after the stop timeout and
exited 137. The fix traps TERM/INT, runs each Striim start in its own
process group, and on the signal stops the groups, then lets the redaction filter drain.

The entrypoint runs inside the image, so this test runs the shipped file's own lines with bash:
everything before the role branches (the helpers, the redirect through redact_stream, the
trap), then the node branch's start, sleep and tail, with the Striim launcher swapped for a
fake JVM that records the SIGTERM it gets. The old entrypoint fails it: without a trap, bash
dies on the signal and the fake JVM never sees it.
"""
import os
import re
import shutil
import signal
import subprocess
import time
from pathlib import Path

import pytest

_ENTRYPOINT = (Path(__file__).resolve().parents[1]
               / "services" / "striim" / "images" / "striim" / "files" / "entrypoint.sh")

# A launcher shaped like sbin/striim-node: a bash wrapper whose child JVM writes the log.
_FAKE_LAUNCHER = """\
#!/bin/bash
cat > "$1/jvm.sh" <<'JVM'
#!/bin/bash
trap 'echo "Shutting down; ProductKey=PK-STOP-1234" >> "$1/striim-node.log"; touch "$1/hook-ran"; exit 0' TERM
echo "Please go to http://node:9080 PK-STOP-1234" >> "$1/striim-node.log"
while :; do sleep 0.1; done
JVM
chmod +x "$1/jvm.sh"
"$1/jvm.sh" "$1"
"""


def _harness(tmp: Path, shorten: bool = True) -> str:
    text = _ENTRYPOINT.read_text()
    preamble = text[:text.index('if [ "${ROLE}" == "primary" ]; then')]
    node = text[text.index('elif [ "${ROLE}" == "node" ]; then'):
                text.index('elif [ "${ROLE}" == "agent" ]; then')]
    start = node.index('echo "Starting node"')
    body = node[start:]
    body = body.replace("/opt/striim/sbin/striim-node start", f"{tmp}/launcher {tmp}")
    body = body.replace("/opt/striim/logs/", f"{tmp}/")
    if shorten:
        body = body.replace("sleep 5", "sleep 0.5").replace("pause 5", "pause 0.5")
    return preamble.replace("rm -rf /opt/striim/elasticsearch/data/* /opt/striim/logs/*", "") + body


# These two RUN the entrypoint, which starts each role under `setsid` -- a util-linux tool the
# Linux container has and macOS does not. Without it the role never starts and the test fails
# for the host, not the entrypoint, so it is skipped with the reason stated.
_NEEDS_SETSID = pytest.mark.skipif(
    shutil.which("setsid") is None,
    reason="needs setsid (util-linux) to run the container entrypoint; not on this host (macOS)")


@_NEEDS_SETSID
def test_sigterm_stops_striim_through_the_redaction_pipe(tmp_path):
    launcher = tmp_path / "launcher"
    launcher.write_text(_FAKE_LAUNCHER)
    launcher.chmod(0o755)
    (tmp_path / "harness.sh").write_text(_harness(tmp_path))
    p = subprocess.Popen(["bash", str(tmp_path / "harness.sh")], stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT,
                         env={"PATH": os.environ["PATH"], "PRODUCT_KEY": "PK-STOP-1234"})
    try:
        deadline = time.time() + 10
        while not (tmp_path / "striim-node.log").exists() and time.time() < deadline:
            time.sleep(0.1)
        time.sleep(1.0)                      # past the (shortened) sleep, into the tail
        t0 = time.time()
        p.send_signal(signal.SIGTERM)
        out, _ = p.communicate(timeout=10)
        took = time.time() - t0
    finally:
        if p.poll() is None:
            p.kill()
            p.wait()
    out = out.decode()
    assert (tmp_path / "hook-ran").exists(), f"the JVM never got SIGTERM:\n{out}"
    assert p.returncode == 143, (p.returncode, out)
    assert took < 5, took
    assert "Please go to http://node:9080" in out, "boot line lost"
    assert "Shutting down" in out, "shutdown line lost: the filter did not drain"
    assert "PK-STOP-1234" not in out and "<redacted>" in out


def test_the_trap_is_installed_before_any_role_starts_striim():
    code = [ln.strip() for ln in _ENTRYPOINT.read_text().splitlines()
            if not ln.lstrip().startswith("#")]
    trap = code.index("trap stop_striim TERM INT")
    first_role = next(i for i, ln in enumerate(code) if ln.startswith('if [ "${ROLE}"'))
    assert trap < first_role
    starts = [ln for ln in code if "sbin/striim-" in ln and " start " in ln]
    assert len(starts) == 4, starts        # dbms and node on the primary, the node, the agent
    for ln in starts:
        assert ln.startswith("setsid nohup "), ln
    assert sum(ln.startswith("follow ") for ln in code) == 3
    assert [ln for ln in code if ln.startswith("tail -f")] == ['tail -f "$1" &'], \
        "a foreground tail drops SIGTERM"


# A stop during start-up. bash defers a trap until the foreground command
# ends, so a foreground `sleep 5` (the node's boot wait; the primary's is 15 s plus two
# keystore JVM runs) held the stop past compose's 10 s timeout. The boot waits now run under
# `wait`, which the trapped signal interrupts at once.

@_NEEDS_SETSID
def test_sigterm_during_the_boot_wait_is_honoured_at_once(tmp_path):
    launcher = tmp_path / "launcher"
    launcher.write_text(_FAKE_LAUNCHER)
    launcher.chmod(0o755)
    (tmp_path / "harness.sh").write_text(_harness(tmp_path, shorten=False))
    p = subprocess.Popen(["bash", str(tmp_path / "harness.sh")], stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT,
                         env={"PATH": os.environ["PATH"], "PRODUCT_KEY": "PK-STOP-1234"})
    try:
        deadline = time.time() + 10
        while not (tmp_path / "striim-node.log").exists() and time.time() < deadline:
            time.sleep(0.1)
        time.sleep(0.5)                      # inside the node's 5 s boot wait
        t0 = time.time()
        p.send_signal(signal.SIGTERM)
        out, _ = p.communicate(timeout=10)
        took = time.time() - t0
    finally:
        if p.poll() is None:
            p.kill()
            p.wait()
    assert (tmp_path / "hook-ran").exists(), out.decode()
    assert p.returncode == 143 and took < 2.5, (p.returncode, took)


def test_no_boot_wait_runs_in_the_foreground():
    code = [ln.strip() for ln in _ENTRYPOINT.read_text().splitlines()
            if not ln.lstrip().startswith("#")]
    # outside the stop path (wait_group .. the trap), which runs with the trap disabled
    start = next(i for i, ln in enumerate(code) if ln.startswith("wait_group() {"))
    body = code[:start] + code[code.index("trap stop_striim TERM INT"):]
    fg = [ln for ln in body if re.search(r"(^|[;{]\s*|do\s+)sleep ", ln)]
    assert fg == [], fg                       # boot waits are `pause N`
    for tool in ("sksConfig.sh", "aksConfig.sh"):
        assert [ln for ln in code if tool in ln and not ln.startswith("fg_group ")] == []
