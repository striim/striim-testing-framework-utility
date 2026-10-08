"""Canonical rows ``slt-canon/1`` (contract C8.2) and the legacy ``legacy-text/1`` profile (C8.4).

A row is a map from column name to a cell: ABSENT (the key is not in the map), NULL (``None``) or
VALUE(type, text). Rows are serialised as sorted JSON cell lists and hashed with their multiplicity, so
a duplicate, a missing row or an extra row always changes the digest; nothing is deduplicated,
truncated or rounded before comparison. Typed conversions happen only through a declaration; an
undeclared lossy value is refused, never ``str()``-ed.

Pure: no I/O besides the bytes handed in. Readers, routes and ownership are ``livetest.exactdata``.
"""
from __future__ import annotations

import base64
import binascii
import csv
import datetime as _dt
import decimal
import hashlib
import io
import json
import re
from collections import Counter
from pathlib import Path

from livetest.assertions.data import normalized_row

PROFILE = "slt-canon/1"
LEGACY_PROFILE = "legacy-text/1"
NULL_MARKER = "<null>"
ABSENT_MARKER = "<absent>"
ORDERS = ("any", "sequence")
DEFAULT_MAX_ROWS = 100_000
MAX_MAX_ROWS = 1_000_000
DEFAULT_MAX_BYTES = 64 * 1024 * 1024
MAX_MAX_BYTES = 512 * 1024 * 1024
SAMPLE_ROWS = 20
SAMPLE_BYTES = 64 * 1024
DB_ROUTES = ("postgres-source", "postgres-target")
BINDING_TOKENS = ("TID", "TID_UPPER", "TID_ORACLE", "NS", "APP", "APP_BARE", "PG_SLOT", "OWNED_DIR",
                  "RUN_ID", "WORKER", "ATTEMPT")          # plus every SENTINEL_* token
_SPEC_KEYS = {"columns", "ignore", "order", "order_by"}
_BLOCK_KEYS = {"version", "max_rows", "max_bytes"}
_INCOMPATIBLE = ("rows", "min_rows", "ordered", "project")
_TYPE = re.compile(r"^(integer|boolean|timestamptz|timestamp|date|json|binary:hex|binary:base64|decimal:(0|[1-9][0-9]?))$")
_INT = re.compile(r"^-?[0-9]+$")
_DEC = re.compile(r"^[+-]?([0-9]+(\.[0-9]*)?|\.[0-9]+)([eE][+-]?[0-9]+)?$")
_TS = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}[T ][0-9]{2}:[0-9]{2}(:[0-9]{2}(\.[0-9]{1,6})?)?(?P<tz>Z|[+-][0-9]{2}:[0-9]{2})?$")
_DATE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
_HEX = re.compile(r"^([0-9a-fA-F]{2})*$")
_UTC = _dt.timezone.utc


class JsonText(str):
    """Raw JSON text of a Postgres json/jsonb cell, as the exact-read connection's typecaster hands it over
    (r1 F1). Only ``_parse_json`` parses it: numbers stay Decimal with their scale, duplicate keys and
    non-finite constants are refused. The driver's default decoder would already have rounded numbers to float."""
    __slots__ = ()


class CanonError(ValueError):
    """A canonicalization refusal. ``code`` is the stable C8 spelling (``invalid-value:actual:amount``)."""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code


# ---------------------------------------------------------------- declaration

def _sha_text(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def canonical_json(value) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


class Declaration:
    """The resolved normalization of one comparison (C8.2). Copied verbatim into evidence."""

    def __init__(self, columns=None, ignore=None, keys=None, order="any", order_by=None, profile=PROFILE):
        self.columns = dict(columns or {})
        self.ignore = list(ignore or [])
        self.keys = list(keys) if keys is not None else None
        self.order = order
        self.order_by = list(order_by) if order_by is not None else None
        self.profile = profile

    def to_dict(self) -> dict:
        return {"columns": dict(self.columns), "ignore": list(self.ignore), "keys": self.keys,
                "order": self.order, "order_by": self.order_by, "profile": self.profile}

    @property
    def sha256(self) -> str:
        return declaration_sha256(self.to_dict())


def declaration_sha256(declaration: dict) -> str:
    return _sha_text(canonical_json(declaration))


def _str_list(value, what: str) -> list:
    if not isinstance(value, list) or not value or not all(isinstance(v, str) and v for v in value):
        raise CanonError("invalid-declaration", f"{what} must be a non-empty list of column names")
    if len(set(value)) != len(value):
        raise CanonError("invalid-declaration", f"{what} repeats a column")
    return value


def declaration(exact, keys=None, *, db_route: bool) -> Declaration:
    """Resolve a spec-level ``exact`` (``true`` or a mapping) into a Declaration. Raises CanonError."""
    if exact is True:
        exact = {}
    if not isinstance(exact, dict):
        raise CanonError("invalid-declaration", f"exact must be true or a mapping, got {exact!r}")
    unknown = set(exact) - _SPEC_KEYS
    if unknown:
        raise CanonError("invalid-declaration", f"unknown exact keys {sorted(unknown)}")
    columns = exact.get("columns", {})
    if not isinstance(columns, dict):
        raise CanonError("invalid-declaration", "exact.columns must be a mapping of column to type")
    for col, typ in columns.items():
        if not isinstance(col, str) or not col or not isinstance(typ, str) or not _TYPE.match(typ):
            raise CanonError("invalid-declaration", f"exact.columns.{col}: unknown type {typ!r}")
        if typ.startswith("decimal:") and int(typ.split(":")[1]) > 38:
            raise CanonError("invalid-declaration", f"exact.columns.{col}: decimal scale must be 0..38")
    ignore = _str_list(exact["ignore"], "exact.ignore") if "ignore" in exact else []
    order = exact.get("order", "any")
    if order not in ORDERS:
        raise CanonError("invalid-declaration", f"exact.order must be any or sequence, got {order!r}")
    order_by = exact.get("order_by")
    if order_by is not None:
        order_by = _str_list(order_by, "exact.order_by")
        if order != "sequence":
            raise CanonError("invalid-declaration", "exact.order_by requires order: sequence")
        if not db_route:
            raise CanonError("invalid-declaration", "exact.order_by is only for a db route")
    elif order == "sequence" and db_route:
        raise CanonError("invalid-declaration", "order: sequence on a db route requires exact.order_by")
    if set(ignore) & set(columns):
        raise CanonError("invalid-declaration", "a column cannot be both typed and ignored")
    if keys is not None:
        keys = _str_list(keys, "keys")
    return Declaration(columns=columns, ignore=ignore, keys=keys, order=order, order_by=order_by)


# ---------------------------------------------------------------- C8.1 manifest document checks

def limits(block: dict) -> tuple[int, int]:
    return int(block.get("max_rows", DEFAULT_MAX_ROWS)), int(block.get("max_bytes", DEFAULT_MAX_BYTES))


def check_manifest(raw: dict, case_dir=None) -> dict:
    """The C8.1 load-time rules over a parsed manifest document. Returns ``{"block", "specs":
    [(kind, index, Declaration)]}`` or None when the manifest uses no exact semantics. Raises CanonError.
    ``case_dir``, when given, also refuses a ``match`` path that escapes it after ``resolve()``."""
    block = raw.get("exact")
    assertions = raw.get("assert") or {}
    if not isinstance(assertions, dict):
        raise CanonError("invalid-assert-shape",
                         f"assert must be a mapping of tiers, not {type(assertions).__name__}")
    specs = []
    for kind in ("data", "file", "diff"):
        tier = assertions.get(kind) or []
        if not isinstance(tier, list):
            # `diff: true` and the like: each of these tiers is a list of mappings (5.4 M2 FS5).
            raise CanonError("invalid-assert-shape",
                             f"assert.{kind} must be a list of mappings (- target: ...), "
                             f"not {type(tier).__name__} {tier!r}")
        for i, spec in enumerate(tier):
            if isinstance(spec, dict) and "exact" in spec:
                if kind == "diff" and isinstance(spec["exact"], bool) and (block is None or not spec["exact"]):
                    continue            # legacy diff.exact: true without the block (legacy-text/1), or false
                specs.append((kind, i, spec))
    if block is None:
        if specs:
            kind, i, _ = specs[0]
            raise CanonError("exact-block-missing", f"assert.{kind}[{i}].exact requires the top-level exact block")
        return None
    if not isinstance(block, dict) or set(block) - _BLOCK_KEYS:
        raise CanonError("invalid-exact-block", f"top-level exact keys must be within {sorted(_BLOCK_KEYS)}")
    if block.get("version") != 1 or isinstance(block.get("version"), bool):
        raise CanonError("invalid-exact-block", f"exact.version {block.get('version')!r} is not supported")
    mr, mb = block.get("max_rows", DEFAULT_MAX_ROWS), block.get("max_bytes", DEFAULT_MAX_BYTES)
    if isinstance(mr, bool) or not isinstance(mr, int) or not 1 <= mr <= MAX_MAX_ROWS:
        raise CanonError("invalid-exact-block", f"exact.max_rows must be 1..{MAX_MAX_ROWS}")
    if isinstance(mb, bool) or not isinstance(mb, int) or not 1 <= mb <= MAX_MAX_BYTES:
        raise CanonError("invalid-exact-block", f"exact.max_bytes must be 1..{MAX_MAX_BYTES}")
    if not specs:
        raise CanonError("invalid-exact-block", "the top-level exact block has no exact spec (vacuous)")
    resolved = []
    for kind, i, spec in specs:
        where = f"assert.{kind}[{i}]"
        if kind in ("data", "file"):
            if "match" not in spec:
                raise CanonError("invalid-exact-spec", f"{where}: exact requires match")
            bad = [k for k in _INCOMPATIBLE if k in spec]
            if bad:
                raise CanonError("invalid-exact-spec", f"{where}: exact cannot be combined with {bad}")
            if case_dir is not None:
                base = Path(case_dir).resolve()
                target = (base / str(spec["match"])).resolve()
                if target != base and base not in target.parents:
                    raise CanonError("invalid-exact-spec", f"{where}: match {spec['match']!r} escapes the case dir")
        if kind == "data":
            route = spec.get("target_db", spec.get("db", "postgres-source"))
            if route not in DB_ROUTES:
                raise CanonError("exact-route-unsupported", f"{where}: route {route!r} (C8.3: postgres only)")
        elif kind == "diff":
            for end in ("source", "target"):
                route = spec.get(f"{end}_db", spec.get("db", "postgres-source"))
                if route not in DB_ROUTES:
                    raise CanonError("exact-route-unsupported", f"{where}: {end} route {route!r} (C8.3: postgres only)")
        elif spec.get("native"):
            raise CanonError("exact-route-unsupported", f"{where}: native-mode file reads are not exact routes")
        try:
            decl = declaration(spec["exact"], spec.get("keys"), db_route=kind != "file")
        except CanonError as e:
            raise CanonError(e.code, f"{where}: {e}") from None
        resolved.append((kind, i, decl))
    return {"block": block, "specs": resolved}


# ---------------------------------------------------------------- cells

def _invalid(side: str, col: str, detail: str = "") -> CanonError:
    return CanonError(f"invalid-value:{side}:{col}", detail)


def _decimal_text(d: decimal.Decimal, scale: int, side: str, col: str) -> str:
    if not d.is_finite():
        raise _invalid(side, col, "not a finite decimal")
    with decimal.localcontext() as ctx:
        ctx.prec = 80
        ctx.traps[decimal.InvalidOperation] = True
        try:
            q = d.quantize(decimal.Decimal(1).scaleb(-scale))
        except decimal.InvalidOperation:
            raise _invalid(side, col, f"{d} does not fit decimal:{scale}") from None
    if q != d:
        raise CanonError(f"lossy-decimal:{side}:{col}", f"{d} is not exactly representable at scale {scale}")
    if q == 0:
        q = q.copy_abs()
    return format(q, "f")


def _json_text(value, side: str, col: str) -> str:
    if isinstance(value, JsonText):     # an element of a json[]/jsonb[] array cell
        value = _parse_json(value, side, col)
    if isinstance(value, bool) or value is None:
        return json.dumps(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        value = decimal.Decimal(repr(value))
    if isinstance(value, decimal.Decimal):
        if not value.is_finite():
            raise _invalid(side, col, "non-finite JSON number")
        return str(value)
    if isinstance(value, list):
        return "[" + ",".join(_json_text(v, side, col) for v in value) + "]"
    if isinstance(value, dict):
        if not all(isinstance(k, str) for k in value):
            raise _invalid(side, col, "JSON object keys must be strings")
        return "{" + ",".join(json.dumps(k, ensure_ascii=False) + ":" + _json_text(value[k], side, col)
                              for k in sorted(value)) + "}"
    raise _invalid(side, col, f"{type(value).__name__} is not JSON")


def _parse_json(text: str, side: str, col: str):
    def pairs(items):
        keys = [k for k, _ in items]
        if len(set(keys)) != len(keys):
            raise _invalid(side, col, "duplicate JSON object key")
        return dict(items)

    def refuse_constant(name):
        raise _invalid(side, col, f"JSON constant {name} is not allowed")

    try:
        return json.loads(text, parse_float=decimal.Decimal, object_pairs_hook=pairs, parse_constant=refuse_constant)
    except json.JSONDecodeError as e:
        raise _invalid(side, col, f"not JSON text: {e}") from None


def _typed(typ: str, value, side: str, col: str, observed: bool = False) -> str:
    """Canonical text of one non-NULL value under a declared type. ``side`` is expected or actual; the
    expected side is golden text unless ``observed`` (a diff's source rows, read like the actual side)."""
    expected = side == "expected" and not observed
    if isinstance(value, JsonText) and typ != "json":
        raise _invalid(side, col, f"a json value needs exact.columns.{col}: json, not {typ}")
    if expected and not isinstance(value, str):
        raise _invalid(side, col, "golden cells are text")
    if expected and value == "":
        raise _invalid(side, col, f"empty cell in a {typ} column (write {NULL_MARKER} for NULL)")
    if typ == "integer":
        if expected:
            if not _INT.match(value):
                raise _invalid(side, col, f"{value!r} is not an integer")
            return str(int(value))
        if isinstance(value, int) and not isinstance(value, bool):
            return str(value)
        if isinstance(value, decimal.Decimal) and value.is_finite() and value.as_tuple().exponent == 0:
            return str(int(value))
        raise _invalid(side, col, f"{type(value).__name__} is not an integer")
    if typ.startswith("decimal:"):
        scale = int(typ.split(":")[1])
        if expected:
            if not _DEC.match(value):
                raise _invalid(side, col, f"{value!r} is not a decimal")
            d = decimal.Decimal(value)
        elif isinstance(value, decimal.Decimal):
            d = value
        elif isinstance(value, int) and not isinstance(value, bool):
            d = decimal.Decimal(value)
        elif isinstance(value, float):
            d = decimal.Decimal(repr(value))
        else:
            raise _invalid(side, col, f"{type(value).__name__} is not a decimal")
        return _decimal_text(d, scale, side, col)
    if typ == "boolean":
        if expected:
            if value not in ("true", "false"):
                raise _invalid(side, col, f"{value!r} is not true or false")
            return value
        if isinstance(value, bool):
            return "true" if value else "false"
        raise _invalid(side, col, f"{type(value).__name__} is not a boolean")
    if typ in ("timestamptz", "timestamp"):
        if expected:
            match = _TS.match(value)
            if not match:
                raise _invalid(side, col, f"{value!r} is not an ISO 8601 timestamp")
            try:
                value = _dt.datetime.fromisoformat(value)
            except ValueError as e:
                raise _invalid(side, col, str(e)) from None
        elif not isinstance(value, _dt.datetime):
            raise _invalid(side, col, f"{type(value).__name__} is not a datetime")
        aware = value.tzinfo is not None and value.utcoffset() is not None
        if typ == "timestamptz":
            if not aware:
                raise CanonError(f"timestamp-without-zone:{side}:{col}", "timestamptz needs an offset")
            return value.astimezone(_UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        if aware:
            raise CanonError(f"timestamp-with-zone:{side}:{col}", "timestamp must not carry an offset")
        return value.strftime("%Y-%m-%dT%H:%M:%S.%f")
    if typ == "date":
        if expected:
            if not _DATE.match(value):
                raise _invalid(side, col, f"{value!r} is not an ISO date")
            try:
                value = _dt.date.fromisoformat(value)
            except ValueError as e:
                raise _invalid(side, col, str(e)) from None
        elif isinstance(value, _dt.datetime) or not isinstance(value, _dt.date):
            raise _invalid(side, col, f"{type(value).__name__} is not a date")
        return value.isoformat()
    if typ.startswith("binary:"):
        if isinstance(value, (bytes, bytearray, memoryview)) and not expected:
            return bytes(value).hex()
        if not isinstance(value, str):
            raise _invalid(side, col, f"{type(value).__name__} is not binary")
        if typ == "binary:hex":
            if not _HEX.match(value):
                raise _invalid(side, col, "not hex")
            return value.lower()
        try:
            return base64.b64decode(value, validate=True).hex()
        except (binascii.Error, ValueError):
            raise _invalid(side, col, "not base64") from None
    if typ == "json":
        if isinstance(value, str):
            value = _parse_json(value, side, col)
        elif expected or not isinstance(value, (dict, list)):
            raise _invalid(side, col, f"{type(value).__name__} is not JSON")
        return _json_text(value, side, col)
    raise CanonError("invalid-declaration", f"unknown type {typ!r}")       # unreachable after declaration()


def _undeclared(value, side: str, col: str, observed: bool = False) -> str:
    if side == "expected" and not observed:
        return value
    if isinstance(value, JsonText):
        raise CanonError(f"undeclared-conversion:{col}:json",
                         f"declare exact.columns.{col}: json instead of an implicit text conversion")
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (str, int)):
        return str(value)
    raise CanonError(f"undeclared-conversion:{col}:{type(value).__name__}",
                     f"declare exact.columns.{col} with its type instead of an implicit text conversion")


def canon_row(row: dict, decl: Declaration, side: str, observed: bool = False) -> list:
    """One row as its sorted cell list: ``[name, null]`` or ``[name, type, text]``. ``keys`` projects,
    then ``ignore`` removes. An absent key is simply not in the list."""
    if decl.keys is not None:
        row = {k: v for k, v in row.items() if k in decl.keys}
    cells = []
    for name, value in row.items():
        if not isinstance(name, str):
            raise _invalid(side, str(name), "column names are text")
        if name in decl.ignore:
            continue
        if value is None:
            cells.append([name, None])
        elif name in decl.columns:
            cells.append([name, decl.columns[name], _typed(decl.columns[name], value, side, name, observed)])
        else:
            cells.append([name, "text", _undeclared(value, side, name, observed)])
    cells.sort(key=lambda c: c[0])
    return cells


def row_bytes(cells: list) -> bytes:
    return json.dumps(cells, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


# ---------------------------------------------------------------- goldens

def parse_golden(data: bytes, *, db_route: bool, tokens: dict | None = None) -> list[dict]:
    """Golden CSV bytes → rows. ``<null>`` is NULL, ``<absent>`` removes the key (file routes only), an
    empty cell stays the empty text. The literal marker strings stay reserved."""
    if data.startswith(b"\xef\xbb\xbf"):
        raise CanonError("golden-bom", "a golden must not start with a UTF-8 byte order mark")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as e:
        raise CanonError("golden-undecodable", str(e)) from None
    if tokens:
        from livetest.substitute import render
        text = render(text, tokens)
    reader = csv.reader(io.StringIO(text, newline=""))
    try:
        header = next(reader)
    except StopIteration:
        raise CanonError("golden-empty", "a golden needs a header row") from None
    if len(set(header)) != len(header) or not all(header):
        raise CanonError("golden-header", f"duplicate or empty column names in {header}")
    rows = []
    for n, record in enumerate(reader, 2):
        if len(record) != len(header):
            raise CanonError("golden-ragged", f"line {n}: {len(record)} cells for {len(header)} columns")
        row = {}
        for col, val in zip(header, record):
            if val == ABSENT_MARKER:
                if db_route:
                    raise CanonError(f"absent-marker-db-route:{col}",
                                     f"{ABSENT_MARKER} has no meaning for db rows; use {NULL_MARKER}")
                continue
            row[col] = None if val == NULL_MARKER else val
        rows.append(row)
    return rows


# ---------------------------------------------------------------- sides, digests, comparison

def _canon_side(rows, decl: Declaration, side: str, max_rows: int, max_bytes: int, observed: bool = False) -> list[bytes]:
    out, total = [], 0
    for row in rows:
        if len(out) >= max_rows:
            raise CanonError("canonical-limit-exceeded", f"{side} side has more than {max_rows} rows")
        b = row_bytes(canon_row(row, decl, side, observed or side == "actual"))
        total += len(b) + 1
        if total > max_bytes:
            raise CanonError("canonical-limit-exceeded", f"{side} side exceeds {max_bytes} canonical bytes")
        out.append(b)
    return out


def digest(rows: list[bytes], decl_sha: str, order: str) -> str:
    h = hashlib.sha256()
    h.update(b"slt-canon/1\n" + b"order=" + order.encode() + b"\n" + b"decl=" + decl_sha.encode() + b"\n")
    for b in (sorted(rows) if order == "any" else rows):
        h.update(b + b"\n")
    return "sha256:" + h.hexdigest()


def logical_rows(rows: list[bytes], bindings: dict) -> list[bytes]:
    """Replace every exact occurrence of a run-binding token value in every ``text`` cell with
    ``${NAME}`` (longest value first, one pass), so rows that differ only by namespace agree."""
    pairs = sorted(((str(v), k) for k, v in (bindings or {}).items() if v not in (None, "")),
                   key=lambda p: (-len(p[0]), p[1]))
    if not pairs:
        return list(rows)
    names = dict(pairs)
    pattern = re.compile("|".join(re.escape(v) for v, _ in pairs))
    out = []
    for b in rows:
        cells = json.loads(b)
        for cell in cells:
            if len(cell) == 3 and cell[1] == "text":
                cell[2] = pattern.sub(lambda m: "${" + names[m.group(0)] + "}", cell[2])
        out.append(row_bytes(cells))
    return out


def _check_order_total(rows, decl: Declaration) -> None:
    seen = set()
    for row in rows:
        if any(k not in row for k in decl.order_by):
            raise CanonError("order-not-total", f"a row lacks an order_by column {decl.order_by}")
        key = row_bytes(canon_row({k: row[k] for k in decl.order_by},
                                  Declaration(columns={k: t for k, t in decl.columns.items() if k in decl.order_by}),
                                  "actual"))
        if key in seen:
            raise CanonError("order-not-total", f"order_by {decl.order_by} has a tie")
        seen.add(key)


def _samples(exp: list[bytes], act: list[bytes], order: str) -> tuple[dict, bool]:
    budget, truncated = SAMPLE_BYTES, False

    def take(counter: Counter) -> list:
        nonlocal budget, truncated
        out = []
        for b in sorted(counter):
            if len(out) >= SAMPLE_ROWS or len(b) > budget:
                truncated = True
                break
            budget -= len(b)
            out.append({"row": json.loads(b), "count": counter[b]})
        return out

    ec, ac = Counter(exp), Counter(act)
    samples = {"missing": take(ec - ac), "extra": take(ac - ec)}
    if order == "sequence":
        idx = next((i for i, (e, a) in enumerate(zip(exp, act)) if e != a), None)
        if idx is None and len(exp) != len(act):
            idx = min(len(exp), len(act))
        if idx is not None:
            pair = {"index": idx}
            for name, side in (("expected", exp), ("actual", act)):
                b = side[idx] if idx < len(side) else None
                if b is not None and len(b) > budget:
                    truncated, b = True, None
                budget -= len(b) if b is not None else 0
                pair[name] = json.loads(b) if b is not None else None
            samples["firstMismatch"] = pair
    return samples, truncated


def compare(expected_rows, actual_rows, decl: Declaration, *, max_rows: int = DEFAULT_MAX_ROWS,
            max_bytes: int = DEFAULT_MAX_BYTES, bindings: dict | None = None, expected_observed: bool = False) -> dict:
    """Compare golden rows (expected) with observed rows (actual) under ``decl``. Returns the comparison
    record's canonical part; raises CanonError on a refusal. Digests always cover every row.
    ``expected_observed``: the expected side is observed rows too (a diff's source), typed like actual."""
    present = set()
    for row in list(expected_rows) + list(actual_rows):
        present.update(row)
    missing_ignore = [c for c in decl.ignore if c not in present]
    if missing_ignore:
        raise CanonError("ignored-column-not-present", f"ignore names {missing_ignore}, present on neither side")
    exp = _canon_side(expected_rows, decl, "expected", max_rows, max_bytes, expected_observed)
    act = _canon_side(actual_rows, decl, "actual", max_rows, max_bytes)
    if decl.order == "sequence" and decl.order_by:
        _check_order_total(actual_rows, decl)
        if expected_observed:
            _check_order_total(expected_rows, decl)
    dsha = decl.sha256
    e_sha, a_sha = digest(exp, dsha, decl.order), digest(act, dsha, decl.order)
    samples, truncated = _samples(exp, act, decl.order)
    return {"profile": PROFILE, "order": decl.order, "declaration": decl.to_dict(), "declarationSha256": dsha,
            "expected": {"sha256": e_sha, "rowCount": len(exp)},
            "actual": {"sha256": a_sha, "rowCount": len(act)},
            "logical": {"expectedSha256": digest(logical_rows(exp, bindings), dsha, decl.order),
                        "actualSha256": digest(logical_rows(act, bindings), dsha, decl.order)},
            "equal": e_sha == a_sha, "samples": samples, "sampleTruncated": truncated,
            "limits": {"maxRows": max_rows, "maxBytes": max_bytes}}


def aggregate(shas: list[str]) -> str:
    """``data.canonicalSha256`` / ``expectedSha256``: sha256 over ``<index>:<sha>\\n`` in index order."""
    return _sha_text("".join(f"{i}:{s}\n" for i, s in enumerate(shas)).encode("utf-8"))


# ---------------------------------------------------------------- legacy-text/1 (C8.4)

def legacy_text_multiset(rows: list) -> Counter:
    """``diff.exact: true`` without the top-level block: the stringified multiset the 1.8.0 diff used.
    Recorded as a comparison, never qualifying (Decimal, datetime and float are ``str()``-ed)."""
    return Counter(frozenset(normalized_row(r).items()) for r in rows)
