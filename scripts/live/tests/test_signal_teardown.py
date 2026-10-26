"""Signal cancellation of a live case uses its ordinary cleanup path."""

import signal
import time
from types import SimpleNamespace

import pytest

from livetest import plugin


def test_sigterm_mid_test_runs_app_teardown():
    calls = []

    class FakeStriim:
        def stop_application(self, app):
            calls.append(("stop", app))

        def undeploy_application(self, app):
            calls.append(("undeploy", app))

        def drop_application(self, app):
            calls.append(("drop", app))

    client = FakeStriim()

    def run_case():
        try:
            signal.raise_signal(signal.SIGTERM)
        finally:
            client.stop_application("NS.App")
            client.undeploy_application("NS.App")
            client.drop_application("NS.App")

    with pytest.raises(KeyboardInterrupt, match="SIGTERM"):
        plugin.LiveItem.runtest(SimpleNamespace(_runtest=run_case))
    assert calls == [("stop", "NS.App"), ("undeploy", "NS.App"), ("drop", "NS.App")]


def test_signal_teardown_timeout_bounds_hung_cleanup(monkeypatch):
    monkeypatch.setattr(plugin, "INTERRUPT_TEARDOWN_TIMEOUT", 0.05)

    def run_case():
        try:
            signal.raise_signal(signal.SIGTERM)
        finally:
            while True:
                time.sleep(0.01)

    start = time.monotonic()
    with pytest.raises(KeyboardInterrupt, match="teardown timed out"):
        plugin._interrupt_teardown(run_case, SimpleNamespace(_slt_cleanup_active=False))
    assert time.monotonic() - start < 1


def test_first_sigterm_inside_cleanup_finishes_app_teardown():
    calls = []
    case = SimpleNamespace(_slt_cleanup_active=False)

    def run_case():
        try:
            pass
        finally:
            case._slt_cleanup_active = True
            signal.raise_signal(signal.SIGTERM)
            for operation in ("stop", "undeploy", "drop"):
                calls.append((operation, "NS.App"))

    case._runtest = run_case
    with pytest.raises(KeyboardInterrupt, match="SIGTERM"):
        plugin.LiveItem.runtest(case)
    assert calls == [("stop", "NS.App"), ("undeploy", "NS.App"), ("drop", "NS.App")]


def test_first_signal_during_hung_cleanup_is_bounded(monkeypatch):
    monkeypatch.setattr(plugin, "INTERRUPT_TEARDOWN_TIMEOUT", 0.05)
    case = SimpleNamespace(_slt_cleanup_active=False)

    def run_case():
        try:
            pass
        finally:
            case._slt_cleanup_active = True
            signal.raise_signal(signal.SIGTERM)
            while True:
                time.sleep(0.01)

    case._runtest = run_case
    start = time.monotonic()
    with pytest.raises(KeyboardInterrupt, match="teardown timed out"):
        plugin.LiveItem.runtest(case)
    assert time.monotonic() - start < 1



def _case_with_pre_cleanup(close, calls):
    case = SimpleNamespace(_slt_cleanup_active=False)

    def run_case():
        try:
            pass
        finally:
            with plugin._pre_cleanup_budget(case):
                close()
            calls.append("ownership cleanup")
    case._runtest = run_case
    return case


def test_signal_during_a_pre_cleanup_close_still_runs_ownership_cleanup():
    calls = []

    def close():
        signal.raise_signal(signal.SIGTERM)
        calls.append("close finished")
    with pytest.raises(KeyboardInterrupt, match="SIGTERM"):
        plugin.LiveItem.runtest(_case_with_pre_cleanup(close, calls))
    assert calls == ["close finished", "ownership cleanup"]


def test_a_hung_pre_cleanup_close_cannot_spend_ownership_cleanups_budget(monkeypatch):
    monkeypatch.setattr(plugin, "INTERRUPT_TEARDOWN_TIMEOUT", 0.2)
    calls = []

    def close():
        signal.raise_signal(signal.SIGTERM)
        while True:
            time.sleep(0.01)
    case = _case_with_pre_cleanup(close, calls)
    inner = case._runtest

    def run_case():
        inner()
        time.sleep(0.15)                # ownership cleanup that needs most of a fresh budget
        calls.append("cleanup done")
    case._runtest = run_case
    with pytest.raises(KeyboardInterrupt, match="SIGTERM"):
        plugin.LiveItem.runtest(case)
    assert calls == ["ownership cleanup", "cleanup done"]
