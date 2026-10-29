"""SLT_UI_FORM=browser: re-save each component in the Flow Designer (headless Chromium).

Save stays disabled until the form changes, so one text field is typed into and restored first.
The UPDATE the page sends must equal what uiform.replay would send: a difference means the UI's
rules moved and replay no longer stands in for it. A validation error the page shows fails the save.
"""
from __future__ import annotations

import json
import urllib.parse

from livetest.uiform import Rmi, UiFormError, components, secret_names, ui_object

TIMEOUT_MS = 60_000


def _login(page, url_base: str, user: str, password: str) -> None:
    page.goto(url_base + "/")
    page.wait_for_selector("input[name=username]", timeout=TIMEOUT_MS)
    page.fill("input[name=username]", user)
    page.fill("input[name=password]", password)
    page.click("button:has-text('Sign In')")
    page.wait_for_function("!location.hash.includes('login')", timeout=TIMEOUT_MS)


def _close_editor(page) -> None:
    """Close an open component editor: it covers the nodes behind it."""
    for selector in ("button:has-text('Cancel'):visible", "button:has-text('close'):visible"):
        if page.locator(".right-sidebar:visible").count() == 0:
            return
        button = page.locator(selector)
        if button.count():
            button.first.click()
            page.wait_for_timeout(1000)


def _clear_popups(page) -> None:
    """Close what covers the canvas: Striim's Sherlock AI card ("Discover Sensitive Data ...")
    opens on the flow page over the right-hand nodes, and a hover tooltip can sit over a node."""
    page.mouse.move(0, 0)
    close = page.locator("[data-test-id=insights-close-btn]:visible")
    if close.count():
        close.first.click()
    tips = page.locator("[role=tooltip]:visible")
    for attempt in range(20):
        if tips.count() == 0:
            return
        if attempt == 0:
            page.keyboard.press("Escape")
        page.wait_for_timeout(250)


def _save_one(page, frames: list, name: str) -> tuple[dict, str]:
    """Save component `name`; the UPDATE it sent and the property touched to enable Save."""
    _close_editor(page)
    _clear_popups(page)
    page.click(f"[data-test-id=node_container_{name}]")
    page.wait_for_selector("[data-test-id=submit-btn]", timeout=TIMEOUT_MS)
    page.wait_for_timeout(1500)
    for _ in range(10):
        more = page.locator("text=/show advanced settings/i >> visible=true")
        if more.count() == 0:
            break
        more.first.click()
        page.wait_for_timeout(500)
    # Inputs carry the Java type as `type` (java.lang.String). A filled one is preferred: an empty
    # one typed into and cleared is sent as "" rather than left out.
    fields = page.locator("[data-test-id^=form-control-] input:not([type=checkbox]):not([type=password]):visible").all()
    field = next((f for f in fields if f.input_value()), fields[0] if fields else None)
    if field is None:
        raise UiFormError(f"{name}: no text field to touch, so Save cannot be enabled")
    touched = field.get_attribute("name") or ""
    field.focus()
    field.press("End")
    field.type("x")
    field.press("Backspace")
    mark = len(frames)
    page.click("[data-test-id=submit-btn]")
    update = None
    for _ in range(TIMEOUT_MS // 250):
        # A field's own error ("Value for Bucket Name is required") has no data-test-id.
        errors = [e.inner_text().strip()
                  for selector in ("[data-test-id=validation-error]:visible",
                                   "text=/^\\s*Value for .+ is required\\s*$/ >> visible=true")
                  for e in page.locator(selector).all()]
        if any(errors):
            raise UiFormError(f"{name}: the UI refused Save: {'; '.join(e for e in errors if e)}")
        update = update or _sent(frames, mark, "CRUDHandler", "UPDATE")
        recompile = update and _sent(frames, update[0], "CreateAlterRecompileStmt")
        if recompile:
            # Both answers must be successes: a refused UPDATE leaves the TQL's component in place.
            for at, msg in (update, recompile):
                reply = _reply(frames, at, msg["callbackIndex"])
                if reply is None:
                    break
                if not reply.get("success"):
                    raise UiFormError(f"{name}: the server refused the UI's {msg['method']}: "
                                      f"{str((reply.get('params') or [''])[0])[:300]}")
            else:
                _close_editor(page)
                return update[1]["params"][3], touched
        page.wait_for_timeout(250)
    raise UiFormError(f"{name}: Save got no answer to UPDATE and recompile within {TIMEOUT_MS // 1000}s")


def _sent(frames: list, start: int, method: str, verb: str | None = None):
    """(index, message) of the first frame sent from `start` calling `method` (with `verb`)."""
    for i in range(start, len(frames)):
        way, msg = frames[i]
        if way == "S" and msg.get("method") == method and (verb is None or msg["params"][2] == verb):
            return i, msg
    return None


def _reply(frames: list, start: int, callback) -> dict | None:
    """The first answer after `start` to callback index `callback`; the UI reuses indexes."""
    for way, msg in frames[start + 1:]:
        if way == "R" and str(msg.get("callbackIndex")) == str(callback):
            return msg
    return None


def resave(url_base: str, user: str, password: str, token: str, apps: list[tuple[str, str]]) -> list[str]:
    from playwright.sync_api import sync_playwright   # optional extra `ui`
    rmi, templates, lines = Rmi(url_base, token), {}, []
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            try:
                page = browser.new_page(viewport={"width": 1600, "height": 1000})
                frames: list = []   # ("S" sent | "R" received, message)

                def frame(way):
                    def record(f):
                        if isinstance(f, str):
                            try:
                                frames.append((way, json.loads(urllib.parse.unquote(f))))
                            except ValueError:
                                pass
                    return record

                def watch(ws):
                    ws.on("framesent", frame("S"))
                    ws.on("framereceived", frame("R"))
                page.on("websocket", watch)
                _login(page, url_base, user, password)
                for ns, app in apps:
                    stored = components(rmi, ns, app)
                    page.goto(f"{url_base}/#/flow/{ns}.{app}")
                    for obj in stored:
                        expected, changes = ui_object(rmi, obj, templates)
                        page.wait_for_selector(f"[data-test-id=node_container_{obj['name']}]", timeout=TIMEOUT_MS)
                        got, touched = _save_one(page, frames, obj["name"])
                        for part in ("adapter", "parser", "formatter"):
                            if isinstance(expected.get(part), dict):
                                want = {k: v for k, v in expected[part]["properties"].items() if k != touched}
                                have = {k: v for k, v in ((got.get(part) or {}).get("properties") or {}).items()
                                        if k != touched}
                                if want != have:
                                    diff = sorted(k for k in set(want) | set(have) if want.get(k) != have.get(k))
                                    secret = secret_names(templates[expected[part]["handler"]])
                                    shown = (lambda k, v: "***" if k.lower() in secret else repr(v))
                                    raise UiFormError(
                                        f"{obj['name']}.{part}: the UI sent a different form than replay: "
                                        + ", ".join(f"{k} ui={shown(k, have.get(k))} replay={shown(k, want.get(k))}"
                                                    for k in diff))
                        lines += changes or [f"{obj['name']}: saved unchanged"]
            finally:
                browser.close()
    finally:
        rmi.close()
    return lines
