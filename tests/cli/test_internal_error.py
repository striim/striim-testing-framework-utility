"""A tier that crashes inside pytest (exit 3, INTERNALERROR) names the error on the console, not only
``pytest-exit:3``: on a Linux test host the PermissionError was only in live/stdout.log."""
import sys
from types import SimpleNamespace

from _clikit import REPO

sys.path.insert(0, str(REPO / "scripts" / "cli"))

from striim_test import dispatch  # noqa: E402

CRASH = r'''
import sys
print("collected 1 item")
print("INTERNALERROR> Traceback (most recent call last):")
print("INTERNALERROR>   File \"x.py\", line 85, in _acquire_native")
print("INTERNALERROR> PermissionError: [Errno 13] Permission denied: '/tmp/slt-locks/a.lock'")
print("INTERNALERROR> ")
print("INTERNALERROR> During handling of the above exception, another exception occurred:")
print("INTERNALERROR> OSError: second")
sys.exit(3)
'''


def test_internal_error_names_the_first_exception_line(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(dispatch, "build_pytest_argv",
                        lambda *a, **k: ([sys.executable, "-c", CRASH], {"SLT_RUN_EPOCH": "e",
                                                                          "SLT_INVOCATION_ID": "i",
                                                                          "SLT_RUN_IDENTITY": "r"}))
    out = dispatch.run_tier(SimpleNamespace(run=tmp_path), "live", tmp_path, label="live", collect_only=False)
    assert (out.returncode, out.reason) == (3, "pytest-exit:3")
    err = capsys.readouterr().err
    assert ("live: pytest-exit:3 (exit 2) PermissionError: [Errno 13] Permission denied: "
            "'/tmp/slt-locks/a.lock' [logs: ") in err


def test_no_internal_error_leaves_the_line_as_it_was(tmp_path):
    (tmp_path / "stdout.log").write_text("nothing useful\n")
    assert dispatch.internal_error(tmp_path) == ""
    assert dispatch.internal_error(tmp_path / "absent") == ""
