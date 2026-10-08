from __future__ import annotations

import json
from pathlib import Path
from unittest import mock

from inttest import harness
from inttest.manifest import TargetSpec


def test_harness_drive_defaults_to_utc_timezone(tmp_path):
    op_jar = tmp_path / "op.jar"
    op_jar.write_text("")
    harness_jar = tmp_path / "harness.jar"
    harness_jar.write_text("")

    captured_argv: list[str] = []

    def fake_run(argv, **kwargs):
        captured_argv.extend(argv)
        req_file = Path(argv[-1])
        req = json.loads(req_file.read_text())
        Path(req["outputFile"]).write_text("[]")
        res = mock.MagicMock()
        res.returncode = 0
        return res

    with mock.patch("inttest.harness.ensure_harness_jar", return_value=harness_jar), \
         mock.patch("inttest.harness._resolve_java", return_value="java"), \
         mock.patch("subprocess.run", side_effect=fake_run):
        harness.drive(op_jar, {}, [])

    assert captured_argv[-2] == "com.striim.testing.inttest.IntegrationProcessor"
    assert "-Duser.timezone=UTC" in captured_argv


def test_harness_drive_respects_explicit_target_timezone(tmp_path):
    op_jar = tmp_path / "op.jar"
    op_jar.write_text("")
    harness_jar = tmp_path / "harness.jar"
    harness_jar.write_text("")

    captured_argv: list[str] = []

    def fake_run(argv, **kwargs):
        captured_argv.extend(argv)
        req_file = Path(argv[-1])
        req = json.loads(req_file.read_text())
        Path(req["outputFile"]).write_text("{}")
        res = mock.MagicMock()
        res.returncode = 0
        return res

    target = TargetSpec(input="in.json", input_path=tmp_path / "in.json", timezone="Asia/Tokyo")

    with mock.patch("inttest.harness.ensure_harness_jar", return_value=harness_jar), \
         mock.patch("inttest.harness._resolve_java", return_value="java"), \
         mock.patch("subprocess.run", side_effect=fake_run):
        harness.drive(op_jar, {}, [], target=target)

    assert "-Duser.timezone=Asia/Tokyo" in captured_argv
    assert "-Duser.timezone=UTC" not in captured_argv
