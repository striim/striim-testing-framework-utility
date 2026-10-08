"""With SLT_STRIIM_DEPS_MANIFEST set, automatic Docker cluster reuse is bound to TODAY's verified
installer set AND to the image the running container actually runs. Unset in a checkout, reuse is
unchanged.

Drives ``livetest.plugin._resolve_striim`` through its warm-cluster and boot-wait branches with
recorders for every docker/cluster call (no Docker, no services). ``livetest.plugin`` imports
``livetest.striim``, which imports ``striim_api``. These branches never use it (``StriimClient``
is replaced below), so when the real module is absent an inert stand-in module is registered for
the import only.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import time
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

from livetest import layout
from livetest import striim_provision as sp

VERSION = sp._DEFAULT_STRIIM_VERSION
ENV = "SLT_STRIIM_DEPS_MANIFEST"


@pytest.fixture
def plugin(monkeypatch):
    try:
        import striim_api  # noqa: F401
    except ImportError:
        monkeypatch.setitem(sys.modules, "striim_api", types.ModuleType("striim_api"))
    from livetest import plugin as _plugin
    return _plugin


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch):
    layout._reset()
    for name in (ENV, "STRIIM_URL", "SLT_SERVICES_HOST"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(time, "sleep", lambda s: None)

    def trap(*args, **kwargs):
        pytest.fail(f"workload subprocess attempted: {args[:1]!r}")

    monkeypatch.setattr(subprocess, "run", trap)
    monkeypatch.setattr(subprocess, "Popen", trap)
    yield
    layout._reset()


def _manifest(root: Path, salt: bytes) -> Path:
    ddir = root / "deps"
    ddir.mkdir(parents=True)
    digests = {}
    for n in sp._required_deps(VERSION):
        payload = b"payload:" + salt + n.encode()
        (ddir / n).write_bytes(payload)
        digests[n] = hashlib.sha256(payload).hexdigest()
    m = root / "manifest.json"
    m.write_text(json.dumps({"schemaVersion": 1, "directory": "deps", "sha256": digests}))
    return m


class _Docker:
    """Build recorder + docker responder. The tag's image carries the build inputs present when
    it was built and gets a new immutable ID per build; the running container keeps the ID it
    was started from until the cluster is brought up again."""

    def __init__(self, ctx: Path):
        self.ctx = ctx
        self.builds = 0
        self.embedded = None
        self.tag_id = None
        self.running_id = None

    def build(self, argv, cwd=None):
        assert argv == ["docker", "compose", "build", "slt-striim"] and cwd == str(self.ctx)
        self.builds += 1
        self.embedded = sp._local_build_inputs(self.ctx)
        self.tag_id = f"sha256:image-{self.builds}"

    def query(self, argv):
        if argv[1:3] == ["container", "inspect"]:
            ok = self.running_id is not None
            return SimpleNamespace(stdout=f"{self.running_id}\n" if ok else "", returncode=0 if ok else 1)
        if argv[1:3] == ["image", "inspect"]:
            ok = self.tag_id is not None
            return SimpleNamespace(stdout=f"{self.tag_id}\n" if ok else "", returncode=0 if ok else 1)
        verb = argv[1]
        if verb == "images":
            return SimpleNamespace(stdout="img\n" if self.embedded is not None else "", returncode=0)
        if verb == "rm":
            return SimpleNamespace(stdout="", returncode=0)
        if verb == "create":
            return SimpleNamespace(stdout="cid\n", returncode=0)
        if verb == "cp":
            dest = Path(argv[3])
            for rel, data in self.embedded.items():
                (dest / rel).parent.mkdir(parents=True, exist_ok=True)
                (dest / rel).write_bytes(data)
            return SimpleNamespace(stdout="", returncode=0)
        pytest.fail(f"unexpected docker call {argv!r}")


def _retained_set_a(tmp_path, monkeypatch):
    """A build context whose image, local identity record AND running container are set A."""
    monkeypatch.setattr(sp, "_in_checkout", lambda: False)
    ctx = tmp_path / "ctx"
    (ctx / "images" / "striim" / "files").mkdir(parents=True)
    (ctx / "images" / "striim" / "Dockerfile").write_text("FROM x\nCOPY ./files /slt-build-inputs/files\n")
    (ctx / "images" / "striim" / "files" / "entrypoint.sh").write_text("#!/bin/sh\n")
    docker = _Docker(ctx)
    monkeypatch.setenv(ENV, str(_manifest(tmp_path / "a", b"A")))
    sp.ensure_image(ctx, run=docker.build, query_run=docker.query)
    assert docker.builds == 1
    docker.running_id = docker.tag_id
    return ctx, docker


def _wire(monkeypatch, plugin, ctx, docker, probes):
    events = []
    answers = iter(probes)
    monkeypatch.setattr(plugin, "probe_reachable", lambda *a, **k: next(answers, True))
    monkeypatch.setattr(plugin, "_STRIIM_DIR", ctx)
    monkeypatch.setattr(plugin, "_cluster_provision_lock", lambda: ctx.parent / "provision.lock")
    monkeypatch.setattr(plugin, "_striim5_running", lambda: True)
    monkeypatch.setattr(plugin, "_resolve_release", lambda config: {"STRIIM_VERSION": VERSION})
    monkeypatch.setattr(plugin, "_running_striim_version", lambda: VERSION)
    monkeypatch.setattr(plugin, "_make_progress", lambda config: ((lambda *a, **k: None), (lambda: None)))
    real_match, real_image, real_running = sp.image_inputs_match, sp.ensure_image, sp.running_image_reason
    monkeypatch.setattr(sp, "image_inputs_match",
                        lambda v, d, run=None: real_match(v, d, run=docker.query))
    monkeypatch.setattr(sp, "ensure_image",
                        lambda d, release=None, run=None, progress=None, query_run=None:
                        real_image(d, release, run=docker.build, progress=progress,
                                   query_run=docker.query))
    monkeypatch.setattr(sp, "running_image_reason",
                        lambda container, version, run=None: real_running(container, version,
                                                                          run=docker.query))

    def down(d, release=None, run=None):
        events.append("down")
        docker.running_id = None

    def up(d, release=None, run=None, progress=None):
        events.append("up")
        docker.running_id = docker.tag_id

    monkeypatch.setattr(sp, "cluster_down", down)
    monkeypatch.setattr(sp, "cluster_up", up)

    def stop(*args, **kwargs):
        raise RuntimeError("stopped after the reuse decision")

    monkeypatch.setattr(plugin, "StriimClient", SimpleNamespace(from_url=stop))
    return events


WARM = [True]                         # first probe answers: reachable cluster
BOOTING = [False, False, False, True]  # three failed probes, then the boot-wait succeeds
BRANCHES = pytest.mark.parametrize("probes", [WARM, BOOTING], ids=["warm", "boot-wait"])


@BRANCHES
def test_retained_set_a_with_valid_set_b_redeploys(tmp_path, monkeypatch, plugin, probes):
    ctx, docker = _retained_set_a(tmp_path, monkeypatch)
    events = _wire(monkeypatch, plugin, ctx, docker, probes)
    monkeypatch.setenv(ENV, str(_manifest(tmp_path / "b", b"B")))     # valid, same version
    config = SimpleNamespace()
    assert plugin._resolve_striim(config) is None
    assert events == ["down", "up"]                                   # not reused
    assert docker.builds == 2                                         # rebuilt from set B
    assert "stopped after the reuse decision" in config._slt_striim_reason


@BRANCHES
def test_same_verified_set_is_reused(tmp_path, monkeypatch, plugin, probes):
    ctx, docker = _retained_set_a(tmp_path, monkeypatch)              # manifest A stays current
    events = _wire(monkeypatch, plugin, ctx, docker, probes)
    config = SimpleNamespace()
    assert plugin._resolve_striim(config) is None
    assert events == [] and docker.builds == 1                        # positive control: reuse


@BRANCHES
@pytest.mark.parametrize("current,reason", [
    ("missing", "SLT_STRIIM_DEPS_MANIFEST is not set"),
    ("malformed", "is not valid JSON"),
])
def test_missing_or_malformed_current_manifest_refuses_reuse(tmp_path, monkeypatch, plugin,
                                                             probes, current, reason):
    ctx, docker = _retained_set_a(tmp_path, monkeypatch)
    events = _wire(monkeypatch, plugin, ctx, docker, probes)
    if current == "missing":
        monkeypatch.delenv(ENV)
    else:
        bad = tmp_path / "bad.json"
        bad.write_text("{not json")
        monkeypatch.setenv(ENV, str(bad))
    config = SimpleNamespace()
    assert plugin._resolve_striim(config) is None
    assert "cannot reuse the Docker cluster" in config._slt_striim_reason
    assert reason in config._slt_striim_reason
    assert events == [] and docker.builds == 1                        # neither reused nor rebuilt


# ---------------------------------------------------------------------------
# Review round 3, R3-1: the running container's image, not just the tag, must match
# ---------------------------------------------------------------------------

@BRANCHES
@pytest.mark.parametrize("running", ["image-a", "unavailable"])
def test_running_image_not_the_verified_tag_redeploys(tmp_path, monkeypatch, plugin, probes, running):
    ctx, docker = _retained_set_a(tmp_path, monkeypatch)              # container runs image A
    image_a = docker.running_id
    # Another stack builds valid set B at the same version: the shared tag now names image B.
    monkeypatch.setenv(ENV, str(_manifest(tmp_path / "b", b"B")))
    sp.ensure_image(ctx, run=docker.build, query_run=docker.query)
    assert docker.builds == 2 and docker.tag_id != image_a and docker.running_id == image_a
    if running == "unavailable":
        docker.running_id = None                                      # identity cannot be read
    events = _wire(monkeypatch, plugin, ctx, docker, probes)
    config = SimpleNamespace()
    assert plugin._resolve_striim(config) is None
    assert events == ["down", "up"]                                   # never reuses the old container
    assert docker.builds == 2                                         # tag B already verified
    assert docker.running_id == docker.tag_id


@BRANCHES
def test_running_image_equal_to_verified_tag_is_reused(tmp_path, monkeypatch, plugin, probes):
    ctx, docker = _retained_set_a(tmp_path, monkeypatch)
    assert docker.running_id == docker.tag_id                         # same immutable image
    events = _wire(monkeypatch, plugin, ctx, docker, probes)
    assert plugin._resolve_striim(SimpleNamespace()) is None
    assert events == [] and docker.builds == 1                        # positive control


@BRANCHES
def test_checkout_without_manifest_reuses_as_before(tmp_path, monkeypatch, plugin, probes):
    ctx, docker = _retained_set_a(tmp_path, monkeypatch)
    monkeypatch.setattr(sp, "_in_checkout", lambda: True)             # a clone, nothing set
    monkeypatch.delenv(ENV)
    events = _wire(monkeypatch, plugin, ctx, docker, probes)
    monkeypatch.setattr(sp, "running_image_reason",
                        lambda *a, **k: pytest.fail("identity check without the manifest"))
    config = SimpleNamespace()
    assert plugin._resolve_striim(config) is None
    assert events == [] and docker.builds == 1                        # reused, nothing verified
    assert "stopped after the reuse decision" in config._slt_striim_reason


@BRANCHES
def test_prefixed_stack_compares_its_own_image_tag(tmp_path, monkeypatch, plugin, probes):
    # The image is tagged image_ref(version), <prefix>-slt-striim:<version>. The unprefixed tag is
    # another image, so the identity check must never read it.
    monkeypatch.setenv("SLT_STACK_PREFIX", "lift")
    ctx, docker = _retained_set_a(tmp_path, monkeypatch)
    tag = sp.image_ref(VERSION)
    assert tag == f"lift-slt-striim:{VERSION}"
    answer = docker.query

    def query(argv):
        if argv[1:3] == ["image", "inspect"] and argv[-1] != tag:
            return SimpleNamespace(stdout="sha256:another-stacks-image\n", returncode=0)
        return answer(argv)

    docker.query = query
    events = _wire(monkeypatch, plugin, ctx, docker, probes)
    config = SimpleNamespace()
    assert plugin._resolve_striim(config) is None
    assert events == [] and docker.builds == 1                        # reused, not redeployed
    assert "stopped after the reuse decision" in config._slt_striim_reason
