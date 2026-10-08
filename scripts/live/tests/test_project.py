"""Hermetic tests for the C1 project manifest resolver (ported from the legacy framework repo).

No Striim, no docker: the resolver is a pure filesystem + env contract (C1). The C1 contract
fixtures (tests/fixtures/project, copied from the legacy repo's migration/contracts/fixtures)
are exercised directly. Unset locations fall back to the livetest.paths defaults;
a set location that is missing raises.
"""
import json
import shutil
from pathlib import Path

import pytest

from livetest import layout, paths, project
from livetest.project import ProjectError

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "project"
NO_DOTENV = {}


@pytest.fixture(autouse=True)
def _clean():
    layout._reset()
    project._ACTIVE = None
    yield
    project._ACTIVE = None
    layout._reset()


def _manifest(tmp_path, text, name="gold-targets.yaml"):
    p = tmp_path / name
    p.write_text(text)
    return p


# ---------------------------------------------------------------------------
# Frozen 0.2 contract fixtures
# ---------------------------------------------------------------------------

def test_app_only_fixture_loads(tmp_path):
    p = _manifest(tmp_path, (FIXTURES / "manifest.app-only.valid.yaml").read_text())
    pr = project.load_project(p)
    assert pr.root == tmp_path.resolve()
    assert pr.targets == ()
    assert pr.suites["live"] == tmp_path.resolve() / "cases" / "live"
    assert pr.suites["integration"] == tmp_path.resolve() / "cases" / "integration"
    assert pr.services_roots == (tmp_path.resolve() / "services",)
    assert pr.state_dir == tmp_path.resolve() / ".state"
    assert pr.framework["mode"] == "wheel"
    assert pr.runners == ()


def test_op_maven_fixture_loads(tmp_path):
    p = _manifest(tmp_path, (FIXTURES / "manifest.op-maven.valid.yaml").read_text())
    pr = project.load_project(p)
    assert len(pr.targets) == 2
    op, udf = pr.targets
    assert (op.name, op.kind, op.build) == ("LookupOp", "op", "maven")
    assert op.path == tmp_path.resolve() / "java" / "OpenProcessors" / "LookupOp"
    assert (udf.name, udf.build) == ("ExampleJsonUdf", "prebuilt")
    assert udf.prebuilt_sha256 == \
        "3f2a9b1c4d5e6f708192a3b4c5d6e7f8091a2b3c4d5e6f708192a3b4c5d6e7f8"
    assert udf.prebuilt_jar == tmp_path.resolve() / "artifacts" / "json-mutator-1.0.0.jar"
    assert pr.suites["unit"] == tmp_path.resolve() / "tests" / "unit"
    assert len(pr.module_roots) == 2


def test_product_native_fixture_loads(tmp_path):
    p = _manifest(tmp_path, (FIXTURES / "manifest.product-native.valid.yaml").read_text())
    pr = project.load_project(p)
    assert pr.targets == ()
    assert pr.suites["live"] == tmp_path.resolve() / "cases" / "live"
    assert len(pr.runners) == 3
    assert pr.runners[0] == tmp_path.resolve() / "runners" / "unit-surefire.yaml"


@pytest.mark.parametrize("fixture,fragment", [
    ("manifest.invalid-unknown-field.yaml", "modules"),
    ("manifest.invalid-ambiguous-target.yaml", "LookupOp"),
    ("manifest.invalid-escaping-path.yaml", "escapes"),
    ("manifest.invalid-prebuilt-missing-hash.yaml", "sha256"),
    ("manifest.invalid-tier-boolean.yaml", "path string"),
], ids=["unknown-field", "ambiguous-target", "escaping-path", "prebuilt-missing-hash", "tier-boolean"])
def test_invalid_fixtures_rejected_offline(tmp_path, fixture, fragment):
    p = _manifest(tmp_path, (FIXTURES / fixture).read_text())
    with pytest.raises(ProjectError, match=fragment):
        project.load_project(p)


# ---------------------------------------------------------------------------
# Location and root
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("value", [None, "", "  "])
def test_no_manifest_means_no_project_and_defaults(monkeypatch, value):
    if value is None:
        monkeypatch.delenv(project.ENV_TARGETS, raising=False)
    else:
        monkeypatch.setenv(project.ENV_TARGETS, value)
    assert project.load_project() is None
    assert project.load_and_activate() is None
    assert project._ACTIVE is None
    assert layout.services_roots(env={}, dotenv=NO_DOTENV) == (paths.services_dir({}, NO_DOTENV),)
    assert layout.state_dir(env={}, dotenv=NO_DOTENV) == paths.state_dir({}, NO_DOTENV)


def test_no_manifest_clears_a_previous_project(tmp_path, monkeypatch):
    m = _manifest(tmp_path, "schemaVersion: 1\ntargets: []\nsuites:\n  live: .\n"
                            "servicesRoots:\n  - .\nstateDir: .state\n")
    project.load_and_activate(m)
    assert layout.planned_state_dir(env={}, dotenv=NO_DOTENV) == tmp_path.resolve() / ".state"
    monkeypatch.delenv(project.ENV_TARGETS, raising=False)
    assert project.load_and_activate() is None
    assert project._ACTIVE is None
    assert layout.planned_state_dir(env={}, dotenv=NO_DOTENV) is None
    assert layout.services_roots(env={}, dotenv=NO_DOTENV) == (paths.services_dir({}, NO_DOTENV),)


def test_gold_targets_set_but_missing_raises_naming_the_key(tmp_path, monkeypatch):
    monkeypatch.setenv(project.ENV_TARGETS, str(tmp_path / "nope.yaml"))
    with pytest.raises(paths.PathConfigError, match="GOLD_TARGETS"):
        project.load_project()


def test_missing_manifest_file_fails_explicitly(tmp_path):
    with pytest.raises(ProjectError, match="not found"):
        project.load_project(tmp_path / "nope.yaml")


def test_env_var_locates_manifest(tmp_path, monkeypatch):
    m = _manifest(tmp_path, "schemaVersion: 1\ntargets: []\nsuites:\n  live: .\n")
    monkeypatch.setenv(project.ENV_TARGETS, str(m))
    pr = project.load_project()
    assert pr.manifest == m.resolve()
    assert pr.root == tmp_path.resolve()


def test_resolution_ignores_invocation_cwd(tmp_path, monkeypatch):
    consumer = tmp_path / "consumer"
    (consumer / "cases").mkdir(parents=True)
    m = consumer / "gold-targets.yaml"
    m.write_text("schemaVersion: 1\ntargets: []\nsuites:\n  live: cases\n")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    pr = project.load_project(m)
    assert pr.root == consumer.resolve()
    assert pr.suites["live"] == consumer.resolve() / "cases"
    assert project.asset_root(pr, ".") == consumer.resolve()


def test_relocated_copy_resolves_manifest_relative(tmp_path):
    a = tmp_path / "a"
    (a / "cases").mkdir(parents=True)
    m = a / "gold-targets.yaml"
    m.write_text("schemaVersion: 1\ntargets: []\nsuites:\n  live: cases\n")
    b = tmp_path / "b"
    shutil.copytree(a, b)
    pr_b = project.load_project(b / "gold-targets.yaml")
    assert pr_b.root == b.resolve()
    assert pr_b.suites["live"] == b.resolve() / "cases"
    assert project.asset_root(pr_b, ".") == b.resolve()


# ---------------------------------------------------------------------------
# Schema strictness
# ---------------------------------------------------------------------------

def test_schema_version_required(tmp_path):
    p = _manifest(tmp_path, "targets: []\nsuites:\n  live: .\n")
    with pytest.raises(ProjectError, match="schemaVersion"):
        project.load_project(p)


def test_schema_version_mismatch_rejected(tmp_path):
    p = _manifest(tmp_path, "schemaVersion: 2\ntargets: []\nsuites:\n  live: .\n")
    with pytest.raises(ProjectError, match="unsupported schemaVersion"):
        project.load_project(p)


def test_framework_mode_value_checked(tmp_path):
    p = _manifest(tmp_path,
                  "schemaVersion: 1\nframework:\n  mode: quantum\n"
                  "targets: []\nsuites:\n  live: .\n")
    with pytest.raises(ProjectError, match="framework.mode"):
        project.load_project(p)


def test_wheel_mode_requires_wheel_and_lock(tmp_path):
    p = _manifest(tmp_path,
                  "schemaVersion: 1\nframework:\n  mode: wheel\n"
                  "targets: []\nsuites:\n  live: .\n")
    with pytest.raises(ProjectError, match="requires framework"):
        project.load_project(p)


def test_unknown_field_rejected_naming_field(tmp_path):
    p = _manifest(tmp_path,
                  "schemaVersion: 1\ntargets: []\nmodules:\n  - X\nsuites:\n  live: .\n")
    with pytest.raises(ProjectError, match=r"modules") as ei:
        project.load_project(p)
    assert "allowed" in str(ei.value)


def test_app_only_requires_live_or_integration_suite(tmp_path):
    p = _manifest(tmp_path, "schemaVersion: 1\ntargets: []\nsuites:\n  unit: tests\n")
    with pytest.raises(ProjectError, match="suites.live"):
        project.load_project(p)


def test_target_kind_invalid(tmp_path):
    p = _manifest(tmp_path,
                  "schemaVersion: 1\ntargets:\n  - {name: X, kind: widget}\n"
                  "suites:\n  live: .\n")
    with pytest.raises(ProjectError, match="kind"):
        project.load_project(p)


def test_op_requires_explicit_build(tmp_path):
    p = _manifest(tmp_path,
                  "schemaVersion: 1\ntargets:\n"
                  "  - {name: X, kind: op, path: m}\nsuites:\n  live: .\n")
    with pytest.raises(ProjectError, match="build is required"):
        project.load_project(p)


def test_app_build_must_be_none(tmp_path):
    p = _manifest(tmp_path,
                  "schemaVersion: 1\ntargets:\n"
                  "  - {name: X, kind: app, build: maven}\nsuites:\n  live: .\n")
    with pytest.raises(ProjectError, match="not allowed"):
        project.load_project(p)


def test_prebuilt_sha256_must_be_64_hex(tmp_path):
    p = _manifest(tmp_path,
                  "schemaVersion: 1\ntargets:\n"
                  "  - name: X\n    kind: udf\n    path: m\n    build: prebuilt\n"
                  "    prebuilt:\n      jar: a.jar\n      sha256: ABC\n"
                  "suites:\n  live: .\n")
    with pytest.raises(ProjectError, match="64 lowercase hex"):
        project.load_project(p)


# ---------------------------------------------------------------------------
# Path policy
# ---------------------------------------------------------------------------

def test_absolute_target_path_rejected(tmp_path):
    p = _manifest(tmp_path,
                  "schemaVersion: 1\ntargets:\n"
                  "  - {name: X, kind: op, path: /etc/passwd, build: maven}\n"
                  "suites:\n  live: .\n")
    with pytest.raises(ProjectError, match="manifest-relative"):
        project.load_project(p)


def test_symlink_escape_rejected(tmp_path):
    outside = tmp_path / "outside"
    (outside / "evil").mkdir(parents=True)
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    (consumer / "link").symlink_to(outside)
    m = consumer / "gold-targets.yaml"
    m.write_text("schemaVersion: 1\ntargets:\n"
                 "  - {name: X, kind: op, path: link/evil, build: maven}\n"
                 "suites:\n  live: .\n")
    with pytest.raises(ProjectError, match="escapes"):
        project.load_project(m)


# ---------------------------------------------------------------------------
# Environment expansion
# ---------------------------------------------------------------------------

def test_expansion_rejected_outside_allowed_fields(tmp_path):
    p = _manifest(tmp_path, "schemaVersion: 1\ntargets: []\nsuites:\n  live: $HOME/cases\n")
    with pytest.raises(ProjectError, match="expansion"):
        project.load_project(p)


def test_services_roots_env_expansion(tmp_path, monkeypatch):
    svc = tmp_path / "svc"
    svc.mkdir()
    monkeypatch.setenv("SLT_TEST_SVC_ROOT", str(svc))
    p = _manifest(tmp_path,
                  "schemaVersion: 1\ntargets: []\nsuites:\n  live: .\n"
                  "servicesRoots:\n  - $SLT_TEST_SVC_ROOT\n")
    pr = project.load_project(p)
    assert pr.services_roots == (svc.resolve(),)


@pytest.mark.parametrize("value", [None, ""])
def test_unset_env_var_falls_back_to_the_default(tmp_path, monkeypatch, value):
    # An unset ${VAR} leaves that location unset, so it falls back.
    for name in ("SLT_NOPE_VAR", "SLT_NOPE_STATE"):
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)
    keep = tmp_path / "keep"
    keep.mkdir()
    monkeypatch.setenv("SLT_TEST_KEEP", str(keep))
    p = _manifest(tmp_path,
                  "schemaVersion: 1\ntargets: []\nsuites:\n  live: .\n"
                  "servicesRoots:\n  - $SLT_NOPE_VAR\n  - ${SLT_TEST_KEEP}\n"
                  "stateDir: ${SLT_NOPE_STATE}/state\n")
    pr = project.load_and_activate(p)
    assert pr.services_roots == (keep.resolve(),)
    assert pr.state_dir is None
    assert layout.services_roots(env={}, dotenv=NO_DOTENV) == (
        keep.resolve(), paths.services_dir({}, NO_DOTENV))
    assert layout.state_dir(env={}, dotenv=NO_DOTENV) == paths.state_dir({}, NO_DOTENV)


def test_unset_path_key_expansion_falls_back(tmp_path, monkeypatch):
    # The paths.KEYS branch of the expansion: an unset SLT_STATE_DIR leaves stateDir unset.
    monkeypatch.delenv("SLT_STATE_DIR", raising=False)
    p = _manifest(tmp_path,
                  "schemaVersion: 1\ntargets: []\nsuites:\n  live: .\n"
                  "stateDir: ${SLT_STATE_DIR}\n")
    pr = project.load_and_activate(p)
    assert pr.state_dir is None
    assert layout.state_dir(env={}, dotenv=NO_DOTENV) == paths.state_dir({}, NO_DOTENV)


def test_expansion_reads_a_path_key_from_dotenv(tmp_path, monkeypatch):
    # A livetest.paths key referenced in the manifest resolves like the engine resolves it:
    # the process environment first, then <project root>/.env.
    root, st = tmp_path / "root", tmp_path / "st"
    root.mkdir(); st.mkdir()
    (root / ".env").write_text(f"SLT_STATE_DIR={st}\n")
    monkeypatch.delenv("SLT_STATE_DIR", raising=False)
    monkeypatch.setenv("SLT_PROJECT_ROOT", str(root))
    # conftest stubs the .env layer out of hermetic tests; this one is about that layer
    monkeypatch.setattr(paths, "dotenv_values",
                        lambda env=None: paths.read_dotenv(paths.dotenv_path(env)))
    p = _manifest(tmp_path, "schemaVersion: 1\ntargets: []\nsuites:\n  live: .\n"
                            "stateDir: ${SLT_STATE_DIR}/run\n")
    assert project.load_project(p).state_dir == st.resolve() / "run"


CHECKOUT = Path(paths.__file__).resolve().parents[3]


@pytest.mark.parametrize("environ,dotenv,want", [
    (None, None, CHECKOUT / "svc"),          # absent everywhere: the running checkout
    ("", None, None),                        # explicitly empty: the entry is left out
    ("  ", None, None),                      # whitespace only: the same
    (None, "", None),                        # empty in .env: the same
    ("", "DOTENV", "DOTENV"),                # an empty environment value falls through to .env
    ("ENV", "DOTENV", "ENV"),                # the environment wins over .env
])
def test_framework_home_defaults_only_when_absent(tmp_path, monkeypatch, environ, dotenv, want):
    root, env_home, dot_home = tmp_path / "root", tmp_path / "env-home", tmp_path / "dot-home"
    root.mkdir()
    named = {"ENV": str(env_home), "DOTENV": str(dot_home)}
    if dotenv is not None:
        (root / ".env").write_text(f"SLT_FRAMEWORK_HOME={named.get(dotenv, dotenv)}\n")
    if environ is None:
        monkeypatch.delenv("SLT_FRAMEWORK_HOME", raising=False)
    else:
        monkeypatch.setenv("SLT_FRAMEWORK_HOME", named.get(environ, environ))
    monkeypatch.setenv("SLT_PROJECT_ROOT", str(root))
    monkeypatch.setattr(paths, "dotenv_values",
                        lambda env=None: paths.read_dotenv(paths.dotenv_path(env)))
    p = _manifest(tmp_path, "schemaVersion: 1\ntargets: []\nsuites:\n  live: .\n"
                            "servicesRoots:\n  - ${SLT_FRAMEWORK_HOME}/svc\n")
    want = {"ENV": env_home / "svc", "DOTENV": dot_home / "svc"}.get(want, want)
    assert project.load_project(p).services_roots == ((want.resolve(),) if want else ())


@pytest.mark.parametrize("value,want", [(None, True), ("", False), ("  ", False)])
def test_framework_home_default_through_an_explicit_env(tmp_path, value, want):
    # The env= form (no process environment, no .env) draws the same line.
    p = _manifest(tmp_path, "schemaVersion: 1\ntargets: []\nsuites:\n  live: .\n"
                            "servicesRoots:\n  - ${SLT_FRAMEWORK_HOME}/svc\n")
    env = {} if value is None else {"SLT_FRAMEWORK_HOME": value}
    roots = project.load_project(p, env=env).services_roots
    assert roots == ((CHECKOUT / "svc",) if want else ())


def test_state_dir_inside_install_tree_rejected(tmp_path):
    p = _manifest(tmp_path,
                  "schemaVersion: 1\ntargets: []\nsuites:\n  live: .\n"
                  "stateDir: /opt/venv/lib/python3.11/site-packages/slt\n")
    with pytest.raises(ProjectError, match="install tree"):
        project.load_project(p)


def test_state_dir_absolute_allowed_outside_install(tmp_path):
    sd = tmp_path / "state"
    p = _manifest(tmp_path,
                  f"schemaVersion: 1\ntargets: []\nsuites:\n  live: .\nstateDir: {sd}\n")
    pr = project.load_project(p)
    assert pr.state_dir == sd.resolve()


# ---------------------------------------------------------------------------
# apply_project: roots wiring and sequential consumers
# ---------------------------------------------------------------------------

def test_apply_project_wires_roots_and_is_idempotent(tmp_path):
    m = _manifest(tmp_path,
                  "schemaVersion: 1\ntargets: []\nsuites:\n  live: .\n"
                  "servicesRoots:\n  - services\nstateDir: .state\n")
    project.load_and_activate(m)
    # Manifest values live in their OWN slot, distinct from the
    # explicit set_roots(...) seam (so an explicit per-run dir can still win).
    assert layout.planned_state_dir(env={}, dotenv=NO_DOTENV) == tmp_path.resolve() / ".state"   # explicit seam untouched
    assert (tmp_path.resolve() / "services") in layout.services_roots(env={}, dotenv=NO_DOTENV)
    assert layout.state_dir() == tmp_path.resolve() / ".state"
    project.load_and_activate(m)
    assert layout.planned_state_dir(env={}, dotenv=NO_DOTENV) == tmp_path.resolve() / ".state"


@pytest.mark.parametrize("entry", [".", "services"])
def test_apply_project_drops_a_root_that_expands_to_the_base_services_dir(tmp_path, monkeypatch, entry):
    # SLT_SERVICES_DIR points the base at the consumer's own tree; a manifest root naming that
    # tree, directly or as the <root>/services expansion, is dropped rather than refused.
    (tmp_path / "services").mkdir()
    monkeypatch.setenv("SLT_SERVICES_DIR", str(tmp_path / "services"))
    m = _manifest(tmp_path,
                  "schemaVersion: 1\ntargets: []\nsuites:\n  live: .\n"
                  f"servicesRoots:\n  - {entry}\n")
    project.load_and_activate(m)
    assert layout.services_roots() == ((tmp_path / "services").resolve(),)


def test_two_consumer_roots_sequentially_never_persist(tmp_path):
    a = tmp_path / "a"
    (a / "sa").mkdir(parents=True)
    ma = a / "gold-targets.yaml"
    ma.write_text("schemaVersion: 1\ntargets: []\nsuites:\n  live: .\n"
                  "servicesRoots:\n  - sa\nstateDir: .stateA\n")
    b = tmp_path / "b"
    (b / "sb").mkdir(parents=True)
    mb = b / "gold-targets.yaml"
    mb.write_text("schemaVersion: 1\ntargets: []\nsuites:\n  live: .\n"
                  "servicesRoots:\n  - sb\nstateDir: .stateB\n")
    project.load_and_activate(ma)
    assert (a.resolve() / "sa") in layout.services_roots(env={}, dotenv=NO_DOTENV)
    assert layout.planned_state_dir(env={}, dotenv=NO_DOTENV) == a.resolve() / ".stateA"
    project.load_and_activate(mb)
    # A's manifest roots must NOT persist into B.
    assert (a.resolve() / "sa") not in layout.services_roots(env={}, dotenv=NO_DOTENV)
    assert (a.resolve() / ".stateA") != layout.planned_state_dir(env={}, dotenv=NO_DOTENV)
    assert (b.resolve() / "sb") in layout.services_roots(env={}, dotenv=NO_DOTENV)
    assert layout.planned_state_dir(env={}, dotenv=NO_DOTENV) == b.resolve() / ".stateB"
    assert layout.state_dir() == b.resolve() / ".stateB"
    assert project.asset_root(project._ACTIVE, ".") == b.resolve()


def test_b_with_neither_field_clears_a_roots(tmp_path, monkeypatch):
    # A sets BOTH axes; B declares NEITHER -> B must inherit nothing from A.
    a = tmp_path / "a"
    (a / "sa").mkdir(parents=True)
    ma = a / "gold-targets.yaml"
    ma.write_text("schemaVersion: 1\ntargets: []\nsuites:\n  live: .\n"
                  "servicesRoots:\n  - sa\nstateDir: .stateA\n")
    project.load_and_activate(ma)
    assert (a.resolve() / "sa") in layout.services_roots()
    assert layout.state_dir() == a.resolve() / ".stateA"
    b = tmp_path / "b"
    b.mkdir()
    mb = b / "gold-targets.yaml"
    mb.write_text("schemaVersion: 1\ntargets: []\nsuites:\n  live: .\n")
    project.load_and_activate(mb)
    # A's service root must NOT leak into B.
    assert (a.resolve() / "sa") not in layout.services_roots()
    assert layout.planned_state_dir(env={}, dotenv=NO_DOTENV) is None
    # No manifest state -> the livetest.paths default, never A's dir.
    assert layout.state_dir(env={}, dotenv=NO_DOTENV) == paths.state_dir({}, NO_DOTENV)


def test_b_with_only_state_dir_clears_a_services(tmp_path):
    # A sets services + state; B sets ONLY state -> B's state wins, A's
    # services roots are cleared.
    a = tmp_path / "a"
    (a / "sa").mkdir(parents=True)
    ma = a / "gold-targets.yaml"
    ma.write_text("schemaVersion: 1\ntargets: []\nsuites:\n  live: .\n"
                  "servicesRoots:\n  - sa\nstateDir: .stateA\n")
    project.load_and_activate(ma)
    b = tmp_path / "b"
    b.mkdir()
    mb = b / "gold-targets.yaml"
    mb.write_text("schemaVersion: 1\ntargets: []\nsuites:\n  live: .\nstateDir: .stateB\n")
    project.load_and_activate(mb)
    assert layout.planned_state_dir(env={}, dotenv=NO_DOTENV) == b.resolve() / ".stateB"
    assert layout.state_dir() == b.resolve() / ".stateB"
    assert (a.resolve() / "sa") not in layout.services_roots()


# ---------------------------------------------------------------------------
# Frozen precedence (C6): explicit > manifest > env
# ---------------------------------------------------------------------------

def test_explicit_state_beats_manifest_state_dir(tmp_path):
    # activate first, THEN an explicit per-run state (both orders covered).
    m = _manifest(tmp_path,
                  "schemaVersion: 1\ntargets: []\nsuites:\n  live: .\nstateDir: .stateM\n")
    project.load_and_activate(m)
    assert layout.state_dir() == tmp_path.resolve() / ".stateM"
    explicit = tmp_path / "explicit"
    layout.set_roots(state=explicit)
    assert layout.state_dir() == explicit.resolve()


def test_explicit_state_set_before_activate_still_wins(tmp_path):
    explicit = tmp_path / "explicit"
    layout.set_roots(state=explicit)
    m = _manifest(tmp_path,
                  "schemaVersion: 1\ntargets: []\nsuites:\n  live: .\nstateDir: .stateM\n")
    # activating the manifest must NOT clobber the explicit per-run dir.
    project.load_and_activate(m)
    assert layout.state_dir() == explicit.resolve()
    assert layout._cfg_manifest_state == tmp_path.resolve() / ".stateM"


def test_manifest_state_beats_env(tmp_path, monkeypatch):
    monkeypatch.setenv("SLT_STATE_DIR", str(tmp_path / "envstate"))
    m = _manifest(tmp_path,
                  "schemaVersion: 1\ntargets: []\nsuites:\n  live: .\nstateDir: .stateM\n")
    project.load_and_activate(m)
    # C6: explicit > manifest > env. No explicit here, so manifest beats env.
    assert layout.state_dir() == tmp_path.resolve() / ".stateM"


def test_no_configured_state_falls_back_to_the_default():
    assert layout.state_dir(env={}, dotenv=NO_DOTENV) == paths.state_dir({}, NO_DOTENV)


def test_manifest_state_beats_a_missing_slt_state_dir(tmp_path):
    # The manifest's stateDir wins, so a stale SLT_STATE_DIR below it is never consulted.
    m = _manifest(tmp_path,
                  "schemaVersion: 1\ntargets: []\nsuites:\n  live: .\nstateDir: .stateM\n")
    project.load_and_activate(m)
    env = {"SLT_STATE_DIR": str(tmp_path / "gone")}
    assert layout.state_dir(env=env, dotenv=NO_DOTENV) == tmp_path.resolve() / ".stateM"


def test_uncreatable_state_dir_fails_explicitly(tmp_path):
    blocker = tmp_path / "blocker"
    blocker.write_text("a file, not a dir")
    m = _manifest(tmp_path,
                  "schemaVersion: 1\ntargets: []\nsuites:\n  live: .\n"
                  "stateDir: blocker/.state\n")
    pr = project.load_project(m)   # offline: loading never creates the state dir
    project.apply_project(pr)
    with pytest.raises((layout.LayoutError, OSError)):
        layout.state_dir()


# ---------------------------------------------------------------------------
# Legacy example: mapping
# ---------------------------------------------------------------------------

def test_example_root_project_mapping(tmp_path):
    consumer = tmp_path / "consumer"
    (consumer / "cases").mkdir(parents=True)
    m = consumer / "gold-targets.yaml"
    m.write_text("schemaVersion: 1\ntargets: []\nsuites:\n  live: cases\n")
    project.load_and_activate(m)
    assert project.example_root(".") == consumer.resolve()
    assert project.example_root(None) == consumer.resolve()
    assert project.example_root("cases") == consumer.resolve() / "cases"


def test_example_root_without_project_is_the_project_root(tmp_path, monkeypatch):
    fake_root = tmp_path / "checkout"
    fake_root.mkdir()
    monkeypatch.setenv("SLT_PROJECT_ROOT", str(fake_root))
    assert project.example_root("examples/foo") == (fake_root / "examples" / "foo").resolve()
    assert project.example_root(None) == fake_root.resolve()
    with pytest.raises(layout.LayoutError):
        project.example_root("..")


def test_example_root_default_is_todays_repo_root(monkeypatch):
    monkeypatch.delenv("SLT_PROJECT_ROOT", raising=False)
    assert project.example_root(None) == paths.project_root()


def test_example_root_missing_project_root_raises_naming_the_key(tmp_path, monkeypatch):
    monkeypatch.setenv("SLT_PROJECT_ROOT", str(tmp_path / "nope"))
    with pytest.raises(paths.PathConfigError, match="SLT_PROJECT_ROOT"):
        project.example_root("examples/foo")


# ---------------------------------------------------------------------------
# identity record (evidence; never secret-bearing values)
# ---------------------------------------------------------------------------

def test_identity_records_roots_and_env_names_only(tmp_path, monkeypatch):
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    m = consumer / "gold-targets.yaml"
    m.write_text("schemaVersion: 1\ntargets: []\nsuites:\n  live: .\nstateDir: .state\n")
    pr = project.load_and_activate(m)
    monkeypatch.setenv("SLT_SECRET_TOKEN", "super-secret-value")
    rec = project.identity(pr)
    dump = json.dumps(rec)
    assert "SLT_SECRET_TOKEN" in rec["env_names"]
    assert "super-secret-value" not in dump
    assert rec["consumer_root"] == str(consumer.resolve())
    assert rec["manifest"] == str(m.resolve())
    assert rec["state_dir"] == str(consumer.resolve() / ".state")


def test_builtin_services_is_the_paths_services_dir():
    assert layout.builtin_services({}, NO_DOTENV) == paths.services_dir({}, NO_DOTENV)
