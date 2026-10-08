"""The agent container must WAIT for its log, not assume it after a fixed sleep.

`tail` is the agent container's last command, so PID 1's exit status IS tail's. The entrypoint
used to run `sleep 5; tail -f striim.agent.log` — and that log is created by the JVM itself,
measured at 3.5-4s after launch on an idle host. Under load that crosses 5s, tail exits 1 with
"cannot open ... No such file", and the container dies reporting a failure that is really a slow
start. It then succeeds on a plain `docker start`, which is what made it read as flakiness rather
than as a fixed-timeout bug — and it cost real time being misdiagnosed as JVM heap exhaustion.

The margin was about one second, so a reversion would look fine on an idle machine and come back
under load. Nothing else in this suite can catch that: the entrypoint runs inside an image, so
these are source-level assertions on the shipped file.
"""
from pathlib import Path

import pytest

_ENTRYPOINT = (Path(__file__).resolve().parents[1]
               / "services" / "striim" / "images" / "striim" / "files" / "entrypoint.sh")


@pytest.fixture(scope="module")
def agent_branch() -> str:
    """The ROLE=agent branch with COMMENTS STRIPPED.

    Stripping matters twice over. The first version of this file asserted against the raw text
    and immediately tripped on its own explanation — the comment quotes the old `sleep 5;
    tail -f` form, so an index-based slice ended inside the prose describing the bug rather
    than at the command. And every assertion below would otherwise be satisfiable by a comment
    mentioning `kill -0`, which is not a liveness check.

    The node branches legitimately sleep before tailing (their log is created by the launcher,
    not the JVM), so only the agent branch is in scope.
    """
    text = _ENTRYPOINT.read_text()
    start = text.index('elif [ "${ROLE}" == "agent" ]; then')
    lines = [ln for ln in text[start:].splitlines() if not ln.lstrip().startswith("#")]
    return "\n".join(lines)


def test_the_agent_waits_for_its_log_rather_than_sleeping(agent_branch):
    assert "while [ ! -f " in agent_branch, "the wait loop is gone"
    assert "${AGENT_LOG}" in agent_branch
    # The specific regression: a fixed sleep as the only thing between launch and tail.
    launch = agent_branch.index("striim-agent start")
    tail = agent_branch.index("follow ")     # follow() backgrounds the tail
    between = agent_branch[launch:tail]
    assert "while [ ! -f " in between, (
        "nothing waits for the log between starting the agent and tailing it — this is the "
        "`sleep 5; tail` race coming back"
    )


def test_the_wait_is_bounded_and_the_bound_is_overridable(agent_branch):
    # Unbounded would turn a dead JVM into a container that hangs instead of failing.
    assert "AGENT_LOG_WAIT_SECONDS" in agent_branch
    assert "WAIT_LIMIT" in agent_branch


def test_a_dead_jvm_is_detected_rather_than_waited_out(agent_branch):
    # sbin/striim-agent runs bin/agent.sh in the FOREGROUND, so the backgrounded $! stays alive
    # for the JVM's lifetime and `kill -0` is a true liveness check. Without this the container
    # would wait the full bound on a JVM that died in the first second.
    assert "kill -0" in agent_branch
    assert "AGENT_PID" in agent_branch


def test_a_failure_reports_the_jvms_own_output(agent_branch):
    # agent.sh sends stdout+stderr to striim-agent-system.log — where a real failure such as
    # "Could not reserve enough space for object heap" appears. The old form printed tail's
    # complaint about a missing file, which says nothing about why, and that single missing
    # detail is what sent an earlier investigation down a wrong path for hours.
    assert "striim-agent-system.log" in agent_branch
    assert "AGENT FAILED TO START" in agent_branch


def test_the_failure_paths_exit_non_zero(agent_branch):
    # A container that logs a failure and then tails anyway is the silent-pass shape again.
    failures = agent_branch.count('echo "AGENT FAILED TO START')
    assert failures == 2, "expected the dead-process and timed-out paths"
    assert agent_branch.count("exit 1") >= failures
