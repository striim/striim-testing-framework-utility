"""WAEvent presence-bitmap guard: static scan of java/ main sources (hermetic).

Catches Java that REASSIGNS a WAEvent's `data`/`before` array (e.g. `event.data = newData;`
or `event.data = map.values().toArray();`) without rebuilding the presence bitmaps. A swapped
array under a stale `dataPresenceBitMap`/`beforePresenceBitMap` ships malformed events that
permanently poison DatabaseWriter's server-wide column-pattern cache -- silently NULLing
columns for every same-bitmap event.

Sanctioned patterns (never flagged):
  * `event.data = null` and fresh array allocation `event.data = new Object[n]` /
    `new Object[]{...}` -- the fresh-construction idiom (`new WAEvent(n, uuid)` + platform
    `setData(i, v)`/`setBefore(i, v)`, which maintain the bits);
  * receiver freshly constructed nearby (`recv = new WAEvent(...)` within the window) --
    no upstream bitmap exists to go stale;
  * explicit rebuild near the swap: any presence-bitmap reference (any *Bitmap* identifier)
    or setData/setBefore repopulation within +/-WINDOW lines.

Receiver typing is heuristic: an assignment counts only when the receiver's nearest
preceding declaration in the same file is WAEvent-typed (`WAEvent e`, `final
com.webaction.proc.events.WAEvent event`, ...). Locals named `data`, `this.data`, and
`.data` on non-WAEvent classes (JsonNodeEvent, TableMeta, ...) are therefore ignored.

Pure rule-evaluation, mirroring enforcement.py: takes raw file text, returns `Violation`s.
Callers (tests/test_waevent_bitmap_rule.py) own corpus discovery.
"""

from __future__ import annotations

import re
from pathlib import Path

from livetest.enforcement import Violation

RULE = "waevent-bitmap-rebuild"

#: +/- line window in which a bitmap rebuild / setData / setBefore / fresh construction
#: sanctions an array swap. Widest legitimate gap in the corpus today is 23 lines
#: (a UDF's column-removal method: swap at the top, bitmap rebuild at the bottom).
WINDOW = 25

# -- comment / string blanking (keeps line structure so line numbers stay true) --------
_STRIP_RE = re.compile(
    r"/\*.*?\*/"  # block comments
    r"|//[^\n]*"  # line comments
    r"|\"(?:\\.|[^\"\\\n])*\""  # string literals
    r"|'(?:\\.|[^'\\\n])*'",  # char literals
    re.S,
)


def _blank_comments_and_strings(text: str) -> str:
    return _STRIP_RE.sub(lambda m: re.sub(r"[^\n]", " ", m.group(0)), text)


# -- receiver typing --------------------------------------------------------------------
# A declaration is "<dotted-type>[<generics>][[]] <name>" followed by = ; , ) or :
# (locals, fields, params, catch params, enhanced-for). Method names are excluded
# (followed by "("), casts are excluded (no whitespace-name after the type token).
_DECL_RE = re.compile(
    r"\b([A-Za-z_$][\w$]*(?:\s*\.\s*[A-Za-z_$][\w$]*)*)"  # dotted type
    r"(?:<[^<>;{}]*(?:<[^<>;{}]*>)?[^<>;{}]*>)?"  # generics (1 nest deep)
    r"(?:\s*\[\s*\])*"  # array suffix
    r"\s+([A-Za-z_$][\w$]*)\s*(?=[=;,):])(?!==)"  # variable name
)

_KEYWORDS = frozenset(
    "abstract assert break case catch class const continue default do else enum extends "
    "final finally for goto if implements import instanceof interface native new package "
    "private protected public return static strictfp super switch synchronized this throw "
    "throws transient try void volatile while var record yield".split()
)


def _declarations(clean: str) -> dict[str, list[tuple[int, str]]]:
    """name -> [(offset, simple type name), ...] in file order."""
    decls: dict[str, list[tuple[int, str]]] = {}
    for m in _DECL_RE.finditer(clean):
        type_token, name = m.group(1), m.group(2)
        first_seg = type_token.split(".", 1)[0].strip()
        if first_seg in _KEYWORDS or name in _KEYWORDS:
            continue
        simple = re.split(r"[.\s]", type_token.strip())[-1]
        decls.setdefault(name, []).append((m.start(), simple))
    return decls


def _receiver_is_waevent(
    decls: dict[str, list[tuple[int, str]]], name: str, offset: int
) -> bool:
    """True iff the nearest declaration of `name` at/above `offset` is WAEvent-typed."""
    best: str | None = None
    for decl_off, simple in decls.get(name, ()):
        if decl_off > offset:
            break
        best = simple
    return best == "WAEvent"


# -- the rule ---------------------------------------------------------------------------
_ASSIGN_RE = re.compile(r"\b([A-Za-z_$][\w$]*)\s*\.\s*(data|before)\s*=(?!=)")
_SAFE_RHS_RE = re.compile(r"^\s*(null\s*;|new\s+[\w$.]+\s*(?:<[^<>]*>)?\s*\[)")
_REBUILD_RE = re.compile(r"(?i)bitmap|\bsetData\s*\(|\bsetBefore\s*\(")


def check_waevent_bitmap_rebuild(
    path: Path, text: str, window: int = WINDOW
) -> list[Violation]:
    clean = _blank_comments_and_strings(text)
    decls = _declarations(clean)
    lines = clean.split("\n")
    out: list[Violation] = []
    for m in _ASSIGN_RE.finditer(clean):
        receiver, member = m.group(1), m.group(2)
        if not _receiver_is_waevent(decls, receiver, m.start()):
            continue
        rhs = clean[m.end() : m.end() + 300]
        if _SAFE_RHS_RE.match(rhs):
            continue
        line = clean.count("\n", 0, m.start()) + 1
        ctx = "\n".join(lines[max(0, line - 1 - window) : line + window])
        if _REBUILD_RE.search(ctx):
            continue
        if re.search(
            r"\b" + re.escape(receiver) + r"\s*=\s*new\s+[\w$.]*\bWAEvent\s*[(<]", ctx
        ):
            continue
        out.append(
            Violation(
                path,
                line,
                RULE,
                f"{receiver}.{member} array swap without a presence-bitmap rebuild within "
                f"+/-{window} lines (rebuild dataPresenceBitMap/beforePresenceBitMap or "
                f"repopulate via setData/setBefore)",
            )
        )
    return out
