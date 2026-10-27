"""Tryout jars: run the Docker cluster with patched product jars swapped into the Striim image.

``SLT_STRIIM_TRYOUT_JARS`` names one or more jars, each with the containers it goes to::

    SLT_STRIIM_TRYOUT_JARS="both:/jars/Platform-1.0.0-SNAPSHOT.jar"
    SLT_STRIIM_TRYOUT_JARS="server:/j/Platform-1.0.0-SNAPSHOT.jar,agent:/j/Agent-1.0.0-SNAPSHOT.jar=Agent-5.4.2.jar"

Entries are separated by commas or newlines. ``TARGET`` is ``server`` (``/opt/striim/lib`` of the
primary and the second node), ``agent`` (``/opt/striim/agent/lib`` of the agent) or ``both``. The
path is absolute (``~`` is expanded). A tryout REPLACES a jar the image already has, keeping that
jar's file name, because the release is read from the ``Platform-<version>.jar`` file name
(``releases.detect_release``) and a second Platform jar is an error. The jar it replaces is
``<artifactId>-<STRIIM_VERSION>.jar``, from the tryout's own ``META-INF/maven/*/*/pom.properties``,
unless ``=<jar name>`` names it. A tryout that matches no jar in a target directory is an error
before any test runs; a jar is never added.

The stock image is never changed. The tryouts go into a thin image ``FROM`` it, tagged
``<stock tag>-tryout-<hash of the tryout set>``; the cluster runs that image while the setting is
set, and the stock image again, with no rebuild, once it is unset. After the cluster is up, each
replaced jar's sha256 is checked inside every container it went to. Docker mode only.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path

TRYOUT_ENV = "SLT_STRIIM_TRYOUT_JARS"
TARGETS = ("server", "agent", "both")
SERVER_LIB = "/opt/striim/lib"
AGENT_LIB = "/opt/striim/agent/lib"
# Image labels of a tryout image: the tryout set's digest (what the tag's suffix abbreviates), the
# stock image it was built FROM, and the resolved records the evidence reports.
LABEL_SET = "org.striim.slt.tryout.set"
LABEL_BASE = "org.striim.slt.tryout.base"
LABEL_RECORDS = "org.striim.slt.tryout.records"
TAG_MARK = "-tryout-"
TAG_HASH_LEN = 12
LIST_TIMEOUT_S = 300.0       # one `docker run` of `ls` under amd64 emulation
BUILD_TIMEOUT_S = 1800.0     # FROM a local image + COPY: minutes at most
EXEC_TIMEOUT_S = 120.0       # one `sha256sum` of a jar inside a container
INSPECT_TIMEOUT_S = 30.0
_PROPS_CAP = 64 * 1024       # a properties entry larger than this is not read
_POM = re.compile(r"^META-INF/maven/[^/]+/[^/]+/pom\.properties$")
_JAR_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]*\.jar$")
_LIST_MARK = "slt-tryout-agent-lib"


class TryoutError(RuntimeError):
    """The tryout setting is malformed, names an unusable jar, matches no jar in the image, or a
    replaced jar's bytes in a container are not the tryout's."""


@dataclass(frozen=True)
class Tryout:
    path: Path
    target: str
    override: str | None      # the jar name to replace, when given with `=`
    sha256: str               # hex
    artifact_ids: tuple       # from every pom.properties in the jar, sorted
    scm_version: str | None   # build.properties scm_version, when present

    def dirs(self) -> tuple:
        return {"server": (SERVER_LIB,), "agent": (AGENT_LIB,), "both": (SERVER_LIB, AGENT_LIB)}[self.target]


def configured_value(env=None) -> str:
    """The setting's raw value: the process environment, then ``.env`` (as SLT_TERADATA_JDBC)."""
    from livetest import paths
    return (paths.setting(TRYOUT_ENV, env) or "").strip()


def parse(value: str) -> list:
    """[(target, path, override)] from the setting's value; raises TryoutError naming the entry."""
    out = []
    for raw in re.split(r"[,\n]", value or ""):
        entry = raw.strip()
        if not entry:
            continue
        target, sep, rest = entry.partition(":")
        target = target.strip().lower()
        if not sep or target not in TARGETS:
            raise TryoutError(f"{TRYOUT_ENV} entry {entry!r}: expected TARGET:PATH[=JAR], with TARGET one of "
                              f"{', '.join(TARGETS)}")
        path, eq, override = rest.partition("=")
        path, override = path.strip(), override.strip()
        if not path:
            raise TryoutError(f"{TRYOUT_ENV} entry {entry!r}: no jar path")
        if eq and not _JAR_NAME.match(override):
            raise TryoutError(f"{TRYOUT_ENV} entry {entry!r}: the jar to replace must be a plain jar file name, "
                              f"got {override!r}")
        p = Path(path).expanduser()
        if not p.is_absolute():
            raise TryoutError(f"{TRYOUT_ENV} entry {entry!r}: the jar path must be absolute")
        out.append((target, p, override or None))
    return out


def _read_entry(zf: zipfile.ZipFile, info: zipfile.ZipInfo) -> str:
    if info.file_size > _PROPS_CAP:
        return ""
    return zf.read(info).decode("utf-8", "replace")


def _property(text: str, key: str) -> str | None:
    for line in text.splitlines():
        line = line.strip()
        if not line or line[0] in "#!":
            continue
        m = re.match(r"([^=:\s]+)\s*[=:]?\s*(.*)$", line)
        if m and m.group(1) == key:
            return m.group(2).strip() or None
    return None


def inspect_jar(path: Path) -> tuple:
    """(artifact ids, scm_version) of a jar; raises TryoutError when it is not a readable jar."""
    try:
        with zipfile.ZipFile(path) as zf:
            infos = zf.infolist()
            ids = {_property(_read_entry(zf, i), "artifactId") for i in infos if _POM.match(i.filename)}
            builds = sorted((i for i in infos if i.filename.rsplit("/", 1)[-1] == "build.properties"),
                            key=lambda i: (i.filename.count("/"), i.filename))
            scm = None
            for info in builds:          # the jar's root build.properties first
                scm = _property(_read_entry(zf, info), "scm_version")
                if scm:
                    break
    except (OSError, zipfile.BadZipFile, ValueError) as e:
        raise TryoutError(f"{TRYOUT_ENV}: {path} is not a readable jar: {type(e).__name__}: {e}") from e
    return tuple(sorted(i for i in ids if i)), scm


def _sha256(path: Path) -> str:
    from livetest import opartifacts
    return opartifacts.file_sha256(path)


def load(value: str) -> list:
    """The configured tryouts, each jar read and hashed. Raises TryoutError on a malformed entry, a missing
    or unreadable jar, a jar with no Maven artifactId and no ``=`` override, or two tryouts replacing the
    same jar in the same directory."""
    tryouts = []
    for target, path, override in parse(value):
        if not path.is_file():
            raise TryoutError(f"{TRYOUT_ENV}: tryout jar {path} does not exist")
        ids, scm = inspect_jar(path)
        if not ids and not override:
            raise TryoutError(f"{TRYOUT_ENV}: {path.name} has no META-INF/maven/*/*/pom.properties artifactId; "
                              f"name the jar it replaces with {target}:{path}=<jar name>")
        tryouts.append(Tryout(path, target, override, _sha256(path), ids, scm))
    return tryouts


def configured(env=None) -> list:
    """The tryouts the setting names ([] when it is unset)."""
    value = configured_value(env)
    return load(value) if value else []


def set_digest(tryouts) -> str:
    """The tryout set's identity: each jar's bytes, target and override. What the tag abbreviates."""
    spec = sorted([t.sha256, t.target, t.override or ""] for t in tryouts)
    return hashlib.sha256(json.dumps(spec, separators=(",", ":")).encode()).hexdigest()


def image_ref(stock_ref: str, tryouts) -> str:
    """``slt-striim:5.4.2`` -> ``slt-striim:5.4.2-tryout-<12 hex>``."""
    return f"{stock_ref}{TAG_MARK}{set_digest(tryouts)[:TAG_HASH_LEN]}"


def strip_tag(tag: str) -> str:
    """The stock version of a tag that may carry a tryout suffix: ``5.4.2-tryout-ab12…`` -> ``5.4.2``."""
    head, mark, tail = tag.rpartition(TAG_MARK)
    return head if mark and re.fullmatch(r"[0-9a-f]+", tail) else tag


def resolve(tryouts, version: str, listing: dict) -> list:
    """The records of a tryout set against the stock image's lib listing ({dir: set of names}): for each
    tryout, the one jar it replaces in every target directory. Raises TryoutError when a tryout matches
    no jar in a target directory, matches more than one, or two tryouts replace the same jar."""
    records, claimed = [], {}
    for t in tryouts:
        if t.override:
            candidates = [t.override]
        else:
            candidates = [f"{a}-{version}.jar" for a in t.artifact_ids]
        chosen = None
        for d in t.dirs():
            present = sorted(c for c in candidates if c in listing.get(d, set()))
            if not present:
                raise TryoutError(
                    f"{TRYOUT_ENV}: tryout {t.path.name} ({t.target}) matches no jar in {d} of the stock image: "
                    f"looked for {', '.join(candidates)}. A tryout only replaces an installed jar; name the one "
                    f"it replaces with {t.target}:{t.path}=<jar name>")
            if len(present) > 1:
                raise TryoutError(
                    f"{TRYOUT_ENV}: tryout {t.path.name} matches several jars in {d}: {', '.join(present)}; name "
                    f"the one it replaces with {t.target}:{t.path}=<jar name>")
            if chosen is not None and present[0] != chosen:
                raise TryoutError(f"{TRYOUT_ENV}: tryout {t.path.name} resolves to {chosen} and {present[0]} in "
                                  f"the two lib directories; name the jar it replaces with =<jar name>")
            chosen = present[0]
            if (d, chosen) in claimed:
                raise TryoutError(f"{TRYOUT_ENV}: {claimed[(d, chosen)]} and {t.path.name} both replace "
                                  f"{d}/{chosen}")
            claimed[(d, chosen)] = t.path.name
        records.append({"file": t.path.name, "sha256": "sha256:" + t.sha256, "target": t.target,
                        "replaces": chosen, "scmVersion": t.scm_version})
    return records


def _capture(argv, timeout):
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout)


def _call(run, argv, timeout, what):
    """``run(argv)``; raises TryoutError on a timeout, a missing docker binary or a non-zero exit."""
    try:
        r = run(argv) if run is not None else _capture(argv, timeout)
    except subprocess.TimeoutExpired as e:
        raise TryoutError(f"{what}: `{' '.join(argv[:3])}` timed out after {timeout:.0f}s") from e
    except OSError as e:
        raise TryoutError(f"{what}: {type(e).__name__}: {e}") from e
    if getattr(r, "returncode", 1) != 0:
        detail = ((getattr(r, "stderr", "") or "") + (getattr(r, "stdout", "") or "")).strip()[-400:]
        raise TryoutError(f"{what}: `{' '.join(argv[:3])}` exited {getattr(r, 'returncode', None)}: {detail}")
    return getattr(r, "stdout", "") or ""


def image_id(ref: str, run=None) -> str | None:
    """The local image ID of ``ref``, or None when it is absent or Docker cannot say."""
    try:
        r = run(["docker", "image", "inspect", "-f", "{{.Id}}", ref]) if run is not None else \
            _capture(["docker", "image", "inspect", "-f", "{{.Id}}", ref], INSPECT_TIMEOUT_S)
    except (OSError, subprocess.TimeoutExpired):
        return None
    value = (getattr(r, "stdout", "") or "").strip()
    return value if getattr(r, "returncode", 1) == 0 and value else None


def image_labels(ref: str, run=None) -> dict | None:
    """The labels of a local image (or a container's image, given a container name), None when unknown."""
    argv = ["docker", "inspect", "-f", "{{json .Config.Labels}}", ref]
    try:
        r = run(argv) if run is not None else _capture(argv, INSPECT_TIMEOUT_S)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if getattr(r, "returncode", 1) != 0:
        return None
    try:
        labels = json.loads((getattr(r, "stdout", "") or "").strip() or "null")
    except ValueError:
        return None
    return labels if isinstance(labels, dict) else {}


def stock_listing(stock_ref: str, run=None) -> dict:
    """{SERVER_LIB: names, AGENT_LIB: names} of the stock image: one short ``docker run`` of ``ls``, named so a
    run interrupted mid-probe is cleared by the next one."""
    probe = f"slt-tryout-probe-{os.getpid()}"
    script = f"ls -1 {SERVER_LIB} && echo {_LIST_MARK} && ls -1 {AGENT_LIB}"
    rm = ["docker", "rm", "-f", probe]
    try:
        (run or (lambda a: _capture(a, INSPECT_TIMEOUT_S)))(rm)
    except (OSError, subprocess.TimeoutExpired):
        pass
    try:
        out = _call(run, ["docker", "run", "--rm", "--name", probe, "--platform", "linux/amd64",
                          "--entrypoint", "sh", stock_ref, "-c", script],
                    LIST_TIMEOUT_S, f"listing the jars of {stock_ref}")
    finally:
        try:
            (run or (lambda a: _capture(a, INSPECT_TIMEOUT_S)))(rm)
        except (OSError, subprocess.TimeoutExpired):
            pass
    server, mark, agent = out.partition(_LIST_MARK)
    if not mark:
        raise TryoutError(f"listing the jars of {stock_ref}: unexpected output {out[-200:]!r}")
    return {SERVER_LIB: {n.strip() for n in server.splitlines() if n.strip()},
            AGENT_LIB: {n.strip() for n in agent.splitlines() if n.strip()}}


def dockerfile(stock_ref: str, tryouts, records) -> str:
    """The thin image: guard that every replaced jar exists, then copy each tryout over it under its name."""
    lines = [f"FROM {stock_ref}"]
    targets = [f"{d}/{r['replaces']}" for t, r in zip(tryouts, records) for d in t.dirs()]
    lines.append("RUN " + " && ".join(f"test -f {p}" for p in targets))   # replace, never add
    for i, (t, r) in enumerate(zip(tryouts, records)):
        for d in t.dirs():
            lines.append(f"COPY --chown=striim:striim tryout-{i}.jar {d}/{r['replaces']}")
    lines.append("COPY tryouts.json /slt-tryout/tryouts.json")
    return "\n".join(lines) + "\n"


def build(stock_ref: str, version: str, tryouts, run=None, stream=None, progress=None) -> tuple:
    """Build the tryout image FROM ``stock_ref``; returns (image ref, records). Always runs ``docker build``:
    with the stock image and the jars unchanged every step is cached, and a rebuilt stock image or changed
    jar bytes rebuild it. ``run`` answers captured docker calls, ``stream`` the build (output shown live)."""
    ref = image_ref(stock_ref, tryouts)
    base = image_id(stock_ref, run=run)
    if base is None:
        raise TryoutError(f"the stock image {stock_ref} is not present to build the tryout image from")
    records = resolve(tryouts, version, stock_listing(stock_ref, run=run))
    if progress:
        progress("cluster", f"building tryout image {ref}")
    with tempfile.TemporaryDirectory(prefix="slt-tryout-") as tmp:
        ctx = Path(tmp)
        for i, t in enumerate(tryouts):
            dest = ctx / f"tryout-{i}.jar"
            shutil.copyfile(t.path, dest)
            if _sha256(dest) != t.sha256:     # the jar changed after it was read: refuse, never build a mix
                raise TryoutError(f"{TRYOUT_ENV}: {t.path} changed while the tryout image was being built")
        (ctx / "tryouts.json").write_text(json.dumps(records, sort_keys=True, indent=2) + "\n")
        (ctx / "Dockerfile").write_text(dockerfile(stock_ref, tryouts, records))
        argv = ["docker", "build", "--platform", "linux/amd64", "-t", ref,
                "--label", f"{LABEL_SET}={set_digest(tryouts)}", "--label", f"{LABEL_BASE}={base}",
                "--label", f"{LABEL_RECORDS}={json.dumps(records, sort_keys=True, separators=(',', ':'))}",
                str(ctx)]
        try:
            r = (stream or (lambda a: subprocess.run(a, timeout=BUILD_TIMEOUT_S)))(argv)
        except subprocess.TimeoutExpired as e:
            raise TryoutError(f"building the tryout image {ref} timed out after {BUILD_TIMEOUT_S:.0f}s") from e
        except OSError as e:
            raise TryoutError(f"building the tryout image {ref}: {type(e).__name__}: {e}") from e
        if getattr(r, "returncode", 0) != 0:
            raise TryoutError(f"building the tryout image {ref} failed (exit {r.returncode}); its output is above")
    return ref, records


def stock_check(containers, run=None) -> tuple:
    """(why the members are not all stock, the members whose labels could not be read). A member carrying
    any tryout image's label is not stock; one that cannot be inspected is not known to be."""
    unread = []
    for c in containers:
        labels = image_labels(c, run=run)
        if labels is None:
            unread.append(c)
        elif labels.get(LABEL_SET):
            return f"{c} runs tryout jars and {TRYOUT_ENV} is unset", unread
    return None, unread


def running_reason(containers, stock_ref: str, tryouts, run=None) -> str | None:
    """Why a running cluster does not match the tryout setting, or None. ``containers`` is one container name or
    several; every one is checked, so a member left on an older image cannot pass on the primary's identity.
    With tryouts set, each must run the tryout image of this set, built FROM the current stock image. With none
    set, a member running any tryout image does not match, so turning the setting off returns the cluster to
    the stock image."""
    containers = (containers,) if isinstance(containers, str) else tuple(containers)
    if not tryouts:
        return stock_check(containers, run=run)[0]
    ref = image_ref(stock_ref, tryouts)
    want = stock_id = None
    for container in containers:
        labels = image_labels(container, run=run)
        if labels is None:
            return f"{container}'s image cannot be inspected for its tryout jars"
        if labels.get(LABEL_SET) != set_digest(tryouts):
            return f"{container} does not run the tryout jars {TRYOUT_ENV} names"
        want = want or image_id(ref, run=run)
        try:
            argv = ["docker", "container", "inspect", "-f", "{{.Image}}", container]
            r = run(argv) if run is not None else _capture(argv, INSPECT_TIMEOUT_S)
            got = (getattr(r, "stdout", "") or "").strip() if getattr(r, "returncode", 1) == 0 else None
        except (OSError, subprocess.TimeoutExpired):
            got = None
        if not want or got != want:
            return f"{container}'s container image is not {ref}"
        stock_id = stock_id or image_id(stock_ref, run=run)
        if labels.get(LABEL_BASE) != stock_id:
            return f"the tryout image {ref} was built from an older {stock_ref}"
    return None


def image_records(ref: str, run=None) -> list:
    """The records a tryout image carries (its label); raises TryoutError when it carries none."""
    labels = image_labels(ref, run=run)
    try:
        records = json.loads((labels or {}).get(LABEL_RECORDS) or "")
    except ValueError:
        records = None
    if not isinstance(records, list) or not records:
        raise TryoutError(f"the image {ref} carries no tryout records")
    return records


def placements(target: str, env=None) -> list:
    """[(container, lib dir)] a target's jar goes to: server -> the primary's and the second node's
    /opt/striim/lib; agent -> the agent's /opt/striim/agent/lib; both -> all three."""
    from livetest import stack
    primary, node, agent = stack.cluster_containers(env)
    server, agent_side = [(primary, SERVER_LIB), (node, SERVER_LIB)], [(agent, AGENT_LIB)]
    return {"server": server, "agent": agent_side, "both": server + agent_side}[target]


def verify(records, env=None, run=None) -> list:
    """Check, inside every container each tryout went to, that the replaced jar's sha256 is the tryout's.
    Returns the checks made ([{container, path, sha256}]); raises TryoutError on any mismatch or a container
    that cannot be asked."""
    checks = []
    for r in records:
        for container, d in placements(r["target"], env):
            path = f"{d}/{r['replaces']}"
            out = _call(run, ["docker", "exec", container, "sha256sum", path], EXEC_TIMEOUT_S,
                        f"verifying tryout {r['file']} in {container}")
            got = "sha256:" + (out.split() or [""])[0].lower()
            if got != r["sha256"]:
                raise TryoutError(f"tryout {r['file']}: {container}:{path} has {got}, not the tryout's "
                                  f"{r['sha256']}")
            checks.append({"container": container, "path": path, "sha256": got})
    return checks


def banner(records, ref: str) -> list:
    """The lines a run prints so a tryout run cannot be mistaken for a stock one."""
    lines = [f"TRYOUT: the Striim cluster runs {ref}, NOT the stock image"]
    for r in records:
        scm = f", scm_version {r['scmVersion']}" if r.get("scmVersion") else ""
        lines.append(f"TRYOUT:   {r['file']} ({r['target']}) replaces {r['replaces']}, {r['sha256']}{scm}")
    return lines
