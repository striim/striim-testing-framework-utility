from __future__ import annotations
import re
import warnings
from dataclasses import dataclass, field
from pathlib import Path
import yaml

from livetest import paths
from livetest.enforcement import WALKER_EXEMPT_DIR_PARTS
from livetest.releases import ReleaseError, check_release_entry, release_matches

# L1 (docs/WRITING-TESTS.md): the third axis, orthogonal to a runner's kind
# (unit|integration|perf|live) and module axes. `gate` is reserved for the L10
# hand-picks (one per module x service) -- rule B never produces it.
VALID_DEPTHS = {"smoke", "gate", "regression", "fault", "customer", "measure", "canary"}
VALID_TOPOLOGIES = {"single", "agent", "cluster"}
# Assertion tiers an `xfail.tiers` may name. The record `type` each assert_* function writes
# (resultschema.build_assertion_result) is the vocabulary; `smoke` is deliberately absent.
XFAIL_TIERS_ALLOWED = frozenset(
    {"data", "diff", "file", "gcs", "json", "monitor", "halt", "checkpoint_history", "jmx"})
XFAIL_TIERS_DEFAULT = XFAIL_TIERS_ALLOWED
VALID_SEED_WHEN = {"pre_deploy", "post_start", "post_recover"}
# Every key load_manifest reads. Anything else is a typo or a key dropped in a schema change,
# and both used to load silently: `timout: 30` simply never applied its timeout, and a manifest
# still carrying a removed lifecycle key quietly reverted to the default -- which for a CDC test
# means seeding before the reader starts, so it fails for a reason the manifest does not show.
VALID_MANIFEST_KEYS = {
    "name", "purpose", "tql", "topology", "requires", "ddl", "seed", "action", "assert", "example",
    "op", "udf", "server_files", "generate", "timeout", "diff_poll", "tags", "disabled",
    "disabled_parallel", "xfail", "expect_halt", "expect_halt_contains", "recover",
    "kafka_cleanup_topics", "depth", "tokens",
    "lifecycle",   # C7.2
    "exact",   # C8.1
}

# `tokens:` may not shadow a token the harness sets itself. The fixed names mirror plugin.py's
# token map (NS/APP/... , striim_group_tokens, striim_url_tokens, releases.resolve_release); the
# per-module ${<TOKEN>_JAR}/${<TOKEN>_NAME} and every service's `provides:` are added at load.
_HARNESS_TOKENS = frozenset({
    "NS", "APP", "APP_BARE", "TID", "TID_UPPER", "TID_ORACLE",
    "APP_GROUP", "SOURCE_GROUP", "STRIIM_WEB_URL",
    "STRIIM_RELEASE", "STRIIM_VERSION", "STRIIM_SERIES", "JAVA_RELEASE",
    "MSSQL_JDBC_VERSION",
    "RUN_ID", "WORKER", "ATTEMPT", "OWNED_DIR"})   # livetest.runident.tokens
_TOKEN_NAME = re.compile(r"[A-Z][A-Z0-9_]*\Z")

# `recover.mode` values. The three are deliberately NOT interchangeable -- they exercise
# different platform paths, and the difference is the point of the phase existing:
#   kill    -- SIGKILL the app node container. No close(), no flush, no rollback: the crash
#              case. Docker mode only (a native Striim has no container to kill).
#   stop    -- STOP APPLICATION. Flow.stopImpl() calls stopDataFlow()+stop() and NEVER
#              flush(), so a DatabaseWriter's pending batch is discarded and its open
#              transaction rolled back. The case field
#              reports lose data on, and the one people assume is safe.
#   quiesce -- QUIESCE APPLICATION. Flow.quiesceFlush() injects a FlushCommandEvent, waits
#              for NODE_APP_QUIESCE_FLUSHED, then checkpoints. The drain. Use it as the
#              CONTROL arm: a test that loses data under `stop` and not under `quiesce` has
#              localised the fault to the shutdown path rather than to the pipeline.
VALID_RECOVER_MODES = {"kill", "stop", "quiesce"}
_RECOVER_KEYS = {"mode", "after", "settle", "expect_running", "times", "every"}
_SERVICE_OUTAGE_KEYS = {"type", "service", "cycles", "delay_before", "down_for", "signal",
                        "ready_timeout", "settle", "concurrent"}
_TOKEN_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
# `capture` (a step of its own, or a `capture:` entry of drop_recreate_app / alter_recompile)
# and the two actions that rebuild an app while it is stopped.
_CAPTURE_KEYS = {"token", "describe", "mon", "field", "timeout", "expect"}
_DROP_RECREATE_KEYS = {"type", "app", "delay_before_stop", "recreate_wait", "seed",
                       "capture", "stopped_seed", "tokens"}
_ALTER_RECOMPILE_KEYS = {"type", "app", "file", "delay_before_stop", "capture", "stopped_seed",
                         "seed"}
# Statements alter_recompile writes itself around the fragment. A fragment carrying one would
# run it twice, or (USE) re-point the rest of the import at another namespace.
_ALTER_FRAGMENT_REFUSED = re.compile(
    r"\b(?:ALTER\s+APPLICATION|RECOMPILE|(?:UN)?DEPLOY\s+APPLICATION|START\s+APPLICATION"
    r"|STOP\s+APPLICATION|END\s+APPLICATION|CREATE\s+(?:OR\s+REPLACE\s+)?APPLICATION)\b"
    r"|^\s*USE\s", re.I | re.M)

class ManifestError(Exception):
    pass


def _diff_poll(raw: dict, path) -> float:
    """§85.3. Validate `diff_poll` loudly rather than letting a bad value reach time.sleep().

    ⚠ Zero is REFUSED, not treated as "as fast as possible": a zero-poll diff spins the CPU issuing
    SELECTs with no gap, which on a shared database changes the thing being measured.
    """
    if "diff_poll" not in raw:
        return 2.0
    value = raw["diff_poll"]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ManifestError(f"{path}: 'diff_poll' must be a number, got {value!r}")
    if value <= 0:
        raise ManifestError(
            f"{path}: 'diff_poll' must be greater than 0, got {value!r} -- a zero poll spins "
            f"the CPU issuing SELECTs and changes what is being measured")
    return float(value)

def _deprecated(path, msg: str) -> None:
    # One shared choke point for schema deprecations so every warning names the
    # manifest file (test.yamls are parsed far from where they live).
    warnings.warn(f"{path}: {msg}", DeprecationWarning, stacklevel=3)

_AFTER_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(ms|s|m)?\s*$")

def _parse_after(value, field_name: str, path=None) -> float:
    """Parse an `after:` delay into seconds. Accepts 30, "30", "30s", "500ms", "2m"."""
    if isinstance(value, bool):   # bool is an int subclass; `after: true` is never a duration
        raise ManifestError(f"{path}: '{field_name}' entry 'after' must be a duration, got {value!r}")
    if isinstance(value, (int, float)):
        text = f"{value}"
    elif isinstance(value, str):
        text = value
    else:
        raise ManifestError(f"{path}: '{field_name}' entry 'after' must be a duration, got {value!r}")
    m = _AFTER_RE.match(text)
    if not m:
        raise ManifestError(
            f"{path}: '{field_name}' entry 'after' must be a duration like '30s', '500ms' or "
            f"'2m' (a bare number is seconds), got {value!r}")
    amount = float(m.group(1))
    seconds = {"ms": amount / 1000.0, "m": amount * 60.0, None: amount, "s": amount}[m.group(2)]
    if seconds <= 0:
        raise ManifestError(
            f"{path}: '{field_name}' entry 'after' must be positive, got {value!r}")
    return seconds

def _parse_zeroable_duration(value, field_name: str, path=None) -> float:
    """Like `_parse_after` -- same units, same regex -- but ZERO is legal.

    `recover.after: 0` (interrupt the instant the seed lands, the widest in-flight window) and
    `recover.settle: 0` (assert immediately, for a test that wants to see the un-drained state)
    are both meaningful. `_parse_after` rejects them because a zero *seed* delay means the
    `after:` key was pointless, and its error text says "entry 'after'", which reads as
    nonsense on a `recover.settle` failure.
    """
    if isinstance(value, bool):
        raise ManifestError(f"{path}: '{field_name}' must be a duration, got {value!r}")
    if isinstance(value, (int, float)):
        text = f"{value}"
    elif isinstance(value, str):
        text = value
    else:
        raise ManifestError(f"{path}: '{field_name}' must be a duration, got {value!r}")
    m = _AFTER_RE.match(text)
    if not m:
        raise ManifestError(
            f"{path}: '{field_name}' must be a duration like '30s', '500ms' or '2m' "
            f"(a bare number is seconds), got {value!r}")
    amount = float(m.group(1))
    return {"ms": amount / 1000.0, "m": amount * 60.0,
            None: amount, "s": amount}[m.group(2)]


def _normalize_file_specs(raw, field_name: str, path=None, allow_when: bool = False,
                          refs_out: list | None = None) -> list:
    # ddl/seed accept a plain string (→ the default "postgres-source" route) or a list
    # of entries in the same style `upload:`/`server_files:` entries use — a bare
    # filename string, or a block mapping led by `file:` with an optional `db:` route:
    #   ddl:
    #     - file: source_oracle_ddl.sql
    #       db: oracle-source
    # Returns a list of (route, file) tuples applied in order -- or, when allow_when is set
    # (`seed:`), (route, file, when, after) 4-tuples:
    #   seed:
    #     - file: seed1.sql
    #       db: postgres-source
    #       when: pre_deploy        # default; runs before DEPLOY
    #     - file: seed2.sql
    #       db: postgres-source
    #       when: post_start        # runs once the app is RUNNING (CDC capture)
    #       after: 30s              # ...and not until 30s past RUNNING
    # `when` is per FILE so one test can seed a baseline before deploy and change data after.
    # `after` is measured from the
    # moment the app reaches RUNNING, not from the previous seed file, so two entries at 10s
    # and 30s run 10s and 30s past RUNNING rather than 10s and 40s.
    #
    # DEPRECATED (still parsed, warns naming the file): the old route-keyed form
    # {source_db|target_db: <route>, file: <name>} — the route VALUE
    # (`<svc>-source`/`<svc>-target`) already carries the direction, so the split key
    # names were redundant; `db:` is the one canonical route key for ddl/seed.
    #
    # `local: true` (mapping form only) reads the file from the CASE dir instead of
    # source_dir -- for test-only files an `example:` case must not ship in the example.
    # The tuples stay (route, file[, when, after]); each entry's (file, local) pair is
    # appended to `refs_out` and load_manifest turns them into TestManifest.local_files.
    if raw is None:
        return []
    # The scalar and bare-filename forms take the defaults: source route, pre_deploy, no delay.
    # They must still produce the same ARITY as the mapping form -- the runner unpacks
    # seed entries as (db, file, when, after).
    _plain = (lambda f: (("postgres-source", f, "pre_deploy", 0.0) if allow_when
                         else ("postgres-source", f)))
    if isinstance(raw, str):
        if refs_out is not None:
            refs_out.append((raw, False))
        return [_plain(raw)]
    if isinstance(raw, list):
        out = []
        for item in raw:
            if isinstance(item, str) and item:
                out.append(_plain(item))
                if refs_out is not None:
                    refs_out.append((item, False))
                continue
            if not isinstance(item, dict) or not item.get("file"):
                raise ManifestError(
                    f"'{field_name}' list items need a 'file' (plus an optional 'db' "
                    f"route): {item!r}")
            if refs_out is not None:
                refs_out.append((item["file"], _parse_local(item, field_name, path)))
            # A ddl/seed file routes to ONE admin; default to the source route.
            route = item.get("db")
            for old_key in ("source_db", "target_db"):
                if old_key in item:
                    _deprecated(path, f"'{field_name}' entry uses the deprecated "
                                f"'{old_key}:' route key — write it as "
                                f"'- file: {item['file']}' + 'db: {item[old_key]}'")
                    route = route or item[old_key]
            if not allow_when:
                # `when`/`after` are meaningless for ddl: schema must exist before the reader
                # and writer start, so ddl has exactly one lifecycle point.
                for key in ("when", "after"):
                    if key in item:
                        raise ManifestError(
                            f"{path}: '{field_name}' entry has '{key}', which only 'seed' "
                            f"entries accept -- ddl always runs before DEPLOY: {item!r}")
                out.append((route or "postgres-source", item["file"]))
                continue
            when = item.get("when", "pre_deploy")
            if when not in VALID_SEED_WHEN:
                raise ManifestError(
                    f"{path}: '{field_name}' entry 'when' must be one of "
                    f"{sorted(VALID_SEED_WHEN)}, got {when!r}")
            after = 0.0
            if "after" in item:
                # Rejected rather than ignored, for the reason expect_halt_contains-without-
                # expect_halt is rejected: a delay that silently does nothing hides the intent
                # of whoever wrote it. Pre-deploy seeding runs before the app exists, so there
                # is nothing to wait for -- an author writing this wants post_start.
                if when not in ("post_start", "post_recover"):
                    raise ManifestError(
                        f"{path}: '{field_name}' entry sets 'after' with 'when: {when}'. "
                        f"'after' delays seeding relative to the app reaching RUNNING (after the "
                        f"start, or after the recover: restore), so it is only meaningful with "
                        f"'when: post_start' or 'when: post_recover': {item!r}")
                after = _parse_after(item["after"], field_name, path)
            out.append((route or "postgres-source", item["file"], when, after))
        return out
    raise ManifestError(f"'{field_name}' must be a string or a list of {{file, db}}")

def _parse_local(item: dict, field_name: str, path) -> bool:
    """A ddl/seed/upload entry's `local:` flag: absent is False, anything but a bool is refused."""
    local = item.get("local", False)
    if not isinstance(local, bool):
        raise ManifestError(
            f"{path}: '{field_name}' entry 'local' must be true or false, got {local!r}")
    return local


def _check_local_files(refs: list, example, path) -> frozenset:
    """The names declared `local: true`, each confirmed to be a file inside the case dir.

    Explicit both ways: a local name is read from the case dir only, a non-local one from
    source_dir only, and nothing falls back from one to the other. So a name declared local
    in one entry and not in another is refused rather than resolved by whichever entry wins."""
    local = {name for name, flag in refs if flag}
    if not local:
        return frozenset()
    if not example:
        raise ManifestError(
            f"{path}: 'local: true' needs 'example:' -- without it every file is already read "
            f"from the test dir: {sorted(local)}")
    both = sorted(local & {name for name, flag in refs if not flag})
    if both:
        raise ManifestError(
            f"{path}: {both} declared both 'local: true' and not -- a file is read from one "
            f"directory; mark every entry naming it the same way")
    case = path.parent.resolve()
    for name in sorted(local):
        if not isinstance(name, str) or Path(name).is_absolute() or ".." in Path(name).parts:
            raise ManifestError(
                f"{path}: local file {name!r} escapes the test dir (absolute or '..')")
        hit = WALKER_EXEMPT_DIR_PARTS & set(Path(name).parts)
        if hit:
            raise ManifestError(
                f"{path}: local file {name!r} lies under {sorted(hit)}, which the isolation "
                f"scan skips -- keep local files where the scan reads them")
        if not (case / name).is_file():
            raise ManifestError(
                f"{path}: local file {name!r} not found in the test dir ({case / name}); "
                f"'local: true' never falls back to the example dir")
    return frozenset(local)


def _external_source(file: str) -> bool:
    """A `server_files` source that is not a test-dir file: an absolute path, a `gs://` object, or
    one built from `${...}` tokens (rendered at run time, so an unset variable fails the run that
    needs it rather than every load of the manifest)."""
    return file.startswith("gs://") or "${" in file or Path(file).is_absolute()


_GS_URL = re.compile(r"gs://[^/\s]+/\S+\Z")


def _normalize_server_files(raw, path=None) -> list:
    # server_files: place a test-dir file onto the Striim server before deploy or after
    # RUNNING. Each entry is {file (test-relative), dest, when, load}. Normally dest is
    # a literal server path. `load: true` (pre_deploy entries only) is different: dest
    # is just the uploaded jar's name, not a path -- the file goes into Striim's
    # UploadedFiles/ via the same docker-vs-native-aware helper op:/udf: modules use
    # (not a literal path like `/opt/striim/...`, which only exists in the docker
    # cluster), then gets registered as a global UDF jar via client.load_jar() -- for a
    # prebuilt jar with no buildable op:/udf: module in-repo. That method UNLOADs first
    # (best-effort, a no-op if nothing's loaded yet) then LOADs, so it's safe on both a
    # fresh cluster and a re-run against an already-loaded namespace (a raw in-TQL LOAD
    # would conflict on the latter). Returns (file, dest, when, load) tuples.
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ManifestError("'server_files' must be a list of {file, dest, when, load} entries")
    out = []
    for item in raw:
        if not isinstance(item, dict) or not item.get("file") or not item.get("dest"):
            raise ManifestError(f"'server_files' item needs 'file' and 'dest': {item!r}")
        when = item.get("when", "pre_deploy")
        if when not in VALID_SEED_WHEN - {"post_recover"}:
            raise ManifestError(
                f"'server_files' when must be one of {sorted(VALID_SEED_WHEN - {'post_recover'})}: {when!r}")
        load = _normalize_load(item.get("load"), item)
        if load and (not isinstance(item["dest"], str) or "/" in item["dest"] or "\\" in item["dest"]
                     or Path(item["dest"]).is_absolute() or item["dest"] in (".", "..")):
            # A load: dest is the jar's NAME in UploadedFiles/, never a path: a path would not
            # name what LOAD registers, and the runner stages the jar under that name.
            raise ManifestError(
                f"{path}: 'server_files' load: dest must be a bare jar name (no '/' or '\\'), "
                f"got {item['dest']!r}")
        src = item["file"]
        if not isinstance(src, str):
            raise ManifestError(f"{path}: 'server_files' file must be a string: {item!r}")
        if _external_source(src):
            # A prebuilt jar (an older release of an OP, say) is a build product, not test data:
            # it may come from outside the test dir. Anything else the server reads stays beside
            # the test, so the case remains self-contained.
            if not load:
                raise ManifestError(
                    f"{path}: 'server_files' file {src!r} is outside the test dir (absolute, gs:// "
                    f"or ${{...}}); only a 'load:' entry (a jar) may come from outside it: {item!r}")
            if src.startswith("gs://") and "${" not in src and not _GS_URL.match(src):
                raise ManifestError(
                    f"{path}: 'server_files' file {src!r} must be gs://<bucket>/<object>: {item!r}")
        out.append((src, item["dest"], when, load))
    return out


# `load:` says HOW to register an uploaded jar, because the two registrations are different
# statements and picking the wrong one fails in a confusing way: a UDF jar goes through
# `LOAD '<path>'` while an OpenProcessor needs `LOAD OPEN PROCESSOR`, which is what registers
# its @PropertyTemplate so `USING Global.<Name>` resolves.
#   False (absent)        -- not a jar; `dest` is a literal server path
#   True / "udf"          -- client.load_jar()                       (back-compat: True == "udf")
#   "open_processor"/"op" -- client.load_open_processor_idempotent()
VALID_SERVER_FILE_LOADS = {"udf", "open_processor"}

def _normalize_load(raw, item):
    if raw is None or raw is False:
        return False
    if raw is True:
        return "udf"
    if isinstance(raw, str):
        v = raw.strip().lower()
        if v == "op":
            v = "open_processor"
        if v in VALID_SERVER_FILE_LOADS:
            return v
    raise ManifestError(
        f"'server_files' load must be true/false or one of "
        f"{sorted(VALID_SERVER_FILE_LOADS)} (\'op\' is accepted for \'open_processor\'): {item!r}")

def _normalize_generate(raw, source_dir, path) -> list:
    # generate: run a data GENERATOR at a lifecycle point and place its output onto the
    # Striim server -- the dynamic analog of `server_files:` (static test-dir files), so a
    # test ships a ~30-line workload.yaml instead of committed binary fixtures. Each entry
    # is {kind, workload (source_dir-relative), dest (server DIRECTORY), when}: the
    # generator writes into a local per-test tmp dir and every file it produces is placed
    # under `dest`. `when` defaults to post_start (CDC-style -- the reader must be RUNNING
    # before the data appears), unlike server_files' pre_deploy default.
    # `workload` is resolved to an absolute path here and must stay confined under
    # source_dir (the same ../-escape rule R2 enforces for `tql`). `kind` is looked up in
    # plugin.GENERATORS at EXECUTION time -- the loader only requires a non-empty string, so
    # a manifest naming a generator this checkout does not ship still parses, and fails
    # loudly (naming the known kinds) when it actually runs.
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ManifestError(
            f"{path}: 'generate' must be a list of {{kind, workload, dest, when}} entries")
    out = []
    for item in raw:
        if not isinstance(item, dict):
            raise ManifestError(f"{path}: 'generate' entries must be mappings: {item!r}")
        kind = item.get("kind")
        if not isinstance(kind, str) or not kind.strip():
            raise ManifestError(
                f"{path}: 'generate' entry needs a non-empty 'kind' (generator name): {item!r}")
        workload = item.get("workload")
        if not isinstance(workload, str) or not workload.strip():
            raise ManifestError(
                f"{path}: 'generate' entry needs a 'workload' file (relative to source_dir): "
                f"{item!r}")
        dest = item.get("dest")
        if not isinstance(dest, str) or not dest.strip():
            raise ManifestError(
                f"{path}: 'generate' entry needs a non-empty 'dest' (server directory): {item!r}")
        when = item.get("when", "post_start")
        if not isinstance(when, str) or when not in VALID_SEED_WHEN - {"post_recover"}:
            raise ManifestError(
                f"{path}: 'generate' when must be one of {sorted(VALID_SEED_WHEN - {'post_recover'})}: {when!r}")
        sdir = Path(source_dir).resolve()
        wpath = (Path(source_dir) / workload).resolve()
        if sdir not in wpath.parents:
            raise ManifestError(
                f"{path}: 'generate' workload escapes source_dir: {workload!r} "
                f"({wpath} is not under {sdir})")
        if not wpath.is_file():
            raise ManifestError(f"{path}: 'generate' workload file not found: {wpath}")
        out.append({"kind": kind, "workload": wpath, "dest": dest, "when": when})
    return out

def _normalize_uploads(raw, path, label: str = "op.upload") -> list:
    # <label>: each item is either a plain string (back-compat) or a {from, to}
    # mapping. Plain string -> uploaded as f"{TID}<basename>" (auto-isolated, empty
    # prefix when serial) exactly as before; the test's own TQL must reference that
    # literal ${TID}-prefixed name. {from, to} -> the author names the uploaded file
    # explicitly; `to` is token-rendered (e.g. "${NS}-<basename>"), and the runner
    # rewrites any "UploadedFiles/<from>" reference found in the TQL to
    # "UploadedFiles/<rendered to>" before deploy (plugin._rendered_tql) -- so a
    # shipped example's TQL can keep a clean, untokenized ConfigFile reference (no
    # test-only tokens baked into customer-facing examples) while each test still
    # uploads its own per-test-unique file, avoiding the cross-test name collisions a
    # handful of shared basenames (e.g. "products_lookup.json") would otherwise hit
    # under parallel runs. Returns [{"from": str, "to": str|None}], "to" is None for
    # the back-compat string form. A {from, to} entry may add `local: true` (read from the
    # case dir, not source_dir); it then carries "local": True.
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ManifestError(f"{path}: '{label}' must be a list of files or {{from, to}} entries")
    out = []
    for item in raw:
        if isinstance(item, str):
            out.append({"from": item, "to": None})
        elif isinstance(item, dict):
            frm, to = item.get("from"), item.get("to")
            if not isinstance(frm, str) or not frm:
                raise ManifestError(f"{path}: '{label}' entry needs a 'from' filename: {item!r}")
            if not isinstance(to, str) or not to:
                raise ManifestError(f"{path}: '{label}' entry needs a 'to' filename: {item!r}")
            # `local: true` is carried only when set, so the common shape stays {from, to}.
            out.append({"from": frm, "to": to, **({"local": True}
                                                  if _parse_local(item, label, path) else {})})
        else:
            raise ManifestError(
                f"{path}: '{label}' entries must be a filename string or {{from, to}}: {item!r}")
    return out

_MODULE_FAMILY = {
    "op": "java/OpenProcessors/",
    "udf": "java/UserDefinedFunctions/",
}
_DEFAULT_TOKEN = {"op": "OP", "udf": "UDF"}
_MODULE_ENTRY_KEYS = {"jar", "token", "upload", "on_agent"}

def _normalize_module_entry(mod: dict, key: str, i: int | None, path) -> dict:
    """Validate one `op:`/`udf:` list entry (or the single-mapping form, `i=None`) and
    return its normalized form: {jar, token, kind, upload}. `key` ("op" or "udf") is
    which top-level block this entry came from -- it fixes the module's `kind` (which
    load mechanism applies: LOAD OPEN PROCESSOR for "op", client.load_jar for "udf"),
    the default token, and the jar-family guard (N2: a `udf:` entry whose jar isn't
    under java/UserDefinedFunctions/ is almost certainly an un-migrated fixture that
    kept the old `op:` spelling -- reject it loudly rather than silently mis-driving it).
    """
    label = f"{key}[{i}]" if i is not None else key
    if not isinstance(mod, dict):
        raise ManifestError(f"{path}: '{label}' must be a mapping, got {mod!r}")
    unknown = set(mod) - _MODULE_ENTRY_KEYS
    if unknown:
        raise ManifestError(
            f"{path}: '{label}' has unknown key(s) {sorted(unknown)} -- allowed: "
            f"{sorted(_MODULE_ENTRY_KEYS)}")
    jar = mod.get("jar")
    if not isinstance(jar, str) or not jar.strip():
        raise ManifestError(
            f"{path}: '{label}.jar' (repo-relative MODULE reference — its dir, or "
            "pom.xml) is required")
    family = _MODULE_FAMILY[key]
    if family not in jar:
        other_key = "udf" if key == "op" else "op"
        raise ManifestError(
            f"{path}: '{label}.jar' ({jar!r}) doesn't look like a {family} module — "
            f"did you mean '{other_key}:'?")
    token = mod.get("token", _DEFAULT_TOKEN[key])
    if not isinstance(token, str) or not _TOKEN_RE.match(token):
        raise ManifestError(
            f"{path}: '{label}.token' must match ^[A-Z][A-Z0-9_]*$ (an uppercase "
            f"identifier used as ${{<token>_JAR}}/${{<token>_NAME}}), got {token!r}")
    upload = _normalize_uploads(mod.get("upload"), path, f"{label}.upload")
    on_agent = mod.get("on_agent", False)
    if not isinstance(on_agent, bool):
        raise ManifestError(
            f"{path}: '{label}.on_agent' must be true or false, got {on_agent!r}")
    if on_agent and key != "op":
        # A UDF jar reaches an agent through the same LOAD path a server uses, so there is
        # nothing to place. Rejecting rather than ignoring: a silently-ignored flag reads as
        # coverage the run does not have.
        raise ManifestError(
            f"{path}: '{label}.on_agent' applies to 'op:' modules only — a udf: jar needs no "
            "agent-side placement")
    return {"jar": jar, "token": token, "kind": key, "upload": upload, "on_agent": on_agent}

def _parse_concurrent(action: dict, i: int, path) -> list:
    """An action's optional `concurrent:` SQL loops, run on background threads."""
    concurrent_raw = action.get("concurrent", [])
    concurrent_ops = []
    if concurrent_raw:
        if not isinstance(concurrent_raw, list):
            raise ManifestError(
                f"{path}: action[{i}] 'concurrent' must be a list, got {concurrent_raw!r}")
        for j, op in enumerate(concurrent_raw):
            if not isinstance(op, dict):
                raise ManifestError(
                    f"{path}: action[{i}] concurrent[{j}] must be a dict, got {op!r}")
            op_db = op.get("db")
            if not op_db:
                raise ManifestError(
                    f"{path}: action[{i}] concurrent[{j}] must have 'db' key")
            op_file = op.get("file")
            if not op_file:
                raise ManifestError(
                    f"{path}: action[{i}] concurrent[{j}] must have 'file' key")
            loop_interval = _parse_zeroable_duration(
                op.get("loop_interval", 1), "loop_interval", path)
            concurrent_ops.append({
                "db": op_db,
                "file": op_file,
                "loop_interval": loop_interval,
            })
    return concurrent_ops


def _normalize_recover(raw, path) -> dict | None:
    """Normalize the `recover:` block into {mode, after, settle, expect_running} or None.

    Absent means no recovery phase, which is every existing test -- the phase is opt-in and
    adds nothing to a manifest that does not ask for it.
    """
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ManifestError(
            f"{path}: 'recover' must be a mapping with a 'mode' key, got {raw!r}")
    unknown = sorted(set(raw) - _RECOVER_KEYS)
    if unknown:
        raise ManifestError(
            f"{path}: 'recover' has unknown key(s) {unknown} -- allowed: "
            f"{sorted(_RECOVER_KEYS)}")
    mode = raw.get("mode")
    if mode not in VALID_RECOVER_MODES:
        raise ManifestError(
            f"{path}: 'recover.mode' must be one of {sorted(VALID_RECOVER_MODES)}, "
            f"got {mode!r}")
    # `after` is measured from the moment the LAST post_start seed/generate/server_file step
    # finished, not from RUNNING: the whole point is to interrupt the app WHILE it is writing,
    # and on a CDC pipeline nothing is being written until the seed lands. Defaults to 0 --
    # interrupt as soon as the data is in, which is the widest in-flight window.
    after = _parse_zeroable_duration(raw.get("after", 0), "recover.after", path)
    # `settle` is the pause between the app returning to RUNNING and the assertions running.
    # Recovery replays asynchronously, so asserting immediately measures a half-recovered
    # target and reports loss that is merely lag. Defaults to 20s, matching SLT_CLUSTER_SETTLE's
    # default for the same reason.
    settle = _parse_zeroable_duration(raw.get("settle", 20), "recover.settle", path)
    # `times` -- interrupt/restore this many times before asserting. Default 1.
    # NOT a knob for flakiness: the recovery regression report this exists to reproduce says plainly that
    # "the first stop usually is okay" and the divergence appears on the second or third, so a
    # single interruption can measure zero and prove nothing.
    times = raw.get("times", 1)
    if isinstance(times, bool) or not isinstance(times, int) or times < 1:
        raise ManifestError(
            f"{path}: 'recover.times' must be an integer >= 1, got {times!r}")
    # Delay before each interruption AFTER the first; the first uses `after`. Defaults to
    # `after` so a repeat cycle is evenly spaced unless told otherwise.
    if "every" in raw:
        every = _parse_zeroable_duration(raw["every"], "recover.every", path)
        if times == 1:
            raise ManifestError(
                f"{path}: 'recover.every' requires 'recover.times' > 1 -- with a single "
                f"interruption there is no interval for it to describe")
    else:
        every = after

    expect_running = raw.get("expect_running", True)
    if not isinstance(expect_running, bool):
        raise ManifestError(
            f"{path}: 'recover.expect_running' must be true/false, got {expect_running!r}")
    # `restore()` returns before the settle loop when expect_running is false, so a settle
    # given alongside it is accepted and discarded. Rejected rather than ignored, for the
    # reason this loader rejects expect_halt_contains without expect_halt: a silent no-op is
    # how assertions rot.
    if "settle" in raw and not expect_running:
        raise ManifestError(
            f"{path}: 'recover.settle' requires 'recover.expect_running: true' -- with the app "
            f"left stopped there is no replay to settle for, so the value would be a no-op")
    # Cycles after the first would interrupt an app the previous cycle deliberately left
    # stopped. `await_left_running` reads the status FIRST and returns for anything that is not
    # RUNNING or transitional, so those cycles report a verified interruption having done
    # nothing -- one real interruption out of `times`, silently. Rejected rather than clamped,
    # because a manifest asking for five interruptions and getting one is the kind of quiet
    # downgrade this loader exists to prevent.
    if times > 1 and not expect_running:
        raise ManifestError(
            f"{path}: 'recover.times: {times}' requires 'recover.expect_running: true' -- the "
            f"app is left stopped after cycle 1, so later cycles would 'interrupt' an already "
            f"stopped app and report success without interrupting anything")
    return {"mode": mode, "after": after, "settle": settle,
            "expect_running": expect_running, "times": times, "every": every}


def _extract_persistent_streams(tql_content: str) -> list[str]:
    """Extract persistent stream names from TQL via CREATE STREAM ... PERSIST USING pattern.

    Persistent streams create two Kafka topics per stream:
      - ${NS}_<streamName> (data topic)
      - ${NS}_<streamName>_CHECKPOINT (checkpoint topic)
    These are auto-registered for cleanup via kafka_cleanup_topics.

    Returns a list of stream names (case-sensitive as declared in TQL).
    """
    # Pattern: CREATE [OR REPLACE] STREAM <stream_name> [OF <type>] ... PERSIST USING <propertySet>
    # Matches case-insensitively but captures the stream name exactly as declared
    pattern = r'CREATE\s+(?:OR\s+REPLACE\s+)?STREAM\s+(\w+)\s+.*?\s+PERSIST\s+USING'
    matches = re.findall(pattern, tql_content, re.IGNORECASE | re.DOTALL)
    return matches


def _service_tokens() -> set:
    from livetest import registry
    names = set()
    for svc in registry.all_services():
        names.update(registry.load_service(svc).provides)
    return names

def _reserved_tokens(modules: list) -> set:
    """Every token name the harness sets itself: fixed names, module and service tokens."""
    reserved = set(_HARNESS_TOKENS)
    for mod in modules:
        reserved.update({f"{mod['token']}_JAR", f"{mod['token']}_NAME"})
    return reserved | _service_tokens()


def _normalize_capture(item, label: str, path, standalone: bool = False) -> dict:
    """One capture: {token, source (describe|mon), component, field, timeout (s or None)}, plus
    `delay_before` (s) on a standalone step; a nested capture runs at its action's STOP."""
    if not isinstance(item, dict):
        raise ManifestError(f"{path}: {label} must be a mapping, got {item!r}")
    allowed = _CAPTURE_KEYS | ({"type", "delay_before"} if standalone else set())
    unknown = sorted(set(item) - allowed)
    if unknown:
        raise ManifestError(
            f"{path}: {label} has unknown key(s) {unknown} -- allowed: {sorted(allowed - {'type'})}")
    token = item.get("token")
    if not isinstance(token, str) or not _TOKEN_NAME.match(token):
        raise ManifestError(f"{path}: {label} 'token' must match [A-Z][A-Z0-9_]*, got {token!r}")
    sources = [k for k in ("describe", "mon") if k in item]
    if len(sources) != 1:
        raise ManifestError(
            f"{path}: {label} needs exactly one of 'describe:' or 'mon:' (the component to read)")
    component = item[sources[0]]
    if not isinstance(component, str) or not component.strip():
        raise ManifestError(f"{path}: {label} '{sources[0]}' must be a non-empty component name")
    fld = item.get("field")
    if not isinstance(fld, str) or not fld.strip():
        raise ManifestError(
            f"{path}: {label} needs a non-empty 'field' (e.g. 'Source Restart Position')")
    timeout = None
    if "timeout" in item:
        timeout = _parse_zeroable_duration(item["timeout"], f"{label} timeout", path)
        if timeout <= 0:
            raise ManifestError(f"{path}: {label} 'timeout' must be greater than 0")
    out = {"token": token, "source": sources[0], "component": component.strip(),
           "field": fld, "timeout": timeout}
    if "expect" in item:
        out["expect"] = _normalize_capture_expect(item["expect"], label, path)
    if standalone:
        out["delay_before"] = _parse_zeroable_duration(
            item.get("delay_before", 0), f"{label} delay_before", path)
    return out


def _normalize_capture_expect(raw, label: str, path):
    """`expect:` on a capture: a literal (string or number, compared as text, token-rendered) or
    a matcher, {present: true} or {matches: <regex>}. {absent: true} is refused: a capture
    exists to bind a value, so an absent one can never satisfy it (assert.monitor checks that)."""
    if isinstance(raw, dict):
        if set(raw) == {"absent"}:
            raise ManifestError(
                f"{path}: {label} 'expect: {{absent: true}}' can never hold -- a capture waits for "
                f"a value; assert a missing figure with assert.monitor's {{absent: true}}")
        from livetest.assertions.monitor import MonitorSpecError, _check_matcher, _is_matcher
        if not _is_matcher(raw):
            raise ManifestError(
                f"{path}: {label} 'expect' must be a literal or one of {{present: true}}, "
                f"{{matches: <regex>}}, got {raw!r}")
        try:
            _check_matcher("expect", raw)
        except MonitorSpecError as e:
            raise ManifestError(f"{path}: {label} {e}") from None
        return dict(raw)
    if isinstance(raw, bool) or not isinstance(raw, (str, int, float)) or raw == "":
        raise ManifestError(
            f"{path}: {label} 'expect' must be a non-empty string or number, or a matcher, got {raw!r}")
    return str(raw)


def _normalize_action_seeds(raw, label: str, path, refs_out: list | None = None) -> list:
    """An action's `seed:` -- [(db, file, after)], `after` (seconds, default 0) measured from the
    moment the action's app is RUNNING again, like a top-level seed's `after` from RUNNING. `when`
    is refused: an action seed has one lifecycle point."""
    if raw is None:
        return []
    items = raw if isinstance(raw, list) else [raw]
    stripped, afters = [], []
    for item in items:
        if isinstance(item, dict):
            if "when" in item:
                raise ManifestError(
                    f"{path}: {label} entry has 'when'; an action seed runs once the action's app "
                    f"is RUNNING again -- use 'after' to delay it: {item!r}")
            afters.append(_parse_after(item["after"], label, path) if "after" in item else 0.0)
            stripped.append({k: v for k, v in item.items() if k != "after"})
        else:
            afters.append(0.0)
            stripped.append(item)
    specs = _normalize_file_specs(stripped, label, path, allow_when=False, refs_out=refs_out)
    return [(db, f, a) for (db, f), a in zip(specs, afters)]


_RECAPTURE_KEYS = {"token", "describe", "mon", "field"}


def _normalize_recapture(item, label: str, path) -> dict:
    """An assert.monitor `recapture:` entry: one read per poll, so `timeout` and `expect` (which
    belong to a polling capture) are refused rather than silently ignored."""
    if isinstance(item, dict):
        unknown = sorted(set(item) - _RECAPTURE_KEYS)
        if unknown:
            raise ManifestError(
                f"{path}: {label} has unknown key(s) {unknown} -- allowed: {sorted(_RECAPTURE_KEYS)} "
                f"(a recapture is read once per poll; the monitor spec's metrics are its check)")
    return _normalize_capture(item, label, path)


def _normalize_captures(raw, label: str, path) -> list:
    if raw is None:
        return []
    if not isinstance(raw, list) or not raw:
        raise ManifestError(f"{path}: {label} must be a non-empty list of captures")
    return [_normalize_capture(c, f"{label}[{j}]", path) for j, c in enumerate(raw)]


def _normalize_overrides(raw, label: str, path, declared: dict) -> dict:
    """drop_recreate_app `tokens:` -- values used only to render the re-created app. A name must
    be one `tokens:` declares (the first deploy renders the same TQL and needs a default); a
    value may carry ${...}, rendered when the action runs (a captured token, typically)."""
    if raw is None:
        return {}
    if not isinstance(raw, dict) or not raw:
        raise ManifestError(f"{path}: {label} must be a non-empty mapping of NAME: value")
    out = {}
    for name, value in raw.items():
        if name not in declared:
            raise ManifestError(
                f"{path}: {label} overrides {name!r}, which 'tokens:' does not declare -- the "
                f"first deploy renders the same TQL, so the name needs a default there")
        if isinstance(value, bool) or not isinstance(value, (str, int, float)):
            raise ManifestError(
                f"{path}: {label} value for {name!r} must be a string or number, got {value!r}")
        out[name] = str(value)
    return out


def _normalize_alter_file(raw, label: str, path, source_dir) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise ManifestError(
            f"{path}: {label} needs a 'file' (a TQL fragment, relative to the test dir)")
    sdir = Path(source_dir).resolve()
    fpath = (Path(source_dir) / raw).resolve()
    if sdir not in fpath.parents:
        raise ManifestError(f"{path}: {label} file escapes the test dir: {raw!r}")
    if not fpath.is_file():
        raise ManifestError(f"{path}: {label} file not found: {fpath}")
    text = re.sub(r"--[^\n]*", "", fpath.read_text())
    if not text.strip():
        raise ManifestError(f"{path}: {label} file {raw!r} is empty")
    hit = _ALTER_FRAGMENT_REFUSED.search(text)
    if hit:
        raise ManifestError(
            f"{path}: {label} file {raw!r} contains {hit.group(0).strip()!r}; the action writes "
            f"USE, UNDEPLOY, ALTER APPLICATION, RECOMPILE, DEPLOY and START itself -- the file "
            f"holds only the CREATE OR REPLACE statements that go between them")
    return raw


def _check_capture_tokens(action_specs: list, modules: list, tokens: dict, path) -> None:
    """A captured name is new: not one the harness or `tokens:` sets (so nothing rendered before
    the capture could have meant a different value), and captured once."""
    reserved, seen = None, set()
    for spec in action_specs:
        caps = [spec] if spec["type"] == "capture" else spec.get("capture", [])
        for cap in caps:
            name = cap["token"]
            if reserved is None:
                reserved = _reserved_tokens(modules)
            if name in reserved or name in tokens:
                raise ManifestError(
                    f"{path}: capture token {name!r} collides with a "
                    f"{'harness-provided' if name in reserved else 'tokens:'} token")
            if name in seen:
                raise ManifestError(f"{path}: capture token {name!r} is captured twice")
            seen.add(name)


def _normalize_tokens(raw, modules: list, path) -> dict:
    """`tokens:` -- a flat ${NAME} -> value map rendered wherever the harness renders tokens.
    Values are strings (numbers cast); a value may not itself contain ${...}, and a name the
    harness sets (NS, TID, service and module tokens, ...) is refused rather than resolved by
    merge order."""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ManifestError(f"{path}: 'tokens' must be a mapping of NAME: value, got {raw!r}")
    reserved = _reserved_tokens(modules)
    out = {}
    for name, value in raw.items():
        if not isinstance(name, str) or not _TOKEN_NAME.match(name):
            raise ManifestError(
                f"{path}: 'tokens' name {name!r} must match [A-Z][A-Z0-9_]*")
        if name in reserved:
            raise ManifestError(
                f"{path}: 'tokens' name {name!r} collides with a harness-provided token")
        if isinstance(value, bool) or not isinstance(value, (str, int, float)):
            raise ManifestError(
                f"{path}: 'tokens' value for {name!r} must be a string or number, got {value!r}")
        value = str(value)
        if "${" in value:
            raise ManifestError(
                f"{path}: 'tokens' value for {name!r} may not contain ${{...}}: {value!r}")
        out[name] = value
    return out

def _normalize_modules(raw, key: str, path) -> list:
    """Validate the `op:`/`udf:` top-level value (`raw`) and return its normalized
    module list: [{jar, token, kind, upload}, ...]. `raw` may be absent (`None` ->
    `[]`), a single mapping (-> a one-entry list), or a list of mappings. `key` ("op"
    or "udf") is which block this is -- see _normalize_module_entry for what it fixes.
    """
    if raw is None:
        return []
    if isinstance(raw, dict):
        return [_normalize_module_entry(raw, key, None, path)]
    if isinstance(raw, list):
        if not raw:
            raise ManifestError(f"{path}: '{key}' list must be non-empty")
        return [_normalize_module_entry(mod, key, i, path) for i, mod in enumerate(raw)]
    raise ManifestError(f"{path}: '{key}' must be a mapping or a list of mappings, got {raw!r}")

@dataclass
class TestManifest:
    name: str
    tql: str
    dir: Path
    depth: str = "regression"
    topology: str = "single"
    requires: list[str] = field(default_factory=list)
    tokens: dict = field(default_factory=dict)  # manifest-declared ${NAME} -> str, merged into
                                         # the harness token map (never shadows a harness token)
    kafka_cleanup_topics: list = field(default_factory=list)  # tokenized topic names the
                                         # APP creates (e.g. a persisted stream's derived
                                         # <ns>_<streamName> data + _CHECKPOINT topics);
                                         # appended to the kafka teardown delete list
    ddl: str | None = None
    seed: str | None = None
    ddl_files: list = field(default_factory=list)   # [(db, file), ...]
    seed_files: list = field(default_factory=list)  # [(db, file, when, after), ...]
    action_specs: list = field(default_factory=list)  # [{type, cycles, delay_before_stop, stop_duration}, ...]
    assert_: dict = field(default_factory=dict)
    timeout: int = 120
    # §85.3. Seconds between diff polls. The default 2.0 is right for a convergence check and
    # WRONG for a measurement: `elapsed_s` can only be as precise as this, so two writers finishing
    # seconds apart are indistinguishable unless a case lowers it (§127.2). Costs one SELECT per
    # table per poll, which is why it is opt-in rather than lowered for everyone.
    diff_poll: float = 2.0
    tags: list[str] = field(default_factory=list)
    disabled: object = None             # truthy => skip (a non-empty string is the reason / ticket ref)
    disabled_parallel: object = None    # truthy => skip, but ONLY under _parallel() (SLT_PARALLEL/xdist); no-op serially
    xfail: dict = field(default_factory=dict)  # {reason: str, strict: bool, tiers: [...]} => expected to fail ON THOSE ASSERTION TIERS ONLY (never a setup failure)
    recover: dict | None = None         # {mode, after, settle, expect_running} => interrupt the app after seeding, restore it, THEN assert
    expect_halt: bool = False           # True => the app is EXPECTED to reach a terminal HALT after seeding (raise-path tests)
    expect_halt_contains: tuple = ()    # normalized tuple of substrings the halt reason must ALL contain; () => any reason (today's behavior). Requires expect_halt: true
    example: str | None = None          # repo-relative OP example dir (Option C pointer)
    purpose: str | None = None          # one-line scenario description (R3); non-empty when present
    modules: list = field(default_factory=list)  # normalized: [{jar, token, kind, upload, on_agent}, ...]
                                         # kind is "op" (LOAD OPEN PROCESSOR) or "udf" (client.load_jar);
                                         # op: entries first, then udf: entries, each in authored order
    op_uploads: list = field(default_factory=list)  # normalized: [{from, to}, ...] (to=None -> back-compat)
                                         # (every module entry's own `upload:`, concatenated in order)
    server_files: list = field(default_factory=list)  # [(file, dest, when, load), ...] placed on the server
    generate_specs: list = field(default_factory=list)  # [{kind, workload (abs Path), dest, when}, ...]
    persistent_stream_names: list = field(default_factory=list)  # stream names detected from TQL
                                         # (auto-detected via regex, no manual config needed)
    source_dir: Path = None             # where tql/ddl/seed/upload are read from
    local_files: frozenset = frozenset()  # ddl/seed/upload names marked `local: true`: read from `dir`
    lifecycle: object = None            # livetest.lifecycle.LifecycleSpec, or None (legacy)
    exact: object = None                # livetest.canon.check_manifest resolution, or None

    def file_path(self, name) -> Path:
        """Where a ddl/seed/upload file is read from: the case dir for a `local: true` entry,
        else source_dir. Every reader of those files goes through here."""
        return Path(self.dir if name in self.local_files else self.source_dir) / name

# The project root (SLT_PROJECT_ROOT; this clone when unset). example:/op.jar resolve here.
def _root(example: str | None = None) -> Path:
    """The project root (or an ``example:`` dir in it), resolved at call time: the active
    project's consumer root, else SLT_PROJECT_ROOT, else this clone (livetest.project.example_root).
    Never fixed at import, so a manifest loaded after activation reads the consumer's files."""
    from livetest import project as _project
    return _project.example_root(example)

def xfail_for_release(xfail: dict, version: str) -> dict:
    """The xfail that applies on Striim ``version``: ``xfail`` itself, or {} when its ``releases``
    names other releases only."""
    if xfail.get("releases") and not release_matches(version, xfail["releases"]):
        return {}
    return xfail

def load_manifest(path: Path) -> TestManifest:
    path = Path(path)
    try:
        raw = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as e:
        raise ManifestError(f"{path}: invalid YAML: {e}") from e
    if not isinstance(raw, dict):
        raise ManifestError(f"{path}: top level must be a mapping")

    name = raw.get("name")
    if not name or not isinstance(name, str):
        raise ManifestError(f"{path}: 'name' is required and must be a string")
    tql = raw.get("tql")
    if not tql or not isinstance(tql, str):
        raise ManifestError(f"{path}: 'tql' is required and must be a string")
    topology = raw.get("topology", "single")
    if not isinstance(topology, str) or topology not in VALID_TOPOLOGIES:
        raise ManifestError(
            f"{path}: 'topology' must be one of {sorted(VALID_TOPOLOGIES)}, got {topology!r}")
    # L1 (docs/WRITING-TESTS.md): validated HERE only when present -- type + enum, same
    # as `topology`'s optional-with-default shape above. Deliberately NOT required at this
    # seam: hundreds of hermetic fixture tests (test_manifest.py, test_recover.py, ...) build
    # minimal ad-hoc YAML that predates `depth:` and isn't testing it. "every REAL manifest
    # under scripts/live/regression carries one" is enforced by
    # the consuming repo's depth checks instead, which
    # scan the real tree rather than every synthetic fixture load_manifest ever sees.
    depth = raw.get("depth", "regression")
    if not isinstance(depth, str) or depth not in VALID_DEPTHS:
        raise ManifestError(
            f"{path}: 'depth' must be one of {sorted(VALID_DEPTHS)}, got {depth!r}")
    unknown = sorted(set(raw) - VALID_MANIFEST_KEYS)
    if unknown:
        raise ManifestError(
            f"{path}: unknown manifest key(s) {unknown} -- allowed: "
            f"{sorted(VALID_MANIFEST_KEYS)}")

    purpose = raw.get("purpose")
    if purpose is not None and (
        not isinstance(purpose, str)
        or not purpose.strip()
        or "\n" in purpose
        or "\r" in purpose
    ):
        raise ManifestError(
            f"{path}: 'purpose' must be a non-empty, single-line string when present")

    expect_halt = raw.get("expect_halt", False)
    if not isinstance(expect_halt, bool):
        raise ManifestError(f"{path}: 'expect_halt' must be true/false, got {expect_halt!r}")

    # `expect_halt_contains` narrows `expect_halt: true` from "any terminal status for any
    # reason" to "a halt whose reason text contains these substrings". Authored as a string
    # or a list of strings; normalized here to a tuple of verbatim strings, with () meaning
    # "not set" (accept any reason -- the pre-existing behavior).
    expect_halt_contains_raw = raw.get("expect_halt_contains")
    if expect_halt_contains_raw is None:
        expect_halt_contains = ()
    elif isinstance(expect_halt_contains_raw, str):
        if not expect_halt_contains_raw.strip():
            raise ManifestError(
                f"{path}: 'expect_halt_contains' string must be non-empty "
                f"(a substring the halt reason must contain)")
        expect_halt_contains = (expect_halt_contains_raw,)
    elif isinstance(expect_halt_contains_raw, list):
        if not expect_halt_contains_raw:
            raise ManifestError(f"{path}: 'expect_halt_contains' list must be non-empty")
        for i, s in enumerate(expect_halt_contains_raw):
            if not isinstance(s, str) or not s.strip():
                raise ManifestError(
                    f"{path}: 'expect_halt_contains[{i}]' must be a non-empty string, got {s!r}")
        expect_halt_contains = tuple(expect_halt_contains_raw)
    else:
        raise ManifestError(
            f"{path}: 'expect_halt_contains' must be a string or a list of strings, "
            f"got {expect_halt_contains_raw!r}")
    # A silent no-op is how assertions rot: reject the key when nothing will ever check it.
    if expect_halt_contains and not expect_halt:
        raise ManifestError(
            f"{path}: 'expect_halt_contains' requires 'expect_halt: true' -- it is only "
            f"checked on the halt path, so without it the key would be a silent no-op")

    recover = _normalize_recover(raw.get("recover"), path)
    # `when: post_recover` seeds run after the (last) recover: restore reaches RUNNING, before
    # the settle -- e.g. a parent that arrives only after the restart.
    if not recover and any(w == "post_recover" for _db, _f, w, _a in
                           _normalize_file_specs(raw.get("seed"), "seed", path, allow_when=True)):
        raise ManifestError(f"{path}: a 'seed' entry has 'when: post_recover' but there is no 'recover:'")
    if recover and not recover["expect_running"] and any(
        w == "post_recover" for _db, _f, w, _a in
        _normalize_file_specs(raw.get("seed"), "seed", path, allow_when=True)
    ):
        raise ManifestError(
            f"{path}: a 'seed' entry has 'when: post_recover' but "
            f"'recover.expect_running' is false")
    # Mutually exclusive, and rejected rather than ordered: `expect_halt` asserts the app
    # reaches a TERMINAL status and stays there, while `recover` asserts it comes back to
    # RUNNING. There is no consistent reading of a manifest asking for both, and picking one
    # silently would make the other key a no-op.
    if recover and expect_halt:
        raise ManifestError(
            f"{path}: 'recover' and 'expect_halt' are mutually exclusive -- one expects the "
            f"app to return to RUNNING, the other expects it to stay terminal")

    assert_ = raw.get("assert")
    # `expect_halt: true` is itself an assertion (the app must reach a terminal HALT), so a
    # halt-expecting test satisfies the "declare at least one assertion" rule on its own.
    if not assert_ and not expect_halt:
        raise ManifestError(
            f"{path}: 'assert' must declare at least one assertion (e.g. smoke: true)")
    # T4-MON. Validated at load so a clock is refused before any stack boots for it.
    if assert_ and assert_.get("monitor") is not None:
        from livetest.assertions.monitor import MonitorSpecError, parse_monitor_specs
        try:
            parse_monitor_specs(assert_["monitor"])
        except MonitorSpecError as e:
            raise ManifestError(f"{path}: {e}") from None
        _monitor_recaptures = []
        for n, spec in enumerate(assert_["monitor"]):
            for j, c in enumerate(spec.get("recapture") or []):
                cap = _normalize_recapture(c, f"assert.monitor[{n}] recapture[{j}]", path)
                # Reading the asserted MON figure back into its own expected value would pass
                # whatever the figure shows.
                if (cap["source"] == "mon" and cap["field"] in spec["metrics"]
                        and cap["component"] == (spec.get("component") or spec.get("target"))):
                    raise ManifestError(
                        f"{path}: assert.monitor[{n}] recapture[{j}] reads MON {cap['component']} "
                        f"{cap['field']!r}, the figure this spec asserts -- it would compare the "
                        f"figure with itself")
                _monitor_recaptures.append(cap)
    else:
        _monitor_recaptures = []
    if assert_ and assert_.get("checkpoint_history") is not None:
        from livetest.assertions.checkpoint_history import (
            CheckpointHistorySpecError, parse_checkpoint_history_spec,
        )
        try:
            parse_checkpoint_history_spec(assert_["checkpoint_history"])
        except CheckpointHistorySpecError as e:
            raise ManifestError(f"{path}: {e}") from None
    if assert_ and assert_.get("jmx") is not None:
        from livetest.assertions.jmx import JmxSpecError, parse_jmx_specs
        try:
            parse_jmx_specs(assert_["jmx"])
        except JmxSpecError as e:
            raise ManifestError(f"{path}: {e}") from None
    # A recover phase whose result nothing inspects proves nothing: the app was interrupted
    # and restarted, and no claim was made about what survived. `smoke: true` alone does not
    # count -- it only re-asserts RUNNING, which `recover` already waits for.
    if recover and not (set(assert_ or {}) - {"smoke"}):
        raise ManifestError(
            f"{path}: 'recover' requires an assertion beyond 'smoke' -- the point of the "
            f"phase is what survived the interruption, and smoke only re-checks RUNNING")

    # Option C: `example:` points at the shipped OP example dir (repo-relative). When
    # set, tql/ddl/seed/upload files are read from THERE (the certified shipped files);
    # only the golden expected/*.csv stays test-local (path.parent). Absent -> the
    # self-contained regression style, everything relative to the test.yaml dir.
    example = raw.get("example")
    if example is not None and not isinstance(example, str):
        raise ManifestError(f"{path}: 'example' must be a repo-relative directory string")
    # `op:` and `udf:` are sibling top-level keys, each a single mapping or a list; the
    # key itself picks the load mechanism (LOAD OPEN PROCESSOR for op:, client.load_jar
    # for udf:) -- no `load:` flag anywhere. Neither is required (a test may drive no
    # module at all).
    op_entries = _normalize_modules(raw.get("op"), "op", path)
    udf_entries = _normalize_modules(raw.get("udf"), "udf", path)
    modules = op_entries + udf_entries
    seen_tokens: dict = {}
    for mod in modules:
        prior = seen_tokens.get(mod["token"])
        if prior is not None:
            raise ManifestError(
                f"{path}: duplicate token {mod['token']!r} -- '{prior['kind']}' entry "
                f"{prior['jar']!r} and '{mod['kind']}' entry {mod['jar']!r} both use it; "
                "tokens must be unique across op:/udf:")
        seen_tokens[mod["token"]] = mod
    op_uploads = [u for mod in modules for u in mod["upload"]]
    tokens = _normalize_tokens(raw.get("tokens"), modules, path)

    requires = raw.get("requires", [])
    if not isinstance(requires, list):
        raise ManifestError(f"{path}: 'requires' must be a list of service names, got {requires!r}")
    tags = raw.get("tags", [])
    if not isinstance(tags, list):
        raise ManifestError(f"{path}: 'tags' must be a list of tag strings, got {tags!r}")

    # kafka_cleanup_topics: extra Kafka topics the test's APP creates that the framework
    # didn't derive itself (e.g. a persisted stream's <ns>_<streamName> data topic + its
    # _CHECKPOINT companion). Each entry is ${...}-token-rendered at registration and
    # appended to the per-test kafka teardown delete list (plugin.py, spec §A.4) — the
    # same best-effort delete that already removes the derived src/tgt topics. Optional;
    # absent/empty is the universal no-op.
    kafka_cleanup_topics = raw.get("kafka_cleanup_topics") or []
    if not isinstance(kafka_cleanup_topics, list) or not all(
            isinstance(t, str) and t.strip() for t in kafka_cleanup_topics):
        raise ManifestError(
            f"{path}: 'kafka_cleanup_topics' must be a list of non-empty topic-name strings")
    if kafka_cleanup_topics and "kafka" not in requires:
        raise ManifestError(
            f"{path}: 'kafka_cleanup_topics' needs service 'kafka' in 'requires' — the "
            "topics are deleted through the kafka admin at teardown")

    disabled = raw.get("disabled")
    if disabled is not None and not isinstance(disabled, (str, bool)):
        raise ManifestError(f"{path}: 'disabled' must be a string reason or a boolean, got {disabled!r}")
    if isinstance(disabled, str) and not disabled.strip():
        raise ManifestError(f"{path}: 'disabled' string must be non-empty (give a reason / ticket ref)")

    disabled_parallel = raw.get("disabled_parallel")
    if disabled_parallel is not None and not isinstance(disabled_parallel, (str, bool)):
        raise ManifestError(
            f"{path}: 'disabled_parallel' must be a string reason or a boolean, got {disabled_parallel!r}")
    if isinstance(disabled_parallel, str) and not disabled_parallel.strip():
        raise ManifestError(f"{path}: 'disabled_parallel' string must be non-empty (give a reason / ticket ref)")

    xfail_raw = raw.get("xfail")
    xfail = {}
    if xfail_raw is not None:
        if isinstance(xfail_raw, str):
            if not xfail_raw.strip():
                raise ManifestError(f"{path}: 'xfail' string must be non-empty (give a reason)")
            xfail = {"reason": xfail_raw}
        elif isinstance(xfail_raw, dict):
            xfail = dict(xfail_raw)
            if "reason" not in xfail:
                raise ManifestError(f"{path}: 'xfail' dict must have a 'reason' key")
            if not isinstance(xfail["reason"], str) or not xfail["reason"].strip():
                raise ManifestError(f"{path}: xfail 'reason' must be a non-empty string")
            if "strict" in xfail:
                if not isinstance(xfail["strict"], bool):
                    raise ManifestError(f"{path}: xfail 'strict' must be true/false, got {xfail['strict']!r}")
            else:
                xfail["strict"] = False
            # `tiers`: which ASSERTION tiers the expected failure may come from. Narrows the
            # marker the way expect_halt_contains narrows expect_halt: without it, an xfail said
            # green for a TQL typo, a missing jar or a DDL error (design §10.1). `smoke` is never
            # allowed -- an app that did not reach RUNNING has not failed on its defect -- and
            # anything that is not an assertion failure at all (deploy, provisioning) never was.
            # (Named `tiers`, not `on`: YAML 1.1 reads a bare `on` as the boolean True.)
            tiers = xfail.get("tiers", sorted(XFAIL_TIERS_DEFAULT))
            if not isinstance(tiers, list) or not tiers or not all(isinstance(t, str) for t in tiers):
                raise ManifestError(f"{path}: xfail 'tiers' must be a non-empty list of assertion tiers "
                                    f"from {sorted(XFAIL_TIERS_ALLOWED)}, got {tiers!r}")
            bad = sorted(set(tiers) - XFAIL_TIERS_ALLOWED)
            if bad:
                raise ManifestError(f"{path}: xfail 'tiers' names {bad}; allowed tiers are "
                                    f"{sorted(XFAIL_TIERS_ALLOWED)} (never 'smoke': an app that did not "
                                    f"reach RUNNING has not failed on its defect)")
            # `releases`: the Striim releases the expected failure applies to (exact, or a letter
            # range inside one patch line). On any other release the xfail is off and the test
            # must pass -- including releases nobody has run yet.
            if "releases" in xfail:
                rels = xfail["releases"]
                if not isinstance(rels, list) or not rels or not all(isinstance(r, str) for r in rels):
                    raise ManifestError(f"{path}: xfail 'releases' must be a non-empty list of Striim "
                                        f"releases or one-line ranges, got {rels!r}")
                for r in rels:
                    try:
                        check_release_entry(r)
                    except ReleaseError as e:
                        raise ManifestError(f"{path}: xfail 'releases': {e}") from e
            unknown = sorted(set(xfail) - {"reason", "strict", "tiers", "releases"})
            if unknown:
                raise ManifestError(f"{path}: xfail has unknown key(s) {unknown}; accepted: reason, strict, tiers, releases")
            xfail["tiers"] = sorted(set(tiers))
        else:
            raise ManifestError(f"{path}: 'xfail' must be a string reason or a dict, got {xfail_raw!r}")
        if "tiers" not in xfail:
            xfail["tiers"] = sorted(XFAIL_TIERS_DEFAULT)

    source_dir = _root(example) if example else path.parent
    # Every ddl/seed/upload name with its `local:` flag, gathered as each block is parsed.
    file_refs: list = []
    ddl_files = _normalize_file_specs(raw.get("ddl"), "ddl", path, refs_out=file_refs)
    seed_files = _normalize_file_specs(raw.get("seed"), "seed", path, allow_when=True,
                                       refs_out=file_refs)
    file_refs += [(u["from"], u.get("local", False)) for u in op_uploads]

    # Parse action section: list of actions (stop_start_cycle, drop_recreate_app, service_outage,
    # capture, alter_recompile) to run between seed and assertions, in order.
    action_raw = raw.get("action", [])
    action_specs = []
    if action_raw:
        if not isinstance(action_raw, list):
            raise ManifestError(f"{path}: 'action' must be a list of actions, got {action_raw!r}")
        for i, action in enumerate(action_raw):
            if not isinstance(action, dict):
                raise ManifestError(f"{path}: action[{i}] must be a dict, got {action!r}")
            action_type = action.get("type")
            if not action_type:
                raise ManifestError(f"{path}: action[{i}] must have a 'type' key")
            if action_type == "stop_start_cycle":
                cycles = action.get("cycles", 1)
                if not isinstance(cycles, int) or cycles < 1:
                    raise ManifestError(
                        f"{path}: action[{i}] 'cycles' must be a positive integer, got {cycles!r}")
                delay_before_stop = _parse_zeroable_duration(
                    action.get("delay_before_stop", 3), "delay_before_stop", path)
                stop_duration = _parse_zeroable_duration(
                    action.get("stop_duration", 2), "stop_duration", path)

                concurrent_ops = _parse_concurrent(action, i, path)

                action_specs.append({
                    "type": "stop_start_cycle",
                    "cycles": cycles,
                    "delay_before_stop": delay_before_stop,
                    "stop_duration": stop_duration,
                    "concurrent": concurrent_ops,
                })
            elif action_type == "drop_recreate_app":
                # Stop -> DROP ... FORCE -> recreate the app from its own rendered TQL block
                # -> DEPLOY -> START. The FORCE is inherited from StriimClient._force_drop
                # (a plain DROP wedges on a stuck adapter). `app` is token-rendered, so a
                # multi-app case can name just the piece it rebuilds (e.g. "${APP}_source").
                unknown = sorted(set(action) - _DROP_RECREATE_KEYS)
                if unknown:
                    raise ManifestError(
                        f"{path}: action[{i}] 'drop_recreate_app' has unknown key(s) {unknown} -- "
                        f"allowed: {sorted(_DROP_RECREATE_KEYS - {'type'})}")
                dr_app = action.get("app")
                if not dr_app or not isinstance(dr_app, str) or not dr_app.strip():
                    raise ManifestError(
                        f"{path}: action[{i}] 'drop_recreate_app' needs a non-empty 'app' key")
                dr_delay = _parse_zeroable_duration(
                    action.get("delay_before_stop", 3), "delay_before_stop", path)
                dr_wait = _parse_zeroable_duration(
                    action.get("recreate_wait", 5), "recreate_wait", path)
                dr_seeds = []
                if "seed" in action:
                    dr_seeds = _normalize_action_seeds(action["seed"], f"action[{i}] seed", path,
                                                       refs_out=file_refs)
                action_specs.append({
                    "type": "drop_recreate_app",
                    "app": dr_app.strip(),
                    "delay_before_stop": dr_delay,
                    "recreate_wait": dr_wait,
                    "seed": dr_seeds,
                    # after STOP, before the drop: read values, then write while stopped
                    "capture": _normalize_captures(
                        action.get("capture"), f"action[{i}] capture", path),
                    "stopped_seed": _normalize_file_specs(
                        action.get("stopped_seed"), "stopped_seed", path, allow_when=False,
                        refs_out=file_refs),
                    "tokens": _normalize_overrides(
                        action.get("tokens"), f"action[{i}] tokens", path, tokens),
                })
            elif action_type == "capture":
                action_specs.append({"type": "capture", **_normalize_capture(
                    action, f"action[{i}] 'capture'", path, standalone=True)})
            elif action_type == "alter_recompile":
                # STOP, capture, stopped_seed, then one import: UNDEPLOY; ALTER APPLICATION;
                # the fragment; ALTER APPLICATION ... RECOMPILE; DEPLOY; START. Unlike
                # drop_recreate_app the app survives, and with it its checkpoint.
                unknown = sorted(set(action) - _ALTER_RECOMPILE_KEYS)
                if unknown:
                    raise ManifestError(
                        f"{path}: action[{i}] 'alter_recompile' has unknown key(s) {unknown} -- "
                        f"allowed: {sorted(_ALTER_RECOMPILE_KEYS - {'type'})}")
                ar_app = action.get("app")
                if not isinstance(ar_app, str) or not ar_app.strip():
                    raise ManifestError(
                        f"{path}: action[{i}] 'alter_recompile' needs a non-empty 'app' key")
                action_specs.append({
                    "type": "alter_recompile",
                    "app": ar_app.strip(),
                    "file": _normalize_alter_file(
                        action.get("file"), f"action[{i}] 'alter_recompile'", path, source_dir),
                    "delay_before_stop": _parse_zeroable_duration(
                        action.get("delay_before_stop", 3), "delay_before_stop", path),
                    "capture": _normalize_captures(
                        action.get("capture"), f"action[{i}] capture", path),
                    "stopped_seed": _normalize_file_specs(
                        action.get("stopped_seed"), "stopped_seed", path, allow_when=False,
                        refs_out=file_refs),
                    "seed": _normalize_action_seeds(action.get("seed"), f"action[{i}] seed", path,
                                                    refs_out=file_refs),
                })
            elif action_type == "service_outage":
                unknown = sorted(set(action) - _SERVICE_OUTAGE_KEYS)
                if unknown:
                    raise ManifestError(
                        f"{path}: action[{i}] 'service_outage' has unknown key(s) {unknown} -- "
                        f"allowed: {sorted(_SERVICE_OUTAGE_KEYS - {'type'})}")
                so_service = action.get("service")
                if not isinstance(so_service, str) or not so_service.strip():
                    raise ManifestError(
                        f"{path}: action[{i}] 'service_outage' needs a non-empty 'service' key")
                so_service = so_service.strip()
                if so_service not in requires:
                    raise ManifestError(
                        f"{path}: action[{i}] 'service_outage' service {so_service!r} must be "
                        f"listed in 'requires'")
                so_cycles = action.get("cycles", 1)
                if isinstance(so_cycles, bool) or not isinstance(so_cycles, int) or so_cycles < 1:
                    raise ManifestError(
                        f"{path}: action[{i}] 'cycles' must be a positive integer, got {so_cycles!r}")
                so_signal = action.get("signal", "KILL")
                if so_signal not in ("KILL", "TERM", "RESTART"):
                    raise ManifestError(
                        f"{path}: action[{i}] 'signal' must be KILL, TERM or RESTART, "
                        f"got {so_signal!r}")
                if so_signal == "RESTART":
                    for so_key in ("down_for", "ready_timeout"):
                        if so_key in action:
                            raise ManifestError(
                                f"{path}: action[{i}] '{so_key}' does not apply to signal RESTART: "
                                f"the service restarts in place and is back when its hook returns")
                    from livetest import registry
                    try:
                        so_hook = registry.load_service(so_service).restart_in_place
                    except registry.RegistryError as e:
                        raise ManifestError(f"{path}: action[{i}] 'service_outage': {e}") from e
                    if not so_hook:
                        raise ManifestError(
                            f"{path}: action[{i}] signal RESTART needs service {so_service!r} "
                            f"to set 'restart_in_place' in its service.yaml")
                action_specs.append({
                    "type": "service_outage",
                    "service": so_service,
                    "cycles": so_cycles,
                    "delay_before": _parse_zeroable_duration(
                        action.get("delay_before", 3), "delay_before", path),
                    "down_for": 0.0 if so_signal == "RESTART" else _parse_zeroable_duration(
                        action.get("down_for", 5), "down_for", path),
                    "signal": so_signal,
                    "ready_timeout": _parse_zeroable_duration(
                        action.get("ready_timeout", 120), "ready_timeout", path),
                    "settle": _parse_zeroable_duration(
                        action.get("settle", 30), "settle", path),
                    "concurrent": _parse_concurrent(action, i, path),
                })
            else:
                raise ManifestError(f"{path}: action[{i}] unknown type {action_type!r}")
    _check_capture_tokens(action_specs, modules, tokens, path)
    local_files = _check_local_files(file_refs, example, path)
    if _monitor_recaptures:
        _reserved = _reserved_tokens(modules)
        for cap in _monitor_recaptures:
            if cap["token"] in _reserved or cap["token"] in tokens:
                raise ManifestError(
                    f"{path}: assert.monitor recapture token {cap['token']!r} collides with a "
                    f"{'harness-provided' if cap['token'] in _reserved else 'tokens:'} token")

    # Auto-detect persistent streams from TQL and collect their derived topic names
    # for automatic cleanup. Each persistent stream creates two topics:
    #   ${NS}_<streamName> (data) + ${NS}_<streamName>_CHECKPOINT (coordinator)
    persistent_stream_names = []
    try:
        tql_path = source_dir / tql
        if tql_path.is_file():
            tql_content = tql_path.read_text()
            persistent_stream_names = _extract_persistent_streams(tql_content)
    except Exception as e:
        # TQL reading/parsing errors are non-fatal for this detection step
        # (if the TQL is actually broken, it will fail at deploy time)
        warnings.warn(f"{path}: could not detect persistent streams from {tql}: {e}",
                      stacklevel=2)

    # C7.2: the lifecycle block is validated at load, before any
    # provisioning (and offline through `striim-test validate`).
    from livetest import lifecycle as _slt_lifecycle
    try:
        _slt_lc_spec = _slt_lifecycle.parse_manifest_block(raw, path)
    except _slt_lifecycle.LifecycleSpecError as e:
        raise ManifestError(str(e)) from None
    # C8.1: the exact block and every exact spec are validated at load, too.
    from livetest import exactdata as _slt_exact
    try:
        _slt_exact_spec = _slt_exact.parse_manifest_block(raw, path, lifecycle=_slt_lc_spec)
    except _slt_exact.ExactSpecError as e:
        raise ManifestError(str(e)) from None
    return TestManifest(
        name=name,
        lifecycle=_slt_lc_spec,
        exact=_slt_exact_spec,
        tql=tql,
        dir=path.parent,
        depth=depth,
        topology=topology,
        requires=list(requires),
        tokens=tokens,
        kafka_cleanup_topics=list(kafka_cleanup_topics),
        ddl=raw.get("ddl"),
        seed=raw.get("seed"),
        ddl_files=ddl_files,
        seed_files=seed_files,
        action_specs=action_specs,
        assert_=dict(raw.get("assert", {})),
        timeout=int(raw.get("timeout", 120)),
        diff_poll=_diff_poll(raw, path),
        tags=list(tags),
        disabled=disabled,
        disabled_parallel=disabled_parallel,
        xfail=xfail,
        recover=recover,
        expect_halt=expect_halt,
        expect_halt_contains=expect_halt_contains,
        example=example,
        purpose=purpose,
        modules=modules,
        op_uploads=op_uploads,
        server_files=_normalize_server_files(raw.get("server_files"), path),
        generate_specs=_normalize_generate(raw.get("generate"), source_dir, path),
        persistent_stream_names=persistent_stream_names,
        source_dir=source_dir,
        local_files=local_files,
    )
