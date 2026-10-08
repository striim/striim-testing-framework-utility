"""An ownership ledger is kept in the configured state dir (livetest.layout), not only the path-key one."""
from livetest import layout, ownership, runident


def _ident():
    return runident.derive("hello_case", {"SLT_RUN_EPOCH": "rv-run"})


def _open(tmp_path, **kw):
    layout._reset()
    try:
        return ownership.Ledger.open(_ident(), **kw)
    finally:
        layout._reset()


def test_ledger_follows_set_roots_state(tmp_path):
    layout._reset()
    try:
        layout.set_roots(state=tmp_path / "configured")
        led = ownership.Ledger.open(_ident(), env={})
    finally:
        layout._reset()
    assert led.path.parent == (tmp_path / "configured").resolve() / "lifecycle" / "ledgers"


def test_ledger_follows_the_manifest_state_dir(tmp_path):
    layout._reset()
    try:
        layout.set_manifest_roots(state=tmp_path / "manifest")
        led = ownership.Ledger.open(_ident(), env={})
    finally:
        layout._reset()
    assert led.path.parent == (tmp_path / "manifest").resolve() / "lifecycle" / "ledgers"


def test_ledger_follows_slt_state_dir_passed_as_env(tmp_path):
    led = _open(tmp_path, env={"SLT_STATE_DIR": str(tmp_path)})
    assert led.path.parent == tmp_path.resolve() / "lifecycle" / "ledgers"


def test_explicit_state_dir_argument_wins(tmp_path):
    layout._reset()
    try:
        layout.set_roots(state=tmp_path / "configured")
        led = ownership.Ledger.open(_ident(), state_dir=tmp_path / "explicit", env={})
    finally:
        layout._reset()
    assert led.path.parent == tmp_path / "explicit" / "lifecycle" / "ledgers"
