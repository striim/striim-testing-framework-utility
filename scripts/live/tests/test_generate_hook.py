"""Hermetic tests for the manifest `generate:` execution hook (plugin._run_generate_specs).

No Striim, no Docker and no ggtrail package: `plugin.GENERATORS` is monkeypatched with a fake
generator that writes stand-in trail/def files, and the two striimfile placement calls are
recorded instead of executed -- so this exercises the when-filtering, the dest rendering and
the "place everything the generator produced" contract on its own.
"""
from pathlib import Path
import subprocess
import sys
import textwrap

import pytest

from livetest import plugin
from livetest.manifest import load_manifest

_LIVE = Path(__file__).resolve().parents[1]
_TOKENS = {"NS": "ns1", "APP": "ns1.App", "TID": "t1_"}

class _Ctx:
    # place_server_file / ensure_server_dir are faked, so nothing here is ever dereferenced.
    mode = "native"

def _fake_generate(workload: Path, out_dir: Path) -> dict:
    # Stands in for livetest.ggtrail.runner.generate_from_yaml: writes the files a real run
    # would produce and returns the documented contract dict.
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    trail_files = []
    for name in ("rt000000", "rt000001"):
        f = out_dir / name
        f.write_bytes(b"trail-bytes")
        trail_files.append(f)
    def_file = out_dir / "schema.def"
    def_file.write_text("Table SCOTT.CUSTOMERS;\n")
    expected_dir = out_dir / "expected"
    expected_dir.mkdir(exist_ok=True)
    (expected_dir / "ops.csv").write_text("TABLE_NAME,OP_TYPE\n")
    return {
        "trail_files": trail_files,
        "def_file": def_file,
        "expected_dir": expected_dir,
        "summary": {"ops": 2, "workload": str(workload)},
    }

@pytest.fixture
def placements(monkeypatch):
    # Record what the hook would put on the server instead of shelling out to docker cp.
    calls = {"ensure": [], "place": [], "generated": []}

    def _fake_gen(workload, out_dir):
        calls["generated"].append((Path(workload), Path(out_dir)))
        return _fake_generate(workload, out_dir)

    monkeypatch.setattr(plugin, "GENERATORS", {"fake": _fake_gen})
    monkeypatch.setattr(plugin, "ensure_server_dir",
                        lambda ctx, dest: calls["ensure"].append(dest))
    monkeypatch.setattr(plugin, "place_server_file",
                        lambda ctx, src, dest: calls["place"].append((Path(src).name, dest)))
    return calls

def _manifest(tmp_path: Path, when: str = "post_start", kind: str = "fake"):
    d = tmp_path / "gen-test"
    d.mkdir()
    (d / "workload.yaml").write_text("seed: 42\n")
    (d / "test.yaml").write_text(textwrap.dedent(f"""
        name: gen-test
        tql: app.tql
        generate:
          - kind: {kind}
            workload: workload.yaml
            dest: '/tmp/${{NS}}-gg/'
            when: {when}
        assert:
          smoke: true
    """))
    return load_manifest(d / "test.yaml")

# --- placement ---------------------------------------------------------------------

def test_places_every_produced_file_into_rendered_dest(tmp_path, placements):
    m = _manifest(tmp_path)
    placed = plugin._run_generate_specs(m, "post_start", _TOKENS, _Ctx())
    # every trail file AND the def file, each under dest joined with its own basename,
    # with ${NS} rendered
    assert placed == ["/tmp/ns1-gg/rt000000", "/tmp/ns1-gg/rt000001", "/tmp/ns1-gg/schema.def"]
    assert placements["place"] == [
        ("rt000000", "/tmp/ns1-gg/rt000000"),
        ("rt000001", "/tmp/ns1-gg/rt000001"),
        ("schema.def", "/tmp/ns1-gg/schema.def"),
    ]
    assert placements["ensure"] == placed        # dir ensured for each file placed

def test_generator_is_called_with_the_resolved_workload_and_a_fresh_tmp_dir(tmp_path, placements):
    m = _manifest(tmp_path)
    plugin._run_generate_specs(m, "post_start", _TOKENS, _Ctx())
    (workload, out_dir), = placements["generated"]
    assert workload == (tmp_path / "gen-test" / "workload.yaml").resolve()
    assert out_dir.is_dir() and out_dir != workload.parent      # local scratch, not the test dir

def test_report_callback_is_invoked(tmp_path, placements):
    m = _manifest(tmp_path)
    seen = []
    plugin._run_generate_specs(m, "post_start", _TOKENS, _Ctx(),
                               report=lambda name, msg: seen.append((name, msg)))
    assert seen and seen[0][0] == "gen-test" and "fake" in seen[0][1]

# --- when-filtering ----------------------------------------------------------------

def test_when_filtering_skips_the_other_phase(tmp_path, placements):
    m = _manifest(tmp_path, when="post_start")
    assert plugin._run_generate_specs(m, "pre_deploy", _TOKENS, _Ctx()) == []
    assert placements["generated"] == []          # the generator never ran
    assert placements["place"] == []

def test_pre_deploy_spec_runs_at_pre_deploy(tmp_path, placements):
    m = _manifest(tmp_path, when="pre_deploy")
    assert plugin._run_generate_specs(m, "post_start", _TOKENS, _Ctx()) == []
    assert plugin._run_generate_specs(m, "pre_deploy", _TOKENS, _Ctx()) == [
        "/tmp/ns1-gg/rt000000", "/tmp/ns1-gg/rt000001", "/tmp/ns1-gg/schema.def"]

def test_no_generate_specs_is_a_noop(tmp_path, placements):
    d = tmp_path / "plain"
    d.mkdir()
    (d / "test.yaml").write_text("name: plain\ntql: app.tql\nassert:\n  smoke: true\n")
    m = load_manifest(d / "test.yaml")
    assert plugin._run_generate_specs(m, "post_start", _TOKENS, _Ctx()) == []
    assert placements["place"] == []

# --- dest directory pre-creation ---------------------------------------------------

def test_ensure_generate_dirs_creates_every_dest_dir(tmp_path, placements):
    # Even a post_start spec's dir is created before deploy, so a FileReader watching it
    # sees the directory at deploy time (ensure_server_dir mkdir -p's the path's PARENT,
    # hence the .keep suffix).
    m = _manifest(tmp_path, when="post_start")
    plugin._ensure_generate_dirs(m, _TOKENS, _Ctx())
    assert placements["ensure"] == ["/tmp/ns1-gg/.keep"]
    assert str(Path(placements["ensure"][0]).parent) == "/tmp/ns1-gg"

# --- unknown kind ------------------------------------------------------------------

def test_unknown_kind_fails_informatively(tmp_path, placements):
    m = _manifest(tmp_path, kind="nope")
    with pytest.raises(pytest.fail.Exception) as e:
        plugin._run_generate_specs(m, "post_start", _TOKENS, _Ctx())
    msg = str(e.value)
    assert "unknown generate kind 'nope'" in msg
    assert "known kinds: ['fake']" in msg          # names what IS registered
    assert placements["place"] == []

# --- registry ----------------------------------------------------------------------

def test_ggtrail_is_registered():
    assert "ggtrail" in plugin.GENERATORS

def test_plugin_import_does_not_pull_in_ggtrail():
    # Importing the plugin must NOT drag in the (optional) ggtrail package -- the import
    # lives inside _run_ggtrail, so a checkout without the package still collects and runs
    # every other test. Checked in a SUBPROCESS: within this suite sibling ggtrail tests
    # have already imported the package, so this process's sys.modules proves nothing.
    r = subprocess.run(
        [sys.executable, "-c",
         "import livetest.plugin, sys; print(any(m == 'livetest.ggtrail' or "
         "m.startswith('livetest.ggtrail.') for m in sys.modules))"],
        capture_output=True, text=True, cwd=_LIVE)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "False", f"livetest.plugin eagerly imported ggtrail: {r.stdout}"

def test_generated_dest_joins_dir_and_basename():
    assert plugin._generated_dest("/tmp/ns1-gg/", "rt000000") == "/tmp/ns1-gg/rt000000"
    assert plugin._generated_dest("/tmp/ns1-gg", "rt000000") == "/tmp/ns1-gg/rt000000"

# --- the shipped regression manifest ------------------------------------------------

def test_shipped_ggtrail_regression_manifest_loads():
    ty = _LIVE / "regression" / "services" / "ggtrail" / "ggtrail-cdc-file-diff" / "test.yaml"
    m = load_manifest(ty)
    (spec,) = m.generate_specs
    assert spec["kind"] == "ggtrail"
    assert spec["when"] == "pre_deploy"
    assert spec["dest"] == "/tmp/${NS}-gg/"
    assert spec["workload"] == (ty.parent / "workload.yaml").resolve()
    assert m.requires == [] and m.topology == "single" and m.timeout == 180
    assert m.purpose and "\n" not in m.purpose
    assert (ty.parent / "app.tql").is_file()
