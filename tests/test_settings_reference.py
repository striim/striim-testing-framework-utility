"""docs/SETTINGS.md lists every setting the framework reads, and is what tools/settings_reference.py
writes.

A setting is any string in the engines' and the CLI's code shaped like one (SLT_*, INT_*, STRIIM_*,
GOLD_TARGETS, or a prefix of one such as STRIIM_BUILD_), any NAME the code reads with
``<...env...>.get("NAME")``, and every key a .env or machine file may hold. Each must be on the
page, or be one of the tool's NOT_SETTINGS: tokens the framework fills in itself, and variables
other programs define.
"""
from __future__ import annotations

import ast
import importlib.util
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PAGE = ROOT / "docs" / "SETTINGS.md"
# A whole name, or a prefix the code completes (STRIIM_BUILD_ + a version).
SHAPE = re.compile(r"(SLT|INT|STRIIM)_[A-Z0-9_]*[A-Z0-9_]|GOLD_TARGETS")


def _tool():
    spec = importlib.util.spec_from_file_location("settings_reference", ROOT / "tools" / "settings_reference.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _listed() -> set:
    names = set(re.findall(r"`([A-Z][A-Z0-9_<>a-z]*)`", PAGE.read_text()))
    # A pattern such as SLT_JDK<release>_HOME covers the fixed part the code builds it from.
    return names | {n.split("<")[0] for n in names if "<" in n}


def test_the_page_is_what_the_tool_writes():
    r = subprocess.run([sys.executable, str(ROOT / "tools" / "settings_reference.py"), "--check"],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def _env_get(node) -> str | None:
    """The literal NAME of an ``<...env...>.get("NAME", ...)`` call: a setting whatever its shape."""
    if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get" and node.args and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)):
        return None
    owner = node.func.value
    owner = owner.attr if isinstance(owner, ast.Attribute) else getattr(owner, "id", "")
    name = node.args[0].value
    return name if "env" in owner.lower() and re.fullmatch(r"[A-Z][A-Z0-9_]*", name) else None


def test_every_setting_the_code_names_is_listed():
    names = {}
    for file in sorted((ROOT / "scripts").rglob("*.py")):
        rel = file.relative_to(ROOT)
        if {"tests", "regression"} & set(rel.parts):
            continue
        tree = ast.parse(file.read_text(), str(file))
        # The literal parts of an f-string are fragments of a name, not names.
        fragments = {id(v) for n in ast.walk(tree) if isinstance(n, ast.JoinedStr) for v in n.values}
        for node in ast.walk(tree):
            if (isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in fragments
                    and SHAPE.fullmatch(node.value)):
                names.setdefault(node.value, rel.as_posix())
            if _env_get(node):
                names.setdefault(_env_get(node), rel.as_posix())
    listed = _listed()
    # A prefix the code matches a family by is listed when a listed setting carries it.
    missing = {n: f for n, f in names.items() if n not in listed and n not in _tool().NOT_SETTINGS
               and not (n.endswith("_") and any(m.startswith(n) and m != n for m in listed))}
    assert not missing, f"describe these in tools/settings_reference.py, then run it: {missing}"


def test_every_key_a_settings_file_may_hold_is_listed():
    from livetest import paths
    from livetest.service_env import declarations
    keys = set(paths.KEYS) | set(paths.SERVICE_KEYS) | set(paths.LICENCE_KEYS)
    keys |= set(declarations({"SLT_FRAMEWORK_DOTENV": os.devnull}))
    assert not keys - _listed() - _tool().NOT_SETTINGS, keys - _listed() - _tool().NOT_SETTINGS


def test_where_follows_the_files_rules():
    rows = dict(re.findall(r"^\| `([A-Z][A-Z0-9_]*)` \| ([^|]+) \|", PAGE.read_text(), re.M))
    assert rows["STRIIM_URL"].strip() == "shell, `.env`"              # a lane key: never the machine file
    assert rows["STRIIM_USER"].strip() == "shell, `.env`, machine"
    assert rows["LICENCE_KEY"].strip() == "shell, machine"             # never .env
    assert rows["SLT_PARALLEL"].strip() == "shell"


def _page_rows():
    return re.findall(r"^\| `([A-Z][A-Z0-9_<>a-z]*)` \| ([^|]*) \|", PAGE.read_text(), re.M)


def test_env_example_names_every_setting_a_dotenv_can_hold():
    example = (ROOT / ".env.example").read_text()
    assigned = set(re.findall(r"^#? ?([A-Z][A-Z0-9_]*)=", example, re.M))
    aliases = {"STRIIM_PASSWORD"}           # another name for STRIIM_PASS, which the file shows
    missing = sorted(n for n, where in _page_rows()
                     if ".env" in where and n not in assigned and n not in aliases)
    assert not missing, f".env.example does not name: {missing}"


def test_env_example_assigns_no_setting_a_dotenv_cannot_hold():
    example = (ROOT / ".env.example").read_text()
    assigned = set(re.findall(r"^#? ?([A-Z][A-Z0-9_]*)=", example, re.M))
    shell_only = sorted(n for n, where in _page_rows() if ".env" not in where and n in assigned)
    assert not shell_only, f".env.example assigns settings .env never reads: {shell_only}"
