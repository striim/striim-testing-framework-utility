"""`--parallel N` makes the controller an xdist controller with N workers, and nothing else: an xdist
worker receives the same options, and must not turn them into xdist options again (on
a Linux test host every worker spawned N workers of its own, recursively, until the host ran out of processes).

The run is a real pytest subprocess over two trivial non-live tests. Its conftest records each
process's depth (controller 1, worker 2) and ends any deeper process at once, and the subprocess has a
timeout, so even a regression stays bounded here."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from tests import _hermetic_child

LIVE = Path(__file__).resolve().parents[1]

CONFTEST = '''
import json, os
from pathlib import Path

def pytest_configure(config):
    depth = int(os.environ.get("XTR_PARALLEL_DEPTH", "0")) + 1
    os.environ["XTR_PARALLEL_DEPTH"] = str(depth)
    rec = {"pid": os.getpid(), "depth": depth, "worker": os.environ.get("PYTEST_XDIST_WORKER"),
           "numprocesses": getattr(config.option, "numprocesses", None)}
    (Path(os.environ["XTR_PARALLEL_OUT"]) / f"{os.getpid()}.json").write_text(json.dumps(rec))
    if depth > 2:
        os._exit(3)          # a worker of a worker: the bug; end it before it spawns anything
'''


def test_parallel_2_starts_exactly_two_workers_and_no_nested_ones(tmp_path):
    out = tmp_path / "procs"
    out.mkdir()
    (tmp_path / "conftest.py").write_text(CONFTEST)
    (tmp_path / "test_tiny.py").write_text("def test_a():\n    pass\n\ndef test_b():\n    pass\n")
    base = {k: v for k, v in os.environ.items()
            if not (k.startswith(("SLT_", "PYTEST_", "XTR_")) or k in ("STRIIM_URL", "PYTHONPATH"))}
    # the plugin set striim-test gives a live child: autoload off (child_env's default), xdist and livetest by -p
    env = _hermetic_child.child_env(tmp_path / "no-such-settings-file", base=base,
                                    PYTHONPATH=str(LIVE), XTR_PARALLEL_OUT=str(out), PYTHONDONTWRITEBYTECODE="1")
    r = subprocess.run([sys.executable, "-m", "pytest", "-p", "livetest.plugin", "-p", "xdist.plugin",
                        "--parallel", "2", "-o", "addopts=", "-p", "no:cacheprovider", "-q", str(tmp_path)],
                       cwd=tmp_path, env=env, capture_output=True, text=True, timeout=120)
    recs = [json.loads(p.read_text()) for p in out.glob("*.json")]
    by_depth = {d: [x for x in recs if x["depth"] == d] for d in (1, 2, 3)}
    assert by_depth[3] == [], f"nested workers started: {by_depth[3]}\n{r.stdout[-2000:]}"
    assert len(by_depth[1]) == 1 and by_depth[1][0]["numprocesses"] == 2, recs
    assert sorted(x["worker"] for x in by_depth[2]) == ["gw0", "gw1"], recs
    assert all(not x["numprocesses"] for x in by_depth[2]), recs     # no worker is an xdist controller
    assert r.returncode == 0, r.stdout[-2000:] + r.stderr[-2000:]
    assert "2 passed" in r.stdout
