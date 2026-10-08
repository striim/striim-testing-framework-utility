"""Docker free space gates the Striim image build, and a cluster that never becomes reachable
leaves its nodes' logs in the run dir (license redacted).

On a Mac run, Docker Desktop's VM had 22.8 GB free, the 21.9 GB image build filled it, the
primary's Derby could not create its temp dir, and the only message was "provisioned cluster
did not become reachable in time" -- nothing in the run dir said why.
"""
import types
from pathlib import Path

import pytest

from livetest import docker_disk
from livetest import striim_provision as sp
from livetest.docker_disk import vm_free_bytes   # the real one: conftest stubs the module attr

GB = 1000 ** 3


def _r(stdout="", rc=0):
    return types.SimpleNamespace(stdout=stdout, returncode=rc, stderr="")


def _docker(answers):
    """A fake runner: the first answers key that prefixes argv wins; records every call."""
    calls = []

    def run(argv, cwd=None):
        calls.append(list(argv))
        for prefix, out in answers.items():
            if list(argv[:len(prefix)]) == list(prefix):
                return out(argv) if callable(out) else out
        return _r("", 1)
    run.calls = calls
    return run


# --- measuring --------------------------------------------------------------------------------

def test_linux_reads_the_docker_root_dir_directly(tmp_path):
    run = _docker({("docker", "info"): _r(f"{tmp_path}|Ubuntu 24.04.3 LTS\n")})
    free, how = vm_free_bytes(run)
    assert free and free > 0
    assert str(tmp_path) in how
    assert not [c for c in run.calls if c[:2] == ["docker", "run"]]


def test_docker_desktop_runs_df_in_a_local_image_without_pulling():
    df = ("Filesystem     1024-blocks      Used Available Capacity Mounted on\n"
          "overlay          153204736 124466176  20971520      86% /\n")
    run = _docker({
        ("docker", "info"): _r("/var/lib/docker|Docker Desktop\n"),
        ("docker", "image", "ls"): _r("slt-striim:5.4.2\nalpine:latest\nmysql:8.0\n"),
        ("docker", "run"): _r(df),
    })
    free, how = vm_free_bytes(run)
    assert free == 20971520 * 1024
    probe = [c for c in run.calls if c[:2] == ["docker", "run"]][0]
    assert "--rm" in probe and probe[probe.index("--pull") + 1] == "never"
    assert "alpine:latest" in probe, "the small image is preferred over the 22 GB one"


def test_no_local_image_to_probe_is_unknown_not_a_guess():
    run = _docker({
        ("docker", "info"): _r("/var/lib/docker|Docker Desktop\n"),
        ("docker", "image", "ls"): _r("slt-striim:5.4.2\n"),
    })
    free, how = vm_free_bytes(run)
    assert free is None and "alpine" in how
    assert not [c for c in run.calls if c[:2] == ["docker", "run"]]


def test_docker_not_answering_is_unknown():
    free, how = vm_free_bytes(_docker({}))
    assert free is None


def test_a_runner_that_raises_is_unknown():
    def boom(argv, cwd=None):
        raise OSError("no docker")
    assert vm_free_bytes(boom)[0] is None


# --- the floor --------------------------------------------------------------------------------

def test_the_build_floor_is_above_what_was_measured_as_not_enough():
    assert docker_disk.MIN_FREE_BUILD_GB >= 35
    assert docker_disk.shortfall(int(22.8 * GB), building=True, env={})
    assert docker_disk.shortfall(40 * GB, building=True, env={}) is None


def test_a_built_image_needs_only_the_run_floor():
    assert docker_disk.shortfall(20 * GB, building=False, env={}) is None
    assert docker_disk.shortfall(1 * GB, building=False, env={})


def test_the_build_floor_is_overridable():
    env = {docker_disk.MIN_FREE_ENV: "20"}
    assert docker_disk.shortfall(int(22.8 * GB), building=True, env=env) is None


def test_unknown_free_space_is_not_a_shortfall():
    assert docker_disk.shortfall(None, building=True, env={}) is None


# --- the pre-build gate -----------------------------------------------------------------------

def _no_image(argv):
    return _r("", 0)


def test_the_build_is_refused_when_docker_is_short_of_space(tmp_path, monkeypatch):
    monkeypatch.setattr(docker_disk, "vm_free_bytes", lambda run=None: (int(22.8 * GB), "t"))
    with pytest.raises(sp.StriimDiskError, match="22.8 GB free"):
        sp.ensure_image(tmp_path, {"STRIIM_VERSION": "5.4.2"},
                        run=lambda argv, cwd=None: pytest.fail("built on a full disk"),
                        query_run=_no_image)


def test_the_build_goes_ahead_with_enough_space(tmp_path, monkeypatch):
    monkeypatch.setattr(docker_disk, "vm_free_bytes", lambda run=None: (60 * GB, "t"))
    built = []
    sp.ensure_image(tmp_path, {"STRIIM_VERSION": "5.4.2"},
                    run=lambda argv, cwd=None: built.append(argv), query_run=_no_image)
    assert built and built[0][:3] == ["docker", "compose", "build"]


def test_unmeasurable_space_warns_and_builds(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(docker_disk, "vm_free_bytes", lambda run=None: (None, "no probe"))
    built = []
    sp.ensure_image(tmp_path, {"STRIIM_VERSION": "5.4.2"},
                    run=lambda argv, cwd=None: built.append(argv), query_run=_no_image)
    assert built
    assert "could not measure" in capsys.readouterr().out


def test_a_current_image_is_reused_without_measuring(tmp_path, monkeypatch):
    monkeypatch.setattr(sp, "image_present", lambda v, run=None: True)
    monkeypatch.setattr(sp, "image_inputs_match", lambda v, sd, run=None: True)
    monkeypatch.setattr(docker_disk, "vm_free_bytes",
                        lambda run=None: pytest.fail("measured for a reuse"))
    sp.ensure_image(tmp_path, {"STRIIM_VERSION": "5.4.2"},
                    run=lambda argv, cwd=None: pytest.fail("rebuilt"))


# --- node logs on a health timeout ------------------------------------------------------------

_PRIMARY_LOG = """\
WAClusterName=c1
ProductKey=PK-SECRET-1234
LicenceKey=LK-SECRET-5678
java.nio.file.NoSuchFileException: /var/striim/wactionrepos/tmp
also the raw value PK-SECRET-1234 in a stack trace
"""


def test_cluster_logs_are_saved_with_the_license_redacted(tmp_path, monkeypatch):
    monkeypatch.setenv("PRODUCT_KEY", "PK-SECRET-1234")
    monkeypatch.setenv("LICENCE_KEY", "LK-SECRET-5678")
    run = _docker({
        ("docker", "compose"): _r("slt-striim\nslt-node\nslt-agent\n"),
        ("docker", "logs"): lambda argv: _r(_PRIMARY_LOG if argv[-1] == "slt-striim"
                                            else "Waiting for primary node to start\n"),
    })
    out = sp.save_cluster_logs(tmp_path / "cluster-logs", tmp_path / "striim", run=run)
    assert out == tmp_path / "cluster-logs"
    saved = {p.name: p.read_text() for p in out.iterdir()}
    assert set(saved) == {"slt-striim.log", "slt-node.log", "slt-agent.log"}
    text = saved["slt-striim.log"]
    assert "wactionrepos/tmp" in text, "the log is what the operator needs"
    for secret in ("PK-SECRET-1234", "LK-SECRET-5678"):
        assert secret not in "".join(saved.values())
    assert "ProductKey=<redacted>" in text
    tails = [c for c in run.calls if c[:2] == ["docker", "logs"]]
    assert tails and all("--tail" in c for c in tails)


def test_saving_logs_never_raises(tmp_path):
    def boom(argv, cwd=None):
        raise OSError("docker gone")
    assert sp.save_cluster_logs(tmp_path / "x", tmp_path, run=boom) is None


def test_the_health_timeout_saves_the_logs_and_names_them():
    src = (Path(__file__).resolve().parents[1] / "livetest" / "plugin.py").read_text()
    i = src.index('"provisioned cluster did not become reachable in time')
    window = src[i - 600:i + 400]
    assert "save_cluster_logs" in window


def test_the_cluster_ready_timeout_saves_the_logs_too():
    # R1 Mac run: the primary answered, the node died on a stale keystore, and the run failed
    # with "not fully formed" and no logs kept.
    src = (Path(__file__).resolve().parents[1] / "livetest" / "plugin.py").read_text()
    i = src.index("Striim cluster is not fully formed")
    window = src[i - 600:i + 200]
    assert "save_cluster_logs" in window


def test_saved_logs_redact_the_servers_colon_form_without_the_env_values(tmp_path, monkeypatch):
    monkeypatch.delenv("PRODUCT_KEY", raising=False)
    monkeypatch.delenv("LICENCE_KEY", raising=False)
    log = "x[GMT] ProductKey: PK-SECRET-1\ny[GMT] License Key: LK-SECRET-2\nok line\n"
    run = _docker({("docker", "compose"): _r("slt-striim\n"), ("docker", "logs"): _r(log)})
    out = sp.save_cluster_logs(tmp_path / "logs", tmp_path, run=run)
    text = (out / "slt-striim.log").read_text()
    assert "PK-SECRET-1" not in text and "LK-SECRET-2" not in text and "ok line" in text


# Review R1: a present-but-stale image (the tag exists, the checkout's build inputs changed)
# rebuilds from the build cache at ~0 GB (measured on the R1 and R2 Mac runs), so it is gated
# at the run floor, as doctor already does, not at the cold-build floor.

def test_a_stale_image_rebuild_is_gated_at_the_run_floor_not_the_build_floor(tmp_path, monkeypatch):
    monkeypatch.setattr(sp, "image_present", lambda v, run=None: True)
    monkeypatch.setattr(sp, "image_inputs_match", lambda v, sd, run=None: False)
    monkeypatch.setattr(docker_disk, "vm_free_bytes", lambda run=None: (25 * GB, "t"))
    said = []
    built = []
    sp.ensure_image(tmp_path, {"STRIIM_VERSION": "5.4.2"}, run=lambda argv, cwd=None: built.append(argv),
                    progress=lambda k, m: said.append(m))
    assert built and built[0][:3] == ["docker", "compose", "build"]
    assert any("rebuilding from cache" in m for m in said), said


def test_a_stale_image_rebuild_still_needs_room_to_run(tmp_path, monkeypatch):
    monkeypatch.setattr(sp, "image_present", lambda v, run=None: True)
    monkeypatch.setattr(sp, "image_inputs_match", lambda v, sd, run=None: False)
    monkeypatch.setattr(docker_disk, "vm_free_bytes", lambda run=None: (1 * GB, "t"))
    with pytest.raises(sp.StriimDiskError, match="running the Striim cluster needs at least 5"):
        sp.ensure_image(tmp_path, {"STRIIM_VERSION": "5.4.2"},
                        run=lambda argv, cwd=None: pytest.fail("built on a full disk"))


def test_no_image_at_all_is_still_the_cold_build_floor(tmp_path, monkeypatch):
    monkeypatch.setattr(sp, "image_present", lambda v, run=None: False)
    monkeypatch.setattr(docker_disk, "vm_free_bytes", lambda run=None: (25 * GB, "t"))
    with pytest.raises(sp.StriimDiskError, match="building the Striim image needs at least 35"):
        sp.ensure_image(tmp_path, {"STRIIM_VERSION": "5.4.2"},
                        run=lambda argv, cwd=None: pytest.fail("built"))


# Review R2: on the STRIIM_HOME route the license values are in os.environ only inside
# cluster_up's override, so the raw-value pass had nothing to replace when the logs were saved.

def test_saved_logs_redact_raw_values_taken_from_striim_home(tmp_path, monkeypatch):
    for var in ("PRODUCT_KEY", "LICENCE_KEY", "COMPANY_NAME", "CLUSTER_NAME"):
        monkeypatch.delenv(var, raising=False)
    home = tmp_path / "Striim"
    (home / "conf").mkdir(parents=True)
    (home / "conf" / "startUp.properties").write_text(
        "WAClusterName=c\nCompanyName=co\nProductKey=PK-HOME-1111\nLicenceKey=LK-HOME-2222\n")
    monkeypatch.setenv("STRIIM_HOME", str(home))
    log = ("invalid license key LK-HOME-2222 rejected\n"
           "PRODUCT_KEY=PK-HOME-1111\n"
           "licensekey: LK-HOME-2222\n"
           "an ordinary line\n")
    run = _docker({("docker", "compose"): _r("slt-striim\n"), ("docker", "logs"): _r(log)})
    out = sp.save_cluster_logs(tmp_path / "logs", tmp_path, run=run)
    text = (out / "slt-striim.log").read_text()
    assert "PK-HOME-1111" not in text and "LK-HOME-2222" not in text
    assert "an ordinary line" in text


def test_saved_logs_redact_every_key_spelling_without_any_values(tmp_path, monkeypatch):
    for var in ("PRODUCT_KEY", "LICENCE_KEY", "STRIIM_HOME"):
        monkeypatch.delenv(var, raising=False)
    log = "PRODUCT_KEY=aa1\nLICENCE_KEY=bb2\nLicenseKey: cc3\nproduct key = dd4\n"
    run = _docker({("docker", "compose"): _r("slt-striim\n"), ("docker", "logs"): _r(log)})
    text = (sp.save_cluster_logs(tmp_path / "logs", tmp_path, run=run) / "slt-striim.log").read_text()
    for v in ("aa1", "bb2", "cc3", "dd4"):
        assert v not in text
