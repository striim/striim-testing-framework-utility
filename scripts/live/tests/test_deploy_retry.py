"""The TQL-import retry for a server-side NPE.

Striim occasionally fails one statement of an import with a Java NPE -- seen in a clean
parallel run as `MetaInfo$User.isUserActive() ... because "u" is null` on CREATE SOURCE,
with the three statements before it succeeding on the same session. It is server state, not
the TQL: the test passes standalone and on re-run. These pin that the retry is narrow (only
that signature), that it drops the namespace first (CREATE NAMESPACE is not idempotent, so a
bare re-import would fail with "already exists" and bury the original error), and that it
gives up after one retry.
"""
from types import SimpleNamespace

import pytest

from livetest import plugin
from livetest.striim import StriimError

_NPE = ('"CREATE OR REPLACE SOURCE PgSource ..." failed: Cannot invoke '
        '"com.webaction.runtime.meta.MetaInfo$User.isUserActive()" because "u" is null')


def _client(failures):
    """A client whose deploy_tql raises the next queued exception (None = success)."""
    calls = {"deploy": 0, "teardown": []}
    queue = list(failures)

    def deploy(tql):
        calls["deploy"] += 1
        exc = queue.pop(0) if queue else None
        if exc:
            raise exc

    return SimpleNamespace(deploy_tql=deploy,
                           teardown_namespace=lambda ns: calls["teardown"].append(ns)), calls


def test_a_clean_import_does_not_retry_or_touch_the_namespace():
    client, calls = _client([None])
    plugin._deploy_tql_once_retrying(client, "TQL", "SLT_ns", "t")
    assert calls["deploy"] == 1 and calls["teardown"] == []


def test_a_server_npe_drops_the_namespace_and_retries_once():
    client, calls = _client([StriimError(_NPE), None])
    plugin._deploy_tql_once_retrying(client, "TQL", "SLT_ns", "t")
    assert calls["deploy"] == 2
    # Dropped BEFORE the second import: the failed one left the namespace and app behind.
    assert calls["teardown"] == ["SLT_ns"]


def test_a_genuine_tql_error_is_not_retried():
    # The failure this branch actually shipped a fix for -- a stale pipeline verb. Retrying it
    # would double every such failure's runtime and say nothing new.
    real = StriimError("'op lowercase:data' failed: expected 1 segment(s) but got 2")
    client, calls = _client([real, None])
    with pytest.raises(StriimError, match="expected 1 segment"):
        plugin._deploy_tql_once_retrying(client, "TQL", "SLT_ns", "t")
    assert calls["deploy"] == 1 and calls["teardown"] == []


def test_a_second_npe_propagates():
    # A deterministic server fault must still fail the test, not loop.
    client, calls = _client([StriimError(_NPE), StriimError(_NPE)])
    with pytest.raises(StriimError, match="isUserActive"):
        plugin._deploy_tql_once_retrying(client, "TQL", "SLT_ns", "t")
    assert calls["deploy"] == 2


def test_the_pattern_matches_the_npe_shape_not_the_one_method():
    # Scoped to the NPE phrasing so the next variant of the same race is covered.
    assert plugin._TRANSIENT_IMPORT_NPE.search(_NPE)
    assert plugin._TRANSIENT_IMPORT_NPE.search(
        'Cannot invoke "com.webaction.Foo.bar()" because "ctx" is null')
    assert not plugin._TRANSIENT_IMPORT_NPE.search("Table qasource.x does not exist")
