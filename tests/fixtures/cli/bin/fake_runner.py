"""Synthetic C2 external runner. Usage: fake_runner.py <mode> <report> [<pidfile>]

Modes: pass, fail-exit, exit0-report-fail, exit1-report-pass, timeout, missing-report,
stale-report, zero-tests, malformed, skip, env-dump, marker.
"""
import json
import os
import subprocess
import sys
import time

mode, report = sys.argv[1], sys.argv[2]


def junit(cases):
    body = "".join(f'<testcase classname="synthetic" name="{n}">{inner}</testcase>' for n, inner in cases)
    with open(report, "w") as f:
        f.write(f'<?xml version="1.0"?><testsuite name="synthetic" tests="{len(cases)}">{body}</testsuite>')


if mode == "pass":
    junit([("a", ""), ("b", "")])
elif mode == "fail-exit":
    sys.exit(1)
elif mode == "exit0-report-fail":
    junit([("a", ""), ("b", '<failure message="synthetic"/>')])
elif mode == "exit1-report-pass":
    junit([("a", "")])
    sys.exit(1)
elif mode == "timeout":
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"])
    with open(sys.argv[3], "w") as f:
        f.write(str(child.pid))
    time.sleep(300)
elif mode in ("missing-report", "stale-report"):
    pass
elif mode == "zero-tests":
    junit([])
elif mode == "malformed":
    with open(report, "w") as f:
        f.write("<testsuite name='synthetic' tests='1'><testcase")
elif mode == "skip":
    junit([("a", ""), ("b", "<skipped/>")])
elif mode == "env-dump":
    junit([("a", "")])
    with open(sys.argv[3], "w") as f:
        json.dump({"argv": sys.argv[1:], "EXPANDED": os.environ.get("EXPANDED"),
                   "LITERAL": os.environ.get("LITERAL")}, f)
elif mode == "marker":
    junit([("a", "")])
    with open(sys.argv[3], "w") as f:
        f.write("launched")
else:
    sys.exit(f"unknown mode {mode}")
