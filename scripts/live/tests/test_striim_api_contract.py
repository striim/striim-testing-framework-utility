import sys

import pytest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "tools" / "python"))
import striim_api


def test_undeploy_passes_app_name_to_status(monkeypatch):
    calls = {"get": None, "delete": None}

    class Resp:
        status_code = 200

        def json(self):
            return {"token": "t", "status": "STOPPED"}

        def raise_for_status(self):
            pass

    monkeypatch.setattr(striim_api.requests, "post", lambda *a, **k: Resp())

    def fake_get(url, **k):
        calls["get"] = url
        return Resp()

    def fake_delete(url, **k):
        calls["delete"] = url
        return Resp()

    monkeypatch.setattr(striim_api.requests, "get", fake_get)
    monkeypatch.setattr(striim_api.requests, "delete", fake_delete)

    api = striim_api.StriimApi("localhost", 9080, "u", "p")
    api.undeploy_application("NS.App")  # must NOT raise TypeError

    assert calls["get"] is not None and "NS.App" in calls["get"]
    assert calls["delete"] is not None and "NS.App" in calls["delete"]


# --- every HTTP call has a timeout ---------------------------------------------------------
# Without one a wedged Striim blocks the caller forever: a live teardown once sat 9m30s in
# stop_application and never reached its forced drop.

def _recording(monkeypatch):
    calls = []

    class Resp:
        status_code = 200
        text = ""

        def json(self):
            return {"token": "t", "status": "STOPPED"}

        def raise_for_status(self):
            pass

    def record(method):
        def fake(url, **kwargs):
            calls.append((method, url, kwargs.get("timeout")))
            return Resp()
        return fake
    for method in ("get", "post", "delete"):
        monkeypatch.setattr(striim_api.requests, method, record(method))
    return calls


def test_every_call_has_the_default_timeout(monkeypatch, tmp_path):
    monkeypatch.delenv("STRIIM_API_TIMEOUT", raising=False)
    calls = _recording(monkeypatch)
    api = striim_api.StriimApi("localhost", 9080, "u", "p")
    tql = tmp_path / "x.tql"; tql.write_text("LIST LIBRARIES;")
    api.deploy_application("NS.App")
    api.start_application("NS.App")
    api.stop_application("NS.App")
    api.undeploy_application("NS.App")
    api.status_application("NS.App")
    api.post_tungsten_file(str(tql))
    api.post_tungsten_line("LIST LIBRARIES;")
    # authenticate, deploy, start, stop, status (inside undeploy), undeploy, status, file, line
    assert len(calls) == 9
    assert all(timeout == striim_api.DEFAULT_TIMEOUT for _, _, timeout in calls), calls


def test_the_override_reaches_every_call(monkeypatch, tmp_path):
    monkeypatch.setenv("STRIIM_API_TIMEOUT", "5,30")
    calls = _recording(monkeypatch)
    api = striim_api.StriimApi("localhost", 9080, "u", "p")
    api.stop_application("NS.App")
    api.post_tungsten_line("LIST LIBRARIES;")
    assert [t for _, _, t in calls] == [(5.0, 30.0)] * 3


def test_a_per_call_timeout_wins_over_the_default(monkeypatch):
    monkeypatch.delenv("STRIIM_API_TIMEOUT", raising=False)
    calls = _recording(monkeypatch)
    api = striim_api.StriimApi("localhost", 9080, "u", "p")
    api.undeploy_application("NS.App", timeout=(10, 60))
    # the status poll inside undeploy and the DELETE both take the caller's timeout
    assert [t for _, _, t in calls[1:]] == [(10, 60), (10, 60)]


@pytest.mark.parametrize("raw,expected", [("300", (10, 300.0)), ("5", (5.0, 5.0)),
                                          ("5,600", (5.0, 600.0)),
                                          ("0", None), ("", striim_api.DEFAULT_TIMEOUT)])
def test_the_override_values(monkeypatch, raw, expected):
    monkeypatch.setenv("STRIIM_API_TIMEOUT", raw)
    assert striim_api._timeout() == expected


@pytest.mark.parametrize("raw", ["10s", "10,", "1,2,3", "-1", "0,600", "0,0"])
def test_a_bad_override_names_the_setting(monkeypatch, raw):
    monkeypatch.setenv("STRIIM_API_TIMEOUT", raw)
    with pytest.raises(ValueError, match="STRIIM_API_TIMEOUT"):
        striim_api._timeout()


def test_the_override_is_read_once_when_the_client_is_created(monkeypatch):
    monkeypatch.setenv("STRIIM_API_TIMEOUT", "5,30")
    calls = _recording(monkeypatch)
    api = striim_api.StriimApi("localhost", 9080, "u", "p")
    monkeypatch.setenv("STRIIM_API_TIMEOUT", "broken")
    api.stop_application("NS.App")                  # no error: the value was validated already
    assert calls[-1][2] == (5.0, 30.0)
    with pytest.raises(ValueError, match="STRIIM_API_TIMEOUT"):
        striim_api.StriimApi("localhost", 9080, "u", "p")


@pytest.mark.parametrize("raw", ["nan", "inf", "10,inf"])
def test_non_finite_overrides_are_refused(monkeypatch, raw):
    monkeypatch.setenv("STRIIM_API_TIMEOUT", raw)
    with pytest.raises(ValueError, match="STRIIM_API_TIMEOUT"):
        striim_api._timeout()


def test_no_timeout_is_also_read_once(monkeypatch):
    monkeypatch.setenv("STRIIM_API_TIMEOUT", "0")
    calls = _recording(monkeypatch)
    api = striim_api.StriimApi("localhost", 9080, "u", "p")
    monkeypatch.setenv("STRIIM_API_TIMEOUT", "5,30")
    api.stop_application("NS.App")
    assert calls[-1][2] is None


def test_no_timeout_overrides_per_call_timeouts_too(monkeypatch):
    monkeypatch.setenv("STRIIM_API_TIMEOUT", "0")
    calls = _recording(monkeypatch)
    api = striim_api.StriimApi("localhost", 9080, "u", "p")
    api.stop_application("NS.App", timeout=(10, 60))
    assert calls[-1][2] is None


def test_a_per_call_timeout_never_exceeds_the_override(monkeypatch):
    monkeypatch.setenv("STRIIM_API_TIMEOUT", "5,30")
    calls = _recording(monkeypatch)
    api = striim_api.StriimApi("localhost", 9080, "u", "p")
    api.stop_application("NS.App", timeout=(10, 120))
    api.stop_application("NS.App", timeout=(2, 20))
    assert [t for _, _, t in calls[-2:]] == [(5.0, 30.0), (2, 20)]


def test_per_call_no_timeout_means_none(monkeypatch):
    monkeypatch.delenv("STRIIM_API_TIMEOUT", raising=False)
    calls = _recording(monkeypatch)
    api = striim_api.StriimApi("localhost", 9080, "u", "p")
    api.stop_application("NS.App", timeout=striim_api.NO_TIMEOUT)
    assert calls[-1][2] is None


@pytest.mark.parametrize("bad", [0, 0.0, -1, (5, 0), (0, 5), False, "10", (1, 2, 3)])
def test_a_per_call_zero_or_malformed_timeout_is_refused_not_unbounded(monkeypatch, bad):
    # A budget that ran down to 0 must not silently mean "no timeout".
    monkeypatch.delenv("STRIIM_API_TIMEOUT", raising=False)
    _recording(monkeypatch)
    api = striim_api.StriimApi("localhost", 9080, "u", "p")
    with pytest.raises(ValueError, match="timeout"):
        api.stop_application("NS.App", timeout=bad)


@pytest.mark.parametrize("given,expected", [([5, 30], (5, 30)), ((5, None), (5, 600)),
                                            ((None, 30), (10, 30)), (20, (10, 20))])
def test_per_call_lists_and_none_halves(monkeypatch, given, expected):
    monkeypatch.delenv("STRIIM_API_TIMEOUT", raising=False)
    calls = _recording(monkeypatch)
    api = striim_api.StriimApi("localhost", 9080, "u", "p")
    api.stop_application("NS.App", timeout=given)
    assert calls[-1][2] == expected


def test_the_login_takes_its_own_timeout(monkeypatch):
    monkeypatch.delenv("STRIIM_API_TIMEOUT", raising=False)
    calls = _recording(monkeypatch)
    striim_api.StriimApi("localhost", 9080, "u", "p", login_timeout=(5, 30))
    assert calls[0][2] == (5, 30)


def test_an_instance_built_without_init_reads_the_override(monkeypatch):
    monkeypatch.setenv("STRIIM_API_TIMEOUT", "0")
    api = striim_api.StriimApi.__new__(striim_api.StriimApi)
    assert api._t(None) is None


def test_parse_timeout_is_the_public_parser():
    assert striim_api.parse_timeout("") == striim_api.DEFAULT_TIMEOUT
    assert striim_api.parse_timeout(None) == striim_api.DEFAULT_TIMEOUT
    assert striim_api.parse_timeout("5,30") == (5.0, 30.0)
    with pytest.raises(ValueError, match="STRIIM_API_TIMEOUT"):
        striim_api.parse_timeout("10s")


def test_the_cli_reports_a_timeout_without_a_traceback(monkeypatch, capsys):
    import runpy
    def slow(*a, **k):
        raise striim_api.requests.exceptions.ReadTimeout("read timed out")
    monkeypatch.setattr(striim_api.requests, "post", slow)
    monkeypatch.setattr(sys, "argv", ["striim_api.py", "status", "NS.App"])
    with pytest.raises(SystemExit) as e:
        runpy.run_path(striim_api.__file__, run_name="__main__")
    assert "did not answer in time" in str(e.value.code)


@pytest.mark.parametrize("err,expected", [
    (lambda: striim_api.requests.exceptions.ConnectTimeout("no route"), "Could not connect"),
    (lambda: striim_api.requests.exceptions.ConnectionError(
        __import__("urllib3").exceptions.ReadTimeoutError(None, None, "slow")), "did not answer")])
def test_the_cli_tells_a_connect_timeout_from_a_read_timeout(monkeypatch, err, expected):
    import runpy
    def fail(*a, **k):
        raise err()
    monkeypatch.setattr(striim_api.requests, "post", fail)
    monkeypatch.setattr(sys, "argv", ["striim_api.py", "status", "NS.App"])
    with pytest.raises(SystemExit) as e:
        runpy.run_path(striim_api.__file__, run_name="__main__")
    assert expected in str(e.value.code)


def test_an_auth_failure_is_a_clear_error_not_a_type_error(monkeypatch):
    class Resp:
        def raise_for_status(self):
            raise striim_api.requests.exceptions.HTTPError("401 Unauthorized")
    monkeypatch.setattr(striim_api.requests, "post", lambda *a, **k: Resp())
    with pytest.raises(RuntimeError, match="authentication failed: 401"):
        striim_api.StriimApi("localhost", 9080, "u", "wrong")


def test_a_bad_per_call_timeout_is_refused_even_with_no_timeout_set(monkeypatch):
    monkeypatch.setenv("STRIIM_API_TIMEOUT", "0")
    _recording(monkeypatch)
    api = striim_api.StriimApi("localhost", 9080, "u", "p")
    with pytest.raises(ValueError, match="timeout"):
        api.stop_application("NS.App", timeout=0)


def test_the_cli_reports_a_malformed_setting_without_a_traceback(monkeypatch):
    import runpy
    monkeypatch.setenv("STRIIM_API_TIMEOUT", "10s")
    monkeypatch.setattr(sys, "argv", ["striim_api.py", "status", "NS.App"])
    with pytest.raises(SystemExit) as e:
        runpy.run_path(striim_api.__file__, run_name="__main__")
    assert "STRIIM_API_TIMEOUT" in str(e.value.code)
