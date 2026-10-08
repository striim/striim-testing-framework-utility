"""Download and validate a published example, without provisioning or executing it."""
from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import tempfile
from urllib.parse import urlsplit
import zipfile

import yaml

from striim_test.errors import CONFIG, CliError

_LIMIT = 128 * 1024 * 1024


def _fail(message):
    raise CliError(CONFIG, f"fetch: {message}")


def _path(name):
    if not isinstance(name, str):
        _fail("object/archive paths must be strings")
    p = PurePosixPath(name)
    if (not name or not p.parts or p.is_absolute() or ".." in p.parts or "\\" in name
            or any(c in name for c in "*?[]:") or str(p) != name or any(ord(c) < 32 for c in name)):
        _fail(f"unsafe object/archive path: {name!r}")
    return name


def _digest(data, expected, label):
    if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected):
        _fail(f"invalid SHA-256 for {label}")
    if hashlib.sha256(data).hexdigest() != expected:
        _fail(f"SHA-256 mismatch for {label}")


def _json(data, label):
    try:
        obj = json.loads(data)
    except (ValueError, UnicodeError):
        _fail(f"invalid JSON in {label}")
    if not isinstance(obj, dict):
        _fail(f"{label} must be a JSON object")
    return obj


def _download(bucket, prefix, endpoint):
    if endpoint:
        url = urlsplit(endpoint)
        if url.scheme not in {"http", "https"} or not url.netloc or url.username or url.password:
            _fail("endpoint must be an explicit http(s) emulator URL without credentials")
        from google.auth.credentials import AnonymousCredentials
        from google.cloud import storage
        client = storage.Client(project="example-fetch", credentials=AnonymousCredentials(),
                                client_options={"api_endpoint": endpoint})
        def read(name):
            from google.api_core.exceptions import GoogleAPIError
            try:
                return client.bucket(bucket).blob(f"{prefix}/{name}").download_as_bytes()
            except GoogleAPIError as exc:
                _fail(f"emulator download failed ({type(exc).__name__}); check endpoint and seeded objects")
        return read
    def read(name):
        return subprocess.check_output(["gcloud", "storage", "cat", f"gs://{bucket}/{prefix}/{name}"],
                                       stderr=subprocess.PIPE)
    return read


def _case_refs(test: dict) -> list:
    """The files an exported case manifest names: its TQL, DDL and seed files and its goldens.
    Kept out of fetch_bundle so the function that writes never reads a golden reference."""
    refs = [test.get("tql")]
    for key in ("ddl", "seed"):
        refs.extend(item["file"] for item in test.get(key, []))
    for key in ("data", "file"):
        refs.extend(item["match"] for item in test.get("assert", {}).get(key, []))
    return refs


def fetch_bundle(gcs_prefix: str, destination: Path, endpoint: str | None = None, *, read=None):
    """`read` is the hermetic transport seam: takes a prefix-relative object name."""
    url = urlsplit(gcs_prefix)
    prefix = url.path.strip("/")
    if url.scheme != "gs" or not re.fullmatch(r"[a-z0-9][a-z0-9._-]*", url.netloc) or url.query or url.fragment:
        _fail("gcs-prefix must be gs://bucket/tool/release")
    _path(prefix)
    if len(prefix.split("/")) != 2:
        _fail("gcs-prefix must name one tool/release publication")
    destination = Path(destination).absolute()
    if destination.is_symlink() or (destination.exists() and
                                   (not destination.is_dir() or any(destination.iterdir()))):
        _fail("destination must be absent or an empty directory")
    try:
        read = read or _download(url.netloc, prefix, endpoint)
        manifest = _json(read("manifest.json"), "manifest.json")
        name = _path(manifest.get("testBundle"))
        if "/" in name or not name.endswith(".zip"):
            _fail("testBundle must name one ZIP object beside manifest.json")
        blob = read(name)
        if len(blob) > _LIMIT:
            _fail("bundle exceeds size limit")
        _digest(blob, manifest.get("testBundleSha256"), "bundle")
        with zipfile.ZipFile(io.BytesIO(blob)) as archive:
            infos = archive.infolist()
            if len(infos) > 1024 or sum(i.file_size for i in infos) > _LIMIT:
                _fail("archive exceeds extraction limits")
            files = {}
            for info in infos:
                path = _path(info.filename)
                mode = info.external_attr >> 16
                if info.is_dir() or stat.S_IFMT(mode) not in (0, stat.S_IFREG) or info.flag_bits & 1:
                    _fail(f"archive entry must be a regular unencrypted file: {path}")
                if path in files:
                    _fail(f"duplicate archive entry: {path}")
                files[path] = archive.read(info)
        metadata = _json(files.get("bundle.json", b""), "bundle.json")
        pin = metadata.get("frameworkPin")
        if metadata.get("schemaVersion") != 1 or not isinstance(pin, str) or not re.fullmatch(r"[0-9a-f]{40}", pin):
            _fail("unsupported bundle schema or invalid framework pin")
        for key in ("frameworkPin", "caseIds", "caseCount"):
            if metadata.get(key) != manifest.get(key):
                _fail(f"manifest/bundle {key} mismatch")
        release = metadata.get("striimVersion")
        if (not release or release != manifest.get("striimVersion")
                or release != manifest.get("testBundleRelease") or release != prefix.split("/")[1]):
            _fail("manifest/bundle/prefix release mismatch")
        module = metadata.get("module")
        if (not isinstance(module, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]*", module)
                or module != manifest.get("module") or module != prefix.split("/")[0]):
            _fail("manifest/bundle/prefix module mismatch")
        ids = metadata.get("caseIds")
        if not isinstance(ids, list) or len(ids) != 1 or metadata.get("caseCount") != 1:
            _fail("first-version bundle must contain exactly one case")
        case = _path(ids[0])
        if "/" in case or f"cases/{case}/test.yaml" not in files or "gold-targets.yaml" not in files:
            _fail("bundle is missing its case or project manifest")
        hashes = metadata.get("files")
        if not isinstance(hashes, dict) or set(hashes) != set(files) - {"bundle.json"}:
            _fail("bundle file inventory mismatch")
        for path, digest in hashes.items():
            _digest(files[path], digest, path)
        jar = _path(metadata.get("jar"))
        if jar not in files or jar != f"cases/{case}/{manifest.get('jar')}" or not jar.endswith(".jar"):
            _fail("bundle jar path mismatch")
        _digest(files[jar], metadata.get("jarSha256"), "jar")
        _digest(files[jar], manifest.get("jarSha256"), "published jar")
        # Validate the exported dependency references before creating any destination files.
        project = yaml.safe_load(files["gold-targets.yaml"])
        if project != {"schemaVersion": 1, "targets": [], "suites": {"live": "cases"}, "stateDir": ".state"}:
            _fail("unexpected example project manifest")
        test = yaml.safe_load(files[f"cases/{case}/test.yaml"])
        if not isinstance(test, dict) or test.get("name") != case or any(k in test for k in ("example", "udf", "op")):
            _fail("unexpected exported case manifest")
        refs = _case_refs(test)
        if test.get("server_files") != [{"file": PurePosixPath(jar).name, "dest": PurePosixPath(jar).name,
                                         "when": "pre_deploy", "load": "udf"}]:
            _fail("unexpected example jar loading declaration")
        for ref in refs:
            if f"cases/{case}/{_path(ref)}" not in files:
                _fail(f"missing case dependency: {ref}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".striim-fetch-", dir=destination.parent) as tmp:
            staged = Path(tmp) / "example"
            staged.mkdir()
            for path, data in files.items():
                target = staged / path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
            if destination.is_symlink() or (destination.exists() and any(destination.iterdir())):
                _fail("destination became non-empty during fetch")
            os.replace(staged, destination)
        return metadata
    except CliError:
        raise
    except (OSError, ValueError, KeyError, TypeError, AttributeError, zipfile.BadZipFile, subprocess.SubprocessError, yaml.YAMLError) as exc:
        _fail(f"download/validation failed: {type(exc).__name__}: {exc}")


def cmd_fetch(args, origins=None):
    metadata = fetch_bundle(args.gcs_prefix, Path(args.destination), args.endpoint)
    print(f"Fetched {metadata['module']} Striim {metadata['striimVersion']} to {args.destination}")
    print(f"Framework pin: {metadata['frameworkPin']}; read README.md before running")
    return 0
