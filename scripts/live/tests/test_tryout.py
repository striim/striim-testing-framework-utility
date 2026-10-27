"""Tryout jars (livetest.tryout): the setting, the replaced-jar resolution, the thin derived image, the
cluster's compose call, the reuse check, the in-container verification and the evidence record. Docker is
never reached: every docker call goes through an injected runner."""
import io
import json
import types
import zipfile
from pathlib import Path

import pytest

from livetest import evidence, infra, tryout
from livetest import striim_provision as sp

V = "5.4.2"
STOCK = f"slt-striim:{V}"
STOCK_ID = "sha256:" + "b" * 64
TRYOUT_ID = "sha256:" + "c" * 64


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for k in (tryout.TRYOUT_ENV, "SLT_STACK_PREFIX", "SLT_STRIIM_DEPS_MANIFEST", "SLT_TERADATA_JDBC"):
        monkeypatch.delenv(k, raising=False)


def _jar(path: Path, artifact="Platform", scm="a1b2c3d", extra_poms=(), body=b"patched") -> Path:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        if artifact:
            zf.writestr(f"META-INF/maven/com.webaction/{artifact}/pom.properties",
                        f"#Generated\nversion=1.0.0-SNAPSHOT\ngroupId=com.webaction\nartifactId={artifact}\n")
        for a in extra_poms:
            zf.writestr(f"META-INF/maven/org.other/{a}/pom.properties", f"artifactId={a}\n")
        if scm:
            zf.writestr("build.properties", f"build_number=7\nscm_version={scm}\n")
        zf.writestr("com/webaction/Patched.class", body)
    path.write_bytes(buf.getvalue())
    return path


def _ns(rc=0, stdout="", stderr=""):
    return types.SimpleNamespace(returncode=rc, stdout=stdout, stderr=stderr)


LISTING = (f"Platform-{V}.jar\nAgent-{V}.jar\nsqljdbc42.jar\n"
           f"slt-tryout-agent-lib\nPlatform-{V}.jar\nAgent-{V}.jar\n")


class FakeDocker:
    """Answers the captured docker calls tryout makes; records every argv."""

    def __init__(self, listing=LISTING, labels=None, container_image=TRYOUT_ID, images=None, sha=None):
        self.calls, self.listing, self.labels = [], listing, labels
        self.container_image = container_image
        self.images = {STOCK: STOCK_ID} if images is None else images
        self.sha = sha or {}

    def __call__(self, argv):
        self.calls.append(list(argv))
        if argv[:2] == ["docker", "rm"]:
            return _ns()
        if argv[:2] == ["docker", "run"]:
            return _ns(stdout=self.listing)
        if argv[:3] == ["docker", "image", "inspect"]:
            ref = argv[-1]
            return _ns(stdout=self.images[ref]) if ref in self.images else _ns(1, stderr="No such image")
        if argv[:3] == ["docker", "container", "inspect"]:
            return _ns(stdout=self.container_image)
        if argv[:2] == ["docker", "inspect"]:
            return _ns(stdout=json.dumps(self.labels)) if self.labels is not None else _ns(1)
        if argv[:2] == ["docker", "exec"]:
            container, path = argv[2], argv[-1]
            return _ns(stdout=f"{self.sha.get((container, path), '0' * 64)}  {path}\n")
        raise AssertionError(f"unexpected docker call {argv}")


# --- the setting ---------------------------------------------------------------------------------

def test_parse_reads_targets_paths_and_overrides():
    got = tryout.parse(" both:/j/Platform-1.0.0-SNAPSHOT.jar ,\nagent:/j/Agent-1.0.0-SNAPSHOT.jar=Agent-5.4.2.jar,")
    assert got == [("both", Path("/j/Platform-1.0.0-SNAPSHOT.jar"), None),
                   ("agent", Path("/j/Agent-1.0.0-SNAPSHOT.jar"), "Agent-5.4.2.jar")]


@pytest.mark.parametrize("value, why", [
    ("/j/Platform.jar", "TARGET:PATH"),                      # no target
    ("nodes:/j/Platform.jar", "TARGET:PATH"),                # unknown target
    ("server:j/Platform.jar", "absolute"),                   # relative path
    ("server:", "no jar path"),
    ("server:/j/P.jar=lib/Platform-5.4.2.jar", "plain jar file name"),
])
def test_parse_refuses_a_malformed_entry(value, why):
    with pytest.raises(tryout.TryoutError, match=why):
        tryout.parse(value)


def test_inspect_jar_reads_artifact_id_and_scm_version(tmp_path):
    jar = _jar(tmp_path / "Platform-1.0.0-SNAPSHOT.jar")
    assert tryout.inspect_jar(jar) == (("Platform",), "a1b2c3d")


def test_load_refuses_a_missing_jar_and_one_naming_nothing_to_replace(tmp_path):
    with pytest.raises(tryout.TryoutError, match="does not exist"):
        tryout.load(f"server:{tmp_path}/absent.jar")
    bare = _jar(tmp_path / "bare.jar", artifact=None)
    with pytest.raises(tryout.TryoutError, match="pom.properties"):
        tryout.load(f"server:{bare}")
    (t,) = tryout.load(f"server:{bare}=Platform-{V}.jar")       # the override names it instead
    assert t.override == f"Platform-{V}.jar" and t.artifact_ids == ()
    (tmp_path / "junk.jar").write_bytes(b"not a zip")
    with pytest.raises(tryout.TryoutError, match="not a readable jar"):
        tryout.load(f"server:{tmp_path / 'junk.jar'}")


def test_configured_reads_the_setting(tmp_path, monkeypatch):
    jar = _jar(tmp_path / "Platform-1.0.0-SNAPSHOT.jar")
    assert tryout.configured() == []
    monkeypatch.setenv(tryout.TRYOUT_ENV, f"both:{jar}")
    (t,) = tryout.configured()
    assert (t.path, t.target, t.scm_version) == (jar, "both", "a1b2c3d")
    assert t.sha256 == __import__("hashlib").sha256(jar.read_bytes()).hexdigest()


# --- which jar it replaces --------------------------------------------------------------------------

def _listing(server=(f"Platform-{V}.jar", f"Agent-{V}.jar"), agent=(f"Platform-{V}.jar", f"Agent-{V}.jar")):
    return {tryout.SERVER_LIB: set(server), tryout.AGENT_LIB: set(agent)}


def test_resolve_replaces_the_installed_jar_of_the_same_artifact(tmp_path):
    (t,) = tryout.load(f"both:{_jar(tmp_path / 'Platform-1.0.0-SNAPSHOT.jar')}")
    (rec,) = tryout.resolve([t], V, _listing())
    assert rec == {"file": "Platform-1.0.0-SNAPSHOT.jar", "sha256": "sha256:" + t.sha256, "target": "both",
                   "replaces": f"Platform-{V}.jar", "scmVersion": "a1b2c3d"}


def test_a_tryout_matching_no_installed_jar_fails_loudly(tmp_path):
    (t,) = tryout.load(f"server:{_jar(tmp_path / 'New-1.0.jar', artifact='Brandnew')}")
    with pytest.raises(tryout.TryoutError, match=f"matches no jar in {tryout.SERVER_LIB}.*Brandnew-{V}.jar"):
        tryout.resolve([t], V, _listing())


def test_both_needs_the_jar_in_both_lib_dirs(tmp_path):
    (t,) = tryout.load(f"both:{_jar(tmp_path / 'Platform-1.0.0-SNAPSHOT.jar')}")
    with pytest.raises(tryout.TryoutError, match=f"no jar in {tryout.AGENT_LIB}"):
        tryout.resolve([t], V, _listing(agent=("other.jar",)))
    (rec,) = tryout.resolve(tryout.load(f"server:{t.path}"), V, _listing(agent=("other.jar",)))
    assert rec["replaces"] == f"Platform-{V}.jar"


def test_the_override_wins_and_must_exist(tmp_path):
    jar = _jar(tmp_path / "Platform-1.0.0-SNAPSHOT.jar")
    (rec,) = tryout.resolve(tryout.load(f"server:{jar}=Agent-{V}.jar"), V, _listing())
    assert rec["replaces"] == f"Agent-{V}.jar"
    with pytest.raises(tryout.TryoutError, match="matches no jar"):
        tryout.resolve(tryout.load(f"server:{jar}=Absent-{V}.jar"), V, _listing())


def test_a_shaded_jar_matching_two_installed_jars_needs_the_override(tmp_path):
    jar = _jar(tmp_path / "Platform-1.0.0-SNAPSHOT.jar", extra_poms=("Agent",))
    with pytest.raises(tryout.TryoutError, match="several jars"):
        tryout.resolve(tryout.load(f"server:{jar}"), V, _listing())
    # A shaded dependency that is not installed does not count.
    jar2 = _jar(tmp_path / "P2.jar", extra_poms=("guava",))
    (rec,) = tryout.resolve(tryout.load(f"server:{jar2}"), V, _listing())
    assert rec["replaces"] == f"Platform-{V}.jar"


def test_two_tryouts_cannot_replace_the_same_jar(tmp_path):
    a = _jar(tmp_path / "a.jar")
    b = _jar(tmp_path / "b.jar", body=b"other")
    with pytest.raises(tryout.TryoutError, match="both replace"):
        tryout.resolve(tryout.load(f"server:{a},both:{b}"), V, _listing())


# --- the derived image ---------------------------------------------------------------------------

def test_the_tag_is_the_stock_tag_plus_a_hash_of_the_tryout_set(tmp_path):
    jar = _jar(tmp_path / "Platform-1.0.0-SNAPSHOT.jar")
    ref = tryout.image_ref(STOCK, tryout.load(f"both:{jar}"))
    assert ref.startswith(f"{STOCK}-tryout-") and len(ref) == len(STOCK) + len("-tryout-") + 12
    assert tryout.image_ref(STOCK, tryout.load(f"server:{jar}")) != ref          # the target counts
    _jar(jar, body=b"rebuilt")
    assert tryout.image_ref(STOCK, tryout.load(f"both:{jar}")) != ref            # so do the bytes
    assert tryout.strip_tag(ref.rsplit(":", 1)[1]) == V and tryout.strip_tag(V) == V


def test_the_dockerfile_only_overwrites_installed_jars_under_their_own_names(tmp_path):
    ts = tryout.load(f"both:{_jar(tmp_path / 'Platform-1.0.0-SNAPSHOT.jar')},"
                     f"agent:{_jar(tmp_path / 'Agent-1.0.0-SNAPSHOT.jar', artifact='Agent')}")
    text = tryout.dockerfile(STOCK, ts, tryout.resolve(ts, V, _listing()))
    lines = text.splitlines()
    assert lines[0] == f"FROM {STOCK}"
    guard = f"RUN test -f /opt/striim/lib/Platform-{V}.jar && test -f /opt/striim/agent/lib/Platform-{V}.jar " \
            f"&& test -f /opt/striim/agent/lib/Agent-{V}.jar"
    assert lines[1] == guard                                     # before any COPY: replace, never add
    copies = [ln for ln in lines if ln.startswith("COPY --chown")]
    assert copies == [f"COPY --chown=striim:striim tryout-0.jar /opt/striim/lib/Platform-{V}.jar",
                      f"COPY --chown=striim:striim tryout-0.jar /opt/striim/agent/lib/Platform-{V}.jar",
                      f"COPY --chown=striim:striim tryout-1.jar /opt/striim/agent/lib/Agent-{V}.jar"]
    assert "SNAPSHOT" not in "".join(copies)


def test_build_derives_from_the_stock_image_with_its_own_tag_and_labels(tmp_path):
    jar = _jar(tmp_path / "Platform-1.0.0-SNAPSHOT.jar")
    ts = tryout.load(f"both:{jar}")
    docker, built = FakeDocker(), {}

    def stream(argv):
        ctx = Path(argv[-1])
        built.update(argv=argv, dockerfile=(ctx / "Dockerfile").read_text(),
                     jar=(ctx / "tryout-0.jar").read_bytes(), records=json.loads((ctx / "tryouts.json").read_text()))
        return _ns()
    ref, records = tryout.build(STOCK, V, ts, run=docker, stream=stream)
    assert ref == tryout.image_ref(STOCK, ts)
    argv = built["argv"]
    assert argv[:2] == ["docker", "build"] and argv[argv.index("-t") + 1] == ref
    labels = dict(a.split("=", 1) for a in argv[argv.index("-t"):] if "=" in a and a.startswith("org."))
    assert labels[tryout.LABEL_BASE] == STOCK_ID and labels[tryout.LABEL_SET] == tryout.set_digest(ts)
    assert json.loads(labels[tryout.LABEL_RECORDS]) == records == built["records"]
    assert built["dockerfile"].startswith(f"FROM {STOCK}\n") and built["jar"] == jar.read_bytes()
    listing = next(i for i, c in enumerate(docker.calls) if c[:2] == ["docker", "run"])
    assert docker.calls[listing - 1][:3] == ["docker", "rm", "-f"]       # a leaked probe is cleared first
    assert docker.calls[listing][docker.calls[listing].index("--entrypoint") + 2] == STOCK
    assert not any(c[:3] == ["docker", "rm", "-f"] and c[-1] == STOCK for c in docker.calls)   # stock untouched


def test_build_refuses_before_building_when_nothing_matches(tmp_path):
    ts = tryout.load(f"server:{_jar(tmp_path / 'X.jar', artifact='Nothing')}")
    with pytest.raises(tryout.TryoutError, match="matches no jar"):
        tryout.build(STOCK, V, ts, run=FakeDocker(), stream=lambda a: pytest.fail("built anyway"))


def test_a_listing_that_times_out_is_an_error_not_a_hang(tmp_path):
    ts = tryout.load(f"server:{_jar(tmp_path / 'P.jar')}")

    def slow(argv):
        if argv[:2] == ["docker", "run"]:
            raise __import__("subprocess").TimeoutExpired(argv, tryout.LIST_TIMEOUT_S)
        return FakeDocker()(argv)
    with pytest.raises(tryout.TryoutError, match="timed out"):
        tryout.build(STOCK, V, ts, run=slow, stream=lambda a: pytest.fail("built anyway"))


# --- provisioning ---------------------------------------------------------------------------------

def _checkout(tmp_path):
    root = tmp_path / "images" / "striim"
    (root / "files").mkdir(parents=True)
    (root / "Dockerfile").write_bytes(b"FROM scratch\n")
    return tmp_path


def test_ensure_image_leaves_a_current_stock_image_alone_and_builds_the_tryout_image(tmp_path, monkeypatch):
    d = _checkout(tmp_path)
    monkeypatch.setattr(sp, "image_present", lambda v, run=None: True)
    monkeypatch.setattr(sp, "image_inputs_match", lambda v, sd, run=None: True)
    monkeypatch.setenv(tryout.TRYOUT_ENV, f"both:{_jar(tmp_path / 'Platform-1.0.0-SNAPSHOT.jar')}")
    builds = []
    ref = sp.ensure_image(d, {"STRIIM_VERSION": V}, run=lambda argv, cwd=None: builds.append(argv),
                          query_run=FakeDocker())
    assert [b[:2] for b in builds] == [["docker", "build"]]           # no `compose build` of the stock image
    assert ref == sp.cluster_image_ref(V) and ref.startswith(f"{STOCK}-tryout-")


def test_with_the_setting_off_ensure_image_returns_the_stock_image_and_builds_nothing(tmp_path, monkeypatch):
    d = _checkout(tmp_path)
    monkeypatch.setattr(sp, "image_present", lambda v, run=None: True)
    monkeypatch.setattr(sp, "image_inputs_match", lambda v, sd, run=None: True)
    ref = sp.ensure_image(d, {"STRIIM_VERSION": V}, run=lambda argv, cwd=None: pytest.fail("built"),
                          query_run=lambda argv: pytest.fail(f"asked docker {argv}"))
    assert ref == STOCK == sp.cluster_image_ref(V)


def test_cluster_up_runs_the_tryout_image_and_never_builds(tmp_path, monkeypatch):
    jar = _jar(tmp_path / "Platform-1.0.0-SNAPSHOT.jar")
    monkeypatch.setenv(tryout.TRYOUT_ENV, f"both:{jar}")
    ref = sp.cluster_image_ref(V)
    seen = {}

    def run(argv, cwd=None):
        override = Path(argv[argv.index("-f", argv.index("compose.spanner-emulator.yaml")) + 1])
        seen.update(argv=argv, override=override.read_text())
    sp.cluster_up(tmp_path, {"STRIIM_VERSION": V}, run=run,
                  query_run=FakeDocker(images={ref: TRYOUT_ID}))
    argv = seen["argv"]
    assert argv[argv.index("up"):argv.index("up") + 3] == ["up", "-d", "--no-build"]
    for service in ("slt-striim", "slt-node", "slt-agent"):
        assert f"  {service}:\n    image: \"{ref}\"\n" in seen["override"]


def test_cluster_up_refuses_a_missing_tryout_image(tmp_path, monkeypatch):
    monkeypatch.setenv(tryout.TRYOUT_ENV, f"both:{_jar(tmp_path / 'P.jar')}")
    with pytest.raises(tryout.TryoutError, match="not built"):
        sp.cluster_up(tmp_path, {"STRIIM_VERSION": V}, run=lambda *a, **k: pytest.fail("compose ran"),
                      query_run=FakeDocker(images={}))


def test_cluster_up_without_tryouts_is_unchanged(tmp_path):
    seen = []
    sp.cluster_up(tmp_path, {"STRIIM_VERSION": V}, run=lambda argv, cwd=None: seen.append(argv))
    assert seen == [["docker", "compose", "-f", "compose.yaml", "-f", "compose.spanner-emulator.yaml",
                     "up", "-d", "--remove-orphans", "slt-striim", "slt-node", "slt-agent"]]


# --- a running cluster --------------------------------------------------------------------------

def test_turning_the_setting_off_redeploys_a_tryout_cluster():
    docker = FakeDocker(labels={tryout.LABEL_SET: "f" * 64})
    assert "unset" in tryout.running_reason("slt-striim", STOCK, [], run=docker)
    assert tryout.running_reason("slt-striim", STOCK, [], run=FakeDocker(labels={})) is None
    assert tryout.running_reason("slt-striim", STOCK, [], run=FakeDocker(labels=None)) is None


def test_a_running_cluster_must_run_this_tryout_set_on_the_current_stock_image(tmp_path):
    ts = tryout.load(f"both:{_jar(tmp_path / 'P.jar')}")
    ref = tryout.image_ref(STOCK, ts)
    good = {tryout.LABEL_SET: tryout.set_digest(ts), tryout.LABEL_BASE: STOCK_ID}
    images = {STOCK: STOCK_ID, ref: TRYOUT_ID}
    assert tryout.running_reason("c", STOCK, ts, run=FakeDocker(labels=good, images=images)) is None
    assert "does not run the tryout jars" in tryout.running_reason("c", STOCK, ts,
                                                                     run=FakeDocker(labels={}, images=images))
    assert "is not" in tryout.running_reason("c", STOCK, ts, run=FakeDocker(
        labels=good, images=images, container_image=STOCK_ID))
    assert "older" in tryout.running_reason("c", STOCK, ts, run=FakeDocker(
        labels={**good, tryout.LABEL_BASE: "sha256:" + "0" * 64}, images=images))
    assert "cannot be inspected" in tryout.running_reason("c", STOCK, ts, run=FakeDocker(labels=None))


def test_the_redeploy_decision_carries_the_tryout_reason(tmp_path, monkeypatch):
    from livetest import plugin
    monkeypatch.setattr(plugin._sp, "image_inputs_match", lambda v, d: True)
    monkeypatch.setattr(plugin._sp, "tryout_reason", lambda v: "the running cluster does not run the tryout jars")
    assert plugin._redeploy_reason(V, V, Path("/svc")) is None              # setting off: never inspected
    monkeypatch.setenv(tryout.TRYOUT_ENV, f"both:{_jar(tmp_path / 'P.jar')}")
    assert "does not run the tryout jars" in plugin._redeploy_reason(V, V, Path("/svc"))
    assert "does not run the tryout jars" in plugin._redeploy_reason(V, V, Path("/svc"), "shared")
    monkeypatch.setattr(plugin._sp, "tryout_reason", lambda v: None)
    assert plugin._redeploy_reason(f"{V}-tryout-0123456789ab", V, Path("/svc")) is None


def test_turning_the_setting_off_redeploys_a_running_tryout_cluster_from_its_tag(monkeypatch):
    from livetest import plugin
    monkeypatch.setattr(plugin._sp, "image_inputs_match", lambda v, d: True)
    monkeypatch.setattr(plugin._sp, "tryout_reason", lambda v: pytest.fail("inspected a stock run"))
    assert "unset" in plugin._redeploy_reason(f"{V}-tryout-0123456789ab", V, Path("/svc"))
    assert "5.4.0.8" in plugin._redeploy_reason("5.4.0.8-tryout-0123456789ab", V, Path("/svc"))   # version first
    assert plugin._redeploy_reason(V, V, Path("/svc")) is None


def test_candidate_reuse_compares_the_container_with_the_tryout_image(tmp_path, monkeypatch):
    monkeypatch.setenv(tryout.TRYOUT_ENV, f"both:{_jar(tmp_path / 'P.jar')}")
    ref = sp.cluster_image_ref(V)
    asked = []

    def run(argv):
        asked.append(argv[-1])
        return _ns(stdout=TRYOUT_ID)
    assert sp.running_image_reason("slt-striim", V, run=run) is None
    assert asked == ["slt-striim", ref]


# --- verification inside the containers -----------------------------------------------------

def _record(target, sha="d" * 64):
    return {"file": "Platform-1.0.0-SNAPSHOT.jar", "sha256": "sha256:" + sha, "target": target,
            "replaces": f"Platform-{V}.jar", "scmVersion": None}


@pytest.mark.parametrize("target, where", [
    ("server", [("slt-striim", tryout.SERVER_LIB), ("slt-node", tryout.SERVER_LIB)]),
    ("agent", [("slt-agent", tryout.AGENT_LIB)]),
    ("both", [("slt-striim", tryout.SERVER_LIB), ("slt-node", tryout.SERVER_LIB), ("slt-agent", tryout.AGENT_LIB)]),
])
def test_verify_checks_every_container_the_jar_went_to(target, where):
    sha = {(c, f"{d}/Platform-{V}.jar"): "d" * 64 for c, d in where}
    docker = FakeDocker(sha=sha)
    checks = tryout.verify([_record(target)], run=docker)
    assert [(c["container"], c["path"]) for c in checks] == [(c, f"{d}/Platform-{V}.jar") for c, d in where]
    assert [a[:3] for a in docker.calls] == [["docker", "exec", c] for c, _ in where]


def test_verify_refuses_a_mismatch_or_a_container_that_cannot_answer():
    sha = {("slt-striim", f"{tryout.SERVER_LIB}/Platform-{V}.jar"): "d" * 64}       # the node still has stock bytes
    with pytest.raises(tryout.TryoutError, match="slt-node"):
        tryout.verify([_record("server")], run=FakeDocker(sha=sha))
    with pytest.raises(tryout.TryoutError, match="exited 1"):
        tryout.verify([_record("agent")], run=lambda argv: _ns(1, stderr="No such container"))


def test_verify_honours_the_stack_prefix(monkeypatch):
    monkeypatch.setenv("SLT_STACK_PREFIX", "alt")
    docker = FakeDocker(sha={("alt-slt-agent", f"{tryout.AGENT_LIB}/Platform-{V}.jar"): "d" * 64})
    assert tryout.verify([_record("agent")], run=docker)[0]["container"] == "alt-slt-agent"


def _config(release=None):
    lines = []
    tr = types.SimpleNamespace(write_line=lambda line, **k: lines.append(line))
    config = types.SimpleNamespace(_slt_release=release or {"STRIIM_VERSION": V},
                                   pluginmanager=types.SimpleNamespace(getplugin=lambda name: tr))
    return config, lines


def test_after_cluster_start_the_tryouts_are_verified_recorded_and_announced(tmp_path, monkeypatch):
    from livetest import plugin
    ts = tryout.load(f"both:{_jar(tmp_path / 'Platform-1.0.0-SNAPSHOT.jar')}")
    rec = {**_record("both", ts[0].sha256), "scmVersion": "a1b2c3d"}
    monkeypatch.setattr(plugin._sp, "tryout_reason", lambda v: None)
    monkeypatch.setattr(plugin._tryout, "image_records", lambda ref: [rec])
    verified = []
    monkeypatch.setattr(plugin._tryout, "verify", lambda records: verified.extend(records) or [
        {"container": "slt-agent", "path": f"{tryout.AGENT_LIB}/Platform-{V}.jar", "sha256": rec["sha256"]}])
    config, lines = _config()
    assert plugin._verify_tryouts(config, ts) is None
    assert verified == [rec]
    assert config._slt_tryouts == [{**rec, "verifiedIn": [f"slt-agent:{tryout.AGENT_LIB}/Platform-{V}.jar"]}]
    assert "NOT the stock image" in lines[0] and "Platform-1.0.0-SNAPSHOT.jar (both) replaces" in lines[1]
    assert "a1b2c3d" in lines[1]


def test_a_failed_verification_is_reported_and_records_nothing(tmp_path, monkeypatch):
    from livetest import plugin
    ts = tryout.load(f"both:{_jar(tmp_path / 'P.jar')}")
    monkeypatch.setattr(plugin._sp, "tryout_reason", lambda v: None)
    monkeypatch.setattr(plugin._tryout, "image_records", lambda ref: [_record("both")])

    def bad(records):
        raise tryout.TryoutError("slt-node:/opt/striim/lib/Platform-5.4.2.jar has sha256:00")
    monkeypatch.setattr(plugin._tryout, "verify", bad)
    config, _ = _config()
    assert "not verified" in plugin._verify_tryouts(config, ts)
    assert not hasattr(config, "_slt_tryouts")
    monkeypatch.setattr(plugin._sp, "tryout_reason", lambda v: "the running cluster does not run the tryout jars")
    assert "does not run" in plugin._verify_tryouts(config, ts)
    config2, _ = _config()
    assert plugin._verify_tryouts(config2, []) is None and config2._slt_tryouts == []


def test_running_version_keeps_the_tryout_tag_for_the_redeploy_decision(monkeypatch):
    from livetest import plugin
    monkeypatch.setattr(plugin.subprocess, "run", lambda *a, **k: _ns(stdout=f"slt-striim:{V}-tryout-0123456789ab\n"))
    assert plugin._running_striim_version() == f"{V}-tryout-0123456789ab"


def test_a_malformed_setting_stops_pytest_configure(monkeypatch):
    from livetest import plugin
    monkeypatch.setenv(tryout.TRYOUT_ENV, "nowhere.jar")
    monkeypatch.setattr(plugin, "_activate_project", lambda: None)
    monkeypatch.setattr(plugin, "_collect_every_case_root", lambda config: None)
    monkeypatch.setattr(plugin.paths, "effective_env", lambda env: {})     # no .env leaks into os.environ
    config = types.SimpleNamespace(option=types.SimpleNamespace(numprocesses=None, dist=None))
    with pytest.raises(pytest.UsageError, match="TARGET:PATH"):
        plugin.pytest_configure(config)


# --- evidence ---------------------------------------------------------------------------------

def test_the_evidence_names_the_verified_tryouts(monkeypatch):
    rec = {**_record("both"), "verifiedIn": ["slt-striim:/opt/striim/lib/Platform-5.4.2.jar"]}
    assert infra.observe_tryouts(types.SimpleNamespace(_slt_tryouts=[rec])) == [rec]
    assert infra.observe_tryouts(types.SimpleNamespace()) == []                    # a stock run
    monkeypatch.setenv(tryout.TRYOUT_ENV, "both:/j/Platform-1.0.0-SNAPSHOT.jar")
    unverified = infra.observe_tryouts(types.SimpleNamespace(_slt_tryouts=None))
    assert set(unverified) == {"unobserved"}                                       # never reads as stock
    config = types.SimpleNamespace(_slt_release={"STRIIM_VERSION": V}, _slt_striim=None, _slt_tryouts=None)
    assert set(infra.observe_striim(config)["tryouts"]) == {"unobserved"}       # asked before verification
    config._slt_tryouts = [rec]
    assert infra.observe_striim(config)["tryouts"] == [rec]                     # not stuck on the first answer


def _envelope():
    path = Path(__file__).resolve().parent / "fixtures" / "evidence" / "contracts" / "evidence.case.valid.json"
    return json.loads(path.read_text())


def test_the_case_envelope_validates_tryout_records():
    doc = _envelope()
    evidence.validate_case(doc)                                     # written before tryouts: still valid
    rec = {**_record("server"), "verifiedIn": ["slt-striim:/opt/striim/lib/Platform-5.4.2.jar"]}
    for good in ([], [rec]):
        doc["runtime"]["striim"]["tryouts"] = good
        evidence.validate_case(doc)
    unverified = json.loads(json.dumps(doc))
    unverified["runtime"]["striim"]["tryouts"] = {"unobserved": "not verified"}
    with pytest.raises(evidence.EvidenceError, match="runtime.striim.tryouts"):
        evidence.validate_case(unverified)              # claims to qualify: refused
    unverified["run"].update(qualifies=False, qualifiesReason="not observed or unreadable: runtime.striim.tryouts")
    evidence.validate_case(unverified)
    for bad in ([{**rec, "verifiedIn": []}], [{**rec, "sha256": "d" * 64}], [{**rec, "target": "nodes"}],
                [{k: v for k, v in rec.items() if k != "replaces"}], "Platform.jar"):
        doc["runtime"]["striim"]["tryouts"] = bad
        with pytest.raises(evidence.EvidenceError):
            evidence.validate_case(doc)


def test_an_unverified_tryout_run_does_not_qualify():
    doc = _envelope()
    doc["runtime"]["striim"]["tryouts"] = {"unobserved": "not verified"}
    assert "runtime.striim.tryouts" in evidence.exact_view(doc)["blocked"]


def test_the_run_record_lists_the_distinct_tryouts():
    rec = {**_record("both"), "verifiedIn": ["slt-striim:/opt/striim/lib/Platform-5.4.2.jar"]}
    docs = [{"runtime": {"striim": {"tryouts": [rec]}}}, {"runtime": {"striim": {"tryouts": [rec]}}},
            {"runtime": {"striim": {"tryouts": {"unobserved": "x"}}}}]
    assert evidence.run_tryouts(docs) == [rec, {"unobserved": "x"}]
    assert evidence.run_tryouts([{"runtime": {"striim": {"tryouts": []}}}, {"runtime": {"striim": {}}}]) == []
