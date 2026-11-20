"""A bare `expect_halt: true` (no `expect_halt_contains`) accepts any terminal status, and with no
post-start work any startup failure, a TQL syntax error included. It is deprecated: loading one
warns, `striim-test doctor` shows the warning, and a later release refuses it."""
from __future__ import annotations

import warnings

import pytest

from livetest.manifest import ManifestError, load_manifest

BASE = "name: h\ntql: app.tql\nexpect_halt: true\n"


def _write(tmp_path, body):
    (tmp_path / "app.tql").write_text("-- x\n")
    p = tmp_path / "test.yaml"
    p.write_text(body)
    return p


def _deprecations(path):
    with warnings.catch_warnings(record=True) as seen:
        warnings.simplefilter("always")
        m = load_manifest(path)
    return m, [w for w in seen if issubclass(w.category, DeprecationWarning)]


def test_bare_expect_halt_is_deprecated(tmp_path):
    path = _write(tmp_path, BASE)
    m, seen = _deprecations(path)
    assert m.expect_halt is True                      # still loads: refused in a later release
    assert len(seen) == 1
    msg = str(seen[0].message)
    assert msg.startswith(f"{path}: ")
    assert "expect_halt_contains" in msg and "later release" in msg


def test_expect_halt_with_contains_is_not_deprecated(tmp_path):
    _m, seen = _deprecations(_write(tmp_path, BASE + "expect_halt_contains: ORA-00001\n"))
    assert seen == []


def test_no_expect_halt_is_not_deprecated(tmp_path):
    _m, seen = _deprecations(_write(tmp_path, "name: h\ntql: app.tql\nassert:\n  smoke: true\n"))
    assert seen == []


def test_expect_halt_false_is_not_deprecated(tmp_path):
    _m, seen = _deprecations(_write(tmp_path, "name: h\ntql: app.tql\nexpect_halt: false\n"
                                              "assert:\n  smoke: true\n"))
    assert seen == []


def test_contains_without_expect_halt_is_still_refused(tmp_path):
    with pytest.raises(ManifestError, match="requires 'expect_halt: true'"):
        load_manifest(_write(tmp_path, "name: h\ntql: app.tql\nexpect_halt_contains: x\n"
                                       "assert:\n  smoke: true\n"))
