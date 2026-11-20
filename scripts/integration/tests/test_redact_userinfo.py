"""URL credentials in integration failure text are stripped up to the last `@` of the authority.

The pattern stopped at the first `@`, so a password containing one kept its tail:
`postgresql://qa:p@ss@db:5432/x` came out as `postgresql://ss@db:5432/x`. It now matches the live
engine's redactor (`livetest.evidence._USERINFO`): the authority ends at the first `/`, `?` or `#`.
"""
from __future__ import annotations

import pytest

from inttest import redact


@pytest.mark.parametrize("text, stripped", [
    ("could not connect to postgresql://qa:p@ss@db:5432/x", "could not connect to postgresql://db:5432/x"),
    ("jdbc:oracle:thin://sys:a@b@c@db:1521/XE failed", "jdbc:oracle:thin://db:1521/XE failed"),
    ("http://admin:pass@host:9080", "http://host:9080"),
    ("http://admin:p@ss@host:9080/a@b", "http://host:9080/a@b"),
    ("http://host:9080/path?mail=a@b.c", "http://host:9080/path?mail=a@b.c"),
    ("http://host:9080?mail=a@b.c", "http://host:9080?mail=a@b.c"),
    ('{"url": "http://u:p@ss@h:1/x", "next": "a@b"}', '{"url": "http://h:1/x", "next": "a@b"}'),
    ("http://host:9080", "http://host:9080"),
])
def test_url_credentials_are_stripped_to_the_last_at_of_the_authority(text, stripped):
    assert redact.redact_text(text, []) == stripped


def test_the_pattern_is_the_live_engines():
    """Each engine imports alone, so the pattern is duplicated; the two copies must stay equal."""
    import ast
    import re
    from pathlib import Path
    live = (Path(__file__).resolve().parents[2] / "live" / "livetest" / "evidence.py").read_text()
    m = re.search(r'^_USERINFO = re\.compile\((.*)\)$', live, re.M)
    assert m, "livetest.evidence._USERINFO not found"
    assert redact._USERINFO.pattern == ast.literal_eval(m.group(1))
