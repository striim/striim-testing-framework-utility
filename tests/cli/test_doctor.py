"""striim-test doctor: every check has a passing and a failing case, and each failure names
exactly what is wrong. Probes (Striim auth, TCP, Postgres login, docker) are injected; nothing
here reaches a network service or Docker, except the two CLI runs, which run under the trap."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from _clikit import REPO, clone_env, run_cli

sys.path.insert(0, str(REPO / "scripts" / "cli"))

from striim_test import doctor  # noqa: E402
from striim_test.errors import CONFIG, INFRA, OK  # noqa: E402
from livetest import paths

REAL_DOTENV_VALUES = paths.dotenv_values


@pytest.fixture(autouse=True)
def _read_test_dotenv(monkeypatch):
    # These tests exercise .env loading; restore it after the root isolation fixture.
    monkeypatch.setattr(paths, "dotenv_values", REAL_DOTENV_VALUES)
    monkeypatch.setattr(paths, "machine_values", lambda *a, **k: {})

UP = lambda: (True, "Docker 29.0")                                    # noqa: E731
DOWN = lambda: (False, "docker not usable: [Errno 2] No such file")   # noqa: E731
AUTH_OK = lambda url, user, pw: (True, "ok")                          # noqa: E731
REFUSED = lambda url, user, pw: (False, "connection error: refused")  # noqa: E731


def fails(checks):
    return [c for c in checks if c.status == doctor.FAIL]


def one_fail(checks):
    bad = fails(checks)
    assert len(bad) == 1, [c.line() for c in checks]
    return bad[0]


@pytest.fixture
def proj(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    return root


def env_checks(root, dotenv="", **env):
    if dotenv:
        (root / ".env").write_text(dotenv)
    return doctor.check_env({"SLT_PROJECT_ROOT": str(root), **env}, REPO)[0]


# --- .env and environment --------------------------------------------------------------------

def test_env_clean(proj):
    (proj / "cases").mkdir()
    checks = env_checks(proj, "# c\nexport SLT_LIVE_CASES=cases\nSTRIIM_URL=http://h:9080\n")
    assert not fails(checks)
    assert checks[0].line() == f"[ ok ] env: {proj / '.env'} (2 keys)"


def test_env_missing_file_is_fine(proj):
    checks = env_checks(proj)
    assert not fails(checks) and "no .env at" in checks[0].message


def test_unknown_key_in_dotenv_names_the_nearest(proj):
    c = one_fail(env_checks(proj, "SLT_LIVE_CASE=cases\n"))
    assert c.code == CONFIG
    assert c.message == (f"SLT_LIVE_CASE (set in {proj / '.env'}) is not a key the framework "
                         f"reads, likely a typo; did you mean SLT_LIVE_CASES?")


def test_unknown_key_in_environment(proj):
    c = one_fail(env_checks(proj, SLT_STACK_PREFX="x"))
    assert c.message.startswith("SLT_STACK_PREFX (set in environment) is not a key")
    assert c.message.endswith("did you mean SLT_STACK_PREFIX?")


def test_unknown_key_with_no_near_match(proj):
    c = one_fail(env_checks(proj, "SLT_ZZZZZZ=1\n"))
    assert c.message.endswith("likely a typo")


def test_known_key_dotenv_cannot_supply(proj):
    c = one_fail(env_checks(proj, "SLT_SERVICES_HOST=host.docker.internal\n"))
    assert c.message.startswith(f"SLT_SERVICES_HOST is set in {proj / '.env'}, but .env supplies only "
                                f"SLT_FRAMEWORK_HOME,")
    assert c.message.endswith("and the service settings; export SLT_SERVICES_HOST in the environment instead")


def test_a_consumer_services_own_keys_are_known(proj, tmp_path, monkeypatch):
    # A consumer service (servicesRoots) that uses SLT_ names -- a consumer teradata keeps
    # SLT_TERADATA_* -- is known in the shell and in .env, including Compose-only settings.
    from livetest import layout
    svc = tmp_path / "roots" / "teradata"
    svc.mkdir(parents=True)
    (svc / "service.yaml").write_text(
        "name: teradata\nisolation: none\nlive_override_env: SLT_TERADATA_HOST\n"
        "docker_env: {port: SLT_TERADATA_HOST_PORT}\nlive_env: {host: SLT_TERADATA_HOST}\n"
        "compose: compose.yaml\n")
    (svc / "compose.yaml").write_text("services:\n  td:\n    devices: ['${SLT_TERADATA_KVM_DEVICE:-/dev/null}']\n")
    layout.set_manifest_roots(services=[tmp_path / "roots"])
    assert "SLT_TERADATA_KVM_DEVICE" in doctor.declared_service_keys({})
    assert not fails(env_checks(proj, SLT_TERADATA_HOST="td.example.com",
                                SLT_TERADATA_KVM_DEVICE="/dev/kvm"))     # a compose-only variable too
    assert not fails(env_checks(proj, "SLT_TERADATA_HOST_PORT=11025\n"))
    _, values = doctor.check_env({"SLT_PROJECT_ROOT": str(proj)}, REPO)
    assert values["SLT_TERADATA_HOST_PORT"] == "11025"
    c = one_fail(env_checks(proj, "SLT_TERADATA_KVM_DEVCIE=/dev/null\n"))
    assert "is not a key" in c.message


def test_known_key_in_environment_is_fine(proj):
    assert not fails(env_checks(proj, SLT_PG_HOST="db", SLT_ORACLE_VIEW_HOST="vpn-name"))


def test_retired_key(proj):
    c = one_fail(env_checks(proj, "SLT_MODE=wheel\n"))
    assert c.message == (f"SLT_MODE (set in {proj / '.env'}) is retired and never read; use "
                         f"SLT_FRAMEWORK_MODE")


@pytest.mark.parametrize("key", ["SLT_LIVE_CASES", "SLT_INT_CASES"])
def test_set_but_missing_path_in_dotenv(proj, key):
    c = one_fail(env_checks(proj, f"{key}=nowhere\n"))
    assert c.message.startswith(f"{key}='nowhere' (set in {proj / '.env'}) does not "
                                f"exist: {proj / 'nowhere'}")


def test_set_but_missing_path_in_environment(proj, tmp_path):
    c = one_fail(env_checks(proj, SLT_STATE_DIR=str(tmp_path / "gone")))
    assert c.message.startswith(f"SLT_STATE_DIR='{tmp_path / 'gone'}' (set in environment) "
                                f"does not exist")


def test_missing_project_root_names_it(tmp_path):
    checks, _ = doctor.check_env({"SLT_PROJECT_ROOT": str(tmp_path / "nope")}, REPO)
    assert one_fail(checks).message.startswith(
        f"SLT_PROJECT_ROOT={str(tmp_path / 'nope')!r} (set in environment) does not exist")


def test_environment_only_path_key(proj, tmp_path):
    (tmp_path / "deps.yaml").write_text("x")
    assert not fails(env_checks(proj, SLT_STRIIM_DEPS_MANIFEST=str(tmp_path / "deps.yaml"),
                                SLT_JDK17_HOME=str(tmp_path)))
    c = one_fail(env_checks(proj, SLT_JDK11_HOME=str(tmp_path / "jdk11")))
    assert c.message == (f"SLT_JDK11_HOME='{tmp_path / 'jdk11'}' (set in environment) does not "
                         f"exist: {tmp_path / 'jdk11'}")


# --- known keys --------------------------------------------------------------------------------

def test_known_keys_from_sources(tmp_path):
    src = tmp_path / "scripts" / "live" / "livetest"
    src.mkdir(parents=True)
    (src / "a.py").write_text('env.get("SLT_ALPHA")\nf"SLT_{name.upper()}_VIEW_HOST"\n'
                              'ns = f"SLT_{slug}"\nk = f"SLT_{a}_{b}"\np = "SLT_PG_"\n')
    (src.parent / "services").mkdir()
    (src.parent / "services" / "compose.yaml").write_text("ports: ['${SLT_BETA_PORT:-1}:1']\n")
    (src.parent / "tests").mkdir()
    (src.parent / "tests" / "test_x.py").write_text('"SLT_FROM_A_TEST"\n')
    exact, patterns = doctor.known_keys(tmp_path)
    assert exact == {"SLT_ALPHA", "SLT_BETA_PORT"}
    assert [p.pattern for p in patterns] == [r"SLT_[A-Z0-9_]+_VIEW_HOST\Z"]


# --- STRIIM_URL shape --------------------------------------------------------------------------

@pytest.mark.parametrize("url", ["http://h:9080", "https://striim.example.com/", "http://203.0.113.5"])
def test_url_shape_ok(url):
    assert doctor.url_problem(url) is None


@pytest.mark.parametrize("url,message", [
    ("h:9080", "STRIIM_URL='h:9080' has no scheme; write it as http://h:9080"),
    ("ftp://h:9080", "STRIIM_URL='ftp://h:9080' must use http:// or https://, not ftp://"),
    ("http://:9080", "STRIIM_URL='http://:9080' has no host"),
    ("http://h:90x0", "STRIIM_URL='http://h:90x0' is not a valid URL: "),
    ("http://h:9080/api/v2", "STRIIM_URL='http://h:9080/api/v2' must be the server root "
                             "(http://h:9080), with no path or query"),
    ("http://h:9080/?x=1", "STRIIM_URL='http://h:9080/?x=1' must be the server root "
                           "(http://h:9080), with no path or query"),
], ids=["no-scheme", "scheme", "no-host", "port", "path", "query"])
def test_url_shape_fails(url, message):
    assert doctor.url_problem(url).startswith(message)


# --- Striim -------------------------------------------------------------------------------------

BUILDS = lambda env: (None, "5.4.0.6")                                # noqa: E731


def striim(env, dotenv=None, probe=AUTH_OK, docker=UP, build=BUILDS):
    return doctor.check_striim(env, dotenv or {}, probe=probe, docker=docker, build=build)


def test_striim_authenticated():
    seen = []
    checks = striim({"STRIIM_URL": "http://s:9080", "STRIIM_USER": "qa", "STRIIM_PASS": "pw"},
                    probe=lambda *a: seen.append(a) or (True, "ok"))
    assert [c.line() for c in checks] == ["[ ok ] striim: http://s:9080 authenticated as 'qa'"]
    assert seen == [("http://s:9080", "qa", "pw")]


def test_striim_bad_shape_is_not_probed():
    c = one_fail(striim({"STRIIM_URL": "s:9080"}, probe=None))
    assert c.message == ("STRIIM_URL='s:9080' has no scheme; write it as http://s:9080 "
                         "(set in environment)")


def test_striim_wrong_password_is_config():
    c = one_fail(striim({"STRIIM_URL": "http://s:9080", "STRIIM_PASS": "x"},
                        probe=lambda *a: (False, "HTTP 401: 'nope'")))
    assert c.code == CONFIG
    assert c.message == ("http://s:9080 (STRIIM_URL, set in environment) as 'admin': "
                         "HTTP 401: 'nope'")


def test_striim_unreachable_is_infra_and_names_the_password_default():
    c = one_fail(striim({"STRIIM_URL": "http://s:9080"}, probe=REFUSED))
    assert c.code == INFRA
    assert c.message == ("http://s:9080 (STRIIM_URL, set in environment) as 'admin' "
                         "(STRIIM_PASS unset, so the Docker default was tried): "
                         "connection error: refused")


def test_striim_settings_from_dotenv_are_enough():
    # The run gets them too (striim-test hands .env's STRIIM_* to the tier child), so no warning.
    seen = []
    checks = striim({}, {"STRIIM_URL": "http://s:9080", "STRIIM_PASS": "pw"},
                    probe=lambda url, user, pw: seen.append((url, user, pw)) or (True, ""))
    assert [c.status for c in checks] == ["ok"], [c.line() for c in checks]
    assert seen == [("http://s:9080", "admin", "pw")]


@pytest.mark.parametrize("env,dotenv", [({"STRIIM_PASSWORD": "pw"}, {}),
                                        ({}, {"STRIIM_PASSWORD": "pw"})], ids=["env", "dotenv"])
def test_striim_password_alone_is_the_password(env, dotenv):
    seen = []
    checks = striim({"STRIIM_URL": "http://s:9080", **env}, dotenv,
                    probe=lambda url, user, pw: seen.append(pw) or (True, ""))
    assert [c.status for c in checks] == ["ok"] and seen == ["pw"]


def test_docker_mode_cluster_already_up():
    c, = striim({})
    assert c.status == "ok" and "a cluster already answers at http://localhost:9080" in c.message


def test_docker_mode_docker_answers():
    c, = striim({}, probe=REFUSED)
    assert c.line() == ("[ ok ] striim: Docker mode (STRIIM_URL unset); Docker 29.0 answers, and "
                        "the run builds Striim 5.4.0.6 and starts a cluster at "
                        "http://localhost:9080")


def test_docker_mode_build_problem_is_named():
    c = one_fail(striim({}, probe=REFUSED, build=lambda env: ("no license", "")))
    assert c.message == "Docker mode (STRIIM_URL unset): no license"


LICENSE = {"COMPANY_NAME": "c", "CLUSTER_NAME": "n", "PRODUCT_KEY": "p", "LICENCE_KEY": "l"}


def install(tmp_path, *versions, props=""):
    lib = tmp_path / "Striim" / "lib"
    lib.mkdir(parents=True)
    for v in versions:
        (lib / f"Platform-{v}.jar").write_text("")
    (lib.parent / "conf").mkdir()
    (lib.parent / "conf" / "startUp.properties").write_text(props)
    return str(lib.parent)


def test_docker_build_license_from_environment():
    assert doctor._docker_build(dict(LICENSE)) == (None, "5.4.2")


def test_docker_build_license_and_release_from_striim_home(tmp_path):
    home = install(tmp_path, "5.4.2", props="WAClusterName=n\nCompanyName=c\nProductKey=p\n"
                                              "LicenceKey=l\n# ProductKey=\n")
    assert doctor._docker_build({"STRIIM_HOME": home}) == (None, "5.4.2")


def test_docker_build_without_a_license():
    problem, release = doctor._docker_build({"COMPANY_NAME": "c"})
    assert release == "5.4.2"
    assert problem == ("Striim needs a license to boot, and CLUSTER_NAME, PRODUCT_KEY, "
                       "LICENCE_KEY are not set; export STRIIM_HOME=<a Striim install> (read "
                       "from its conf/startUp.properties), or export COMPANY_NAME, CLUSTER_NAME, "
                       "PRODUCT_KEY and LICENCE_KEY")


def test_docker_build_ambiguous_install(tmp_path):
    home = install(tmp_path, "5.4.0.6", "5.4.2")
    problem, _ = doctor._docker_build({"STRIIM_HOME": home, **LICENSE})
    assert problem.startswith(f"STRIIM_HOME={home!r} picks the Striim release: ambiguous install: "
                              f"multiple Platform-*.jar")


def test_docker_mode_without_docker():
    c = one_fail(striim({}, probe=REFUSED, docker=DOWN))
    assert c.code == INFRA
    assert c.message == ("Docker mode (STRIIM_URL unset) needs Docker: docker not usable: "
                         "[Errno 2] No such file; start Docker, or set STRIIM_URL to your "
                         "Striim server")


def test_docker_mode_port_taken_by_something_else():
    c = one_fail(striim({"SLT_SERVICES_HOST": "gw"}, probe=lambda *a: (False, "HTTP 404: ''")))
    assert c.message == ("Docker mode (STRIIM_URL unset), but http://gw:9080 answers HTTP 404: "
                         "''; stop what is on that port, or set STRIIM_URL to it")


def test_docker_check_reads_docker_info():
    class P:
        def __init__(self, rc, out="", err=""):
            self.returncode, self.stdout, self.stderr = rc, out, err
    assert doctor._docker_answers(lambda argv: P(0, "29.4.0\n")) == (True, "Docker 29.4.0")
    assert doctor._docker_answers(lambda argv: P(1, err="x\nCannot connect to the Docker "
                                                        "daemon\n")) == \
        (False, "'docker info' failed: Cannot connect to the Docker daemon")

    def missing(argv):
        raise FileNotFoundError(2, "No such file or directory", "docker")
    ok, why = doctor._docker_answers(missing)
    assert not ok and why.startswith("docker not usable: [Errno 2]")


# --- Docker disk (a 21.9 GB image build filled a VM that had 22.8 GB free) ----------

GB = 1000 ** 3


def disk(env=None, free=40 * GB, present=False):
    return doctor.check_docker_disk(env or {}, {}, disk=lambda: (free, "df in alpine"),
                                    present=lambda release: present)


def test_docker_disk_enough_to_build():
    c, = disk()
    assert c.status == "ok"
    assert c.line() == ("[ ok ] docker disk: 40.0 GB free (df in alpine); building the Striim "
                        "image needs at least 35 GB")


def test_docker_disk_short_for_a_build_is_infra_and_says_how_much():
    c = one_fail(disk(free=int(22.8 * GB)))
    assert c.code == INFRA
    assert c.message.startswith("Docker has 22.8 GB free, and building the Striim image needs "
                                "at least 35 GB")


def test_docker_disk_with_the_image_built_needs_only_room_to_run():
    c, = disk(free=20 * GB, present=True)
    assert c.status == "ok" and "running the Striim cluster needs at least 5 GB" in c.message


def test_docker_disk_unknown_is_a_warning():
    c, = doctor.check_docker_disk({}, {}, disk=lambda: (None, "no small local image"),
                                  present=lambda release: False)
    assert c.status == "warn"
    assert "could not measure" in c.message and "no small local image" in c.message


def test_docker_disk_not_checked_against_your_own_striim():
    assert disk({"STRIIM_URL": "http://s:9080"}) == []


def test_run_checks_skips_the_disk_when_the_striim_check_failed(tmp_path):
    checks = doctor.run_checks({"SLT_PROJECT_ROOT": str(tmp_path)}, REPO,
                               probe=REFUSED, docker=DOWN,
                               disk=lambda: pytest.fail("measured without Docker"))
    assert not [c for c in checks if c.subject == "docker disk"]


# --- where the license and the installers come from -------------------------------

def test_license_from_the_environment_names_the_settings_never_the_values():
    c, = doctor.check_license(dict(LICENSE))
    assert c.line() == ("[ ok ] license: CLUSTER_NAME, COMPANY_NAME, PRODUCT_KEY, LICENCE_KEY "
                        "from the environment")


def test_license_from_striim_home_names_the_file(tmp_path):
    home = install(tmp_path, "5.4.2", props="WAClusterName=n1\nCompanyName=c1\n"
                                              "ProductKey=PK-9\nLicenceKey=LK-9\n")
    c, = doctor.check_license({"STRIIM_HOME": home})
    assert c.status == "ok"
    assert c.message == (f"CLUSTER_NAME, COMPANY_NAME, PRODUCT_KEY, LICENCE_KEY from "
                         f"{home}/conf/startUp.properties (STRIIM_HOME)")
    for value in ("n1", "c1", "PK-9", "LK-9"):
        assert value not in c.line()


def test_license_mixed_sources_are_each_named(tmp_path):
    home = install(tmp_path, "5.4.2", props="ProductKey=PK-9\nLicenceKey=LK-9\n")
    c, = doctor.check_license({"STRIIM_HOME": home, "COMPANY_NAME": "c", "CLUSTER_NAME": "n"})
    assert c.message == (f"CLUSTER_NAME, COMPANY_NAME from the environment; PRODUCT_KEY, "
                         f"LICENCE_KEY from {home}/conf/startUp.properties (STRIIM_HOME)")


def test_license_missing_is_left_to_the_striim_check():
    assert doctor.check_license({"COMPANY_NAME": "c"}) == []


def _manifest(tmp_path, names, digest="a" * 64, directory="files"):
    d = tmp_path / directory
    d.mkdir(parents=True, exist_ok=True)
    for n in names:
        (d / n).write_text("x")
    m = tmp_path / "deps-manifest.json"
    import json
    from livetest.striim_provision import REQUIRED_DEPS
    m.write_text(json.dumps({"schemaVersion": 1, "directory": directory,
                             "sha256": {n: digest for n in REQUIRED_DEPS}}))
    return m


def test_installers_from_a_manifest_are_named_and_counted(tmp_path):
    from livetest.striim_provision import REQUIRED_DEPS
    m = _manifest(tmp_path, REQUIRED_DEPS)
    c, = doctor.check_installers({"SLT_STRIIM_DEPS_MANIFEST": str(m)})
    assert c.status == "ok"
    assert c.message == (f"{m} (SLT_STRIIM_DEPS_MANIFEST) names all 9 files for Striim 5.4.2, "
                         f"all in {tmp_path / 'files'}; their digests are checked before the "
                         f"build")


def test_installers_a_missing_file_is_named(tmp_path):
    from livetest.striim_provision import REQUIRED_DEPS
    m = _manifest(tmp_path, REQUIRED_DEPS[1:])
    c = one_fail(doctor.check_installers({"SLT_STRIIM_DEPS_MANIFEST": str(m)}))
    assert c.code == CONFIG
    assert c.message == (f"{m} (SLT_STRIIM_DEPS_MANIFEST): {REQUIRED_DEPS[0]} is not in "
                         f"{tmp_path / 'files'}")


def test_installers_a_bad_manifest_is_named(tmp_path):
    m = tmp_path / "m.json"
    m.write_text('{"schemaVersion": 1, "directory": "../x", "sha256": {}}')
    c = one_fail(doctor.check_installers({"SLT_STRIIM_DEPS_MANIFEST": str(m)}))
    assert "escapes the manifest directory" in c.message


def test_installers_without_a_manifest_says_the_build_downloads():
    c, = doctor.check_installers({})
    assert c.line() == ("[ ok ] installers: SLT_STRIIM_DEPS_MANIFEST unset; the first build "
                        "downloads about 6.3 GB of Striim packages and drivers")


def test_run_checks_reports_license_and_installers_in_docker_mode(tmp_path):
    checks = doctor.run_checks({"SLT_PROJECT_ROOT": str(tmp_path), **LICENSE}, REPO,
                               probe=AUTH_OK, disk=lambda: (None, "t"))
    subjects = [c.subject for c in checks]
    assert "license" in subjects and "installers" in subjects
    mine = doctor.run_checks({"SLT_PROJECT_ROOT": str(tmp_path), "STRIIM_URL": "http://s:9080"},
                             REPO, probe=AUTH_OK)
    assert not {"license", "installers"} & {c.subject for c in mine}


# --- cases and services -------------------------------------------------------------------------

def case(root, name, requires):
    d = root / name
    d.mkdir(parents=True)
    (d / "test.yaml").write_text(f"name: {name}\nrequires: {requires}\n")
    return d


def test_case_selection(tmp_path):
    a = case(tmp_path / "suite", "a", "[postgres]")
    b = case(tmp_path / "suite", "b", "[postgres, oracle]")
    files, checks = doctor.case_files([a, b / "test.yaml", tmp_path / "suite"])
    assert not checks and len(files) == 4
    need, bad = doctor.required_services(files)
    assert not bad and need == {"postgres": ["a", "b", "a", "b"], "oracle": ["b", "b"]}


def test_case_selection_fails(tmp_path):
    files, checks = doctor.case_files([tmp_path / "nope"])
    assert files == [] and one_fail(checks).message == \
        f"{tmp_path / 'nope'}: no test.yaml there ({tmp_path / 'nope'})"
    d = case(tmp_path, "c", "postgres")
    _, bad = doctor.required_services([d / "test.yaml"])
    assert one_fail(bad).message == (f"{d / 'test.yaml'}: 'requires' must be a list of service "
                                     f"names, got 'postgres'")


def test_case_manifest_loads():
    files, _ = doctor.case_files([REPO / "samples" / "live" / "01-plain-replication"])
    c, = doctor.check_manifests(files)
    assert c.status == doctor.OK_ and c.subject == "case 01-plain-replication"
    assert c.message == "test.yaml loads (plain-replication)"


def test_case_manifest_error_is_config_and_names_the_key(tmp_path):
    d = tmp_path / "typo"
    d.mkdir()
    (d / "test.yaml").write_text("name: typo\ntql: app.tql\ntimout: 30\nassert: {smoke: true}\n")
    c = one_fail(doctor.check_manifests([d / "test.yaml"]))
    assert c.subject == "case typo" and c.code == CONFIG
    assert "unknown manifest key(s) ['timout']" in c.message


def test_case_manifests_load_with_their_own_tiers_loader(project):
    # review D1: an integration or perf case has no `tql:`, so the live loader failed it
    from striim_test import project_io
    tier_of = doctor.case_tiers(project_io.load(project))
    files, _ = doctor.case_files([project.parent / "cases"])
    checks = doctor.check_manifests(files, tier_of)
    assert not fails(checks), [c.line() for c in checks]
    got = {c.subject: c.message for c in checks}
    assert got["case p1"] == "test.yaml loads as a perf case (p1)"
    assert got["case needs-absent"] == "test.yaml loads as an integration case (needs-absent)"
    assert got["case hello-single"].startswith("test.yaml loads (")


def test_a_case_in_a_root_two_tiers_share_is_not_checked(tmp_path):
    # review R2-D1: with suites {live: cases, integration: cases} doctor picked live and failed an
    # integration case on "'tql' is required"; `run` refuses the path and asks for --tier
    from striim_test import project_io
    (tmp_path / "cases" / "int-twin").mkdir(parents=True)
    (tmp_path / "cases" / "int-twin" / "test.yaml").write_text("name: int-twin\nrequires: [postgres]\n")
    (tmp_path / "gold-targets.yaml").write_text(
        "schemaVersion: 1\ntargets: []\nsuites: {live: cases, integration: cases}\n")
    tier_of = doctor.case_tiers(project_io.load(tmp_path / "gold-targets.yaml"))
    c, = doctor.check_manifests([tmp_path / "cases" / "int-twin" / "test.yaml"], tier_of)
    assert c.status == doctor.WARN, c.line()
    assert c.message == ("not checked: in the case roots of live and integration; "
                         "`run` needs --tier")


def test_a_case_outside_every_case_root_is_not_checked(project, tmp_path):
    from striim_test import project_io
    d = tmp_path / "stray"
    d.mkdir()
    (d / "test.yaml").write_text("name: stray\nrequires: [postgres]\n")
    c, = doctor.check_manifests([d / "test.yaml"], doctor.case_tiers(project_io.load(project)))
    assert c.status == doctor.WARN and c.subject == "case stray"
    assert c.message.startswith("not checked: outside every case root (")


def test_case_manifest_checks_exact_and_lifecycle(tmp_path):
    d = tmp_path / "vacuous"
    d.mkdir()
    (d / "test.yaml").write_text("name: v\ntql: app.tql\nexact: {version: 1}\n"
                                 "assert: {smoke: true}\n")
    assert "no exact spec (vacuous)" in one_fail(doctor.check_manifests([d / "test.yaml"])).message


def services(need, env=None, tcp=lambda h, p: None, login=None, docker=UP, running=lambda c: True,
             publisher=None):
    kw = {} if publisher is None else {"publisher": publisher}
    return doctor.check_services(need, env or {}, tcp=tcp, login=login or {}, docker=docker,
                                 running=running, **kw)


def test_unknown_service():
    c = one_fail(services({"postgress": ["a"]}))
    assert c.subject == "service postgress" and c.code == CONFIG
    assert c.message.startswith("required by a: unknown service 'postgress' (no ")


def test_customer_provided_reached_and_logged_in():
    seen = []
    env = {"SLT_PG_HOST": "db.example.com", "SLT_PG_PORT": "6543", "SLT_PG_ADMIN_USER": "adm"}
    c, = services({"postgres": ["a"]}, env, tcp=lambda h, p: seen.append((h, p)),
                  login={"postgres": lambda base: seen.append(base["admin_user"])})
    assert c.line() == ("[ ok ] service postgres: customer-provided (SLT_PG_HOST=db.example.com) at "
                        "db.example.com:6543 (login ok)")
    assert seen == [("db.example.com", "6543"), "adm"]


def test_customer_provided_unreachable():
    c = one_fail(services({"postgres": ["a"]}, {"SLT_PG_HOST": "db.example.com"},
                          tcp=lambda h, p: "[Errno 111] Connection refused"))
    assert c.code == INFRA
    assert c.message == ("customer-provided (SLT_PG_HOST=db.example.com): cannot connect to "
                         "db.example.com:5432: [Errno 111] Connection refused")


def test_customer_provided_login_refused():
    c = one_fail(services({"postgres": ["a"]}, {"SLT_PG_HOST": "db.example.com"},
                          login={"postgres": lambda base: "password authentication failed"}))
    assert c.code == CONFIG
    assert c.message == ("customer-provided (SLT_PG_HOST=db.example.com): db.example.com:5432 refused login "
                         "as 'postgres': password authentication failed")


def test_docker_service_running_and_reached():
    c, = services({"oracle": ["a"]}, {"SLT_ORA_HOST_PORT": "11521"})
    assert c.line() == "[ ok ] service oracle: Docker (slt-oracle) at localhost:11521"


def test_docker_service_running_but_unreachable():
    c = one_fail(services({"postgres": ["a"]}, {"SLT_SERVICES_HOST": "gw"},
                          tcp=lambda h, p: "timed out"))
    assert c.message == "Docker (slt-postgres): cannot connect to gw:5432: timed out"


def test_docker_service_not_running_docker_answers():
    seen = []
    c, = services({"postgres": ["a"]}, {"SLT_PG_HOST_PORT": "55432"}, running=lambda c: False,
                  tcp=lambda h, p: seen.append((h, p)) or "[Errno 111] Connection refused")
    assert c.line() == ("[ ok ] service postgres: Docker (slt-postgres) not running; Docker 29.0 "
                        "answers, and the run starts it")
    assert seen == [("localhost", "55432")]     # the port the run will publish on is free


def test_docker_service_not_running_but_its_host_port_is_taken():
    # Another Postgres held 5432, doctor passed, and the run failed at `docker compose up`
    c = one_fail(services({"postgres": ["a"]}, running=lambda c: False, tcp=lambda h, p: None,
                          publisher=lambda port, env: ""))
    assert c.code == CONFIG
    assert c.message == ("Docker (slt-postgres) is not running, but localhost:5432 already answers: "
                         "something else holds the host port the run publishes postgres on; set "
                         "SLT_PG_HOST_PORT to a free port in .env")


def test_a_port_published_by_a_sibling_of_the_same_stack_is_not_taken(tmp_path, monkeypatch):
    # slt-kafka stopped while slt-zookeeper of its own compose project still
    # publishes 2181; `docker compose up` just restarts slt-kafka
    from livetest import registry
    d = tmp_path / "mq"
    d.mkdir()
    (d / "service.yaml").write_text("name: mq\nisolation: none\ncontainer: slt-mq\n"
                                    "docker_defaults: {host: localhost, port: 9092, zk_port: 2181}\n"
                                    "docker_env: {port: SLT_MQ_HOST_PORT, zk_port: SLT_MQ_ZK_HOST_PORT}\n")
    monkeypatch.setattr(registry, "_SERVICES_DIR", tmp_path)
    free = lambda h, p: None if p == 2181 else "[Errno 111] Connection refused"
    c, = services({"mq": ["a"]}, running=lambda c: False, tcp=free,
                  publisher=lambda port, env: "slt-mq-zookeeper" if port == 2181 else "")
    assert c.status == "ok", c.line()
    c = one_fail(services({"mq": ["a"]}, running=lambda c: False, tcp=free,
                          publisher=lambda port, env: "other-project-zk" if port == 2181 else ""))
    assert "set SLT_MQ_ZK_HOST_PORT to a free port" in c.message


def test_a_taken_port_of_a_service_env_does_not_forward_names_the_shell(tmp_path, monkeypatch):
    # A consumer service's own variables are not among the settings .env forwards to the run, so
    # "set it in .env" would not move the port
    from livetest import registry
    d = tmp_path / "mongodb"
    d.mkdir()
    (d / "service.yaml").write_text("name: mongodb\nisolation: none\ncontainer: slt-mongodb\n"
                                    "docker_defaults: {host: localhost, port: 57017}\n"
                                    "docker_env: {port: MYTESTS_MONGODB_HOST_PORT}\n")
    monkeypatch.setattr(registry, "_SERVICES_DIR", tmp_path)
    c = one_fail(services({"mongodb": ["a"]}, running=lambda c: False, tcp=lambda h, p: None,
                          publisher=lambda port, env: ""))
    assert c.message.endswith("set MYTESTS_MONGODB_HOST_PORT to a free port: export it in the "
                              "shell, since .env does not pass it to the run")


def test_docker_service_not_running_no_docker():
    c = one_fail(services({"postgres": ["a"]}, running=lambda c: False, docker=DOWN))
    assert c.code == INFRA
    assert c.message == ("Docker (slt-postgres) is not running and the run cannot start it: "
                         "docker not usable: [Errno 2] No such file; start Docker, or point "
                         "SLT_PG_HOST at your own postgres")


def test_opt_in_service_warns_until_opted_in(tmp_path, monkeypatch):
    from livetest import registry
    (tmp_path / "emu").mkdir()
    (tmp_path / "emu" / "service.yaml").write_text(
        "name: emu\nisolation: none\ncontainer: slt-emu\nopt_in_env: SLT_EMU\n"
        "docker_defaults: {host: localhost, port: 9010}\n")
    monkeypatch.setattr(registry, "_SERVICES_DIR", tmp_path)
    c, = services({"emu": ["s1"]})
    # review D2: nothing in the live tier skips a case for opt_in_env; it only keeps the service
    # out of bring-ups that no selected case asked for (preflight._gated_out)
    assert c.line() == ("[warn] service emu: opt-in: `start all` and other bring-ups not tied to a "
                        "case leave it out unless SLT_EMU=1 or SLT_EMULATORS=1; a live case that "
                        "requires it (s1) still starts it")
    for opted in ({"SLT_EMULATORS": "1"}, {"SLT_EMU": "1"}):
        c, = services({"emu": ["s1"]}, opted)
        assert c.line() == "[ ok ] service emu: Docker (slt-emu) at localhost:9010"


def test_absent_directory_mount_is_a_warning_and_a_file_like_one_a_failure(tmp_path, monkeypatch):
    # Docker creates an absent directory source, so the run is
    # allowed and doctor names it; an absent file-like source is the mistake preflight exists for
    from livetest import registry
    d = tmp_path / "db"
    d.mkdir()
    (d / "service.yaml").write_text("name: db\nisolation: none\ncontainer: slt-db\ncompose: compose.yaml\n"
                                    "docker_defaults: {host: localhost, port: 5432}\n")
    (d / "compose.yaml").write_text("services:\n  db:\n    image: x\n    volumes:\n      - ./data:/var/lib/db\n")
    monkeypatch.setattr(registry, "_SERVICES_DIR", tmp_path)
    warn, ok = services({"db": ["s1"]})
    assert warn.line() == (f"[warn] service db: bind-mount source data does not exist in {d}; Docker "
                           f"creates it as an empty directory when it starts db")
    assert ok.line() == "[ ok ] service db: Docker (slt-db) at localhost:5432"
    (d / "compose.yaml").write_text("services:\n  db:\n    image: x\n    volumes:\n      - ./init.sql:/init.sql\n")
    c = one_fail(services({"db": ["s1"]}))
    assert c.code == CONFIG and c.message.startswith("required by s1: live/db: missing bind-mount source init.sql")


# --- ownership (an undeclared live run is refused; doctor reports the effective value) --

def test_ownership_unset_is_flagged_as_refused():
    c, = doctor.check_ownership({}, {})
    assert c.status == doctor.FAIL and c.code == CONFIG
    assert c.line() == ("[FAIL] ownership: SLT_INFRA_OWNERSHIP is unset, so live runs will be refused; "
                        "set SLT_INFRA_OWNERSHIP=shared (with SLT_KEEP_SERVICES=1) to reuse a kept stack, "
                        "or SLT_INFRA_OWNERSHIP=exclusive for a stack this run owns")


def test_ownership_unknown_value_is_flagged():
    c, = doctor.check_ownership({"SLT_INFRA_OWNERSHIP": "mine"}, {})
    assert c.line() == ("[FAIL] ownership: SLT_INFRA_OWNERSHIP='mine' (set in environment) is not an "
                        "accepted value, so live runs will be refused; use exclusive or shared")


def test_ownership_shared_without_keep_services_is_flagged():
    c, = doctor.check_ownership({}, {"SLT_INFRA_OWNERSHIP": "shared"})
    assert c.status == doctor.FAIL and "requires SLT_KEEP_SERVICES=1" in c.message
    assert "live runs will be refused" in c.message


def test_ownership_reports_the_effective_value_and_the_shell_wins():
    dot = {"SLT_INFRA_OWNERSHIP": "shared", "SLT_KEEP_SERVICES": "1"}
    c, = doctor.check_ownership({}, dot)
    assert c.status == doctor.OK_ and c.message.startswith("shared (SLT_INFRA_OWNERSHIP, set in ")
    c, = doctor.check_ownership({"SLT_INFRA_OWNERSHIP": "exclusive"}, {})
    assert c.line() == "[ ok ] ownership: exclusive (SLT_INFRA_OWNERSHIP, set in environment)"
    c, = doctor.check_ownership({"SLT_INFRA_OWNERSHIP": "exclusive"}, dot)      # the shell wins over .env
    assert c.message.startswith("exclusive (SLT_INFRA_OWNERSHIP, set in environment)")


def test_ownership_exclusive_with_keep_from_the_shell_warns():
    c, = doctor.check_ownership({"SLT_INFRA_OWNERSHIP": "exclusive", "SLT_KEEP_SERVICES": "1"}, {})
    assert c.status == doctor.WARN and "keeps its own stack at session end" in c.message
    assert "unset SLT_KEEP_SERVICES" in c.message


def test_ownership_exclusive_from_the_shell_with_keep_in_dotenv_warns_it_is_not_used():
    c, = doctor.check_ownership({"SLT_INFRA_OWNERSHIP": "exclusive"}, {"SLT_KEEP_SERVICES": "1"})
    assert c.status == doctor.WARN and "is not used for an exclusive run" in c.message


def test_ownership_from_dotenv_says_it_reaches_striim_test_only():
    c, = doctor.check_ownership({}, {"SLT_INFRA_OWNERSHIP": "shared", "SLT_KEEP_SERVICES": "1"})
    assert c.message.count("striim-test only, export for direct pytest") == 2
    c, = doctor.check_ownership({"SLT_INFRA_OWNERSHIP": "shared", "SLT_KEEP_SERVICES": "1"}, {})
    assert "striim-test only" not in c.message


def test_ledgers_none_left_is_ok(tmp_path):
    c, = doctor.check_ledgers({"SLT_STATE_DIR": str(tmp_path)}, {})
    assert c.line() == "[ ok ] ledgers: no leftover ownership ledgers"


def test_ledgers_left_by_a_killed_run_are_named_with_the_replay_command(tmp_path):
    import json
    from livetest import ownership
    d = tmp_path / "lifecycle" / "ledgers"
    d.mkdir(parents=True)
    (d / "t1.json").write_text(json.dumps({"ledgerVersion": ownership.LEDGER_VERSION,
                                           "identity": {"runId": "r1", "case": "hello-single"},
                                           "entries": [{"kind": "pg-slot", "name": "slt_t1", "state": "confirmed"}]}))
    c, = doctor.check_ledgers({"SLT_STATE_DIR": str(tmp_path)}, {})
    assert c.status == doctor.WARN
    assert c.line() == ("[warn] ledgers: case 'hello-single' run 'r1': 1 object(s) left (pg-slot), not cleaned up "
                        "(killed, timed out or a failed cleanup); reclaim it with "
                        f"python -m livetest.ownership replay {d / 't1.json'}")


# --- service settings from .env and --targets ----------------

@pytest.fixture
def clone_dir(tmp_path, monkeypatch):
    """A clone root whose .env the lookups really read (the hermetic fixture blanks .env)."""
    from livetest import paths
    clone = tmp_path / "clone"
    clone.mkdir()
    monkeypatch.setattr(paths, "dotenv_values", lambda env=None: paths.read_dotenv(paths.dotenv_path(env)))
    monkeypatch.setattr(paths, "_default_project_root", lambda: clone)
    return clone


def test_service_settings_none_set(clone_dir, proj):
    c, = doctor.check_service_settings({"SLT_PROJECT_ROOT": str(proj)})
    assert c.line() == "[ ok ] service settings: none set; each required service runs in Docker with its defaults"


def test_service_settings_name_where_each_is_set_and_the_shell_wins(clone_dir, proj):
    (proj / ".env").write_text("SLT_PG_HOST=db.example\nSLT_PG_PORT=6432\n")
    (clone_dir / ".env").write_text("SLT_ORA_HOST=ora.example\nSLT_PG_HOST=clone-db\n")
    c, = doctor.check_service_settings({"SLT_PROJECT_ROOT": str(proj), "SLT_PG_PORT": "7000"})
    only = "striim-test only, export for direct pytest"
    assert c.line() == (f"[ ok ] service settings: SLT_ORA_HOST (set in {clone_dir / '.env'}; {only}); "
                        f"SLT_PG_HOST (set in {proj / '.env'}; {only}); "
                        f"SLT_PG_PORT (set in environment, overrides {proj / '.env'})")


def _udf_case(root):
    d = root / "udf"
    d.mkdir(parents=True)
    (d / "test.yaml").write_text("name: udf\nrequires: [postgres]\nudf:\n  jar: java/Udf\n")
    return d


def _upload_checks(proj, case, **env):
    checks = doctor.run_checks({"SLT_PROJECT_ROOT": str(proj), **env}, REPO, cases=[case], probe=AUTH_OK,
                               tcp=lambda h, p: None, login={"postgres": lambda base: None},
                               docker=UP, running=lambda c: False)
    return [c for c in checks if c.subject == "uploads"]


def test_uploads_native_needs_the_servers_own_uploadedfiles(proj, tmp_path):
    # STRIIM_HOME named another install, with no UploadedFiles/, and doctor passed
    case, other = _udf_case(tmp_path / "suite"), tmp_path / "other-install"
    other.mkdir()
    c, = _upload_checks(proj, case, STRIIM_URL="http://localhost:49080", STRIIM_HOME=str(other))
    assert c.status == doctor.FAIL and c.code == CONFIG
    assert c.message == (f"{other / 'UploadedFiles'} does not exist, and udf upload(s) into it: STRIIM_HOME is not "
                         f"the running server's install; export STRIIM_HOME=<the install root of the Striim "
                         f"server at http://localhost:49080, on this host>")
    c, = _upload_checks(proj, case, STRIIM_URL="http://localhost:49080")
    assert c.status == doctor.FAIL and "STRIIM_HOME is unset" in c.message
    (other / "UploadedFiles").mkdir()
    c, = _upload_checks(proj, case, STRIIM_URL="http://localhost:49080", STRIIM_HOME=str(other))
    assert c.status == doctor.OK_ and c.message == f"{other / 'UploadedFiles'} is writable (udf)"
    (other / "UploadedFiles").chmod(0o500)
    try:
        c, = _upload_checks(proj, case, STRIIM_URL="http://localhost:49080", STRIIM_HOME=str(other))
        assert c.status == doctor.FAIL and "is not writable" in c.message
    finally:
        (other / "UploadedFiles").chmod(0o700)


def test_uploads_not_checked_in_docker_mode_or_without_an_uploading_case(proj, tmp_path):
    assert _upload_checks(proj, _udf_case(tmp_path / "suite")) == []
    plain = case(tmp_path / "suite", "plain", "[postgres]")
    assert _upload_checks(proj, plain, STRIIM_URL="http://localhost:49080") == []


def test_env_check_accepts_service_settings_in_dotenv(proj):
    assert not fails(env_checks(proj, "SLT_PG_HOST=db.example\nINT_PG_HOST=db.example\n"))


def test_service_check_probes_a_host_set_in_dotenv(clone_dir, proj, tmp_path):
    (proj / ".env").write_text("SLT_PG_HOST=db.example\n")
    case = tmp_path / "case"
    case.mkdir()
    (case / "test.yaml").write_text("requires: [postgres]\n")
    seen = []
    checks = doctor.run_checks({"SLT_PROJECT_ROOT": str(proj), "STRIIM_URL": "http://h:9080"}, REPO,
                               cases=[case], probe=AUTH_OK, tcp=lambda h, p: seen.append(h),
                               login={"postgres": lambda base: None})
    c, = [c for c in checks if c.subject == "service postgres"]
    assert c.status == doctor.OK_ and c.message.startswith("customer-provided (SLT_PG_HOST=db.example) at db.example:")
    assert seen == ["db.example"]


def _ledger(state: Path, run: str) -> Path:
    import json
    from livetest import ownership
    d = state / "lifecycle" / "ledgers"
    d.mkdir(parents=True)
    (d / "t1.json").write_text(json.dumps({"ledgerVersion": ownership.LEDGER_VERSION,
                                           "identity": {"runId": run, "case": "hello-single"},
                                           "entries": [{"kind": "pg-slot", "name": "slt_t1", "state": "confirmed"}]}))
    return d / "t1.json"


def test_targets_ownership_and_ledgers_read_what_the_projects_child_reads(clone_dir, project, tmp_path,
                                                                           monkeypatch):
    monkeypatch.delenv("GOLD_TARGETS", raising=False)
    root = Path(project).parent
    (clone_dir / ".env").write_text("SLT_INFRA_OWNERSHIP=exclusive\n")
    (root / ".env").write_text("SLT_INFRA_OWNERSHIP=shared\nSLT_KEEP_SERVICES=1\n")
    ledger = _ledger(root / ".state", "r-project")
    (tmp_path / "clone-state").mkdir()
    env = {"SLT_STATE_DIR": str(tmp_path / "clone-state")}

    def lines(location):
        checks = doctor.run_checks(env, REPO, probe=AUTH_OK, location=location)
        return {c.subject: c for c in checks if c.subject in ("ownership", "ledgers")}

    before = lines(None)                                   # no manifest: the clone's .env and state
    assert before["ownership"].message.startswith(f"exclusive (SLT_INFRA_OWNERSHIP, set in {clone_dir / '.env'};")
    assert before["ledgers"].line() == "[ ok ] ledgers: no leftover ownership ledgers"

    location, bad = doctor.project_location(project, env)
    assert not bad and location["SLT_PROJECT_ROOT"] == str(root.resolve())
    after = lines(location)
    assert after["ownership"].message.startswith(f"shared (SLT_INFRA_OWNERSHIP, set in {root.resolve() / '.env'};")
    assert after["ledgers"].status == doctor.WARN and str(ledger.resolve()) in after["ledgers"].message

    (root / ".env").write_text("STRIIM_URL=http://h:9080\n")          # the project leaves it unset: the clone's
    assert lines(location)["ownership"].message.startswith(
        f"exclusive (SLT_INFRA_OWNERSHIP, set in {clone_dir / '.env'};")


def test_targets_without_a_manifest_change_nothing_and_a_bad_one_is_named(tmp_path, monkeypatch):
    monkeypatch.delenv("GOLD_TARGETS", raising=False)
    assert doctor.project_location(None, {}) == ({}, [])
    location, (c,) = doctor.project_location(str(tmp_path / "nope.yaml"), {})
    assert location == {} and c.status == doctor.FAIL and c.subject == "targets"


def test_ledgers_of_a_state_dir_not_created_yet(tmp_path):
    # A fresh project's stateDir does not exist until its first run
    c, = doctor.check_ledgers({"SLT_STATE_DIR": str(tmp_path / "fresh" / ".state")}, {})
    assert c.line() == f"[ ok ] ledgers: no leftover ownership ledgers (no state yet at {tmp_path / 'fresh' / '.state'})"


def test_exit_codes():
    ok, warn = doctor._ok("a", "b"), doctor._warn("a", "b")
    assert doctor.exit_code([ok, warn]) == OK
    assert doctor.exit_code([ok, doctor._fail("a", "b", INFRA)]) == INFRA
    assert doctor.exit_code([doctor._fail("a", "b", INFRA), doctor._fail("a", "b")]) == CONFIG


# --- the command ------------------------------------------------------------------------------

def test_doctor_command_names_each_problem(clone, trap, elsewhere):
    """End to end from a copied clone: a .env typo, a bad STRIIM_URL and a missing case, one
    line each, exit 2. The trap proves no docker call and no connect (the URL is refused before
    any probe)."""
    (clone / ".env").write_text("SLT_LIVE_CASE=cases\nSTRIIM_URL=striim.example.com:9080\n")
    # Without the suite's SLT_PRE_UP=0: doctor runs no hook, and it would list the switch as a
    # service setting.
    r = run_cli(["doctor", "--case", "missing"], cwd=elsewhere,
                env=trap.env(clone_env(clone, SLT_PRE_UP=None)))
    assert r.rc == 2, r.stderr
    assert r.stdout.splitlines() == [
        f"[ ok ] env: {clone / '.env'} (2 keys)",
        f"[FAIL] env: SLT_LIVE_CASE (set in {clone / '.env'}) is not a key the framework reads, "
        f"likely a typo; did you mean SLT_LIVE_CASES?",
        "[ ok ] service settings: none set; each required service runs in Docker with its defaults",
        "[FAIL] ownership: SLT_INFRA_OWNERSHIP is unset, so live runs will be refused; set "
        "SLT_INFRA_OWNERSHIP=shared (with SLT_KEEP_SERVICES=1) to reuse a kept stack, or "
        "SLT_INFRA_OWNERSHIP=exclusive for a stack this run owns",
        "[ ok ] ledgers: no leftover ownership ledgers",
        f"[FAIL] striim: STRIIM_URL='striim.example.com:9080' has no scheme; write it as "
        f"http://striim.example.com:9080 (set in {clone / '.env'})",
        f"[FAIL] case: missing: no test.yaml there ({elsewhere / 'missing'})",
        "doctor: 4 problem(s)",
    ]
    assert trap.calls() == []
    assert not [e for e in trap.audit() if e["event"] == "socket.connect"]
    assert r.run_dir is None


def test_doctor_command_clean(clone, elsewhere, tmp_path):
    """A clean setup exits 0: Striim at a URL that authenticates (a local stub), no case."""
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            body = b'{"token": "t"}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{srv.server_port}"
        (clone / ".env").write_text("SLT_INFRA_OWNERSHIP=shared\nSLT_KEEP_SERVICES=1\n")
        r = run_cli(["doctor"], cwd=elsewhere,
                    env=clone_env(clone, STRIIM_URL=url, STRIIM_PASS="pw",
                                  SLT_PRE_UP=None))   # as above
    finally:
        srv.shutdown()
    assert r.rc == 0, (r.stdout, r.stderr)
    assert r.stdout.splitlines()[1:] == [
        "[ ok ] service settings: none set; each required service runs in Docker with its defaults",
        f"[ ok ] ownership: shared (SLT_INFRA_OWNERSHIP, set in {clone / '.env'}; striim-test only, export "
        f"for direct pytest); SLT_KEEP_SERVICES=1 (set in {clone / '.env'}; striim-test only, export for "
        f"direct pytest)",
        "[ ok ] ledgers: no leftover ownership ledgers",
        f"[ ok ] striim: {url} authenticated as 'admin'",
        "doctor: all checks passed"]


def test_a_malformed_api_timeout_fails_doctor(proj):
    # The project's .env as the run reads it: with --targets, run_checks passes the manifest's
    # location env (SLT_PROJECT_ROOT) here, not the bare shell env.
    (proj / ".env").write_text("STRIIM_API_TIMEOUT=10s\n")
    checks = doctor.check_api_timeout({"SLT_PROJECT_ROOT": str(proj)})
    bad = [c for c in checks if "STRIIM_API_TIMEOUT" in c.message]
    assert bad and bad[0].status == "FAIL" and str(proj / ".env") in bad[0].message


def test_a_valid_api_timeout_passes_doctor(proj):
    checks = doctor.check_api_timeout({"SLT_PROJECT_ROOT": str(proj), "STRIIM_API_TIMEOUT": "5,30"})
    assert [c.status for c in checks if "STRIIM_API_TIMEOUT" in c.message] == ["ok"]


def test_an_api_timeout_from_machine_env_is_reported_against_machine_env(proj, tmp_path, monkeypatch):
    machine = tmp_path / "machine.env"
    monkeypatch.setattr(paths, "machine_env_path", lambda *a, **k: machine)
    monkeypatch.setattr(paths, "machine_values", lambda *a, **k: {"STRIIM_API_TIMEOUT": "10s"})
    checks = doctor.check_api_timeout({"SLT_PROJECT_ROOT": str(proj)})
    bad = [c for c in checks if "STRIIM_API_TIMEOUT" in c.message]
    assert bad and bad[0].status == "FAIL" and str(machine) in bad[0].message
