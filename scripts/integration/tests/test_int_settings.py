"""Integration-tier settings read in one place each: INT_KEEP_RESOURCES (keep a passed case's
per-test resources) and INT_EXTRA_CLASSPATH (drivers the op jar cannot bundle)."""
from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

from inttest import harness, plugin


def _request(*, failed=False, flag=False):
    rep = SimpleNamespace(failed=failed)
    return SimpleNamespace(node=SimpleNamespace(rep_setup=SimpleNamespace(failed=False), rep_call=rep),
                           config=SimpleNamespace(getoption=lambda name, default=None: flag))


def test_a_passed_case_drops_its_resources_by_default(monkeypatch):
    monkeypatch.delenv("INT_KEEP_RESOURCES", raising=False)
    assert plugin._keep_resources(_request()) is False


def test_int_keep_resources_keeps_a_passed_cases_resources(monkeypatch):
    monkeypatch.setenv("INT_KEEP_RESOURCES", "1")
    assert plugin._keep_resources(_request()) is True


@pytest.mark.parametrize("failed, flag", [(True, False), (False, True)])
def test_a_failure_or_the_flag_keeps_them_without_the_setting(monkeypatch, failed, flag):
    monkeypatch.delenv("INT_KEEP_RESOURCES", raising=False)
    assert plugin._keep_resources(_request(failed=failed, flag=flag)) is True


def test_int_extra_classpath_appends_each_entry_in_order(monkeypatch):
    monkeypatch.delenv("INT_EXTRA_CLASSPATH", raising=False)
    base = harness._extra_jars()
    monkeypatch.setenv("INT_EXTRA_CLASSPATH", os.pathsep.join(["/drivers/a.jar", "", "/drivers/b.jar"]) + " ")
    assert harness._extra_jars() == base + ["/drivers/a.jar", "/drivers/b.jar"]


def test_int_extra_classpath_unset_or_blank_adds_nothing(monkeypatch):
    monkeypatch.delenv("INT_EXTRA_CLASSPATH", raising=False)
    base = harness._extra_jars()
    monkeypatch.setenv("INT_EXTRA_CLASSPATH", "   ")
    assert harness._extra_jars() == base
