"""Every shipped service resolves with nothing set, and needs no private image.

The framework ships stock services (public images, default settings) and
recipes that build from a public base. A service that needs a private or locally built image
lives in the consumer's repo and comes in through servicesRoots. These tests hold the line for every compose file under
scripts/*/services:

* each `${VAR}` has a default, or is set in the tracked `.env` beside the compose file, except
  the Striim license, which is the customer's input to the Striim recipe;
* each image is on the public list below, or is built from an in-repo Dockerfile whose every
  FROM is on that list;
* `docker compose config` accepts each file with nothing set (skipped without the docker CLI).

Adding an image means adding it to PUBLIC_IMAGES, deliberately.
"""
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
COMPOSE_FILES = sorted(p for p in (REPO / "scripts").glob("*/services/**/compose*.y*ml"))

# Pulled from public registries as they are; also the only bases a shipped Dockerfile may use.
PUBLIC_IMAGES = {
    "postgres:16", "mysql:8.0", "python:3.12-slim",
    "fsouza/fake-gcs-server:1.54.0",
    "gvenzl/oracle-free:23.26.2-slim-faststart",
    "gcr.io/cloud-spanner-emulator/emulator:1.5.55",
    "confluentinc/cp-zookeeper:7.6.1", "confluentinc/cp-kafka:7.6.1",
    "confluentinc/cp-schema-registry:7.6.1",
    "mcr.microsoft.com/mssql/server:2022-latest",
    "opentext/vertica-k8s:25.3.0-8-multiarch",
    "amd64/ubuntu:22.04", "ubuntu:24.04",
}
# The Striim recipe's customer inputs: the license the cluster boots with. No default on purpose.
CUSTOMER_INPUTS = {"live/striim/compose.yaml": {"CLUSTER_NAME", "COMPANY_NAME", "PRODUCT_KEY",
                                                "LICENCE_KEY"}}

_NAME = re.compile(r"[A-Za-z_]\w*")


def _unset(s: str, missing: set, seen: set | None = None) -> str:
    """Interpolate ``s`` as compose does with nothing set: ${V:-d}/${V-d} give d, ${V:+x}/${V+x}
    give "", and a bare ${V} or $V gives "" and is added to ``missing``. Every referenced name,
    in any form, is added to ``seen``."""
    seen = set() if seen is None else seen
    out, i = [], 0
    while i < len(s):
        if s.startswith("$$", i):
            out.append("$$"); i += 2; continue
        if s[i] != "$":
            out.append(s[i]); i += 1; continue
        if i + 1 < len(s) and s[i + 1] == "{":
            m = _NAME.match(s, i + 2)
            seen.add(m.group(0))
            j, depth = m.end(), 1          # j ends on the brace that closes this ${
            while True:
                if s.startswith("${", j):
                    depth += 1
                    j += 1
                elif s[j] == "}":
                    depth -= 1
                    if not depth:
                        break
                j += 1
            body = s[m.end():j]
            if body.startswith((":-", "-")):
                out.append(_unset(body.split("-", 1)[1], missing, seen))
            elif body.startswith((":+", "+")):
                _unset(body.split("+", 1)[1], set(), seen)   # never used while V is unset
            else:
                missing.add(m.group(0))
            i = j + 1
            continue
        m = _NAME.match(s, i + 1)
        if m:
            seen.add(m.group(0)); missing.add(m.group(0)); i = m.end()
        else:
            out.append("$"); i += 1
    return "".join(out)


def _key(path: Path) -> str:
    return str(path.relative_to(REPO / "scripts")).replace("/services/", "/", 1)


def _strings(node):
    if isinstance(node, dict):
        for k, v in node.items():
            yield from _strings(k)
            yield from _strings(v)
    elif isinstance(node, list):
        for v in node:
            yield from _strings(v)
    elif isinstance(node, str):
        yield node


def _dotenv(path: Path) -> set:
    env = path.parent / ".env"
    if not env.is_file():
        return set()
    return {ln.split("=", 1)[0].strip() for ln in env.read_text().splitlines()
            if "=" in ln and not ln.lstrip().startswith("#")}


def _resolve(ref: str) -> str:
    """An image reference as compose resolves it with nothing set."""
    return _unset(ref, set())


def test_the_catalog_ships_only_the_public_services():
    keys = {_key(p) for p in COMPOSE_FILES}
    # Teradata's VM image and disks are private too. Both tiers keep a
    # connection-only teradata definition (service.yaml, no compose) for your own instance.
    assert not [k for k in keys if k.split("/")[1] == "teradata"], keys
    assert len(keys) == 17, sorted(keys)


@pytest.mark.parametrize("path", COMPOSE_FILES, ids=_key)
def test_every_variable_has_a_default(path):
    doc = yaml.safe_load(path.read_text())
    unset = set()
    for s in _strings(doc):
        _unset(s, unset)
    unset -= _dotenv(path)
    assert unset == CUSTOMER_INPUTS.get(_key(path), set()), \
        f"{_key(path)}: no default for {sorted(unset)}"


def _dockerfile(path: Path, build) -> Path:
    if isinstance(build, str):
        return path.parent / build / "Dockerfile"
    ctx = path.parent / build.get("context", ".")
    return ctx / build.get("dockerfile", "Dockerfile")


@pytest.mark.parametrize("path", COMPOSE_FILES, ids=_key)
def test_every_image_is_public_or_built_here_on_a_public_base(path):
    services = yaml.safe_load(path.read_text()).get("services") or {}
    built_here = {_resolve(s["image"]) for s in services.values() if "build" in s and "image" in s}
    for name, svc in services.items():
        build = svc.get("build")
        if build is None and "image" not in svc:
            continue                       # an override file: it adds settings, not images
        if build is None:
            image = _resolve(svc["image"])
            if image in built_here:        # the node and agent run the primary's Striim build
                continue
            assert image in PUBLIC_IMAGES, f"{_key(path)} {name}: {image} is not public"
            continue
        df = _dockerfile(path, build)
        assert df.is_file(), f"{_key(path)} {name}: no {df}"
        stages, bases = set(), []
        for ln in df.read_text().splitlines():
            m = re.match(r"\s*FROM\s+(?:--\S+\s+)*(\S+)(?:\s+AS\s+(\S+))?", ln, re.I)
            if m:
                bases.append(m.group(1))
                if m.group(2):
                    stages.add(m.group(2))
        assert bases, f"{df}: no FROM"
        for b in bases:
            assert b in stages or b in PUBLIC_IMAGES, \
                f"{_key(path)} {name}: {df.name} builds FROM {b}, which is not public"


def _unexpected_warnings(paths, stderr: str) -> set:
    """The variables `docker compose config` warns are unset, less those the files use only in
    a defaulted form. Compose v2.33 also warns for ${X:+${X}-}, which is defined
    with nothing set; newer Compose does not. A bare ${VAR} is still reported either way."""
    warned = set(re.findall(r'The \\?"(\w+)\\?" variable is not set', stderr))
    seen, bare = set(), set()
    for p in paths:
        for s in _strings(yaml.safe_load(Path(p).read_text())):
            _unset(s, bare, seen)
    return warned - (seen - bare)


_V233 = ('time="2026-09-25T08:21:38-07:00" level=warning msg="The \\"{0}\\" variable is not set. '
         'Defaulting to a blank string."\n')


def test_compose_2_33_warnings_for_defaulted_names_are_not_failures():
    spanner = REPO / "scripts" / "integration" / "services" / "spanner" / "compose.yaml"
    assert _unexpected_warnings([spanner], _V233.format("INT_STACK_PREFIX") * 3) == set()
    striim = REPO / "scripts" / "live" / "services" / "striim" / "compose.yaml"
    got = _unexpected_warnings([striim], _V233.format("SLT_STACK_PREFIX") + _V233.format("PRODUCT_KEY"))
    assert got == {"PRODUCT_KEY"}, "a bare ${VAR} must still count"


def _clean_env() -> dict:
    keep = ("PATH", "HOME", "DOCKER_HOST", "DOCKER_CONFIG", "DOCKER_CONTEXT", "XDG_RUNTIME_DIR")
    return {k: os.environ[k] for k in keep if k in os.environ}


@pytest.mark.skipif(shutil.which("docker") is None, reason="no docker CLI")
@pytest.mark.parametrize("path", COMPOSE_FILES, ids=_key)
def test_docker_compose_config_resolves_with_nothing_set(path):
    if subprocess.run(["docker", "compose", "version"], capture_output=True,
                      env=_clean_env()).returncode:
        pytest.skip("no docker compose plugin")
    paths, key = [path], _key(path)
    if path.name != "compose.yaml":        # an override is only ever applied on its base
        paths, key = [path.parent / "compose.yaml", path], _key(path.parent / "compose.yaml")
    files = [a for p in paths for a in ("-f", str(p))]
    r = subprocess.run(["docker", "compose", *files, "config", "-q"],
                       capture_output=True, text=True, cwd=path.parent, env=_clean_env(),
                       timeout=60)
    assert r.returncode == 0, r.stderr
    assert _unexpected_warnings(paths, r.stderr) == CUSTOMER_INPUTS.get(key, set()), r.stderr
