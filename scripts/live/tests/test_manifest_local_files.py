"""`local: true` on a ddl/seed/upload entry of an `example:` case: the file is read from the CASE
dir, not the example dir, so test-only material need not ship in the published example."""
from pathlib import Path
import textwrap

import pytest

from livetest import enforcement, inputs, manifest, plugin
from livetest.manifest import ManifestError, load_manifest


@pytest.fixture
def dirs(tmp_path, monkeypatch):
    """(root, example dir, case dir); `example: ex` resolves to the example dir."""
    root = tmp_path / "repo"
    example = root / "ex"
    case = root / "cases" / "c1"
    example.mkdir(parents=True)
    case.mkdir(parents=True)
    (example / "app.tql").write_text("CREATE APPLICATION a;\nEND APPLICATION a;\n")
    for name in ("target_pg_ddl.sql", "source_seed.sql", "config.json", "blob.txt"):
        (example / name).write_text("-- shipped\n")
    monkeypatch.setattr(manifest, "_root", lambda ex=None: example if ex else root)
    return root, example, case


def _case(case: Path, body: str) -> Path:
    (case / "test.yaml").write_text(textwrap.dedent(body))
    return case / "test.yaml"


_FULL = """
    name: c1
    example: ex
    tql: app.tql
    ddl:
      - file: target_pg_ddl.sql
      - file: test_views.sql
        local: true
    seed:
      - file: source_seed.sql
      - file: recovery_seed.sql
        local: true
        when: post_recover
    recover: {mode: kill}
    server_files:
      - {file: blob.txt, dest: "/tmp/${NS}/blob.txt"}
    op:
      jar: java/OpenProcessors/MyOp
      upload:
        - {from: config.json, to: "${NS}-config.json"}
        - {from: config_kill.json, to: "${NS}-kill.json", local: true}
    action:
      - type: drop_recreate_app
        app: ${APP}
        seed:
          - {file: action_seed.sql, local: true}
        stopped_seed:
          - {file: stopped_seed.sql, local: true}
    assert:
      data: [{table: t, rows: 1}]
"""
_LOCAL = ("test_views.sql", "recovery_seed.sql", "config_kill.json", "action_seed.sql",
          "stopped_seed.sql")


def _full(case: Path) -> Path:
    for name in _LOCAL:
        (case / name).write_text("-- test-only\n")
    return _case(case, _FULL)


def test_local_entries_resolve_to_the_case_dir(dirs):
    _root, example, case = dirs
    m = load_manifest(_full(case))
    assert m.local_files == frozenset(_LOCAL)
    for name in _LOCAL:
        assert m.file_path(name) == case / name


def test_non_local_entries_still_resolve_to_the_example_dir(dirs):
    _root, example, case = dirs
    m = load_manifest(_full(case))
    assert m.source_dir == example
    for name in ("target_pg_ddl.sql", "source_seed.sql", "config.json"):
        assert m.file_path(name) == example / name
    # the upload entry shape is unchanged unless the flag is set
    assert m.op_uploads == [{"from": "config.json", "to": "${NS}-config.json"},
                            {"from": "config_kill.json", "to": "${NS}-kill.json", "local": True}]


def test_the_input_snapshot_reads_each_file_where_the_runner_does(dirs):
    _root, example, case = dirs
    p = _full(case)
    m = load_manifest(p)
    got = {path.name: path for _role, path in inputs.referenced(m, p)}
    for name in _LOCAL:
        assert got[name] == case / name
    assert got["target_pg_ddl.sql"] == example / "target_pg_ddl.sql"
    inputs.snapshot(m, p)            # strict: every referenced file exists where it is looked for


def test_upload_reads_a_local_file_from_the_case_dir(dirs, monkeypatch):
    _root, _example, case = dirs
    m = load_manifest(_full(case))
    (case / "config_kill.json").write_text('{"table": "${NS}_t"}')
    sent = []
    monkeypatch.setattr(plugin.opartifacts, "upload_artifacts", lambda ctx, paths: sent.extend(paths))
    paths, renames = plugin.upload_op_uploads(m, None, {"TID": "", "NS": "ns1"})
    assert renames == {"config.json": "ns1-config.json", "config_kill.json": "ns1-kill.json"}
    assert {p.name: p.read_text() for p in sent}["ns1-kill.json"] == '{"table": "ns1_t"}'


def test_a_missing_local_file_is_refused_at_load_and_never_falls_back(dirs):
    _root, example, case = dirs
    (example / "only_here.sql").write_text("-- shipped\n")   # present in the example dir only
    p = _case(case, """
        name: c1
        example: ex
        tql: app.tql
        seed:
          - {file: only_here.sql, local: true}
        assert: {smoke: true}
    """)
    with pytest.raises(ManifestError, match=r"local file 'only_here.sql' not found in the test dir"):
        load_manifest(p)


@pytest.mark.parametrize("name", ["../escape.sql", "sub/../../escape.sql", "/etc/escape.sql"])
def test_a_local_path_escaping_the_case_dir_is_refused(dirs, name):
    _root, _example, case = dirs
    (case.parent / "escape.sql").write_text("-- outside\n")
    p = _case(case, f"""
        name: c1
        example: ex
        tql: app.tql
        ddl:
          - {{file: "{name}", local: true}}
        assert: {{smoke: true}}
    """)
    with pytest.raises(ManifestError, match="escapes the test dir"):
        load_manifest(p)


def test_a_local_file_under_a_scan_exempt_dir_is_refused(dirs):
    _root, _example, case = dirs
    (case / "expected").mkdir()
    (case / "expected" / "seed.sql").write_text("-- hidden from the scan\n")
    p = _case(case, """
        name: c1
        example: ex
        tql: app.tql
        seed:
          - {file: expected/seed.sql, local: true}
        assert: {smoke: true}
    """)
    with pytest.raises(ManifestError, match="isolation scan skips"):
        load_manifest(p)


def test_local_without_example_is_refused(tmp_path):
    d = tmp_path / "c1"
    d.mkdir()
    (d / "seed.sql").write_text("-- x\n")
    p = _case(d, """
        name: c1
        tql: app.tql
        seed:
          - {file: seed.sql, local: true}
        assert: {smoke: true}
    """)
    with pytest.raises(ManifestError, match="'local: true' needs 'example:'"):
        load_manifest(p)


def test_a_name_declared_both_local_and_not_is_refused(dirs):
    _root, _example, case = dirs
    (case / "source_seed.sql").write_text("-- test copy\n")
    p = _case(case, """
        name: c1
        example: ex
        tql: app.tql
        seed:
          - file: source_seed.sql
          - {file: source_seed.sql, local: true, when: post_start}
        assert: {smoke: true}
    """)
    with pytest.raises(ManifestError, match="declared both 'local: true' and not"):
        load_manifest(p)


def test_local_must_be_a_boolean(dirs):
    _root, _example, case = dirs
    p = _case(case, """
        name: c1
        example: ex
        tql: app.tql
        ddl:
          - {file: x.sql, local: "yes"}
        assert: {smoke: true}
    """)
    with pytest.raises(ManifestError, match="'local' must be true or false"):
        load_manifest(p)


def test_the_isolation_scan_reads_local_files(dirs):
    root, example, case = dirs
    p = _full(case)
    (case / "test_views.sql").write_text("CREATE TABLE gorphan_late (id int);\n")
    (case / "notes.sql").write_text("CREATE TABLE never_named (id int);\n")   # not referenced
    m = load_manifest(p)
    files = enforcement.files_to_scan(m, p, root)
    assert {f for f in files if f.parent == case} == {p, *(case / n for n in _LOCAL)}
    assert example / "app.tql" in files
    hits = [v for f in files for v in enforcement.check_postgres_prefix(f, f.read_text())]
    assert [(v.file.name, v.message) for v in hits] == [
        ("test_views.sql", "untokenized CREATE TABLE: 'gorphan_late'")]
