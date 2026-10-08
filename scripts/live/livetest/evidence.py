"""The evidence envelope v2 for live cases (C7.4; C4 1.9.0: the complete envelope).

One envelope per executed outcome (pass, fail, error, skip), written once from
``pytest_runtest_makereport``, so a case that raised still produces its record. Path:
``<junit dir>/evidence/<pytest item name>/<run id>/evidence.json``. Without ``--junitxml`` nothing is
written. The junit ``<properties>`` gain ``slt_evidence_json`` and ``slt_qualifies``.

Every field is computed from what the run observed, never a placeholder: input hashes from the input
snapshot and the bytes recorded at the send sites, canonical data from the ``slt-canon/1`` comparisons,
identities from bounded probes. A field that is inapplicable carries a reason only where C4 allows it; a
value that could not be read is ``{"unreadable": ...}`` or ``{"unobserved": ...}`` and the case never
qualifies. The document is redacted, validated (``validate_case``, which recomputes ``run.qualifies``)
and written exclusively. A build, validation or write failure is an evidence error and part of the run's
outcome (``fold_evidence_errors``), never only a warning.
"""
from __future__ import annotations

import functools
import hashlib
import json
import os
import platform
import re
import socket
import subprocess
import sys
from pathlib import Path

EVIDENCE_VERSION = 2
READ_CAP = 16 * 1024 * 1024
GIT_TIMEOUT_S = 5.0
DIRTY_CAP = 1000
SECRET_MIN_LEN = 8
REDACTED = "[redacted]"
NO_DATA_REASON = "no exact data assertion"
NO_BYTES_FORM = {"unobserved": "the sent bytes were not snapshotted"}
NO_TQL_FORM = {"unreadable": "the case's TQL was not read into the input snapshot"}
UNREADABLE_GOLDEN = "the golden could not be re-read at finalization"
CLEANUP_STATUSES = {"ok", "failed", "skipped"}
NO_CLEANUP_RECORD = {"status": "skipped",
                     "detail": "no cleanup record: the case stopped before its ownership ledger cleanup ran"}
NO_LEDGER_GAP = "no ownership ledger result for this case"
TOP_KEYS = {"evidenceVersion", "kind", "run", "inputs", "runtime", "lifecycle", "assertions", "data", "reports",
            "resources", "integrity", "review"}
RUN_KEYS = {"runId", "attempt", "caseId", "suite", "tier", "startedAt", "endedAt", "status", "skipReason",
            "failure", "qualifies", "qualifiesReason", "invocation", "nodeid"}
INPUT_KEYS = {"source", "framework", "productBuild", "caseAssets", "logicalInputsSha256", "renderedInputs",
              "renderedManifestSha256", "bindings"}
FRAMEWORK_KEYS = {"mode", "provenance", "importPath", "wheelSha256", "lockSha256"}
ASSET_KEYS = {"manifestSha256", "tqlSha256", "goldens", "files"}
RENDERED_KEYS = {"role", "name", "templateSha256", "renderedSha256", "uses"}
RUNTIME_KEYS = {"os", "arch", "python", "interpreter", "mode", "importPath", "resourceRoot", "stateDir", "hostId",
                "jdk", "striim", "services"}
OBSERVED_KEYS = {"url", "version", "container", "image", "imageId", "consoleReachable"}
DATA_KEYS = {"profile", "canonicalSha256", "expectedSha256", "rowCount", "sampleTruncated", "normalization",
             "comparisons"}
COMPARISON_KEYS = {"index", "type", "target", "route", "profile", "order", "declaration", "declarationSha256",
                   "expected", "actual", "logical", "equal", "samples", "sampleTruncated", "limits", "error"}
INTEGRITY_KEYS = {"goldens", "evidenceErrors"}
# Where a {"reason": ...} form is legal, and which reasons (C4 1.9.0; "*" is any list index).
REASONS = {
    ("inputs", "source"): {"not-a-git-worktree"},
    ("inputs", "productBuild"): {"app-only case: no op/udf"},
    ("runtime", "jdk"): {"live tier process runs no JVM"},
    ("runtime", "striim", "observed", "imageId"): {"native endpoint"},
    ("runtime", "striim", "observed", "image"): {"native endpoint"},
    ("runtime", "services", "*", "image"): {"external service"},
    ("runtime", "services", "*", "imageId"): {"external service"},
    ("data",): {NO_DATA_REASON},
}
# Identity fields redaction keeps verbatim (C4 1.9.0): run identity, bindings, resource names.
_RUN_IDENTITY_KEYS = RUN_KEYS - {"failure", "skipReason", "qualifiesReason"}
# The legacy partial envelope, still readable (never qualifies).
PARTIAL_MISSING_41 = ["inputs.source", "inputs.productBuild", "inputs.renderedManifestSha256", "data"]
_TOP_KEYS_41 = {"evidenceVersion", "partial", "run", "inputs", "runtime", "lifecycle", "assertions",
                "reports", "resources", "review"}
_RUN_KEYS_41 = RUN_KEYS - {"invocation", "nodeid"}
_STATUSES = {"passed", "failed", "skipped", "error"}
_SAFE = re.compile(r"[^A-Za-z0-9._-]+")
_SHA = re.compile(r"^sha256:[0-9a-f]{64}$")
_SECRET_NAME = re.compile(r"(?i)(PASS|PASSWORD|SECRET|TOKEN|KEY|CREDENTIAL|AUTH|COMPANY_NAME|CLUSTER_NAME)")
_USERINFO = re.compile(r"(?P<s>[A-Za-z][A-Za-z0-9+.-]*://)[^/@\s]+@")
_FORMS = ("unreadable", "unobserved")


class EvidenceError(ValueError):
    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code


class Envelope:
    def __init__(self, doc: dict, complete: bool):
        self.doc, self.complete = doc, complete
        self.qualifies = bool(complete and doc["run"]["qualifies"])


def _safe(part: str) -> str:
    return _SAFE.sub("_", str(part)).strip("._") or "_"


def envelope_path(junit_path, item_name: str, run_id: str) -> Path:
    return Path(junit_path).parent / "evidence" / _safe(item_name) / _safe(run_id) / "evidence.json"


def _sha(path: Path):
    try:
        return "sha256:" + hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError:
        return None


def dumps(doc: dict) -> str:
    """The envelope's canonical serialization; a non-JSON value raises instead of being ``str()``-ed."""
    def refuse(value):
        raise TypeError(f"not JSON-serializable in evidence: {type(value).__name__}")
    return json.dumps(doc, sort_keys=True, indent=2, ensure_ascii=False, default=refuse) + "\n"


# ---------------------------------------------------------------- redaction

_REGISTERED_SECRETS: set = set()
_TOKEN_MIN_LEN = 3                  # below this a secret is redacted only where a whole string equals it
_TOKEN_CHARS = r"A-Za-z0-9_.\-/@"   # a short secret inside a longer identifier (slt-striim) is not that secret; a colon
                                     # delimits (admin:striim), it does not extend an identifier
# An explicit credential assignment: a secret-named key, its delimiter (= or :) and an optional quote. The value after
# it is that credential whatever its length (password=ab, password:striim).
_ASSIGNMENT = r"(?P<pre>(?i:[A-Za-z0-9_.\-]*(?:PASS|PWD|SECRET|TOKEN|KEY|CREDENTIAL|AUTH)[A-Za-z0-9_.\-]*)[\"']?\s*[=:]\s*[\"']?)"


_CREDENTIAL_KEY = re.compile(r"(?i)(PASS|SECRET|KEY|TOKEN|COMPANY_NAME|CLUSTER_NAME)")

# A licence's company or cluster name is secret, except a public name: a Striim licence holds
# COMPANY_NAME=Striim, the product and company name. Only these keys are exempt, so a password
# that happens to be the same word (the Docker cluster's default is "striim") stays a secret.
PUBLIC_NAMES = frozenset({"striim"})
# The whole key must name a licence name (LIC_COMPANY_NAME too), so CLUSTER_NAME_PASSWORD is not one.
_LICENCE_NAME = re.compile(r"(?i)(?:[A-Za-z0-9_]*_)?(?:COMPANY|CLUSTER)_NAME")


def _public_licence_name(key: str, value) -> bool:
    return bool(_LICENCE_NAME.fullmatch(key)) and str(value).strip().lower() in PUBLIC_NAMES


def register_secrets(mapping) -> None:
    """Resolved credentials that never pass through the environment -- for example a resolved service's admin DSN
    passwords -- become known secrets for this process. Only non-empty string values under
    credential-like keys count; a ``*_port`` (gcs ``token_port: 4444``) or any other number is not a credential."""
    for key, value in (mapping or {}).items():
        key = str(key)
        if _CREDENTIAL_KEY.search(key) and not key.lower().endswith("_port") and isinstance(value, str) and value \
                and not _public_licence_name(key, value):
            _REGISTERED_SECRETS.add(value)


def secret_env_names(env=None) -> list[str]:
    """The environment variables whose values ``known_secrets`` treats as secrets: the secret-named ones that are set."""
    env = os.environ if env is None else env
    return sorted(k for k, v in env.items() if _SECRET_NAME.search(k) and v and not _public_licence_name(k, v))


def known_secrets(env=None, extra=()) -> list[str]:
    """Every known secret value, longest first: secret-named environment variables, registered resolved credentials
    and ``extra``. None is dropped for being short; ``redact_text`` decides how each is matched."""
    env = os.environ if env is None else env
    values = {str(env[k]) for k in secret_env_names(env)}
    values |= _REGISTERED_SECRETS
    values |= {str(v) for v in extra if v}
    return sorted(values, key=lambda v: (-len(v), v))


def _secret_pattern(secret: str):
    return re.compile(rf"(?<![{_TOKEN_CHARS}]){re.escape(secret)}(?![{_TOKEN_CHARS}])")


def _assignment_pattern(secret: str):
    return re.compile(_ASSIGNMENT + re.escape(secret) + rf"(?![{_TOKEN_CHARS}])")


def _contains_secret(text: str, secret: str) -> bool:
    """The redactor's matching rules: a long secret anywhere; a short one as the whole value, as the value of an
    explicit credential assignment (any length) or as a whole token (3 or more characters)."""
    if not secret or secret not in text:
        return False
    if len(secret) >= SECRET_MIN_LEN or text == secret:
        return True
    if _assignment_pattern(secret).search(text):
        return True
    return len(secret) >= _TOKEN_MIN_LEN and _secret_pattern(secret).search(text) is not None


def host_id(hostname: str | None = None) -> str:
    return hashlib.sha256((hostname or socket.gethostname()).encode("utf-8")).hexdigest()[:16]


def redact_text(text, secrets, *, home: str | None = None, hostname: str | None = None):
    if not isinstance(text, str):
        return text
    text = _USERINFO.sub(r"\g<s>", text)
    for secret in secrets:
        if not secret or secret not in text:
            continue
        if len(secret) >= SECRET_MIN_LEN:           # long: every occurrence
            text = text.replace(secret, REDACTED)
        elif text == secret:                          # short: an exact value (a sample cell) ...
            text = REDACTED
        else:                                         # ... an assigned credential of any length (password:ab) ...
            text = _assignment_pattern(secret).sub(lambda m: m.group("pre") + REDACTED, text)
            if len(secret) >= _TOKEN_MIN_LEN:         # ... or a whole token, never inside an identifier (slt-striim)
                text = _secret_pattern(secret).sub(REDACTED, text)
    home = str(Path.home()) if home is None else home
    if home and len(home) > 1 and home in text:
        text = text.replace(home, "~")
    hostname = socket.gethostname() if hostname is None else hostname
    if hostname and len(hostname) >= 3 and hostname in text:
        text = text.replace(hostname, host_id(hostname))
    return text


@functools.lru_cache(maxsize=1)
def _grammar() -> dict:
    """Envelope fields whose value comes from a fixed vocabulary of the schema itself."""
    from livetest import canon
    profiles, kinds = {canon.PROFILE, canon.LEGACY_PROFILE}, {"data", "diff", "file"}
    return {
        ("kind",): {"case"},
        ("run", "status"): set(_STATUSES),
        ("lifecycle", "mode"): {"initial-load", "cdc", "legacy", "unknown"},
        ("lifecycle", "ready", "reason"): {"satisfied"},
        ("lifecycle", "completion", "reason"): {"satisfied"},
        ("lifecycle", "cleanup", "status"): set(CLEANUP_STATUSES),
        ("assertions", "*", "type"): kinds,
        ("assertions", "*", "status"): {"passed", "failed"},
        ("data", "profile"): profiles,
        ("data", "comparisons", "*", "type"): kinds,
        ("data", "comparisons", "*", "profile"): profiles,
        ("data", "comparisons", "*", "order"): set(canon.ORDERS),
        ("data", "comparisons", "*", "declaration", "profile"): {canon.PROFILE},
        ("data", "comparisons", "*", "declaration", "order"): set(canon.ORDERS),
    }


def _column_type(value: str) -> bool:
    """C8.2's own rule for a declared column type (``integer``, ``decimal:2``): the grammar, not a free string."""
    from livetest import canon
    try:
        canon.declaration({"columns": {"c": value}}, None, db_route=True)
    except canon.CanonError:
        return False
    return True


# The only two paths whose ``columns`` mapping is declaration syntax -- the canonical
# declaration itself and the manifest spec an exact record echoes. Any other ``columns`` mapping (a retained runtime
# result row, a sample) is runtime data and is redacted and scanned as usual.
_COLUMN_TYPE_PATHS = (("data", "comparisons", "*", "declaration", "columns"),
                      ("assertions", "*", "spec", "exact", "columns"))


def _syntax(path: tuple, value) -> bool:
    """The schema's own grammar fixes this value -- a comparison's type, profile and order,
    a canonical declaration's profile, order and column types (``decimal:2``), an allowlisted reason, a status enum.
    Such a token is determined by the schema, never by runtime data, so it can carry no credential, while validation
    and the declaration digest do read it: redaction leaves it alone and the secret scan skips it. Every other string
    in those sections -- an identifier, a path, any free text -- is redacted and scanned as usual."""
    if not isinstance(value, str):
        return False
    p = tuple("*" if isinstance(x, int) else x for x in path)
    if p[-1:] == ("reason",) and value in REASONS.get(p[:-1], ()):
        return True
    if len(p) == 6 and p[:5] in _COLUMN_TYPE_PATHS:
        return _column_type(value)      # a declared column type, in a declaration or in the spec a record echoes
    return value in _grammar().get(p, ())


def _kept(path: tuple) -> bool:
    if path[:1] == ("run",) and len(path) == 2 and path[1] in _RUN_IDENTITY_KEYS:
        return True
    if path[:2] == ("inputs", "bindings"):
        return True
    return len(path) == 4 and path[0] == "resources" and path[1] in ("owned", "reused", "foreign") and path[3] == "name"


def redact(doc, secrets, *, home: str | None = None, hostname: str | None = None, _path: tuple = ()):
    """A redacted copy: every string except ``sha256:`` values and the identity allowlist. Hashes were
    computed on the raw bytes before this runs."""
    if isinstance(doc, dict):
        return {k: redact(v, secrets, home=home, hostname=hostname, _path=_path + (k,)) for k, v in doc.items()}
    if isinstance(doc, list):
        return [redact(v, secrets, home=home, hostname=hostname, _path=_path + (i,)) for i, v in enumerate(doc)]
    if isinstance(doc, str) and not doc.startswith("sha256:") and not _kept(_path) and not _syntax(_path, doc):
        return redact_text(doc, secrets, home=home, hostname=hostname)
    return doc


# ---------------------------------------------------------------- identities

def source_identity(root, run=None) -> dict:
    """``inputs.source``: ``{repo, head, dirty[]}`` from bounded git calls; a non-worktree is the reason
    ``not-a-git-worktree``; a timeout or any other failure is ``{unreadable}``."""
    run = run or (lambda argv: subprocess.run(argv, capture_output=True, text=True, timeout=GIT_TIMEOUT_S))
    root = str(root)
    try:
        head = run(["git", "-C", root, "rev-parse", "HEAD"])
        if head.returncode != 0:
            if "not a git repository" in (head.stderr or "").lower():
                return {"reason": "not-a-git-worktree"}
            return {"unreadable": f"git rev-parse HEAD rc {head.returncode}: {(head.stderr or '').strip()[:200]}"}
        status = run(["git", "-C", root, "status", "--porcelain=v1", "-z"])
        if status.returncode != 0:
            return {"unreadable": f"git status rc {status.returncode}: {(status.stderr or '').strip()[:200]}"}
    except subprocess.TimeoutExpired:
        return {"unreadable": f"git did not answer within {GIT_TIMEOUT_S:.0f}s"}
    except OSError as e:
        return {"unreadable": f"git could not run: {e}"}
    dirty = [entry[3:] for entry in (status.stdout or "").split("\0") if len(entry) > 3]
    if len(dirty) > DIRTY_CAP:
        return {"unreadable": f"more than {DIRTY_CAP} dirty paths"}
    return {"repo": root, "head": (head.stdout or "").strip(), "dirty": dirty}


def consumer_root(config) -> Path:
    """The project whose cases run, as ``livetest.project`` resolves it: the active project's root
    (``striim-test --targets``), else the ``GOLD_TARGETS`` manifest's dir (a set but missing one
    raises ``PathConfigError``), else ``paths.project_root()`` (``SLT_PROJECT_ROOT``, else this
    checkout). ``config`` is kept for the plugin's call sites."""
    from livetest import paths, project
    if project._ACTIVE is not None:
        return project._ACTIVE.root
    named = project.load_project()
    return named.root if named is not None else paths.project_root()


def _source(config) -> dict:
    cached = getattr(config, "_slt_source_identity", None)
    if cached is None:
        cached = source_identity(consumer_root(config))
        try:
            config._slt_source_identity = cached
        except Exception:               # noqa: BLE001
            pass
    return cached


def framework_identity(livetest_module, env=None) -> dict:
    env = os.environ if env is None else env
    mode = env.get("SLT_FRAMEWORK_MODE") or "unknown"
    ident = {"mode": mode, "provenance": getattr(livetest_module, "__provenance__", None),
             "importPath": str(Path(livetest_module.__file__).parent), "wheelSha256": None, "lockSha256": None}
    if mode != "wheel":
        return ident
    path = env.get("SLT_RUN_IDENTITY")
    if not path:
        why = {"unobserved": "SLT_RUN_IDENTITY is not set: the wheel and lock digests were not observed"}
        return {**ident, "wheelSha256": why, "lockSha256": why}
    try:
        with open(path, "rb") as f:
            doc = json.loads(f.read(1024 * 1024))
        fw = doc.get("framework") or {}
        for key in ("wheelSha256", "lockSha256"):
            value = fw.get(key)
            ident[key] = value if isinstance(value, str) and _SHA.match(value) else {"unobserved": f"{path}: no {key}"}
    except (OSError, ValueError) as e:
        why = {"unreadable": f"{path}: {e}"}
        ident.update(wheelSha256=why, lockSha256=why)
    return ident


# ---------------------------------------------------------------- outcome

def final_outcome(status: str, lc_state, cleanup: dict, resources: dict, *, exact: dict | None = None
                  ) -> tuple[str, bool, str | None]:
    """One source of truth for the case's final status and whether it qualifies. ``exact`` (the C7.3
    1.9.0 clauses) is ``{data, integrity, assertions, blocked}``; legacy callers omit it."""
    cleanup = cleanup or NO_CLEANUP_RECORD
    if status == "passed" and cleanup.get("status") == "failed":
        status = "failed"
    reason = None
    lc = lc_state.to_dict() if hasattr(lc_state, "to_dict") else (lc_state or {})
    if lc.get("mode") in (None, "legacy", "unknown"):
        reason = "legacy lifecycle (no lifecycle block) never qualifies"
    elif status != "passed":
        reason = f"run status {status}"
    elif (lc.get("ready") or {}).get("reason") != "satisfied":
        reason = "readiness witness not satisfied"
    elif (lc.get("completion") or {}).get("reason") != "satisfied":
        reason = "completion witness not satisfied"
    elif cleanup.get("status") != "ok":
        reason = f"cleanup {cleanup.get('status')}: {cleanup.get('detail')}"
    elif not resources.get("cleanupVerified"):
        reason = "cleanup not verified"
    elif resources.get("verificationGaps"):
        reason = f"verification gaps: {resources['verificationGaps']}"
    elif lc.get("faultInjected"):
        reason = "a lifecycle fault was injected"
    if reason is None and exact is not None:
        reason = _exact_reason(exact)
    return status, reason is None, reason


def _exact_reason(exact: dict) -> str | None:
    from livetest import canon
    data = exact.get("data") or {}
    comps = data.get("comparisons") if isinstance(data, dict) else None
    if not comps or not any(c.get("profile") == canon.PROFILE and not c.get("error") for c in comps):
        return NO_DATA_REASON
    for c in comps:
        if not (c.get("profile") == canon.PROFILE and c.get("equal") is True and (c.get("actual") or {}).get("owned") is True):
            why = c.get("error") or ("not equal" if c.get("equal") is not True else "not owned")
            return f"comparison {c.get('index')} ({c.get('profile')} {c.get('type')} {c.get('target')}) does not qualify: {why}"
    for rec in exact.get("assertions") or []:
        if rec.get("status") != "passed":
            return f"assertion {rec.get('type')} {rec.get('target')} {rec.get('status')}"
    changed = [p for p, g in ((exact.get("integrity") or {}).get("goldens") or {}).items() if not g.get("unchanged")]
    if changed:
        return f"golden changed during the run: {changed[0]}"
    if exact.get("blocked"):
        return f"not observed or unreadable: {exact['blocked'][0]}"
    striim = exact.get("striim") if isinstance(exact.get("striim"), dict) else {}
    observed, expected = striim.get("observed"), striim.get("expected")
    want = expected.get("STRIIM_VERSION") if isinstance(expected, dict) else None
    got = observed.get("version") if isinstance(observed, dict) else None
    if not (isinstance(want, str) and isinstance(got, str)):     # No witness, no qualification
        return "the running Striim version is not witnessed against the expected release"
    if got != want:
        return f"the running Striim version {got} is not the expected release {want}"
    errors = (exact.get("integrity") or {}).get("evidenceErrors")
    if errors:
        return f"evidence errors: {errors[0]}"
    return None


def forms(doc, _path: str = "") -> list[str]:
    """Paths holding an ``{unreadable}``/``{unobserved}`` form."""
    out = []
    if isinstance(doc, dict):
        if len(doc) == 1 and next(iter(doc)) in _FORMS:
            return [_path or "."]
        for k, v in doc.items():
            out += forms(v, f"{_path}.{k}" if _path else k)
    elif isinstance(doc, list):
        for i, v in enumerate(doc):
            out += forms(v, f"{_path}[{i}]")
    return out


def _blocked(doc: dict) -> list[str]:
    return forms({"inputs": doc.get("inputs"), "runtime": doc.get("runtime")})


def exact_view(doc: dict) -> dict:
    return {"data": doc.get("data"), "integrity": doc.get("integrity"), "assertions": doc.get("assertions"),
            "blocked": _blocked(doc), "striim": (doc.get("runtime") or {}).get("striim")}


# ---------------------------------------------------------------- report helpers

def _report_status(report) -> str | None:
    when, outcome = getattr(report, "when", None), getattr(report, "outcome", None)
    if when == "call":
        return {"passed": "passed", "failed": "failed", "skipped": "skipped"}.get(outcome, "error")
    if when == "setup" and outcome == "failed":
        return "error"
    if when == "setup" and outcome == "skipped":
        return "skipped"
    return None


def _skip_reason(report):
    lr = getattr(report, "longrepr", None)
    if isinstance(lr, tuple) and len(lr) == 3:
        reason = str(lr[2])
    elif lr is not None:
        reason = str(lr)
    else:
        return None
    return reason[len("Skipped: "):] if reason.startswith("Skipped: ") else reason


def _failure(report):
    text = getattr(report, "longreprtext", "") or ""
    lines = [ln for ln in text.splitlines() if ln.strip()]
    return lines[-1][:2000] if lines else None


# ---------------------------------------------------------------- the envelope

def _case_assets(assets: dict) -> dict:
    """A case asset whose bytes were not read is explicit, never a null digest."""
    assets = dict(assets)
    if assets.get("tqlSha256") is None:
        assets["tqlSha256"] = dict(NO_TQL_FORM)
    return assets


def _rendered(entry: dict) -> dict:
    """A send whose bytes were never snapshotted is an explicit unobserved form (the case
    then never qualifies), never a null digest."""
    entry = dict(entry)
    for key in ("templateSha256", "renderedSha256"):
        if entry.get(key) is None:
            entry[key] = dict(NO_BYTES_FORM)
    return entry


def _inputs(item, manifest, config) -> dict:
    import livetest
    snap = getattr(item, "_slt_inputs", None)
    ident = getattr(item, "_slt_ident", None)
    if snap is None:
        none = {"unreadable": "no input snapshot: the case stopped before its inputs were read"}
        assets = logical = rendered_manifest = none
        rendered = []
    else:
        assets, logical = _case_assets(snap.case_assets()), snap.logical_inputs_sha256()
        rendered = [_rendered(r) for r in snap.rendered_inputs()]
        rendered_manifest = snap.rendered_manifest_sha256()
    if manifest is None:
        product = {"unreadable": "the manifest could not be loaded"}
    elif getattr(manifest, "modules", None):
        product = {"unobserved": "op/udf artifact bytes are not observed at upload (artifact capture is unavailable)"}
    else:
        product = {"reason": "app-only case: no op/udf"}
    from livetest import runident
    return {"source": _source(config), "framework": framework_identity(livetest), "productBuild": product,
            "caseAssets": assets, "logicalInputsSha256": logical, "renderedInputs": rendered,
            "renderedManifestSha256": rendered_manifest,
            "bindings": runident.tokens(ident) if ident is not None else {}}


def _runtime(config) -> dict:
    import livetest
    from livetest import infra, layout
    try:
        resource_root = str(layout.builtin_services())
    except Exception as e:              # noqa: BLE001
        resource_root = {"unreadable": f"{type(e).__name__}: {e}"}
    try:
        planned = layout.planned_state_dir()
        state_dir = str(planned) if planned is not None else None
    except Exception as e:              # noqa: BLE001
        state_dir = {"unreadable": f"{type(e).__name__}: {e}"}
    return {"os": f"{platform.system()}-{platform.release()}", "arch": platform.machine(),
            "python": platform.python_version(), "interpreter": sys.executable,
            "mode": os.environ.get("SLT_FRAMEWORK_MODE") or "unknown",
            "importPath": str(Path(livetest.__file__).parent), "resourceRoot": resource_root, "stateDir": state_dir,
            "hostId": host_id(), "jdk": {"reason": "live tier process runs no JVM"},
            "striim": infra.observe_striim(config), "services": infra.observe_services(config)}


def _integrity(golden: dict) -> dict:
    """A golden that could not be re-read carries the explicit unreadable form, not a null
    final digest; ``unchanged`` stays what the two digests say."""
    golden = dict(golden)
    if golden.get("finalSha256") is None:
        golden["finalSha256"] = {"unreadable": UNREADABLE_GOLDEN}
    return golden


def case_envelope(item, report, status: str) -> dict:
    from livetest import exactdata
    from livetest import lifecycle as _lifecycle
    from livetest import runident
    config = item.config
    try:
        from livetest.manifest import load_manifest
        manifest = load_manifest(item.manifest_path)
    except Exception:                   # noqa: BLE001 - an invalid manifest still gets a record
        manifest = None
    lc = getattr(item, "_slt_lc", None)
    ident = getattr(item, "_slt_ident", None)
    run_id = ident.run_id if ident is not None else (os.environ.get("SLT_RUN_EPOCH") or "unknown")
    cleanup = getattr(item, "_slt_cleanup", None) or dict(NO_CLEANUP_RECORD)
    infra = getattr(config, "_slt_infra", None)
    ledger = dict(getattr(item, "_slt_resources", None) or {"owned": [], "reused": [], "foreign": [],
                                                           "cleanupVerified": False, "verificationGaps": [NO_LEDGER_GAP]})
    fault = ledger.pop("faultInjected", None)
    resources = {"infrastructure": infra.record() if infra is not None else None, **ledger}
    lifecycle = lc.to_dict() if lc is not None else {"mode": "unknown", "ready": None, "completion": None}
    lifecycle.update({"cleanup": cleanup, "faultInjected": fault,
                      "identity": runident.record(ident) if ident is not None else None})
    snap = getattr(item, "_slt_inputs", None)
    goldens = {}
    if snap is not None:
        goldens = {snap.rel(p): _integrity(g) for p, g in snap.verify_goldens().items()}
    doc = {
        "evidenceVersion": EVIDENCE_VERSION,
        "kind": "case",
        "inputs": _inputs(item, manifest, config),
        "runtime": _runtime(config),
        "lifecycle": lifecycle,
        "assertions": list(getattr(item, "_slt_records", []) or []),
        "data": exactdata.data_section(list(getattr(item, "_slt_data", None) or [])),
        "reports": [{"kind": "junit-xml", "path": str(getattr(config.option, "xmlpath", None)),
                     "invocation": os.environ.get("SLT_INVOCATION_ID")},
                    {"kind": "slt-json-v1", "path": _sidecar_path(config)}],
        "resources": resources,
        "integrity": {"goldens": goldens, "evidenceErrors": []},
        "review": None,
    }
    status, qualifies, why = final_outcome(status, lifecycle, cleanup, resources, exact=exact_view(doc))
    try:
        rel = os.path.relpath(Path(item.path).parent, config.rootpath)
    except Exception:                   # noqa: BLE001
        rel = str(Path(item.path).parent)
    doc["run"] = {"runId": run_id, "attempt": getattr(ident, "attempt", None), "caseId": f"live:{rel}::{item.name}",
                  "suite": rel, "tier": "live", "startedAt": getattr(lc, "started_at", None),
                  "endedAt": _lifecycle.now_iso(), "status": status,
                  "skipReason": _skip_reason(report) if status == "skipped" else None,
                  "failure": _failure(report) if status in ("failed", "error") else None,
                  "qualifies": qualifies, "qualifiesReason": why,
                  "invocation": os.environ.get("SLT_INVOCATION_ID"),
                  "nodeid": getattr(item, "nodeid", None) or f"{rel}/test.yaml::{item.name}"}
    return doc


def _sidecar_path(config) -> str | None:
    xml = getattr(getattr(config, "option", None), "xmlpath", None)
    if not xml:
        return None
    p = Path(xml)
    return str(p.with_name(p.stem + ".slt.json"))


# ---------------------------------------------------------------- validation

def _fail(code: str, detail: str):
    raise EvidenceError(code, detail)


def _keys(section, want: set, where: str):
    if not isinstance(section, dict):
        _fail("evidence-invalid", f"{where} must be an object")
    if set(section) != want:
        extra, missing = sorted(set(section) - want), sorted(want - set(section))
        _fail("evidence-invalid", f"{where} keys: unknown {extra}, missing {missing}")


def _is_form(value) -> bool:
    return isinstance(value, dict) and len(value) == 1 and next(iter(value)) in _FORMS


def _reason_violations(node, path: tuple = ()) -> list[str]:
    out = []
    if isinstance(node, dict):
        if set(node) == {"reason"}:
            pattern = tuple("*" if isinstance(p, int) else p for p in path)
            allowed = REASONS.get(pattern)
            if allowed is None or node["reason"] not in allowed:
                out.append(f"{'.'.join(map(str, path))}: {node['reason']!r}")
            return out
        for k, v in node.items():
            out += _reason_violations(v, path + (k,))
    elif isinstance(node, list):
        for i, v in enumerate(node):
            out += _reason_violations(v, path + (i,))
    return out


def _sha_violations(node, path: str = "") -> list[str]:
    out = []
    if isinstance(node, dict):
        for k, v in node.items():
            here = f"{path}.{k}" if path else k
            if (k == "sha256" or k.endswith("Sha256")) and isinstance(v, str) and not _SHA.match(v):
                out.append(here)
            out += _sha_violations(v, here)
    elif isinstance(node, list):
        for i, v in enumerate(node):
            out += _sha_violations(v, f"{path}[{i}]")
    return out


def _values(node, path: tuple = ()):
    """Every ``(path, string value)`` (not keys): the secret scan's scope."""
    if isinstance(node, dict):
        for k, v in node.items():
            yield from _values(v, path + (k,))
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from _values(v, path + (i,))
    elif isinstance(node, str):
        yield path, node


def _strings(node):
    if isinstance(node, dict):
        for k, v in node.items():
            yield k
            yield from _strings(v)
    elif isinstance(node, list):
        for v in node:
            yield from _strings(v)
    elif isinstance(node, str):
        yield node


def _nonempty(value) -> bool:
    return isinstance(value, str) and value != ""


def _count(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _observed(value, where: str, *, allow_none: bool = False) -> None:
    """An identity is an observed value, an allowlisted reason or an unreadable/unobserved form -- never null or
    empty; the reason allowlist is checked separately."""
    if _nonempty(value) or _is_form(value) or (allow_none and value is None):
        return
    if isinstance(value, dict) and set(value) == {"reason"} and _nonempty(value["reason"]):
        return
    _fail("evidence-invalid", f"{where} must be an observed value or a non-qualifying form, got {value!r}")


def _form_or_reason(value) -> bool:
    """An ``{unreadable}``/``{unobserved}`` form or a ``{reason}`` with its text (the allowlist is checked separately)."""
    return isinstance(value, dict) and len(value) == 1 and next(iter(value)) in _FORMS + ("reason",) \
        and _nonempty(next(iter(value.values())))


def _digest_or_form(value) -> bool:
    return (isinstance(value, str) and _SHA.match(value) is not None) or _form_or_reason(value) and "reason" not in value


_VERSION = re.compile(r"^[0-9]+(\.[0-9A-Za-z_-]+)+$")


def _check_identities(doc: dict) -> None:
    """Each structured identity against its own variants: an arbitrary string or
    dictionary is neither an observation nor a non-qualifying form."""
    inputs, runtime = doc["inputs"], doc["runtime"]
    source = inputs["source"]
    if isinstance(source, dict) and not _is_form(source) and set(source) != {"reason"}:
        _keys(source, {"repo", "head", "dirty"}, "inputs.source")
        if not _nonempty(source["repo"]) or not re.fullmatch(r"[0-9a-f]{40}", str(source["head"])) \
                or not isinstance(source["dirty"], list) or not all(isinstance(d, str) for d in source["dirty"]):
            _fail("evidence-invalid", "inputs.source must be {repo, head: 40-hex, dirty: [paths]}")
    elif not _form_or_reason(source):
        _fail("evidence-invalid", f"inputs.source must be {{repo, head, dirty}}, its reason or a non-qualifying form, "
                                  f"got {source!r}")
    fw = inputs["framework"]
    if not _is_form(fw) and (not _nonempty(fw["mode"]) or not _nonempty(fw["importPath"])):
        _fail("evidence-invalid", "inputs.framework.mode and importPath are required")
    if not _is_form(fw) and fw["mode"] == "wheel":
        for key in ("wheelSha256", "lockSha256"):
            if not _digest_or_form(fw[key]):
                _fail("evidence-invalid", f"inputs.framework.{key} must be sha256:<hex> or a non-qualifying form in "
                                          f"wheel mode, got {fw[key]!r}")
    product = inputs["productBuild"]
    if isinstance(product, dict) and "artifacts" in product:
        _keys(product, {"artifacts"}, "inputs.productBuild")
        if not isinstance(product["artifacts"], list) or not product["artifacts"]:
            _fail("evidence-invalid", "inputs.productBuild.artifacts must be a non-empty list")
        for a in product["artifacts"]:
            _keys(a, {"name", "sha256"}, "inputs.productBuild.artifacts[]")
            if not _nonempty(a["name"]) or not _digest_or_form(a["sha256"]):
                _fail("evidence-invalid", f"inputs.productBuild.artifacts[] needs its name and observed sha256, got {a!r}")
    elif not _form_or_reason(product):
        _fail("evidence-invalid", f"inputs.productBuild must be its artifacts, its reason or a non-qualifying form, "
                                  f"got {product!r}")
    for key in ("os", "arch", "python", "interpreter", "mode", "importPath"):
        if not _nonempty(runtime[key]):
            _fail("evidence-invalid", f"runtime.{key} is required")
    if not re.fullmatch(r"[0-9a-f]{16}", str(runtime["hostId"])):
        _fail("evidence-invalid", "runtime.hostId must be 16 hex characters")
    _observed(runtime["resourceRoot"], "runtime.resourceRoot")
    _observed(runtime["stateDir"], "runtime.stateDir", allow_none=True)
    _observed(runtime["jdk"], "runtime.jdk")
    striim = runtime["striim"]
    expected = striim["expected"]
    if not (_form_or_reason(expected) and "reason" not in expected) and not (
            isinstance(expected, dict) and isinstance(expected.get("STRIIM_VERSION"), str)
            and _VERSION.match(expected["STRIIM_VERSION"])):
        _fail("evidence-invalid", "runtime.striim.expected must be the resolved release (its STRIIM_VERSION) or a "
                                  f"non-qualifying form, got {expected!r}")
    obs = striim["observed"]
    if not _is_form(obs):
        _observed(obs["url"], "runtime.striim.observed.url")
        version = obs["version"]
        if not (isinstance(version, str) and _VERSION.match(version)) and not (_form_or_reason(version) and "reason" not in version):
            _fail("evidence-invalid", f"runtime.striim.observed.version must be a release version or a non-qualifying "
                                      f"form, got {obs['version']!r}")
        _observed(obs["image"], "runtime.striim.observed.image")
        _observed(obs["imageId"], "runtime.striim.observed.imageId")
        native = obs["imageId"] == {"reason": "native endpoint"}
        _observed(obs["container"], "runtime.striim.observed.container", allow_none=native)
        if not isinstance(obs["consoleReachable"], bool):
            _fail("evidence-invalid", "runtime.striim.observed.consoleReachable must be a boolean")
    services = runtime["services"]
    if not _is_form(services):
        if not isinstance(services, list):
            _fail("evidence-invalid", "runtime.services must be a list or a non-qualifying form")
        for i, svc in enumerate(services):
            # An external service of a connection-only definition has no container, as a native Striim
            # endpoint has none; every other service names the container the run used.
            keys = ("name", "status") if isinstance(svc, dict) and svc.get("status") == "external" \
                and svc.get("container") is None else ("name", "container", "status")
            if not isinstance(svc, dict) or not all(_nonempty(svc.get(k)) for k in keys):
                _fail("evidence-invalid", f"runtime.services[{i}] needs name, container and status")
            _observed(svc.get("image"), f"runtime.services[{i}].image")
            _observed(svc.get("imageId"), f"runtime.services[{i}].imageId")


def _check_tql(inputs: dict) -> None:
    """Every live case declares a TQL (``manifest.load_manifest`` requires it), so its
    digest is the sha256 of the application whose bytes the snapshot read, or an explicit unreadable/unobserved form
    that keeps the case from qualifying -- never a null that cannot be told apart from a removed witness."""
    tql = inputs["caseAssets"]["tqlSha256"]
    if not _digest_or_form(tql):
        _fail("evidence-invalid", f"inputs.caseAssets.tqlSha256 must be the deployed application's sha256:<hex> or a "
                                  f"non-qualifying form, got {tql!r}")


def _check_assertion_binding(records, comparisons, bindings=None, owned=None) -> None:
    """Every canonical comparison is the comparison of one assertion record with the same
    variant -- type, target, route and outcome -- so a recorded comparison cannot be re-typed (a data comparison
    disguised as an unwitnessed diff) or invented without the record the assertion left. A diff's source is bound
    to that record too (``_diff_source_bound``)."""
    from livetest import canon
    if not isinstance(records, list):
        _fail("evidence-invalid", "assertions must be the run's assertion records")
    available = [r for r in records if isinstance(r, dict)]
    for i, c in enumerate(comparisons):
        if c.get("profile") != canon.PROFILE:
            continue
        status = "failed" if (c.get("error") or c.get("equal") is not True) else "passed"
        key = (c["type"], c.get("target"), c.get("route"), status)
        match = [j for j, r in enumerate(available) if (r.get("type"), r.get("target"), r.get("db"), r.get("status")) == key]
        if not match:
            _fail("evidence-invalid", f"data.comparisons[{i}]: no {c['type']} assertion record for "
                                      f"{c.get('target')!r} on {c.get('route')!r} that {status}")
        if c["type"] == "diff":
            match = [j for j in match if _diff_source_bound(c, available[j], bindings, owned)]
            if not match:
                _fail("evidence-invalid", f"data.comparisons[{i}]: diff source {(c.get('expected') or {}).get('source')!r} "
                                          f"is not the source table this run witnessed and owned")
        available.pop(match[0])


# The ledger states of a table this attempt created (an intent that was never confirmed witnessed nothing).
_CREATED_STATES = ("confirmed", "deleted", "verified-absent", "delete-failed", "not-deleted")


def _diff_source_bound(c: dict, record: dict, bindings, owned) -> bool:
    """A diff's ``expected.source`` is the source its assertion record
    declared -- the record's ``source_db`` route and its ``source``, which the recorder writes rendered (``exactdata``)
    -- and, for an owned comparison, a table this attempt's ledger created on that route. Another owned table, or any
    unowned one, is not the witnessed source. A token the envelope does not bind is
    NOT a wildcard -- it would admit any other owned table the attempt created -- so an unbound token fails closed."""
    from livetest import substitute
    source = (c.get("expected") or {}).get("source")
    if source is None:                  # an error comparison that failed before its source was rendered
        return "error" in c
    spec = record.get("spec") if isinstance(record.get("spec"), dict) else {}
    route = spec.get("source_db", spec.get("db", "postgres-source"))
    template = spec.get("source")
    if not (isinstance(source, str) and isinstance(route, str) and isinstance(template, str)
            and source.startswith(route + ":")):
        return False
    bound = bindings if isinstance(bindings, dict) else {}
    pattern, at = "", 0
    for m in substitute._TOKEN.finditer(template):
        value = bound.get(m.group(1))
        if not isinstance(value, str):
            return False
        pattern += re.escape(template[at:m.start()]) + re.escape(value)
        at = m.end()
    table = source[len(route) + 1:]
    if re.fullmatch(pattern + re.escape(template[at:]), table) is None:
        return False
    if "error" in c or (c.get("actual") or {}).get("owned") is not True:
        return True
    return any(isinstance(r, dict) and r.get("kind") == "pg-table" and r.get("db") == route
               and r.get("name") == table.lower() and r.get("state") in _CREATED_STATES for r in (owned or []))


@functools.lru_cache(maxsize=1)
def _diff_source():
    """``<route>:<schema.table>``, the source witness ``exactdata.assert_exact_diff`` writes."""
    from livetest import canon
    routes = "|".join(re.escape(r) for r in canon.DB_ROUTES)
    return re.compile(rf"^(?:{routes}):[A-Za-z_][A-Za-z0-9_]*\.[A-Za-z_][A-Za-z0-9_]*$")


_DECLARATION_KEYS = {"columns", "ignore", "keys", "order", "order_by", "profile"}
_COMPARISON_COMMON = {"index", "type", "target", "route", "profile", "order", "declaration", "declarationSha256",
                      "equal", "expected", "actual"}


def _declaration_violation(decl: dict, *, db_route: bool) -> str | None:
    """A declaration is valid only if C8.2's own rules (``canon.declaration``) resolve its fields back to
    exactly it; a recomputed hash over an invalid type, order or column list is not enough."""
    from livetest import canon
    try:
        spec = {"columns": decl["columns"], "order": decl["order"]}
        if decl["ignore"] != []:
            spec["ignore"] = decl["ignore"]
        if decl["order_by"] is not None:
            spec["order_by"] = decl["order_by"]
        resolved = canon.declaration(spec, decl["keys"], db_route=db_route)
    except canon.CanonError as e:
        return str(e)
    except Exception as e:              # noqa: BLE001 - a malformed field is a violation, never a crash
        return f"{type(e).__name__}: {e}"
    return None if resolved.to_dict() == decl else "its fields do not resolve to themselves"


def _check_comparison(c: dict, i: int) -> None:
    """One comparison against its variant -- success, error or legacy -- with its hashes, counts and declaration
    consistent: ``equal`` must be what the digests say, never a free-standing claim."""
    from livetest import canon
    where = f"data.comparisons[{i}]"
    if c.get("profile") == canon.LEGACY_PROFILE:
        _keys(c, {"index", "type", "target", "route", "profile", "equal", "actual"}, where)
        if c["type"] != "diff" or not isinstance(c["equal"], bool) or c["actual"] != {"owned": False}:
            _fail("evidence-invalid", f"{where}: a legacy-text/1 comparison is a diff, equal bool, actual {{owned: false}}")
        return
    if c.get("profile") != canon.PROFILE:
        _fail("evidence-invalid", f"{where}: profile {c.get('profile')!r}")
    if c.get("type") not in ("data", "diff", "file") or not _nonempty(c.get("target")):
        _fail("evidence-invalid", f"{where}: type and target are required")
    decl = c.get("declaration")
    _keys(decl, _DECLARATION_KEYS, f"{where}.declaration")
    if decl["profile"] != canon.PROFILE or c["order"] != decl["order"] or \
            c["declarationSha256"] != canon.declaration_sha256(decl):
        _fail("evidence-invalid", f"{where}: declaration, order and declarationSha256 disagree")
    why = _declaration_violation(decl, db_route=c["type"] != "file")
    if why:
        _fail("evidence-invalid", f"{where}.declaration is not a canonical declaration: {why}")
    if "error" in c:
        _keys(c, _COMPARISON_COMMON | {"error"}, where)
        _keys(c["expected"], {"source", "templateSha256"}, f"{where}.expected")
        _keys(c["actual"], {"owned"}, f"{where}.actual")
        if c["equal"] is not False or not _nonempty(c["error"]) or not isinstance(c["actual"]["owned"], bool):
            _fail("evidence-invalid", f"{where}: an error comparison is equal false with its error and owned")
        src, tpl = c["expected"]["source"], c["expected"]["templateSha256"]
        if not (src is None or _nonempty(src)) or not (tpl is None or (isinstance(tpl, str) and _SHA.match(tpl))):
            _fail("evidence-invalid", f"{where}.expected: source and templateSha256 are null or observed")
        return
    _keys(c, _COMPARISON_COMMON | {"logical", "samples", "sampleTruncated", "limits"}, where)
    e, a, lg, lim = c["expected"], c["actual"], c["logical"], c["limits"]
    _keys(e, {"sha256", "rowCount", "source", "templateSha256"}, f"{where}.expected")
    _keys(a, {"sha256", "rowCount", "owned"}, f"{where}.actual")
    _keys(lg, {"expectedSha256", "actualSha256"}, f"{where}.logical")
    _keys(lim, {"maxRows", "maxBytes"}, f"{where}.limits")
    if not all(isinstance(x, str) and _SHA.match(x) for x in (e["sha256"], a["sha256"], lg["expectedSha256"], lg["actualSha256"])):
        _fail("evidence-invalid", f"{where}: expected, actual and logical digests are required")
    # The golden witness. A data or file comparison names its golden and that golden's template digest;
    # a diff compares two tables, so it names its source table and has no template.
    if c["type"] == "diff":
        # The source names the table it read, not any nonempty string.
        witnessed = _nonempty(e["source"]) and _diff_source().match(e["source"]) is not None \
            and e["templateSha256"] is None
    else:
        witnessed = _nonempty(e["source"]) and isinstance(e["templateSha256"], str) and _SHA.match(e["templateSha256"])
    if not witnessed:
        _fail("evidence-invalid", f"{where}.expected: a {c['type']} comparison needs its golden witness "
                                  f"(source {e['source']!r}, templateSha256 {e['templateSha256']!r})")
    if not (_count(e["rowCount"]) and _count(a["rowCount"]) and isinstance(a["owned"], bool)
            and isinstance(c["sampleTruncated"], bool) and all(_count(lim[k]) and lim[k] > 0 for k in lim)):
        _fail("evidence-invalid", f"{where}: counts, owned, sampleTruncated and limits are required")
    if c["equal"] is not (e["sha256"] == a["sha256"]):
        _fail("evidence-invalid", f"{where}: equal {c['equal']!r} disagrees with the expected and actual digests")
    samples = c["samples"]
    if not isinstance(samples, dict) or set(samples) - {"missing", "extra", "firstMismatch"} \
            or not isinstance(samples.get("missing"), list) or not isinstance(samples.get("extra"), list):
        _fail("evidence-invalid", f"{where}: samples must be {{missing, extra[, firstMismatch]}}")
    if c["equal"] and (e["rowCount"] != a["rowCount"] or lg["expectedSha256"] != lg["actualSha256"]
                       or samples["missing"] or samples["extra"] or "firstMismatch" in samples):
        _fail("evidence-invalid", f"{where}: an equal comparison has differing counts, logical digests or samples")


def validate_case(doc: dict, secrets=()) -> None:
    """The complete case envelope (C4 1.9.0). Raises EvidenceError."""
    from livetest import exactdata
    if not isinstance(doc, dict):
        _fail("evidence-invalid", "the envelope must be an object")
    if "partial" in doc:
        _fail("evidence-partial", "a partial envelope is not C4-complete and is never written")
    _keys(doc, TOP_KEYS, "envelope")
    if doc["evidenceVersion"] != EVIDENCE_VERSION or doc["kind"] != "case":
        _fail("evidence-invalid", "evidenceVersion must be 2 and kind 'case'")
    run = doc["run"]
    _keys(run, RUN_KEYS, "run")
    if run["status"] not in _STATUSES or not isinstance(run["qualifies"], bool):
        _fail("evidence-invalid", f"run.status {run['status']!r} / run.qualifies {run['qualifies']!r}")
    if not isinstance(run["invocation"], str) or not run["invocation"]:
        _fail("evidence-invalid", "run.invocation must be the run's SLT_INVOCATION_ID (C5 1.9.0)")
    if not isinstance(run["nodeid"], str) or not run["nodeid"]:
        _fail("evidence-invalid", "run.nodeid must name the pytest item")
    inputs = doc["inputs"]
    _keys(inputs, INPUT_KEYS, "inputs")
    if not _is_form(inputs["framework"]):
        _keys(inputs["framework"], FRAMEWORK_KEYS, "inputs.framework")
        if inputs["framework"]["mode"] == "wheel":
            for key in ("wheelSha256", "lockSha256"):
                if inputs["framework"][key] is None:
                    _fail("evidence-invalid", f"inputs.framework.{key} is required in wheel mode")
    if not _is_form(inputs["caseAssets"]):
        _keys(inputs["caseAssets"], ASSET_KEYS, "inputs.caseAssets")
        if not isinstance(inputs["caseAssets"]["manifestSha256"], str):
            _fail("evidence-invalid", "inputs.caseAssets.manifestSha256 is required")
        for group in ("goldens", "files"):
            for rel, sha in inputs["caseAssets"][group].items():
                if not isinstance(sha, str) or not _SHA.match(sha):
                    _fail("evidence-invalid", f"inputs.caseAssets.{group}[{rel!r}] is not a sha256")
        _check_tql(inputs)
    for key in ("logicalInputsSha256", "renderedManifestSha256"):
        if not _is_form(inputs[key]) and not (isinstance(inputs[key], str) and _SHA.match(inputs[key])):
            _fail("evidence-invalid", f"inputs.{key} is required as sha256:<hex>")
    if not isinstance(inputs["renderedInputs"], list):
        _fail("evidence-invalid", "inputs.renderedInputs must be a list")
    for entry in inputs["renderedInputs"]:
        _keys(entry, RENDERED_KEYS, "inputs.renderedInputs[]")
        # A recorded send carries the bytes it sent, or says explicitly that it could not.
        if not _nonempty(entry["role"]) or not _nonempty(entry["name"]) or not _count(entry["uses"]) or entry["uses"] < 1:
            _fail("evidence-invalid", f"inputs.renderedInputs[] needs its role, name and use count, got {entry!r}")
        for key in ("templateSha256", "renderedSha256"):
            if not _digest_or_form(entry[key]):
                _fail("evidence-invalid", f"inputs.renderedInputs[{entry['name']!r}].{key} must be sha256:<hex> or a "
                                          f"non-qualifying form, got {entry[key]!r}")
    if not isinstance(inputs["bindings"], dict) or not all(isinstance(v, str) for v in inputs["bindings"].values()):
        _fail("evidence-invalid", "inputs.bindings must map token names to strings")
    runtime = doc["runtime"]
    _keys(runtime, RUNTIME_KEYS, "runtime")
    striim = runtime["striim"]
    _keys(striim, {"expected", "observed"}, "runtime.striim")
    if not _is_form(striim["observed"]):
        _keys(striim["observed"], OBSERVED_KEYS, "runtime.striim.observed")
    _check_identities(doc)
    lc = doc["lifecycle"]
    for key in ("ready", "completion"):
        rec = lc.get(key)
        if rec is not None and not {"witness", "at", "reason"} <= set(rec):
            _fail("evidence-invalid", f"lifecycle.{key} must keep C4 witness/at and a reason")
    if (lc.get("cleanup") or {}).get("status") not in CLEANUP_STATUSES:
        _fail("evidence-invalid", f"lifecycle.cleanup.status {(lc.get('cleanup') or {}).get('status')!r} invalid")
    data = doc["data"]
    if set(data) != {"reason"}:
        _keys(data, DATA_KEYS, "data")
        if not isinstance(data["comparisons"], list) or not data["comparisons"]:
            _fail("evidence-invalid", "data.comparisons must be a non-empty list (else data is the no-data reason)")
        for i, c in enumerate(data["comparisons"]):
            if not isinstance(c, dict) or set(c) - COMPARISON_KEYS or not {"index", "type", "profile", "equal"} <= set(c):
                _fail("evidence-invalid", f"data.comparisons[] keys {sorted(c) if isinstance(c, dict) else c!r}")
            _check_comparison(c, i)
        _check_assertion_binding(doc["assertions"], data["comparisons"], (doc.get("inputs") or {}).get("bindings"),
                                 (doc.get("resources") or {}).get("owned"))
        recomputed = exactdata.aggregates(data["comparisons"])
        if any(data[k] != v for k, v in recomputed.items()):
            _fail("evidence-invalid", "data aggregates are not recomputable from data.comparisons")
        if [c["index"] for c in data["comparisons"]] != list(range(len(data["comparisons"]))):
            _fail("evidence-invalid", "data.comparisons indexes must be 0..n-1 in order")
    if not isinstance(doc["reports"], list) or [r.get("kind") for r in doc["reports"]] != ["junit-xml", "slt-json-v1"]:
        _fail("evidence-invalid", "reports must be [junit-xml, slt-json-v1]")
    _keys(doc["reports"][0], {"kind", "path", "invocation"}, "reports[junit-xml]")
    if doc["reports"][0]["invocation"] != run["invocation"]:
        _fail("evidence-invalid", "reports[junit-xml].invocation must equal run.invocation")
    _keys(doc["reports"][1], {"kind", "path"}, "reports[slt-json-v1]")
    if not {"infrastructure", "owned", "reused", "foreign", "cleanupVerified", "verificationGaps"} <= set(doc["resources"]):
        _fail("evidence-invalid", "resources section incomplete")
    _keys(doc["integrity"], INTEGRITY_KEYS, "integrity")
    assets = inputs["caseAssets"]
    declared = assets["goldens"] if not _is_form(assets) else None
    for rel, g in doc["integrity"]["goldens"].items():
        where = f"integrity.goldens[{rel!r}]"
        _keys(g, {"inputSha256", "finalSha256", "unchanged"}, where)
        # Both digests are observed values, and agreement is recomputed from them: the
        # unchanged flag is never the witness of itself.
        if not (isinstance(g["inputSha256"], str) and _SHA.match(g["inputSha256"])):
            _fail("evidence-invalid", f"{where}.inputSha256 must be the snapshotted golden's sha256:<hex>, "
                                      f"got {g['inputSha256']!r}")
        if not _digest_or_form(g["finalSha256"]):
            _fail("evidence-invalid", f"{where}.finalSha256 must be sha256:<hex> or a non-qualifying form, "
                                      f"got {g['finalSha256']!r}")
        if g["unchanged"] is not (isinstance(g["finalSha256"], str) and g["finalSha256"] == g["inputSha256"]):
            _fail("evidence-invalid", f"{where}.unchanged {g['unchanged']!r} is not what its digests say")
        if declared is not None and declared.get(rel) != g["inputSha256"]:
            _fail("evidence-invalid", f"{where}.inputSha256 is not the case asset golden {rel!r} "
                                      f"({declared.get(rel)!r})")
    if declared is not None and set(declared) != set(doc["integrity"]["goldens"]):
        _fail("evidence-invalid", f"integrity.goldens must cover exactly the case's goldens {sorted(declared)}, "
                                  f"got {sorted(doc['integrity']['goldens'])}")
    if doc["review"] is not None:
        _fail("evidence-invalid", "review must be null")
    bad = _sha_violations(doc)
    if bad:
        _fail("evidence-invalid", f"not sha256:<hex>: {bad[:5]}")
    bad = _reason_violations(doc)
    if bad:
        _fail("evidence-reason", f"a reason is only for an allowlisted inapplicable field: {bad[:5]}")
    for path, text in _values(doc):
        if _syntax(path, text):         # schema grammar, never a credential (and never redacted)
            continue
        for secret in secrets:
            if _contains_secret(text, secret):
                _fail("evidence-secret", "a known secret value is present in the envelope")
    _status, qualifies, why = final_outcome(run["status"], lc, lc.get("cleanup"), doc["resources"], exact=exact_view(doc))
    if qualifies != run["qualifies"]:
        _fail("evidence-qualifies", f"run.qualifies {run['qualifies']} but the document gives {qualifies} ({why})")


def validate_partial_41(doc: dict) -> None:
    """The sections the legacy writer wrote with its partial marker (C4 v2 + C7.3/C7.4). Raises ValueError."""
    if set(doc) != _TOP_KEYS_41:
        raise ValueError(f"envelope keys {sorted(doc)} != {sorted(_TOP_KEYS_41)}")
    if doc["evidenceVersion"] != EVIDENCE_VERSION:
        raise ValueError("evidenceVersion must be 2")
    if doc["partial"].get("missing") != PARTIAL_MISSING_41:
        raise ValueError("partial.missing must list the complete-envelope fields")
    if set(doc["run"]) != _RUN_KEYS_41 or doc["run"]["status"] not in _STATUSES:
        raise ValueError(f"run section invalid: {doc['run']}")
    if not isinstance(doc["run"]["qualifies"], bool):
        raise ValueError("run.qualifies must be a boolean")
    lc = doc["lifecycle"]
    for key in ("ready", "completion"):
        rec = lc.get(key)
        if rec is not None and not {"witness", "at", "reason"} <= set(rec):
            raise ValueError(f"lifecycle.{key} must keep C4 witness/at and a reason")
    if lc["cleanup"].get("status") not in CLEANUP_STATUSES:
        raise ValueError(f"lifecycle.cleanup.status {lc['cleanup'].get('status')!r} invalid")
    if not {"infrastructure", "owned", "reused", "foreign", "cleanupVerified", "verificationGaps"} <= set(doc["resources"]):
        raise ValueError("resources section incomplete")


def read(path) -> Envelope:
    """Read one envelope by path (bounded). A v1 sidecar and other versions are refused; a 4.1 partial
    envelope is incomplete and never qualifies; the item and run-id directories must match the document."""
    path = Path(path)
    try:
        if path.stat().st_size > READ_CAP:
            _fail("evidence-too-large", f"{path} exceeds {READ_CAP} bytes")
        with open(path, "rb") as f:
            doc = json.loads(f.read(READ_CAP + 1))
    except (OSError, ValueError) as e:
        if isinstance(e, EvidenceError):
            raise
        _fail("evidence-unreadable", f"{path}: {e}")
    if isinstance(doc, dict) and "schema_version" in doc:
        _fail("unsupported-evidence", ".slt.json v1 carries no provenance")
    if not isinstance(doc, dict) or doc.get("evidenceVersion") != EVIDENCE_VERSION:
        _fail("unsupported-evidence-version", f"{path}: evidenceVersion {doc.get('evidenceVersion') if isinstance(doc, dict) else None!r}")
    if "partial" in doc:
        try:
            validate_partial_41(doc)
        except ValueError as e:
            _fail("evidence-invalid", f"{path}: {e}")
        env = Envelope(doc, complete=False)
    else:
        validate_case(doc)
        env = Envelope(doc, complete=True)
    run = doc["run"]
    item = str(run.get("nodeid") or "").rsplit("::", 1)[-1] if run.get("nodeid") else None
    if path.parent.name != _safe(run["runId"]) or (item is not None and path.parent.parent.name != _safe(item)):
        _fail("evidence-path-mismatch", f"{path} does not match run {run['runId']!r} / item {item!r}")
    return env


def to_v1(doc: dict) -> dict:
    """The C4 adapter: the v1 sidecar fields an envelope determines (name, nodeid, status, skip reason and
    assertion records)."""
    run = doc["run"]
    return {"name": str(run["nodeid"]).rsplit("::", 1)[-1], "nodeid": run["nodeid"], "status": run["status"],
            "skip_reason": run["skipReason"], "assertions": list(doc["assertions"])}


# ---------------------------------------------------------------- writing and the outcome

def write_case_envelope(junit_path, item_name: str, run_id: str, doc: dict, secrets=None) -> Path:
    """Redact, validate and write exclusively. An existing envelope is ``evidence-exists``, never overwritten."""
    secrets = known_secrets() if secrets is None else secrets
    doc = redact(doc, secrets)
    validate_case(doc, secrets)
    text = dumps(doc)
    path = envelope_path(junit_path, item_name, run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    except FileExistsError:
        _fail("evidence-exists", f"{path} already exists; evidence is never overwritten")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    return path


def _record_error(item, report, error: str) -> None:
    error = redact_text(error, known_secrets())[:2000]
    item._slt_evidence_error = error
    config = item.config
    config.__dict__.setdefault("_slt_evidence_errors", []).append(
        {"nodeid": getattr(item, "nodeid", None), "name": getattr(item, "name", None), "error": error})
    for target in (getattr(item, "user_properties", None), getattr(report, "user_properties", None)):
        if isinstance(target, list) and ("slt_evidence_error", error) not in target:
            target.append(("slt_evidence_error", error))
    print(f"[slt] EVIDENCE ERROR for {getattr(item, 'name', '?')}: {error}")


def finalize_from_report(item, report) -> Path | None:
    """H5: called for every phase report of a live item; writes the envelope once, for the phase that
    decides the outcome (call, or a failed/skipped setup). Never raises: a build, validation or write
    failure is recorded as an evidence error and folded into the run outcome at session finish."""
    try:        # The v1 record facts travel with the report to an xdist controller
        report._slt_v1 = {"name": getattr(item, "name", None), "topology": getattr(item, "_slt_topology", "single"),
                          "services": list(getattr(item, "_slt_services", None) or []),
                          "assertions": list(getattr(item, "_slt_records", None) or [])}
    except Exception:                   # noqa: BLE001 - a report that cannot carry them is still reported
        pass
    status = _report_status(report)
    if status is None or getattr(item, "_slt_evidence_path", None) is not None \
            or getattr(item, "_slt_evidence_error", None) is not None:
        return None
    junit = getattr(getattr(item.config, "option", None), "xmlpath", None)
    if not junit:
        return None
    try:
        doc = case_envelope(item, report, status)
        path = write_case_envelope(junit, item.name, doc["run"]["runId"], doc)
    except Exception as e:              # noqa: BLE001 - recorded, and part of the outcome
        _record_error(item, report, f"{getattr(e, 'code', type(e).__name__)}: {e}")
        return None
    item._slt_evidence_path = path
    item.config.__dict__.setdefault("_slt_evidence_paths", []).append(path)
    props = [("slt_evidence_json", str(path)), ("slt_qualifies", "true" if doc["run"]["qualifies"] else "false")]
    for target in (getattr(item, "user_properties", None), getattr(report, "user_properties", None)):
        if isinstance(target, list):
            target.extend(p for p in props if p not in target)
    return path


def stage_on(m, role: str, name, src, dest=None) -> Path:
    """Stage the bytes of ``src`` once into a private file (``dest``, or a fresh temp dir) and record exactly those
    bytes in the case's input snapshot; the caller transfers the staged file, so the evidence names the bytes sent even
    if ``src`` changes during the run. Without a snapshot on the manifest the bytes are staged only.
    (Here, not in ``livetest.inputs``: that module never writes a file.)"""
    import shutil
    import tempfile
    try:
        data = Path(src).read_bytes()
    except OSError:
        if dest is not None:
            raise
        return Path(src)                # unreadable: the transfer of src fails as it did before staging
    dest = Path(dest) if dest is not None else Path(tempfile.mkdtemp(prefix=_STAGE_PREFIX)) / Path(src).name
    dest.write_bytes(data)
    shutil.copymode(src, dest)          # a 0755 script or a 0600 key arrives with its mode, as a direct copy did
    snap = getattr(m, "_slt_inputs", None)
    if snap is not None:
        snap.record(role, name, data, path=src)
    return dest


_STAGE_PREFIX = "slt-stage-"


def unstage(path) -> None:
    """Remove a file ``stage_on`` staged into its own temp dir, with that dir; any other path is left alone."""
    import shutil
    import tempfile
    parent = Path(path).parent
    if parent.name.startswith(_STAGE_PREFIX) and parent.parent == Path(tempfile.gettempdir()):
        shutil.rmtree(parent, ignore_errors=True)


def sidecar_write_failed(config, path, exc) -> None:
    """``_slt_write_sidecar`` could not write the v1 sidecar: a session-level evidence error, folded
    into the outcome (exit 1, the JUnit global property) like any other."""
    try:
        errors = config.__dict__.setdefault("_slt_evidence_errors", [])
        error = redact_text(f"sidecar-write-failed: {path}: {exc!r}", known_secrets())[:2000]
        if not any(e.get("nodeid") is None and e.get("error") == error for e in errors):   # the fold rewrites it once more
            errors.append({"nodeid": None, "name": "session", "error": error})
    except Exception:                   # noqa: BLE001
        pass


def fold_evidence_errors(session, write_sidecar=None) -> None:
    """Session finish (C7.4 1.9.0): any evidence error fails the run -- exit status 1, and each affected
    passed case becomes ``error`` in JUnit and in the v1 sidecar. Never raises; folds once."""
    config = session.config
    errors = [e for e in (getattr(config, "_slt_evidence_errors", None) or []) if not e.get("_folded")]
    if not errors:
        return
    for e in errors:
        e["_folded"] = True
    detail = "evidence error: " + "; ".join(f"{e['name']}: {e['error']}" for e in errors)
    try:
        if session.exitstatus == 0:
            session.exitstatus = 1
    except Exception:                   # noqa: BLE001
        pass
    results = getattr(config, "_slt_results", None) or {}
    # A worker's errors reach the xdist controller through note_report, where _slt_results is empty (the v1
    # sidecar under xdist is still to come): the JUnit case is still marked, by nodeid and item name.
    affected = {e["nodeid"]: results.get(e["nodeid"]) or {"name": e["name"], "nodeid": e["nodeid"]} for e in errors
                if e.get("nodeid") is not None}     # a session-level error (a sidecar write) marks no case
    for tr in affected.values():
        if tr.get("status") == "passed":
            tr["status"] = "error"
    if write_sidecar is not None:
        write_sidecar(config)
    _junit_mark_error(config, affected, detail[:4000], "slt_evidence_error")


def finalize_session(session, write_sidecar=None) -> None:
    """After session teardown (C7.4): a failed teardown of owned infrastructure fails the run. The exit
    status becomes 1, every live case this process reported is marked ``error`` with the teardown
    failure in JUnit (still unwritten: the plugin's session-finish hook runs first) and in the v1
    sidecar, and each v2 envelope this process wrote records ``resources.infrastructure.teardown`` and
    never qualifies (the same writer amending its own envelope, re-validated). Evidence errors fold too.
    Never raises."""
    fold_evidence_errors(session, write_sidecar)
    config = session.config
    failures = getattr(config, "_slt_teardown_failures", None)
    if not failures:
        return
    detail = "infrastructure teardown failed: " + "; ".join(f"{f['resource']}: {f['error']}" for f in failures)
    try:
        if session.exitstatus == 0:
            session.exitstatus = 1
    except Exception:                   # noqa: BLE001
        pass
    for path in list(getattr(config, "_slt_evidence_paths", []) or []):
        try:
            doc = json.loads(Path(path).read_text())
            infra = getattr(config, "_slt_infra", None)
            doc["resources"]["infrastructure"] = infra.record() if infra is not None else \
                {**(doc["resources"].get("infrastructure") or {}), "teardown": {"status": "failed", "failures": failures}}
            if doc["run"]["status"] == "passed":
                doc["run"]["status"] = "error"
                doc["run"]["failure"] = detail[:2000]
            doc["run"]["qualifies"], doc["run"]["qualifiesReason"] = False, detail
            secrets = known_secrets()
            doc = redact(doc, secrets)
            validate_case(doc, secrets)
            tmp = Path(path).with_name(Path(path).name + ".teardown")
            tmp.write_text(dumps(doc))
            os.replace(tmp, path)
        except Exception as e:          # noqa: BLE001 - the exit status and junit already carry the failure
            print(f"[slt] WARNING: could not record the teardown failure in {path}: {e!r}")
    results = getattr(config, "_slt_results", None) or {}
    for tr in results.values():
        if tr.get("status") == "passed":
            tr["status"] = "error"
    if write_sidecar is not None:
        write_sidecar(config)
    _junit_mark_error(config, results, detail, "slt_infrastructure_teardown")


def _junit_mark_error(config, results, detail: str, prop: str) -> None:
    """Mark each passed live testcase in the pending JUnit report with an ``<error>``. Uses the junitxml
    plugin's per-case reporters (their XML is frozen at test teardown); when that shape is not available,
    a testsuite property still carries the failure."""
    try:                                # the junitxml plugin object, found by its interface (no pytest-private import)
        xml = next((pl for pl in config.pluginmanager.get_plugins()
                    if hasattr(pl, "node_reporters_ordered") and hasattr(pl, "add_global_property")), None)
    except Exception:                   # noqa: BLE001
        xml = None
    if xml is None:
        return
    marked = 0
    try:
        for reporter in getattr(xml, "node_reporters_ordered", []):
            el = reporter.to_xml()
            nodeid = getattr(reporter, "id", None)
            if el.find("failure") is not None or el.find("error") is not None or el.find("skipped") is not None:
                continue
            if results and not any(el.get("name") == str(tr.get("name")) or nodeid == nid
                                   for nid, tr in results.items()):
                continue
            if not results and prop == "slt_evidence_error":
                continue
            import xml.etree.ElementTree as ET
            for p in el.iter("property"):
                if p.get("name") == "slt_qualifies":
                    p.set("value", "false")
            err = ET.SubElement(el, "error", message=detail[:1000])
            err.text = detail
            marked += 1
        if marked:
            xml.stats["passed"] = max(0, xml.stats.get("passed", 0) - marked)
            xml.stats["error"] = xml.stats.get("error", 0) + marked
    except Exception:                   # noqa: BLE001
        pass
    try:
        xml.add_global_property(prop, detail[:1000])
    except Exception:                   # noqa: BLE001
        pass


# ---------------------------------------------------------------- invocation binding (C5 1.9.0)

_SESSION: dict = {}


def bind_invocation(config) -> None:
    """``pytest_sessionstart``: remember the session's config for ``note_report`` and add the JUnit global
    property ``slt_invocation`` when the junitxml plugin is present (the controller under xdist). Never raises."""
    _SESSION["config"] = config
    invocation = os.environ.get("SLT_INVOCATION_ID")
    if not invocation:
        return
    try:
        xml = next((pl for pl in config.pluginmanager.get_plugins()
                    if hasattr(pl, "node_reporters_ordered") and hasattr(pl, "add_global_property")), None)
        if xml is not None:
            xml.add_global_property("slt_invocation", invocation)
    except Exception:                   # noqa: BLE001
        pass


def note_report(report) -> None:
    """``pytest_runtest_logreport``: an evidence error recorded on the report (by this process, or by an xdist
    worker whose report reached the controller) is recorded once per nodeid on this session's config."""
    config = _SESSION.get("config")
    if config is None:
        return
    # On the xdist controller, a worker's report (xdist sets report.node) carries the v1 record
    # facts, so the controller's sidecar holds every case, whichever worker ran it.
    v1 = getattr(report, "_slt_v1", None)
    if isinstance(v1, dict) and getattr(report, "node", None) is not None and not hasattr(config, "workerinput"):
        from types import SimpleNamespace
        try:
            from livetest.plugin import _slt_collect_report
            _slt_collect_report(config, SimpleNamespace(
                name=v1.get("name") or str(report.nodeid).rsplit("::", 1)[-1], nodeid=report.nodeid,
                _slt_topology=v1.get("topology") or "single", _slt_services=list(v1.get("services") or []),
                _slt_records=list(v1.get("assertions") or []), user_properties=[]), report)
        except Exception as e:          # noqa: BLE001 - a lost record shows as a counts-agree failure, never silently
            print(f"[slt] WARNING: could not record {report.nodeid} in the controller's results sidecar: {e!r}")
    for key, value in getattr(report, "user_properties", None) or []:
        if key != "slt_evidence_error":
            continue
        errors = config.__dict__.setdefault("_slt_evidence_errors", [])
        if not any(e.get("nodeid") == report.nodeid for e in errors):
            errors.append({"nodeid": report.nodeid, "name": str(report.nodeid).rsplit("::", 1)[-1], "error": str(value)})


# ---------------------------------------------------------------- the run record (C4 1.9.0, run-evidence.json)

JUNIT_CAP = 64 * 1024 * 1024
JSON_CAP = 16 * 1024 * 1024
ENVELOPE_LIMIT = 10000
DETAIL_ITEMS = 20
RUN_CHECKS = ("junit-present", "junit-wellformed", "junit-invocation", "counts-agree",
              "one-envelope-per-executed-case", "no-foreign-envelopes", "envelope-valid", "case-identity",
              "cleanup-record", "identity", "goldens-unchanged", "no-evidence-errors")
_STATUS_ORDER = ("error", "failed", "skipped", "passed")


def _sha_bounded(path: Path, cap: int):
    h, n = hashlib.sha256(), 0
    with open(path, "rb") as f:
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            n += len(chunk)
            if n > cap:
                return None, n
            h.update(chunk)
    return "sha256:" + h.hexdigest(), n


def _ref(path: Path):
    try:
        sha, _n = _sha_bounded(path, JSON_CAP)
    except OSError:
        return None
    return {"path": str(path), "sha256": sha} if sha else None


def _load_json(path: Path):
    try:
        if path.stat().st_size > JSON_CAP:
            return None
        with open(path, "rb") as f:
            return json.loads(f.read(JSON_CAP + 1))
    except (OSError, ValueError):
        return None


def _selection_doc(tier_dir: Path):
    sel = _load_json(tier_dir / "selection.json")
    if sel is not None:
        return sel, tier_dir / "selection.json"
    workers = sorted(tier_dir.glob("selection-*.json"))
    return (_load_json(workers[0]), workers[0]) if workers else (None, None)


def _junit_facts(path: Path):
    """``(facts, problem)``; the file is size-checked before it is parsed."""
    import xml.etree.ElementTree as ET
    size = path.stat().st_size
    if size > JUNIT_CAP:
        return None, f"{path} is {size} bytes, over the {JUNIT_CAP}-byte cap; not parsed"
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError as e:
        return None, f"{path}: {e}"
    if root.tag not in ("testsuite", "testsuites"):
        return None, f"{path}: root <{root.tag}> is not <testsuites>/<testsuite>"
    suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
    facts = {"tests": 0, "failures": 0, "errors": 0, "skipped": 0, "invocationProperty": None,
             "globalEvidenceError": None, "cases": []}
    for suite in suites:
        props = suite.find("properties")
        for prop in (props.findall("property") if props is not None else []):
            if prop.get("name") == "slt_invocation":
                facts["invocationProperty"] = prop.get("value")
            elif prop.get("name") == "slt_evidence_error":
                facts["globalEvidenceError"] = prop.get("value")
    for case in root.iter("testcase"):
        status = ("failed" if case.find("failure") is not None else "error" if case.find("error") is not None
                  else "skipped" if case.find("skipped") is not None else "passed")
        facts["tests"] += 1
        facts[{"failed": "failures", "error": "errors", "skipped": "skipped"}.get(status, "passed")] = \
            facts.get({"failed": "failures", "error": "errors", "skipped": "skipped"}.get(status, "passed"), 0) + 1
        facts["cases"].append({"name": case.get("name"), "classname": case.get("classname"), "status": status,
                               "props": {p.get("name"): p.get("value") for p in case.iter("property")}})
    facts.pop("passed", None)
    return facts, None


def _results_status(results: dict) -> dict:
    out = {}
    for key, status in (("passed", "passed"), ("xfailed", "skipped"), ("skipped", "skipped"),
                        ("failed", "failed"), ("errors", "error")):
        for e in (results or {}).get(key, []):
            nid = e.get("nodeid")
            prev = out.get(nid)
            if prev is None or _STATUS_ORDER.index(status) < _STATUS_ORDER.index(prev):
                out[nid] = status
    return out


_RESULT_OUTCOMES = ("passed", "failed", "skipped", "xfailed")


def _results_multiplicity(results) -> list[str]:
    """The guard's raw ``results.json`` records, before ``_results_status`` folds them. Per nodeid: at
    most one outcome record (the call's passed/failed/skipped/xfailed, or a setup skip), at most one error per
    setup/teardown phase and no outcome after a setup error. A setup/teardown error beside its call outcome is the
    legitimate combination; a repeated record is a disagreement between sources, never folded away."""
    if not isinstance(results, dict):
        return ["results.json is not an object"]
    problems, per = [], {}
    for key in _RESULT_OUTCOMES + ("errors",):
        entries = results.get(key, [])
        if not isinstance(entries, list):
            problems.append(f"results.json {key} is not a list")
            continue
        for e in entries:
            nodeid = e.get("nodeid") if isinstance(e, dict) else None
            if not _nonempty(nodeid):
                problems.append(f"results.json {key} record without a nodeid: {e!r}"[:300])
                continue
            rec = per.setdefault(nodeid, {"outcomes": [], "errors": []})
            if key == "errors":
                rec["errors"].append(e.get("when"))
            else:
                rec["outcomes"].append(key)
    for nodeid, rec in sorted(per.items()):
        outcomes, errors = rec["outcomes"], rec["errors"]
        if len(outcomes) > 1:
            problems.append(f"{nodeid}: {len(outcomes)} outcome records in results.json {outcomes}")
        if any(w not in ("setup", "teardown") for w in errors) or len(set(errors)) != len(errors):
            problems.append(f"{nodeid}: results.json errors {errors} (at most one per setup and teardown phase)")
        if "setup" in errors and outcomes:
            problems.append(f"{nodeid}: results.json has a setup error and the outcome {outcomes}")
    return problems


def _raw_invocation(path: Path):
    doc = _load_json(path)
    if not isinstance(doc, dict):
        return None, None, None
    run = doc.get("run") if isinstance(doc.get("run"), dict) else {}
    return run.get("invocation"), run.get("runId"), run.get("nodeid")


def _junit_key(nodeid: str) -> tuple[str, str]:
    """The (classname, name) pytest's junitxml writes for a nodeid (``mangle_test_address``)."""
    path, bracket, params = str(nodeid).partition("[")
    names = path.split("::")
    names[0] = re.sub(r"\.py$", "", names[0].replace("/", "."))
    names[-1] += bracket + params
    return ".".join(names[:-1]), names[-1]


def _reconcile(tier_dir: Path, junit, facts, results, status_results: dict, mine) -> tuple[bool, list | str]:
    """Every source must be readable and schema-valid, and the executed case sets of the JUnit, the
    guard's results, the v1 sidecar and this invocation's envelopes must match in both directions, one record per
    case (a JUnit testcase repeated only for an extra teardown error is accounted for), with one status."""
    problems = []
    if junit is None:
        problems.append("no readable JUnit")
    if results is None:
        problems.append("results.json is missing or unreadable")
    sidecar, side_nodes = _load_json(tier_dir / "junit.slt.json"), []
    if sidecar is None:
        problems.append("the v1 sidecar junit.slt.json is missing or unreadable")
    else:
        try:
            from livetest.resultschema import validate as _v1_validate
            _v1_validate(sidecar)
            side_nodes = [t["nodeid"] for t in sidecar["tests"]]
        except Exception as e:          # noqa: BLE001 - a malformed sidecar is a failed check, never a crash
            problems.append(f"the v1 sidecar is not schema-valid: {e}"[:300])
    if not problems:
        problems += _results_multiplicity(results)
    if problems:
        return False, problems
    side_status, dup_side = {}, []
    for t in sidecar["tests"]:
        if t["nodeid"] in side_status:
            dup_side.append(t["nodeid"])
        side_status[t["nodeid"]] = t["status"]
    env_status = {}
    for _p, e in mine:
        env_status.setdefault(e.doc["run"]["nodeid"], e.doc["run"]["status"])
    known = set(status_results) | set(side_status) | set(env_status)
    by_key = {_junit_key(n): n for n in known}
    junit_status, unmatched, dups = {}, [], []
    for case in facts["cases"]:
        nodeid = by_key.get((case.get("classname") or "", case["name"]))
        if nodeid is None:
            unmatched.append(f"{case.get('classname')}::{case['name']} ({case['status']})")
            continue
        junit_status.setdefault(nodeid, []).append(case["status"])
    for nodeid, statuses in junit_status.items():
        if len(statuses) > 1 and sorted(statuses, key=_STATUS_ORDER.index)[1:] != ["error"] * (len(statuses) - 1) \
                and statuses.count("error") < len(statuses) - 1:
            dups.append(f"{nodeid}: {len(statuses)} JUnit testcases {statuses}")
    problems += [f"unmatched JUnit testcase {u}" for u in unmatched]
    problems += [f"duplicate v1 record {n}" for n in dup_side] + dups
    for nodeid in sorted(known | set(junit_status)):
        j = junit_status.get(nodeid)
        seen = {"results": status_results.get(nodeid), "junit": sorted(j, key=_STATUS_ORDER.index)[0] if j else None,
                "v1": side_status.get(nodeid), "envelope": env_status.get(nodeid)}
        if len(set(seen.values())) != 1 or None in seen.values():
            problems.append(f"{nodeid}: {seen}")
    return not problems, problems[:DETAIL_ITEMS]


def check(tier_dir, invocation: str, identity_path, *, label: str | None = None) -> tuple[Path, dict]:
    """Bind one tier's evidence to its fresh JUnit invocation and write ``<tier>/run-evidence.json`` exclusively.

    Every check is named with a bounded detail; ``valid`` is every check passing, and ``qualified`` is
    ``valid`` with every selected case executed, nothing skipped and every case qualifying. Never picks the
    more favourable of disagreeing sources."""
    tier_dir, identity_path = Path(tier_dir), Path(identity_path)
    checks = []

    def add(name, ok, detail=None):
        checks.append({"name": name, "ok": bool(ok), "detail": None if ok else detail})

    junit_path = tier_dir / "junit.xml"
    junit = None
    if not junit_path.is_file():
        add("junit-present", False, f"{junit_path} does not exist")
        add("junit-wellformed", False, "no JUnit")
        add("junit-invocation", False, "no JUnit")
    else:
        add("junit-present", True)
        facts, problem = _junit_facts(junit_path)
        sha, nbytes = _sha_bounded(junit_path, JUNIT_CAP)
        add("junit-wellformed", facts is not None, problem)
        if facts is not None:
            junit = {"path": str(junit_path), "sha256": sha, "bytes": nbytes,
                     **{k: facts[k] for k in ("tests", "failures", "errors", "skipped", "invocationProperty")}}
            add("junit-invocation", facts["invocationProperty"] == invocation,
                f"JUnit slt_invocation {facts['invocationProperty']!r} != {invocation!r} (a stale or foreign JUnit)")
        else:
            add("junit-invocation", False, "the JUnit could not be read")

    sel, sel_path = _selection_doc(tier_dir)
    results = _load_json(tier_dir / "results.json")
    selected = [e for e in (sel or {}).get("selected", [])]
    by_node = {e["nodeid"]: e["id"] for e in selected}
    status_results = _results_status(results)
    executed = sorted(n for n in status_results if n in by_node) + sorted(n for n in status_results if n not in by_node)
    skipped = [{"id": by_node.get(e["nodeid"], e["nodeid"]), "reason": e.get("reason", "")}
               for e in (results or {}).get("skipped", []) + (results or {}).get("xfailed", [])]
    deselected = [{"id": e["id"], "reason": e["reason"]} for e in (sel or {}).get("deselected", [])]
    not_executed = [e["id"] for e in selected if e["nodeid"] not in status_results]

    envelopes, unreadable, foreign = [], [], []
    paths = sorted((tier_dir / "evidence").glob("*/*/evidence.json"))[:ENVELOPE_LIMIT + 1]
    for path in paths[:ENVELOPE_LIMIT]:
        inv, run_id, nodeid = _raw_invocation(path)
        if inv != invocation:
            foreign.append(f"{path.relative_to(tier_dir)} (invocation {inv!r})")
        try:
            env = read(path)
            if not env.complete:
                unreadable.append(f"{path.relative_to(tier_dir)}: a legacy partial envelope")
            envelopes.append((path, env))
        except EvidenceError as e:
            unreadable.append(f"{path.relative_to(tier_dir)}: {e}"[:300])
            envelopes.append((path, None))
    if len(paths) > ENVELOPE_LIMIT:
        unreadable.append(f"more than {ENVELOPE_LIMIT} envelopes")
    add("no-foreign-envelopes", not foreign, foreign[:DETAIL_ITEMS])
    add("envelope-valid", not unreadable, unreadable[:DETAIL_ITEMS])
    mine = [(p, e) for p, e in envelopes if e is not None and e.doc["run"].get("invocation") == invocation]

    counts = {}
    for _p, e in mine:
        counts[e.doc["run"]["nodeid"]] = counts.get(e.doc["run"]["nodeid"], 0) + 1
    raw_nodes = {}
    for p, e in envelopes:
        raw_nodes[p] = e.doc["run"].get("nodeid") if e is not None else _raw_invocation(p)[2]
    one = [f"{n}: {counts.get(n, 0)} envelopes" for n in executed if counts.get(n, 0) != 1]
    one += [f"{n}: an envelope for a case with no executed outcome" for n in counts if n not in status_results]
    add("one-envelope-per-executed-case", not one, one[:DETAIL_ITEMS])
    strangers = [f"{p.relative_to(tier_dir)}: {n!r}" for p, n in raw_nodes.items() if n not in by_node]
    add("case-identity", sel is not None and not strangers,
        "no selection record" if sel is None else strangers[:DETAIL_ITEMS])
    # over every envelope document this invocation wrote, read raw: a record without its cleanup also fails
    # validation, and the check must still name it
    no_cleanup = []
    for p in paths[:ENVELOPE_LIMIT]:
        raw = _load_json(p)
        run = (raw or {}).get("run") if isinstance((raw or {}).get("run"), dict) else {}
        if run.get("invocation") == invocation and \
                (((raw or {}).get("lifecycle") or {}).get("cleanup") or {}).get("status") not in CLEANUP_STATUSES:
            no_cleanup.append(str(p.relative_to(tier_dir)))
    add("cleanup-record", not no_cleanup, no_cleanup[:DETAIL_ITEMS])

    identity = _load_json(identity_path)
    ident_problems = []
    if not isinstance(identity, dict):
        ident_problems.append(f"{identity_path} is missing or unreadable")
    else:
        fw = identity.get("framework") if isinstance(identity.get("framework"), dict) else {}
        for p, e in mine:
            if identity.get("runEpoch") and e.doc["run"]["runId"] != identity["runEpoch"]:
                ident_problems.append(f"{p.relative_to(tier_dir)}: runId {e.doc['run']['runId']!r} != identity "
                                      f"runEpoch {identity['runEpoch']!r}")
            efw = e.doc["inputs"].get("framework") or {}
            for key in ("wheelSha256", "lockSha256"):
                if isinstance(efw.get(key), str) and efw.get(key) != fw.get(key):
                    ident_problems.append(f"{p.relative_to(tier_dir)}: {key} differs from identity.json")
    add("identity", not ident_problems, ident_problems[:DETAIL_ITEMS])
    changed = [f"{p.relative_to(tier_dir)}: {g}" for p, e in mine
               for g, rec in (e.doc["integrity"]["goldens"] or {}).items() if not rec.get("unchanged")]
    add("goldens-unchanged", not changed, changed[:DETAIL_ITEMS])

    add("counts-agree", *_reconcile(tier_dir, junit, facts if junit is not None else None, results, status_results, mine))
    errors = [f"{c['name']}: {c['props']['slt_evidence_error']}" for c in (facts["cases"] if junit is not None else [])
              if c["props"].get("slt_evidence_error")]
    if junit is not None and facts.get("globalEvidenceError"):
        errors.append(f"session: {facts['globalEvidenceError']}")
    errors += [f"{p.relative_to(tier_dir)}: {x}" for p, e in mine for x in e.doc["integrity"]["evidenceErrors"]]
    add("no-evidence-errors", not errors, errors[:DETAIL_ITEMS])

    assert [c["name"] for c in checks] != [], "no checks"
    checks.sort(key=lambda c: RUN_CHECKS.index(c["name"]))
    valid = all(c["ok"] for c in checks)
    cases = [{"caseId": e.doc["run"]["caseId"], "nodeid": e.doc["run"]["nodeid"],
              "envelope": {"path": str(p), "sha256": _ref(p)["sha256"] if _ref(p) else None},
              "status": e.doc["run"]["status"], "qualifies": e.doc["run"]["qualifies"],
              "qualifiesReason": e.doc["run"]["qualifiesReason"],
              "dataSha256": (e.doc["data"] or {}).get("canonicalSha256"),
              "logicalInputsSha256": e.doc["inputs"]["logicalInputsSha256"]} for p, e in mine]
    reason = None
    if not valid:
        reason = "evidence invalid: " + ", ".join(c["name"] for c in checks if not c["ok"])
    elif not selected:
        reason = "no case was selected"
    elif not_executed:
        reason = f"selected but not executed: {not_executed[:DETAIL_ITEMS]}"
    elif skipped:
        reason = f"skipped: {[s['id'] for s in skipped][:DETAIL_ITEMS]}"
    else:
        bad = next((c for c in cases if not c["qualifies"]), None)
        if bad is not None:
            reason = f"{bad['nodeid']} does not qualify: {bad['qualifiesReason']}"
    fw = (identity or {}).get("framework") if isinstance((identity or {}).get("framework"), dict) else {}
    mode = (identity or {}).get("mode")
    def _artifact(key, sha_key):
        value = fw.get(sha_key)
        return {"path": None, "sha256": value} if isinstance(value, str) else (value or {"reason": "not recorded"})
    record = {
        "evidenceVersion": EVIDENCE_VERSION, "kind": "run",
        "runId": (identity or {}).get("runEpoch"), "invocation": invocation,
        "tier": (sel or {}).get("tier"), "label": label, "createdAt": _now_iso(),
        "identity": {"identityJsonSha256": (_ref(identity_path) or {}).get("sha256"),
                     "runEpoch": (identity or {}).get("runEpoch"),
                     "infraOwnership": (identity or {}).get("infraOwnership"),
                     "interpreter": (identity or {}).get("interpreter"),
                     "manifest": (identity or {}).get("manifest"),
                     "framework": {"mode": mode, "packages": (identity or {}).get("packages"),
                                   "provenance": next((e.doc["inputs"]["framework"].get("provenance") for _p, e in mine), None),
                                   "wheel": _artifact("wheel", "wheelSha256"), "lock": _artifact("lock", "lockSha256")}},
        "reports": {"junit": junit, "sidecar": _ref(tier_dir / "junit.slt.json"),
                    "results": _ref(tier_dir / "results.json"), "selection": _ref(sel_path) if sel_path else None},
        "selection": {"selected": [e["id"] for e in selected], "executed": [by_node.get(n, n) for n in executed],
                      "skipped": skipped, "deselected": deselected, "notExecuted": not_executed},
        "cases": cases, "checks": checks, "valid": valid, "qualified": reason is None, "qualifiedReason": reason,
        "installedWheel": mode == "wheel" and all(isinstance(fw.get(k), str) for k in ("wheelSha256", "lockSha256")),
    }
    record = redact(record, known_secrets())
    out = tier_dir / "run-evidence.json"
    try:
        fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    except FileExistsError:
        _fail("evidence-exists", f"{out} already exists; a run record is never overwritten")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(dumps(record))
    return out, record


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def main(argv=None) -> int:
    """``python -m livetest.evidence check --tier-dir T --invocation I --identity F [--label L]``.
    Exit 0: the run record was written (valid or not); 3: a record already exists; 2: usage or a crash."""
    import argparse
    ap = argparse.ArgumentParser(prog="python -m livetest.evidence")
    sub = ap.add_subparsers(dest="command", required=True)
    c = sub.add_parser("check", help="write <tier>/run-evidence.json for one tier invocation")
    c.add_argument("--tier-dir", required=True)
    c.add_argument("--invocation", required=True)
    c.add_argument("--identity", required=True)
    c.add_argument("--label")
    args = ap.parse_args(argv)
    try:
        path, record = check(args.tier_dir, args.invocation, args.identity, label=args.label)
    except EvidenceError as e:
        print(f"evidence check: {e}", file=sys.stderr)
        return 3 if e.code == "evidence-exists" else 2
    print(json.dumps({"path": str(path), "valid": record["valid"], "qualified": record["qualified"],
                      "failed": [c["name"] for c in record["checks"] if not c["ok"]]}))
    return 0


# legacy callers and tests
validate_41_sections = validate_partial_41
_junit_teardown_error = lambda config, results, detail: _junit_mark_error(config, results, detail, "slt_infrastructure_teardown")  # noqa: E731


if __name__ == "__main__":
    sys.exit(main())
