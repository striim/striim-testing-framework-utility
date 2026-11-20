"""A root test never takes a real stack's lock: the machine-wide lock dir is the run's own."""
from __future__ import annotations

import os
from pathlib import Path


def test_the_lock_dir_is_this_runs_own_in_process_and_for_children():
    from livetest import stack
    assert os.environ["SLT_LOCK_DIR"] == str(stack._LOCK_DIR)
    assert stack._LOCK_DIR != Path("/tmp/slt-locks")
    assert stack._LOCK_DIR.is_dir() and stack._LOCK_DIR.name.startswith("slt-locks")
