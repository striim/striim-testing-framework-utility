"""The upgrade guide's exposure section matches what a run does to a stack it reuses.

Under SLT_INFRA_OWNERSHIP=shared a running service and a running cluster are reused as they are, never
recreated (livetest.infra.resolve_service, inttest.services.shared_reuse, the shared reuse decision in
livetest.plugin), so a kept stack keeps the ports and debugger it was started with. The guide used to say
the first bring-up after the upgrade recreated every running service container, which left a reader
believing their exposure had been narrowed.
"""
from __future__ import annotations

import re
import shlex
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
UPGRADING = (REPO / "docs" / "UPGRADING.md").read_text(encoding="utf-8")


def _section(heading: str) -> str:
    unreleased = re.split(r"^## ", UPGRADING, flags=re.M)[1]
    m = re.search(rf"^### {re.escape(heading)}.*?$(.*?)(?=^### |\Z)", unreleased, re.M | re.S)
    assert m, heading
    return m.group(1)


def test_the_guide_does_not_promise_that_a_bring_up_recreates_a_reused_stack():
    ports = " ".join(_section("Service ports bind locally").split())
    assert "first framework bring-up after the upgrade recreates" not in ports
    assert "SLT_INFRA_OWNERSHIP=shared" in ports
    assert "neither a run nor `livetest.cli start` recreates a running container" in ports
    debugger = " ".join(_section("JDWP debugger off by default").split())
    assert "a shared run never redeploys the cluster" in debugger


def test_each_migration_command_names_its_targets():
    # Both CLIs take one or more targets (nargs="+"): a bare `stop` is a usage error, not a stop.
    block = re.search(r"```bash\n(.*?)```", _section("Service ports bind locally"), re.S).group(1)
    commands = [c.strip() for line in block.splitlines() for c in line.split("#")[0].split("&&") if c.strip()]
    assert len(commands) == 4
    for command in commands:
        argv = shlex.split(command)
        assert argv[:2] == ["python", "-m"] and argv[2] in ("livetest.cli", "inttest.cli"), command
        assert argv[3] in ("start", "stop") and argv[4:] == ["all"], command
