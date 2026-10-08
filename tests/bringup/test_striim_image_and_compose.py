"""Bring-up defects 1-3 found on a Linux test host (2026-09-15), checked on the checkout's striim service tree.

1. The striim entrypoint is executable, and the Dockerfile chmods it after the COPY and before CMD.
2. ``slt-striim`` takes ``SLT_STRIIM_PRIMARY_CPUS`` exactly as ``slt-node`` takes ``SLT_STRIIM_NODE_CPUS``,
   and ``cluster_up`` hands the knob to compose.
3. The Striim default release is 5.4.2 in both engines, compose and the dependency fetch.

The root conftest puts both engines on the path.
"""
import os
import re
import stat
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
TREES = {
    "checkout": REPO / "scripts/live/services/striim",
}
DEFAULT = "5.4.2"


# ---- 1: executable entrypoint -----------------------------------------------------------------

@pytest.mark.parametrize("tree", TREES)
@pytest.mark.parametrize("rel", ["images/striim/files/entrypoint.sh", "download-dependencies.sh"])
def test_scripts_are_executable(tree, rel):
    # A checkout honours the git mode, so this fails on a checkout whose blob is still 100644.
    assert (TREES[tree] / rel).stat().st_mode & stat.S_IXUSR, f"{tree}/{rel} is not executable"


@pytest.mark.parametrize("tree", TREES)
def test_dockerfile_chmods_the_entrypoint_before_cmd(tree):
    lines = [ln.strip() for ln in (TREES[tree] / "images/striim/Dockerfile").read_text().splitlines()]
    copy = lines.index("COPY --chown=striim:striim ./files/entrypoint.sh /app/")
    chmod = lines.index("RUN chmod 0755 /app/entrypoint.sh")
    cmd = lines.index('CMD [ "/app/entrypoint.sh" ]')
    assert copy < chmod < cmd
    assert not any("entrypoint.sh /app" in ln for ln in lines[chmod + 1:cmd]), "a later COPY would undo the chmod"


# ---- 2: primary CPU knob ----------------------------------------------------------------------

def _render(value, env):
    """Compose's ``${VAR:-default}`` for one scalar (the only form the cpus lines use)."""
    m = re.fullmatch(r"\$\{(\w+):-([^}]*)\}", str(value))
    assert m, f"unexpected cpus expression {value!r}"
    return env.get(m.group(1)) or m.group(2)


@pytest.mark.parametrize("tree", TREES)
def test_compose_cpus_knobs(tree):
    services = yaml.safe_load((TREES[tree] / "compose.yaml").read_text())["services"]
    assert services["slt-striim"]["cpus"] == "${SLT_STRIIM_PRIMARY_CPUS:-0}"
    assert services["slt-node"]["cpus"] == "${SLT_STRIIM_NODE_CPUS:-0}"

    host24 = {"SLT_STRIIM_PRIMARY_CPUS": "12", "SLT_STRIIM_NODE_CPUS": "12"}
    rendered = {s: float(_render(services[s]["cpus"], host24)) for s in ("slt-striim", "slt-node")}
    assert rendered == {"slt-striim": 12.0, "slt-node": 12.0} and sum(rendered.values()) <= 24
    assert {s: _render(services[s]["cpus"], {}) for s in ("slt-striim", "slt-node")} == {"slt-striim": "0", "slt-node": "0"}


def test_cluster_up_passes_the_primary_knob_to_compose(monkeypatch):
    from livetest import striim_provision as sp
    monkeypatch.delenv("STRIIM_HOME", raising=False)
    monkeypatch.setenv("SLT_STRIIM_PRIMARY_CPUS", "12")
    monkeypatch.setenv("SLT_STRIIM_NODE_CPUS", "12")
    seen = {}

    def run(argv, cwd=None, **_):
        seen.update(argv=argv, cpus=(os.environ.get("SLT_STRIIM_PRIMARY_CPUS"), os.environ.get("SLT_STRIIM_NODE_CPUS")),
                    version=os.environ.get("STRIIM_VERSION"))

    sp.cluster_up(Path("/x"), {"STRIIM_VERSION": DEFAULT}, run=run)
    assert seen["argv"][:2] == ["docker", "compose"] and "slt-striim" in seen["argv"]
    assert seen["cpus"] == ("12", "12") and seen["version"] == DEFAULT


# ---- 3: default release -----------------------------------------------------------------------

def test_engine_defaults():
    from livetest import releases as live_releases, striim_provision as sp
    from inttest import releases as int_releases
    assert sp._DEFAULT_STRIIM_VERSION == DEFAULT
    assert f"striim-dbms-{DEFAULT}-Linux.deb" in sp.REQUIRED_DEPS
    for mod in (live_releases, int_releases):
        rel = mod.resolve_release({})
        assert (rel["STRIIM_VERSION"], rel["STRIIM_SERIES"], rel["JAVA_RELEASE"]) == (DEFAULT, "5.4", "17")


@pytest.mark.parametrize("tree", TREES)
def test_compose_and_fetch_defaults(tree):
    compose = (TREES[tree] / "compose.yaml").read_text()
    assert re.findall(r"\$\{STRIIM_VERSION:-([^}]*)\}", compose) == [DEFAULT] * 4
    fetch = (TREES[tree] / "download-dependencies.sh").read_text()
    assert f'STRIIM_VERSION="${{STRIIM_VERSION:-{DEFAULT}}}"' in fetch
