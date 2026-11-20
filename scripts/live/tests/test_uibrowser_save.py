"""`uibrowser._save_one` and `resave`'s form comparison against a fake page (no browser).

What decides a SLT_UI_FORM=browser save: the field touched to enable Save, the UPDATE the page
sent, a validation error or a refused reply failing the save, and a form that differs from
replay's failing it with secrets masked. `test_uibrowser.py` covers the popup helpers only.
"""
from __future__ import annotations

import sys
from types import ModuleType, SimpleNamespace

import pytest

from livetest import uibrowser
from livetest.uiform import UiFormError

FIELDS = "[data-test-id^=form-control-] input:not([type=checkbox]):not([type=password]):visible"
ERRORS = ("[data-test-id=validation-error]:visible", "text=/^\\s*Value for .+ is required\\s*$/ >> visible=true")


class Field:
    def __init__(self, page, name, value):
        self.page, self.name, self.value = page, name, value

    def input_value(self):
        return self.value

    def get_attribute(self, attr):
        return self.name if attr == "name" else None

    def focus(self):
        self.page.calls.append(("focus", self.name))

    def press(self, key):
        self.page.calls.append(("press", self.name, key))

    def type(self, text):
        self.page.calls.append(("type", self.name, text))


class Text:
    def __init__(self, text):
        self.text = text

    def inner_text(self):
        return self.text


class Locator:
    def __init__(self, items):
        self.items = list(items)

    def count(self):
        return len(self.items)

    def all(self):
        return self.items

    @property
    def first(self):
        return self.items[0]


class Page:
    """Answers the selectors `_save_one` reads. Clicking Save appends the frames `on_submit` names."""

    def __init__(self, frames, fields, on_submit, errors=()):
        self.frames, self.fields, self.on_submit, self.errors = frames, fields, on_submit, list(errors)
        self.calls = []
        self.mouse = SimpleNamespace(move=lambda x, y: None)
        self.keyboard = SimpleNamespace(press=lambda key: None)

    def locator(self, selector):
        if selector == FIELDS:
            return Locator(self.fields)
        if selector == ERRORS[0]:
            return Locator(Text(e) for e in self.errors)
        return Locator([])          # editor, popups, "show advanced settings", the required-field text

    def click(self, selector):
        self.calls.append(("click", selector))
        if selector == "[data-test-id=submit-btn]":
            self.frames.extend(self.on_submit)

    def wait_for_selector(self, selector, timeout=None):
        pass

    def wait_for_timeout(self, ms):
        pass


UPDATE_OBJ = {"adapter": {"handler": "Global.FileReader", "properties": {"directory": "/data", "wildcard": "*.csv"}}}


def _answers(update_ok=True, recompile_ok=True):
    return [("S", {"method": "CRUDHandler", "params": ["a", "b", "UPDATE", UPDATE_OBJ], "callbackIndex": 7}),
            ("R", {"callbackIndex": 7, "success": update_ok, "params": ["Invalid property wildcard"]}),
            ("S", {"method": "CreateAlterRecompileStmt", "params": [], "callbackIndex": 8}),
            ("R", {"callbackIndex": 8, "success": recompile_ok, "params": ["recompile failed"]})]


def _page(**kw):
    frames = [("S", {"method": "CRUDHandler", "params": ["a", "b", "UPDATE", {}], "callbackIndex": 7}),
              ("R", {"callbackIndex": 7, "success": False})]   # an earlier save's frames, before the mark
    page = Page(frames, kw.pop("fields", None) or [], kw.pop("on_submit", _answers()), **kw)
    page.fields = page.fields or [Field(page, "empty", ""), Field(page, "directory", "/data")]
    return page


def test_save_touches_a_filled_field_and_returns_the_update_it_sent():
    page = _page()
    got, touched = uibrowser._save_one(page, page.frames, "Src")
    assert (got, touched) == (UPDATE_OBJ, "directory")      # the filled field, not the empty first one
    assert ("type", "directory", "x") in page.calls and ("press", "directory", "Backspace") in page.calls
    assert page.calls.index(("type", "directory", "x")) < page.calls.index(("click", "[data-test-id=submit-btn]"))
    assert ("click", "[data-test-id=node_container_Src]") in page.calls


def test_a_validation_error_on_the_form_fails_the_save():
    page = _page(errors=["Value for Bucket Name is required"], on_submit=[])
    with pytest.raises(UiFormError, match="Src: the UI refused Save: Value for Bucket Name is required"):
        uibrowser._save_one(page, page.frames, "Src")


@pytest.mark.parametrize("update_ok, recompile_ok, method", [(False, True, "CRUDHandler"),
                                                             (True, False, "CreateAlterRecompileStmt")])
def test_a_refused_update_or_recompile_fails_the_save(update_ok, recompile_ok, method):
    page = _page(on_submit=_answers(update_ok, recompile_ok))
    with pytest.raises(UiFormError, match=f"Src: the server refused the UI's {method}"):
        uibrowser._save_one(page, page.frames, "Src")


def test_a_form_without_a_text_field_cannot_be_saved():
    page = _page()
    page.fields = []
    with pytest.raises(UiFormError, match="no text field to touch"):
        uibrowser._save_one(page, page.frames, "Src")
    assert ("click", "[data-test-id=submit-btn]") not in page.calls


def test_save_without_an_answer_gives_up_at_the_timeout(monkeypatch):
    monkeypatch.setattr(uibrowser, "TIMEOUT_MS", 1000)
    page = _page(on_submit=_answers()[:1])                    # the UPDATE is sent and never answered
    with pytest.raises(UiFormError, match="Save got no answer to UPDATE and recompile within 1s"):
        uibrowser._save_one(page, page.frames, "Src")


# --- resave: the page's form against replay's ----------------------------------------------

@pytest.fixture
def resave(monkeypatch):
    """`resave` with Playwright, the RMI calls and the per-component save replaced by fakes."""
    def run(sent, expected, secret=()):
        class Browser:
            def new_page(self, viewport=None):
                return SimpleNamespace(on=lambda event, fn: None, goto=lambda url: None,
                                       wait_for_selector=lambda selector, timeout=None: None)

            def close(self):
                pass

        class Playwright:
            def __enter__(self):
                return SimpleNamespace(chromium=SimpleNamespace(launch=lambda: Browser()))

            def __exit__(self, *exc):
                return False

        api = ModuleType("playwright.sync_api")
        api.sync_playwright = Playwright
        monkeypatch.setitem(sys.modules, "playwright", ModuleType("playwright"))
        monkeypatch.setitem(sys.modules, "playwright.sync_api", api)
        monkeypatch.setattr(uibrowser, "Rmi", lambda url, token: SimpleNamespace(close=lambda: None))
        monkeypatch.setattr(uibrowser, "components", lambda rmi, ns, app: [{"name": "Src"}])
        monkeypatch.setattr(uibrowser, "_login", lambda *a: None)
        monkeypatch.setattr(uibrowser, "_close_card", lambda page, wait_ms=0: None)

        def ui_object(rmi, obj, templates):
            templates["Global.Handler"] = "template"
            return expected, []
        monkeypatch.setattr(uibrowser, "ui_object", ui_object)
        monkeypatch.setattr(uibrowser, "secret_names", lambda template: set(secret))
        monkeypatch.setattr(uibrowser, "_save_one", lambda page, frames, name: (sent, "directory"))
        return uibrowser.resave("http://striim.local:9080", "admin", "pw", "token", [("NS", "App")])
    return run


def _form(**props):
    return {"adapter": {"handler": "Global.Handler", "properties": props}}


def test_resave_passes_when_the_page_sent_what_replay_would_apart_from_the_touched_field(resave):
    assert resave(_form(directory="/datax", password="s3"), _form(directory="/data", password="s3")) == [
        "Src: saved unchanged"]


def test_resave_fails_on_a_different_form_and_masks_secret_values(resave):
    with pytest.raises(UiFormError) as e:
        resave(_form(directory="/data", password="ui-secret", wildcard="*.csv"),
               _form(directory="/data", password="replay-secret", wildcard="*.txt"), secret={"password"})
    message = str(e.value)
    assert message.startswith("Src.adapter: the UI sent a different form than replay: ")
    assert "password ui=*** replay=***" in message and "wildcard ui='*.csv' replay='*.txt'" in message
    assert "secret" not in message.replace("***", "")
