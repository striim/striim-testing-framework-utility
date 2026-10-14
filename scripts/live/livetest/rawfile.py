"""Byte-exact file inventories; records never stand in for the original bytes."""
from __future__ import annotations

import hashlib
import posixpath
import re
import time

from livetest import canon, exactdata, ownership, strictjson
from livetest.assertions import AssertionFailed
from livetest.resultschema import build_assertion_result
from livetest.substitute import render


def validate_specs(specs):
    for spec in specs:
        if "raw" not in spec:
            continue
        def refuse(message):
            raise ValueError(f"invalid-raw-file-spec: {message}")
        if set(spec) != {"path", "raw"} or not isinstance(spec["path"], str):
            refuse("raw specs take only path and raw")
        if not spec["path"].startswith("${OWNED_DIR}/") or ".." in spec["path"].split("/"):
            refuse("path must be a file under ${OWNED_DIR}")
        raw = spec["raw"]
        if not isinstance(raw, dict) or set(raw) - {"files", "format", "max_bytes"}:
            refuse("raw takes files, format and max_bytes")
        if raw.get("format", "lines") not in ("lines", "json"):
            refuse("format must be lines or json")
        limit = raw.get("max_bytes", canon.DEFAULT_MAX_BYTES)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= canon.MAX_MAX_BYTES:
            refuse("max_bytes is outside the supported byte budget")
        files = raw.get("files")
        if not isinstance(files, dict) or not files:
            refuse("files must be a non-empty inventory")
        for name, values in files.items():
            if not isinstance(name, str) or name in (".", "..") or "/" in name or not name:
                refuse("inventory names must be basenames")
            if not isinstance(values, dict) or set(values) - {"bytes", "sha256", "records"} or not {"bytes", "sha256"} <= set(values):
                refuse("each file needs bytes and sha256, with optional records")
            for field in ("bytes", "records"):
                if field in values and (isinstance(values[field], bool) or not isinstance(values[field], int) or values[field] < 0):
                    refuse(f"{name}.{field} must be a non-negative integer")
            if not isinstance(values["sha256"], str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", values["sha256"]):
                refuse(f"{name}.sha256 must be sha256: followed by 64 lowercase hex digits")
        if sum(v["bytes"] for v in files.values()) > limit:
            refuse("expected bytes exceed max_bytes")
    return specs


def _inventory(directory):
    ownership._safe_path(directory)
    # Include hidden files: an exact inventory must not hide an unexpected dotfile.
    return [(node, path) for node in ownership._nodes()
            for pattern in ("*", ".[!.]*", "..?*")
            for path in ownership.docker_ls(posixpath.join(directory, pattern), nodes=[node])]


def _read_inventory(spec, tokens, remaining, inventory, read):
    path = render(spec["path"], tokens)
    directory = posixpath.dirname(path)
    raw = spec["raw"]
    limit = raw.get("max_bytes", canon.DEFAULT_MAX_BYTES)
    deadline, total, rows = time.monotonic() + remaining, 0, []
    found = inventory(directory)
    if len(found) > len(raw["files"]):
        raise ValueError("raw-file-inventory-mismatch: extra files or duplicate names across nodes")
    names = [posixpath.basename(p) for _, p in found]
    if sorted(names) != sorted(raw["files"]):
        raise ValueError(f"raw-file-inventory-mismatch: expected {sorted(raw['files'])}, found {sorted(names)}")
    for node, file in found:
        if posixpath.dirname(file) != directory or posixpath.normpath(file) != file:
            raise ValueError("raw-file-inventory-mismatch: inventory escaped the output directory")
        data = read(node, file, limit - total, max(0.05, deadline - time.monotonic()))
        total += len(data)
        if total > limit:
            raise ValueError("raw-file-limit-exceeded: output exceeds max_bytes")
        name = posixpath.basename(file)
        row = {"name": name, "bytes": len(data), "sha256": "sha256:" + hashlib.sha256(data).hexdigest()}
        if "records" in raw["files"][name]:
            if raw.get("format", "lines") == "json":
                try:
                    row["records"] = len(strictjson.events(data.decode("utf-8")))
                except ValueError as e:
                    raise ValueError(f"raw-file-invalid-json: {e}") from None
            else:
                # LF-delimited records: CR is data, not a line-ending conversion.
                row["records"] = data.count(b"\n") + int(bool(data) and not data.endswith(b"\n"))
        rows.append(row)
    return sorted(rows, key=lambda r: r["name"])


def assert_raw_files(specs, *, timeout, tokens, mode, inventory=None, read=None, owned=None,
                     status_probe=None, lifecycle=False, poll=2):
    validate_specs(specs)
    records = []
    for spec in specs:
        expected = sorted([{"name": n, **v} for n, v in spec["raw"]["files"].items()], key=lambda r: r["name"])
        deadline, actual, detail, ok = time.monotonic() + timeout, [], "", False
        while True:
            if status_probe:
                status_probe()
            try:
                if mode != "docker":
                    raise ValueError("raw-file-route-unsupported: raw files require Docker mode")
                path = render(spec["path"], tokens)
                if owned is not None and not owned("owned-file", None, path):
                    raise ValueError("raw-file-target-not-owned: output directory was not allocated by this attempt")
                actual = _read_inventory(spec, tokens, max(0.05, deadline - time.monotonic()),
                                         inventory or _inventory, read or exactdata.head_file)
                ok = actual == expected
                detail = "raw files equal" if ok else "raw-file-mismatch: byte hash, size or per-file count differs"
            except Exception as e:
                detail = str(e)
            if ok or lifecycle or time.monotonic() >= deadline or detail.startswith(("raw-file-route-unsupported", "raw-file-target-not-owned")):
                break
            time.sleep(min(poll, max(0, deadline - time.monotonic())))
        records.append(build_assertion_result(type="file", status="passed" if ok else "failed", spec=spec,
                       target=render(spec["path"], tokens), detail=detail,
                       expected={"kind": "rows", "rows": expected}, actual={"kind": "rows", "rows": actual}))
    if any(r["status"] == "failed" for r in records):
        raise AssertionFailed("; ".join(r["detail"] for r in records if r["status"] == "failed"), records)
    return records
