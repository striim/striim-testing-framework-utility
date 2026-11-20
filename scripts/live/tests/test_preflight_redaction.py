"""The pre-flight redactor and the URL credential strip.

Pre-flight writes a service's failure reason to the outcomes file and the log through
``service_healing._redact``. It masked ``password=x`` only: a quoted key, credentials in a URL and an
``IDENTIFIED BY`` clause came through, and a known secret in any other wording did too. It now runs
the evidence redactor (known secret values, URL credentials) before its own key patterns.
``strip_userinfo`` stopped at the first ``@`` of a password containing one.
"""
from __future__ import annotations

import pytest

from livetest import evidence, infra
from livetest.service_healing import _redact


@pytest.mark.parametrize("text", [
    "password=hunter2",
    '{"password": "hunter2"}',
    "{'password': 'hunter2'}",
    "PWD=hunter2;UID=qa",
    "could not connect to postgresql://qa:hunter2@db:5432/x",
    "ALTER USER qa IDENTIFIED BY hunter2",
    'CREATE USER qa IDENTIFIED BY "hunter2"',
    "connect http://admin:p@hunter2@host:9080/x failed",
])
def test_preflight_reason_does_not_carry_a_credential(text):
    assert "hunter2" not in _redact(text)


def test_a_known_secret_is_redacted_in_any_wording(monkeypatch):
    monkeypatch.setenv("SLT_PG_PASSWORD", "hunter2")
    assert "hunter2" not in _redact("Login failed. PWD is hunter2")


def test_the_reason_keeps_its_words_around_a_redacted_value():
    out = _redact('{"password": "hunter2", "host": "db"}')
    assert '"host": "db"' in out and "password" in out
    assert _redact("connection refused: db:5432") == "connection refused: db:5432"


def test_the_reason_is_still_capped():
    assert len(_redact("x" * 10000)) == 4096
    assert len(_redact("x" * 10000, 65536)) == 10000


@pytest.mark.parametrize("url, stripped", [
    ("http://admin:p@ss@host:9080", "http://host:9080"),
    ("http://admin:pass@host:9080", "http://host:9080"),
    ("http://admin:p@ss@host:9080/a@b", "http://host:9080/a@b"),
    ("http://host:9080/path?mail=a@b.c", "http://host:9080/path?mail=a@b.c"),
    ("http://host:9080?mail=a@b.c", "http://host:9080?mail=a@b.c"),
    ("http://host:9080", "http://host:9080"),
])
def test_strip_userinfo_removes_everything_up_to_the_last_at_of_the_authority(url, stripped):
    assert infra.strip_userinfo(url) == stripped


def test_evidence_redaction_strips_a_password_containing_at():
    assert evidence.redact_text("jdbc postgresql://qa:p@ss@db:5432/x", [], home="", hostname="") \
        == "jdbc postgresql://db:5432/x"
