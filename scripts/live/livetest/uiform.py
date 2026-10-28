"""Save every component as the Flow Designer would before the app deploys (SLT_UI_FORM).

Unset (the default) changes nothing. `replay` re-saves each component over the UI's own websocket
call; `browser` re-saves it in the Flow Designer with Playwright and checks the UI sent what
`replay` would. Measured on 5.4.0.6C (CRUDHandler READ after a TQL create, against the UPDATE a
UI save sends): the server stores every declared property, an empty default as null; the save
drops nulls and '' for an optional property whose default is '', sends a null Boolean as false,
a null conditionally required property as '' and a number as text for a String property. TQL accepts
'' for a required property and the UI does not, so a UI user types something: PLACEHOLDER.
"""
from __future__ import annotations

import json
import os
import re
import urllib.parse

MODES = ("replay", "browser")
PLACEHOLDER = "ui-required"
QV = "com.webaction.runtime.QueryValidator"
_PARTS = ("adapter", "parser", "formatter")
_DEPLOY = re.compile(r"(?:^|(?<=;))\s*(DEPLOY|START)\s+APPLICATION\b", re.I | re.M)
_USE = re.compile(r"(?:^|(?<=;))\s*USE\s+(\w+)\s*;", re.I | re.M)
_APP = re.compile(r"(?:^|(?<=;))\s*CREATE\s+(?:OR\s+REPLACE\s+)?APPLICATION\s+(?:(\w+)\.)?(\w+)", re.I | re.M)


class UiFormError(Exception):
    pass


def mode() -> str | None:
    value = (os.environ.get("SLT_UI_FORM") or "").strip().lower()
    if value and value not in MODES:
        raise UiFormError(f"SLT_UI_FORM={value!r}: expected one of {', '.join(MODES)} or unset")
    return value or None


def split(tql: str) -> tuple[str, str, list[tuple[str, str]]]:
    """(statements before the first DEPLOY/START APPLICATION, the rest, [(namespace, app)] created before it)."""
    m = _DEPLOY.search(tql)
    head, tail = (tql, "") if m is None else (tql[:m.start(1)], tql[m.start(1):])
    apps, ns = [], None
    for s in sorted(list(_USE.finditer(head)) + list(_APP.finditer(head)), key=lambda x: x.start()):
        if s.re is _USE:
            ns = s.group(1)
        elif s.group(1) or ns:
            apps.append((s.group(1) or ns, s.group(2)))
    return head, tail, apps


def ui_properties(stored: dict, template: dict) -> dict:
    """The property map a UI save sends for a component the server stored as `stored`."""
    types = {p["name"].lower(): p for p in template.get("propertyMap", [])}
    out = {}
    for key, value in stored.items():
        p = types.get(key.lower())
        kind = (p or {}).get("type", "")
        if p and p.get("required") and value in (None, ""):
            value = 1 if kind in ("java.lang.Integer", "java.lang.Long") else PLACEHOLDER
        elif value is None and kind == "java.lang.Boolean":
            value = False
        elif value is None and (p or {}).get("conditionalRequired"):
            value = ""   # the form registers a conditionally required field, so it is sent
        elif value == "" and p and not p.get("conditionalRequired") and p.get("defaultValue") == "":
            value = None   # an empty optional field is left out of the save
        if value is None:
            continue
        if kind == "java.lang.String" and isinstance(value, (int, float)) and not isinstance(value, bool):
            value = str(value)
        out[key] = value
    return out


def secret_names(template: dict) -> set:
    """Lower-cased names of the template's Password properties, whose values are never printed."""
    return {p["name"].lower() for p in template.get("propertyMap", [])
            if p.get("type") == "com.webaction.security.Password"}


class Rmi:
    """The Flow Designer's websocket RPC (ws://<host>/rmiws/, URL-encoded JSON)."""

    def __init__(self, url_base: str, token: str, timeout: float = 60):
        import websocket   # optional extra `ui`
        self.token, self._n = token, 0
        self.ws = websocket.create_connection(url_base.replace("http", "ws", 1) + "/rmiws/",
                                              cookie=f"token={token}", timeout=timeout)
        try:
            self.call(QV, "createNewContext", token)
        except BaseException:
            self.ws.close()
            raise

    def call(self, cls: str, method: str, *params):
        self._n += 1
        n = str(self._n)
        self.ws.send(urllib.parse.quote(json.dumps(
            {"class": cls, "method": method, "params": list(params), "callbackIndex": self._n},
            separators=(",", ":"))))
        while True:
            reply = json.loads(urllib.parse.unquote(self.ws.recv()))
            if str(reply.get("callbackIndex")) == n:
                if not reply.get("success"):
                    what = params[3].get("id") if len(params) > 3 and isinstance(params[3], dict) else ""
                    detail = (reply.get("params") or [""])[0]
                    raise UiFormError(f"{method} {params[2] if len(params) > 2 else ''} {what} failed: "
                                      f"{str(detail)[:300]}")
                return reply["params"][0]

    def read(self, oid: str, kind: str) -> dict:
        found = self.call(QV, "CRUDHandler", self.token, None, "READ", {"id": oid, "type": kind})
        # An unknown id comes back as {'noField': None}, not as nothing.
        if not found or str(found[0].get("id", "")).lower() != oid.lower():
            raise UiFormError(f"no {kind} {oid}")
        return found[0]

    def update(self, ns: str, app: str, obj: dict) -> None:
        """Save `obj` as the UI's Save does: UPDATE, then recompile the application."""
        self.call(QV, "CRUDHandler", self.token, app, "UPDATE", obj)
        self.call(QV, "CreateAlterRecompileStmt", self.token, "APPLICATION", f"{ns}.{app}")

    def close(self) -> None:
        self.ws.close()


def components(rmi: Rmi, ns: str, app: str) -> list[dict]:
    """The app's components that carry an adapter, parser or formatter, as the server stores them."""
    out, pending = [], [(f"{ns}.APPLICATION.{app}", "APPLICATION")]
    while pending:
        # A sub-flow's members are listed under its FLOW, not under the application.
        objects = rmi.read(*pending.pop(0)).get("objects") or {}
        for kind, ids in sorted(objects.items()):
            for oid in ids:
                if kind == "FLOW":
                    pending.append((oid, kind))
                    continue
                obj = rmi.read(oid, kind)
                if any(isinstance(obj.get(p), dict) for p in _PARTS):
                    out.append(obj)
    if not out:
        raise UiFormError(f"{ns}.{app}: no component to save, so the UI form would be untested")
    return out


def ui_object(rmi: Rmi, obj: dict, templates: dict) -> tuple[dict, list[str]]:
    """`obj` as a UI save sends it, and one line per value that differs from what is stored."""
    sent, changes = dict(obj), []
    # Object fields as the UI writes them (5.4.0.6C): READ gives `from` as a list and omits
    # `enrichment`, which UPDATE refuses as a null Set.
    if obj.get("type") == "OPENPROCESSOR":
        if isinstance(sent.get("from"), list) and len(sent["from"]) == 1:
            sent["from"] = sent["from"][0]
        sent.setdefault("enrichment", [])
    sent.setdefault("version", 0)
    for part in _PARTS:
        section = obj.get(part)
        if not isinstance(section, dict):
            continue
        handler = section["handler"]
        if handler not in templates:
            templates[handler] = rmi.read(handler, "PROPERTYTEMPLATE")
        props = ui_properties(section.get("properties") or {}, templates[handler])
        secret = secret_names(templates[handler])
        for key, value in (section.get("properties") or {}).items():
            if props.get(key, None) != value and not (value is None and key not in props):
                old, new = ("***", "***") if key.lower() in secret else (repr(value), repr(props.get(key)))
                changes.append(f"{obj['name']}.{key}: {old} -> {new}")
        sent[part] = {**section, "properties": props}
    return sent, changes


def replay(url_base: str, token: str, apps: list[tuple[str, str]]) -> list[str]:
    """Re-save every component of `apps` as the UI would; the changed values, one line each."""
    rmi, templates, lines = Rmi(url_base, token), {}, []
    try:
        for ns, app in apps:
            for obj in components(rmi, ns, app):
                sent, changes = ui_object(rmi, obj, templates)
                rmi.update(ns, app, sent)
                lines += changes or [f"{obj['name']}: saved unchanged"]
    finally:
        rmi.close()
    return lines


def deploy(client, tql: str) -> list[str]:
    """client.deploy_tql(tql), with every component re-saved the SLT_UI_FORM way before DEPLOY."""
    how = mode()
    if how is None:
        client.deploy_tql(tql)
        return []
    head, tail, apps = split(tql)
    if not apps:
        raise UiFormError("SLT_UI_FORM: no CREATE APPLICATION before the first DEPLOY/START APPLICATION")
    client.deploy_tql(head)
    try:
        if how == "replay":
            lines = replay(client.api.url_base, client.api.auth_token, apps)
        else:
            from livetest import uibrowser
            lines = uibrowser.resave(client.api.url_base, client.api.username, client.api.password,
                                     client.api.auth_token, apps)
    except UiFormError:
        raise
    except ImportError as e:
        raise UiFormError(f"SLT_UI_FORM={how} needs the `ui` extra (docs/SETTINGS.md): {e}") from e
    except Exception as e:
        # Never an expected halt: the save itself failed, not the application.
        raise UiFormError(f"SLT_UI_FORM={how}: the save failed: {type(e).__name__}: {e}") from e
    if tail.strip():
        client.deploy_tql(tail)
    return [f"[slt] ui-form {how}: {line}" for line in lines]
