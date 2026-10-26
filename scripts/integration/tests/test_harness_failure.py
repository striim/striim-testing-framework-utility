"""drive() hands back the operator's exception chain apart from everything it printed."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest import mock

import pytest

from inttest import harness


def _drive(tmp_path, fake_run):
    op_jar = tmp_path / "op.jar"
    op_jar.write_text("")
    with mock.patch("inttest.harness.ensure_harness_jar", return_value=tmp_path / "h.jar"), \
         mock.patch("inttest.harness._resolve_java", return_value="java"), \
         mock.patch("subprocess.run", side_effect=fake_run):
        return harness.drive(op_jar, {}, [], timeout=5)


def test_a_failed_run_carries_the_exception_chain_it_wrote(tmp_path):
    def fake_run(argv, **kwargs):
        req = json.loads(Path(argv[-1]).read_text())
        Path(req["errorFile"]).write_text("java.lang.IllegalStateException: refused\n")
        return mock.MagicMock(returncode=1, stdout="WARN refused? no: retrying", stderr="trace")

    with pytest.raises(harness.HarnessError) as e:
        _drive(tmp_path, fake_run)
    assert e.value.failure == "java.lang.IllegalStateException: refused\n"
    assert "WARN refused? no: retrying" in str(e.value)


def test_a_run_that_wrote_no_chain_has_none(tmp_path):
    with pytest.raises(harness.HarnessError) as e:
        _drive(tmp_path, lambda argv, **kw: mock.MagicMock(returncode=1, stdout="", stderr="died"))
    assert e.value.failure is None


def test_a_timeout_has_no_chain(tmp_path):
    def fake_run(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, 5, output="refused", stderr="")

    with pytest.raises(harness.HarnessError) as e:
        _drive(tmp_path, fake_run)
    assert e.value.failure is None
