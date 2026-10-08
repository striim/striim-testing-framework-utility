"""Input snapshot for live cases (C8.3, C4 inputs).

``snapshot(m, manifest_path)`` runs once per case, after the disabled skips and before any provisioning.
It reads the bytes of every file the case references -- manifest, TQL, DDL, seed, server-file sources,
upload sources, generate workloads, lifecycle sentinel SQL and every ``match`` golden of a data or file
assertion. Goldens, the only bytes an exact assertion parses, are kept with a 16 MiB per-file and 64 MiB total
cap; every other input is hashed in chunks and its bytes are not kept. A missing input or an oversized golden
fails the case before provisioning, naming the file. ``strict=False`` (a case without an ``exact:`` block) leaves
a missing input out and hashes an oversized golden without keeping it, so such a case skips or fails where it
did before the snapshot existed.
Exact assertions parse the snapshot bytes and never re-read a golden; ``verify_goldens`` re-hashes them
at finalization. Read-only: nothing here writes a file.

The recorder (``Snapshot.record``, or ``record_on(m, ...)`` from code that holds only the manifest) is
called at the points where rendered bytes are sent -- SQL files, the TQL, upload content, server files and
lifecycle sentinel SQL -- and keeps ``(role, name, templateSha256, renderedSha256) -> uses``. An input that
is never sent is never listed; nothing is hashed for bytes that were not sent.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

PER_FILE_CAP = 16 * 1024 * 1024
TOTAL_CAP = 64 * 1024 * 1024


class InputSnapshotError(Exception):
    """A referenced input is missing, unreadable or over its cap, or a lookup names a file never snapshotted."""


def _sha(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _file_sha(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return "sha256:" + h.hexdigest()


def referenced(m, manifest_path) -> list[tuple[str, Path]]:
    """``(role, path)`` for every file the case references, in manifest order, without duplicates."""
    manifest_path = Path(manifest_path)
    src = Path(getattr(m, "source_dir", None) or manifest_path.parent)
    case = Path(getattr(m, "dir", None) or manifest_path.parent)
    local_names = getattr(m, "local_files", None) or frozenset()

    def at(name):                    # a `local: true` ddl/seed/upload is read from the case dir
        return (case if name in local_names else src) / name

    out = [("manifest", manifest_path)]
    if getattr(m, "tql", None):
        out.append(("tql", src / m.tql))
    out += [("ddl", at(entry[1])) for entry in (getattr(m, "ddl_files", None) or [])]
    out += [("seed", at(entry[1])) for entry in (getattr(m, "seed_files", None) or [])]
    out += [("server-file", local) for entry in (getattr(m, "server_files", None) or [])
            if (local := _server_file_source(src, entry[0])) is not None]
    for spec in getattr(m, "action_specs", None) or []:            # action-time files
        if spec.get("file"):
            out.append(("tql-fragment", src / spec["file"]))
        out += [("seed", at(e[1])) for k in ("stopped_seed", "seed") for e in spec.get(k) or []]
    out += [("upload", at(u["from"])) for u in (getattr(m, "op_uploads", None) or []) if u.get("from")]
    out += [("workload", Path(g["workload"])) for g in (getattr(m, "generate_specs", None) or []) if g.get("workload")]
    lc = getattr(m, "lifecycle", None)
    if lc is not None and getattr(lc, "sentinel", None):
        out += [("sentinel", src / lc.sentinel[k]) for k in ("insert", "delete") if lc.sentinel.get(k)]
    for kind in ("data", "file"):
        for spec in (getattr(m, "assert_", None) or {}).get(kind) or []:
            if isinstance(spec, dict) and spec.get("match"):
                out.append(("golden", case / str(spec["match"])))
    seen, unique = set(), []
    for role, path in out:
        key = str(Path(path).resolve())
        if key not in seen:
            seen.add(key)
            unique.append((role, Path(path)))
    return unique


def _server_file_source(src: Path, name: str):
    """The local file a `server_files` entry reads, or None when it is known only at run time: a
    `gs://` object, or a `${...}` the environment does not set (the run records the bytes it
    sends either way)."""
    if "${" in name:
        from livetest.substitute import SubstitutionError, render
        try:
            name = render(name, dict(os.environ))
        except SubstitutionError:
            return None
    if name.startswith("gs://"):
        return None
    return src / name


def _bytes(value) -> bytes:
    return value if isinstance(value, (bytes, bytearray)) else str(value).encode("utf-8")


def _canonical(value) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def record_on(m, role: str, name, rendered=None, *, path=None, template=None) -> None:
    """Record sent bytes through the snapshot the plugin attached to the manifest (no-op without one)."""
    snap = getattr(m, "_slt_inputs", None)
    if snap is not None:
        snap.record(role, name, rendered, path=path, template=template)


class Snapshot:
    def __init__(self, entries: dict, case_dir=None):
        self.entries = entries          # resolved path -> {"role", "path", "bytes", "sha256"}
        self.case_dir = Path(case_dir) if case_dir is not None else None
        self.rendered: dict = {}        # (role, name, templateSha256, renderedSha256) -> uses

    def record(self, role: str, name, rendered=None, *, path=None, template=None) -> None:
        """One send of an input. ``rendered`` None means the template bytes were sent verbatim."""
        template_sha = None
        if path is not None:
            entry = self.entries.get(str(Path(path).resolve()))
            template_sha = entry["sha256"] if entry else None
        if template_sha is None and template is not None:
            template_sha = _sha(_bytes(template))
        rendered_sha = _sha(_bytes(rendered)) if rendered is not None else template_sha
        if template_sha is None:
            template_sha = rendered_sha
        key = (role, str(name), template_sha, rendered_sha)
        self.rendered[key] = self.rendered.get(key, 0) + 1

    def rendered_inputs(self) -> list:
        return [{"role": r, "name": n, "templateSha256": t, "renderedSha256": s, "uses": u}
                for (r, n, t, s), u in sorted(self.rendered.items())]

    def rel(self, path) -> str:
        base = self.case_dir or Path(".")
        return os.path.relpath(str(path), str(base))

    def case_assets(self) -> dict:
        assets = {"manifestSha256": None, "tqlSha256": None, "goldens": {}, "files": {}}
        for e in self.entries.values():
            if e["role"] == "manifest":
                assets["manifestSha256"] = e["sha256"]
            elif e["role"] == "tql":
                assets["tqlSha256"] = e["sha256"]
            elif e["role"] == "golden":
                assets["goldens"][self.rel(e["path"])] = e["sha256"]
            else:
                assets["files"][self.rel(e["path"])] = e["sha256"]
        return assets

    def logical_inputs_sha256(self) -> str:
        """Template bytes only, so identities (namespace, run, worker) never change it."""
        assets = self.case_assets()
        files = {**assets["goldens"], **assets["files"]}
        if assets["tqlSha256"] is not None:
            files.update({self.rel(e["path"]): e["sha256"] for e in self.entries.values() if e["role"] == "tql"})
        return _sha(_canonical({"manifestSha256": assets["manifestSha256"], "files": files}))

    def rendered_manifest_sha256(self) -> str:
        rendered = sorted({(r, n, s) for (r, n, _t, s) in self.rendered})
        return _sha(_canonical({"manifestSha256": self.case_assets()["manifestSha256"],
                                "rendered": [list(x) for x in rendered]}))

    def _entry(self, path) -> dict:
        entry = self.entries.get(str(Path(path).resolve()))
        if entry is None:
            raise InputSnapshotError(f"input-not-snapshotted: {path} was not read before provisioning")
        return entry

    def golden(self, path) -> bytes:
        return self._entry(path)["bytes"]

    def sha256(self, path) -> str:
        return self._entry(path)["sha256"]

    def goldens(self) -> dict:
        return {e["path"]: e["sha256"] for e in self.entries.values() if e["role"] == "golden"}

    def verify_goldens(self) -> dict:
        """``{path: {inputSha256, finalSha256, unchanged}}``: each golden re-hashed now (bounded read)."""
        out = {}
        for e in self.entries.values():
            if e["role"] != "golden":
                continue
            try:
                with open(e["path"], "rb") as f:
                    final = _sha(f.read(PER_FILE_CAP + 1))
            except OSError:
                final = None
            out[e["path"]] = {"inputSha256": e["sha256"], "finalSha256": final, "unchanged": final == e["sha256"]}
        return out


def snapshot(m, manifest_path, *, strict: bool = True) -> Snapshot:
    entries, total = {}, 0
    for role, path in referenced(m, manifest_path):
        key = str(Path(path).resolve())
        try:
            if role != "golden":
                entries[key] = {"role": role, "path": str(path), "bytes": None, "sha256": _file_sha(path)}
                continue
            with open(path, "rb") as f:
                data = f.read(PER_FILE_CAP + 1)
            if not strict and (len(data) > PER_FILE_CAP or total + len(data) > TOTAL_CAP):
                entries[key] = {"role": role, "path": str(path), "bytes": None, "sha256": _file_sha(path)}
                continue
        except OSError as e:
            if not strict:
                continue
            raise InputSnapshotError(f"input-missing: {role} {path}: {e.strerror or e}") from None
        if len(data) > PER_FILE_CAP:
            raise InputSnapshotError(f"input-too-large: {role} {path} exceeds {PER_FILE_CAP} bytes")
        total += len(data)
        if total > TOTAL_CAP:
            raise InputSnapshotError(f"input-too-large: the case's inputs exceed {TOTAL_CAP} bytes at {path}")
        entries[key] = {"role": role, "path": str(path), "bytes": data, "sha256": _sha(data)}
    return Snapshot(entries, case_dir=Path(getattr(m, "dir", None) or Path(manifest_path).parent))
