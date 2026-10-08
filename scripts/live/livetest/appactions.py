"""Runtime of the `capture` and `alter_recompile` actions, and of the `capture:` /
`stopped_seed:` / `tokens:` keys `drop_recreate_app` shares with them (docs/TEST-YAML.md,
"`action: capture`" and "`action: alter_recompile`"). manifest.py validates the specs; plugin.py
owns the client, the admins and the run's token map and passes them in, so everything here runs
against a fake client in tests/test_appactions.py.
"""
from __future__ import annotations

import json
import re
import time

from livetest.substitute import render


class CaptureError(Exception):
    pass


def qualify(component: str, ns: str) -> str:
    """A TQL component name qualified by the test's namespace; a name with a dot is used as is
    (the rule assert.jmx uses)."""
    return component if "." in component else f"{ns}.{component}"


def _text(value):
    """A field's value as the text a token carries, or None when it has none yet. A checkpoint
    field (`{"CheckpointText": ...}`) is its text; a structured value is its JSON."""
    if isinstance(value, dict) and "CheckpointText" in value:
        value = value["CheckpointText"]
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return json.dumps(value, sort_keys=True)
    text = str(value)
    return text if text != "" else None


def describe_values(output, field: str) -> list[str]:
    """Every non-empty value of `field` anywhere in a DESCRIBE output, in document order. The
    restart position sits under `Checkpoint[]`, so the search is recursive."""
    found, stack = [], [output]
    while stack:
        node = stack.pop(0)
        if isinstance(node, dict):
            for key, value in node.items():
                if key == field:
                    text = _text(value)
                    if text is not None:
                        found.append(text)
                stack.append(value)
        elif isinstance(node, list):
            stack.extend(node)
    return found


def mon_values(body: dict, field: str) -> list[str]:
    """`field` of a MON body: a top-level figure, as assert.monitor reads it."""
    text = _text(body.get(field)) if isinstance(body, dict) else None
    return [text] if text is not None else []


def _seen(tree, source: str) -> str:
    if source == "mon":
        keys = sorted(tree) if isinstance(tree, dict) else []
    else:
        keys = sorted({k for obj in (tree or []) if isinstance(obj, dict) for k in obj})
    shown = ", ".join(keys[:40]) + (", ..." if len(keys) > 40 else "")
    return f"top-level fields seen: [{shown}]"


def read_once(client, spec: dict, ns: str, tokens: dict):
    """One read of a capture's field: (value or None, what was read, fqn). Two different values
    raise rather than guess which one the author meant."""
    fqn = qualify(render(spec["component"], tokens), ns)
    source, field = spec["source"], spec["field"]
    if source == "describe":
        tree = client.describe(fqn)
        values = describe_values(tree, field)
    else:
        tree = client.mon(fqn)
        values = mon_values(tree, field)
    distinct = list(dict.fromkeys(values))
    if len(distinct) > 1:
        raise CaptureError(
            f"capture {spec['token']}: {source.upper()} {fqn} shows {len(distinct)} different "
            f"values for {field!r}: {distinct}")
    return (distinct[0] if distinct else None), tree, fqn


def expect_miss(expect, value, tokens: dict) -> str | None:
    """Why `value` does not satisfy a capture's `expect:` (a literal or a present/matches
    matcher, token-rendered; a token inside `matches` is regex-escaped), or None."""
    if isinstance(expect, dict):
        from livetest.assertions.monitor import _matcher_miss, render_spec
        want = render_spec({"metrics": {"v": expect}}, tokens)["metrics"]["v"]
        return _matcher_miss(want, value)
    want = render(expect, tokens)
    return None if value == want else f"expected {want!r}"


def capture(client, spec: dict, ns: str, tokens: dict, default_timeout: float, report=None,
            poll: float = 2.0, sleep=time.sleep, clock=time.monotonic) -> str:
    """Read one field and bind it to ``tokens[spec['token']]`` for everything rendered later.

    Polls until the field has a non-empty value, and satisfies `expect:` when the spec has one,
    or the timeout runs out (a checkpoint, and MON more so, trails the events)."""
    source, field = spec["source"], spec["field"]
    timeout = spec["timeout"] if spec.get("timeout") is not None else float(default_timeout)
    deadline = clock() + timeout
    while True:
        value, tree, fqn = read_once(client, spec, ns, tokens)
        miss = None
        if value is not None and "expect" in spec:
            miss = expect_miss(spec["expect"], value, tokens)
        if value is not None and miss is None:
            tokens[spec["token"]] = value
            if report:
                report(f"action: captured ${{{spec['token']}}} = {value!r} "
                       f"from {source.upper()} {fqn} {field!r}")
            return value
        if clock() >= deadline:
            if miss is not None:
                raise CaptureError(
                    f"capture {spec['token']}: {source.upper()} {fqn} {field!r} shows {value!r}, "
                    f"{miss} (waited {timeout:.0f}s)")
            raise CaptureError(
                f"capture {spec['token']}: {source.upper()} {fqn} showed no value for {field!r} "
                f"within {timeout:.0f}s; {_seen(tree, source)}")
        sleep(poll)


def run_captures(client, specs: list, ns: str, tokens: dict, default_timeout: float,
                 report=None, **kw) -> None:
    for spec in specs:
        capture(client, spec, ns, tokens, default_timeout, report=report, **kw)


def override_tokens(overrides: dict, tokens: dict) -> dict:
    """The token map a `tokens:` override renders with: each value rendered against the run's
    tokens first, so it can carry a captured one (``"^ ${RESTART}"``)."""
    return {**tokens, **{name: render(value, tokens) for name, value in overrides.items()}}


def apply_upload_renames(text: str, upload_renames: dict) -> str:
    """Rewrite a literal `UploadedFiles/<from>` to the uploaded `<to>` name (op.upload {from, to})."""
    for from_name, to_name in upload_renames.items():
        text = text.replace(f"UploadedFiles/{from_name}", f"UploadedFiles/{to_name}")
    return text


def deploy_statement(full_tql: str, app: str) -> str:
    """The TQL's own `DEPLOY APPLICATION <app> ...;` (it may name deployment groups), or the plain
    form when the TQL has none -- the rule drop_recreate_app follows."""
    dep = re.search(r"DEPLOY\s+APPLICATION\s+" + re.escape(app) + r"\b[^;]*;", full_tql, flags=re.I)
    return dep.group(0) if dep else f"DEPLOY APPLICATION {app};"


def alter_tql(ns: str, app: str, fragment: str, deploy_cmd: str) -> str:
    """The documented in-place upgrade, as one import: the app keeps its checkpoint."""
    return (f"USE {ns};\nUNDEPLOY APPLICATION {app};\nALTER APPLICATION {app};\n"
            f"{fragment.strip()}\nALTER APPLICATION {app} RECOMPILE;\n"
            f"{deploy_cmd}\nSTART APPLICATION {app};\n")

