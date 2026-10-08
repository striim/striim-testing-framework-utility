import os
import pytest
from livetest.striimfile import _check, StriimFileError


def test_check_accepts_plain_path():
    assert _check("/tmp/SLT_x-filewriter") == "/tmp/SLT_x-filewriter"


def test_check_rejects_shell_metachars():
    with pytest.raises(StriimFileError):
        _check("/tmp/out; rm -rf /")


def test_check_rejects_trailing_newline():
    # \Z (not $) anchors at the true end of string, so a trailing newline can't slip
    # through into the f-string shell command (e.g. `cat <path>*`).
    with pytest.raises(StriimFileError):
        _check("/tmp/out\n")
    with pytest.raises(StriimFileError):
        _check("/tmp/out\nrm -rf /")


# --- OP resume checkpoints must not survive a run ---------------------------------------
# Dropping the app and namespace does not remove them: they are plain files in Striim's
# working dir. Left behind, the next run's same-named app resumes from the previous run's
# position -- and for ChangeReader that is fatal, because the change stream's retention
# window has passed and the first partition query dies with OUT_OF_RANGE. Measured: 0 such
# errors in the first of two back-to-back full suite runs, 18 in the second.

class _Ctx:
    def __init__(self, mode="docker"):
        self.mode = mode


def test_clear_op_checkpoints_targets_this_namespace_and_known_suffixes():
    from livetest.striimfile import clear_op_checkpoints
    calls = []
    clear_op_checkpoints(_Ctx(), "SLT_changereader_raw_records",
                         run=lambda argv: calls.append(argv))
    assert calls, "must issue a removal on the app nodes"
    cmd = calls[0][-1]
    assert cmd.startswith("rm -f ")
    # anchored on the dot after the namespace -- see the prefix-sibling test below
    for suffix in (".position.json", ".position.bk", ".cursors.json", ".cursors.bk"):
        assert f"*SLT_changereader_raw_records.*{suffix}*" in cmd
    assert "/opt/striim/" in cmd


def test_clear_op_checkpoints_covers_every_app_node():
    from livetest import stack
    from livetest.striimfile import clear_op_checkpoints
    seen = []
    clear_op_checkpoints(_Ctx(), "SLT_x", run=lambda argv: seen.append(argv[2]))
    assert sorted(seen) == sorted(stack.app_nodes()), \
        "a single-topology app may run on either node, so clear both"


def test_clear_op_checkpoints_rejects_an_unsafe_namespace():
    from livetest.striimfile import clear_op_checkpoints, StriimFileError
    import pytest as _pytest
    with _pytest.raises(StriimFileError):
        clear_op_checkpoints(_Ctx(), "SLT_x; rm -rf /", run=lambda argv: None)


def _run_cleanup_in(tmp_path, namespace, names):
    """Create `names` in tmp_path, run the generated cleanup there with sh, return the survivors."""
    import subprocess
    from livetest.striimfile import clear_op_checkpoints
    for name in names:
        (tmp_path / name).write_text("{}")
    cmds = []
    clear_op_checkpoints(_Ctx(), namespace, run=lambda argv: cmds.append(argv[-1]))
    subprocess.run(["sh", "-c", cmds[0].replace("/opt/striim", str(tmp_path))], check=True)
    return {p.name for p in tmp_path.iterdir()}


def test_clear_op_checkpoints_does_not_touch_a_prefix_sibling_namespace(tmp_path):
    """The isolation claim, tested on the case that actually broke it.

    A bare `*<ns>*` substring glob also matches every namespace having this one as a PREFIX,
    and real pairs exist: reader-streaming-to-file vs -to-file-pg. Under
    SLT_PARALLEL that deleted a concurrently-running sibling's LIVE checkpoint and its
    crash-safety backup. An earlier version of this test used "SLT_other", which is not a
    prefix sibling, so it passed while the bug was present.
    """
    mine = ("ExampleReaderOp_h_SLT_reader_streaming_to_file.app__"
            "SLT_reader_streaming_to_file.Src.position.json")
    sibling = ("ExampleReaderOp_h_SLT_reader_streaming_to_file_pg.app__"
               "SLT_reader_streaming_to_file_pg.Src.position.json")
    left = _run_cleanup_in(tmp_path, "SLT_reader_streaming_to_file", [mine, sibling])
    assert mine not in left, "must clean its own"
    assert sibling in left, "must NOT remove the -pg sibling, whose OP may be writing it right now"


def test_clear_op_checkpoints_sweeps_tmp_and_the_un_namespaced_fallback(tmp_path):
    """Both stores write `<live>.tmp` during an atomic persist, and
    PositionCheckpointStore.resolveBaseName degrades to the bare prefix when the app and
    component names are unresolvable -- a checkpoint no namespace glob can match, which
    restore() will happily read back. The bare prefix is recognised by shape, so any OP's
    fallback is swept without naming it."""
    swept = ["ExampleReaderOp_h_SLT_mine.App__c.position.json.tmp",
             "ExampleReaderOp.position.json", "ExampleReaderOp.position.bk",
             "OtherReaderV2.cursors.json", "OtherReaderV2.cursors.json.tmp"]
    kept = ["ExampleReaderOp_h_SLT_other.App__c.position.json", "SLT_other.App.cursors.json",
            "Example-Op.position.json", "startUp.properties"]
    assert _run_cleanup_in(tmp_path, "SLT_mine", swept + kept) == set(kept)


def test_clear_op_checkpoints_is_a_noop_off_the_docker_cluster():
    """`mode == "native"` means "not our compose cluster", which includes a REMOTE Striim.
    Deleting local files then leaves the real server's checkpoints in place -- bug unfixed --
    while running rm over a directory never established to be the server's."""
    from livetest.striimfile import clear_op_checkpoints
    calls = []
    clear_op_checkpoints(_Ctx(mode="native"), "SLT_mine", run=lambda argv: calls.append(argv))
    assert calls == []


def test_clear_op_checkpoints_declines_to_guess_a_native_home(monkeypatch):
    # The first version fell back to the CONTAINER path "/opt/striim" when STRIIM_HOME was
    # unset. On a native host that is a real local directory which may have nothing to do
    # with this install -- and this feeds an `rm -f`. Declining to clean is the safe failure.
    from livetest import striimfile
    monkeypatch.delenv("STRIIM_HOME", raising=False)
    calls = []
    striimfile.clear_op_checkpoints(_Ctx(mode="native"), "SLT_x",
                                    run=lambda argv: calls.append(argv))
    assert calls == [], "with no STRIIM_HOME there is no directory we may delete from"


def test_clear_op_checkpoints_rejects_path_traversal_in_the_namespace():
    # _SAFE_PATH validates PATHS and so permits "/" and "." -- fine for a path, wrong for a
    # segment interpolated into an rm glob.
    from livetest.striimfile import clear_op_checkpoints, StriimFileError
    import pytest as _pytest
    for bad in ("../../etc", "SLT_x/../..", "SLT_x.y/z"):
        with _pytest.raises(StriimFileError):
            clear_op_checkpoints(_Ctx(), bad, run=lambda argv: None)


# --- FileWriter output discovery: the declared name AND its numeric rollover siblings ---
# Striim 5.4.2's RolloverFilenameFormat splits the name at its FIRST dot and puts the
# sequence before the extension: FileName 'rows.json' writes rows.00.json, rows.01.json, ...
# An extensionless name keeps the old shape (rows -> rows.00). The reader used to glob
# `<path>*` (rows.json*), which never sees rows.00.json: readiness polled zero rows and the
# pre-deploy clear left the files behind. Read and clear must select the same files, in
# Docker and native mode alike, and nothing else in the directory.

import subprocess as _subprocess
from types import SimpleNamespace as _NS


def _docker_ctx():
    return _NS(mode="docker")


def _native_ctx():
    return _NS(mode="native")


def _fake_docker(monkeypatch, nodes=("n1", "n2"), dead=()):
    """Run each `docker exec <node> ...` command locally, as the container would. A node in
    `dead` answers like a missing container does."""
    from livetest import striimfile
    monkeypatch.setattr(striimfile.stack, "app_nodes", lambda: tuple(nodes))
    calls = []

    def run(argv):
        calls.append(argv)
        assert argv[:2] == ["docker", "exec"] and argv[2] in nodes
        if argv[2] in dead:
            return _subprocess.CompletedProcess(
                argv, 1, "", f"Error response from daemon: No such container: {argv[2]}\n")
        return _subprocess.run(argv[3:], capture_output=True, text=True)
    return run, calls


def _populate(d, names):
    for n in names:
        (d / n).write_text(f"{n}\n")


def test_output_names_extension_rollover():
    from livetest.striimfile import _output_names
    got = _output_names("rows.json", ["rows.00.json", "rows.01.json", "rows.json", "other"])
    assert got == ["rows.json", "rows.00.json", "rows.01.json"]


def test_output_names_extensionless_legacy():
    from livetest.striimfile import _output_names
    assert _output_names("out", ["out", "out.00", "out.01", "out.x", "outer", "out.00.bak"]) == \
        ["out", "out.00", "out.01"]


def test_output_names_multi_dot_uses_the_first_dot():
    # The product splits at the FIRST dot: rows.a.json -> rows.00.a.json.
    from livetest.striimfile import _output_names
    names = ["rows.00.a.json", "rows.a.00.json", "rows.a.json", "rows.01.a.json"]
    assert _output_names("rows.a.json", names) == ["rows.a.json", "rows.00.a.json", "rows.01.a.json"]


def test_output_names_excludes_siblings():
    from livetest.striimfile import _output_names
    names = ["rows.json.bak", "rows2.00.json", "rowsX.json", "rows.00.jsonl", "rows.0a.json",
             "rows..json", "rows.00.json.tmp", "rows.00.csv", "xrows.00.json"]
    assert _output_names("rows.json", names) == []


def test_output_names_orders_by_sequence_and_deduplicates():
    from livetest.striimfile import _output_names
    got = _output_names("rows.json", ["rows.10.json", "rows.02.json", "rows.json", "rows.02.json"])
    assert got == ["rows.json", "rows.02.json", "rows.10.json"]


def test_output_names_keeps_legacy_append_for_an_extension_name():
    # The old <path>* glob also covered a sequence appended after the whole name.
    from livetest.striimfile import _output_names
    assert _output_names("rows.json", ["rows.json.00"]) == ["rows.json.00"]


@pytest.mark.parametrize("ctx_kind", ["docker", "native"])
def test_read_finds_the_542_rollover_file(tmp_path, monkeypatch, ctx_kind):
    from livetest.striimfile import read_server_files
    (tmp_path / "rows.00.json").write_text('{"id":1}\n{"id":2}\n')
    run, _ = _fake_docker(monkeypatch, nodes=("n1",))
    ctx = _docker_ctx() if ctx_kind == "docker" else _native_ctx()
    assert read_server_files(ctx, str(tmp_path / "rows.json"), run=run) == '{"id":1}\n{"id":2}\n'


@pytest.mark.parametrize("ctx_kind", ["docker", "native"])
def test_read_and_clear_select_the_same_files(tmp_path, monkeypatch, ctx_kind):
    from livetest.striimfile import read_server_files, clear_server_files
    mine = ["rows.json", "rows.00.json", "rows.01.json", "rows.json.00"]
    others = ["rows.json.bak", "rows2.00.json", "rowsX.json", "rows.00.csv", "other.txt"]
    _populate(tmp_path, mine + others)
    run, _ = _fake_docker(monkeypatch, nodes=("n1",))
    ctx = _docker_ctx() if ctx_kind == "docker" else _native_ctx()
    path = str(tmp_path / "rows.json")
    # Base name first, then by sequence number (ties by name).
    assert read_server_files(ctx, path, run=run) == \
        "rows.json\nrows.00.json\nrows.json.00\nrows.01.json\n"
    clear_server_files(ctx, path, run=run)
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted(others)


@pytest.mark.parametrize("ctx_kind", ["docker", "native"])
def test_extensionless_read_and_clear(tmp_path, monkeypatch, ctx_kind):
    from livetest.striimfile import read_server_files, clear_server_files
    _populate(tmp_path, ["out.00", "out.01", "outer", "out.x"])
    run, _ = _fake_docker(monkeypatch, nodes=("n1",))
    ctx = _docker_ctx() if ctx_kind == "docker" else _native_ctx()
    path = str(tmp_path / "out")
    assert read_server_files(ctx, path, run=run) == "out.00\nout.01\n"
    clear_server_files(ctx, path, run=run)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["out.x", "outer"]


@pytest.mark.parametrize("ctx_kind", ["docker", "native"])
def test_dots_in_parent_directories_are_not_the_extension(tmp_path, monkeypatch, ctx_kind):
    from livetest.striimfile import read_server_files, clear_server_files
    d = tmp_path / "run.v2.d"
    d.mkdir()
    _populate(d, ["out.00", "out.v2.d"])
    # A first-dot split of the whole path would look for run.00.v2.d/out -- never here.
    (tmp_path / "run.00.v2.d").mkdir()
    _populate(tmp_path / "run.00.v2.d", ["out"])
    run, _ = _fake_docker(monkeypatch, nodes=("n1",))
    ctx = _docker_ctx() if ctx_kind == "docker" else _native_ctx()
    assert read_server_files(ctx, str(d / "out"), run=run) == "out.00\n"
    clear_server_files(ctx, str(d / "out"), run=run)
    assert sorted(p.name for p in d.iterdir()) == ["out.v2.d"]
    assert (tmp_path / "run.00.v2.d" / "out").exists()


@pytest.mark.parametrize("ctx_kind", ["docker", "native"])
def test_rollover_files_without_a_final_newline_keep_one_record_per_line(tmp_path, monkeypatch, ctx_kind):
    # JSONFormatter with EventsAsArrayOfJsonObjects false ends each file without a newline.
    # Joined as-is, 2 + 1 records read as 2 lines, and a lifecycle file-lines wait for 3 hung.
    from livetest.striimfile import read_server_files
    from livetest.lifecycle import _lines
    (tmp_path / "rows.00.json").write_text('{"id":1}\n{"id":2}')
    (tmp_path / "rows.01.json").write_text('{"id":3}')
    (tmp_path / "rows.02.json").write_text("")
    run, _ = _fake_docker(monkeypatch, nodes=("n1",))
    ctx = _docker_ctx() if ctx_kind == "docker" else _native_ctx()
    got = read_server_files(ctx, str(tmp_path / "rows.json"), run=run)
    assert got == '{"id":1}\n{"id":2}\n{"id":3}'
    assert _lines(got) == 3


@pytest.mark.parametrize("ctx_kind", ["docker", "native"])
def test_a_single_file_reads_back_exactly(tmp_path, monkeypatch, ctx_kind):
    # No newline is added after the last file: byte-exact assertions see the file as written.
    from livetest.striimfile import read_server_files
    (tmp_path / "rows.00.json").write_text('{"id":1}')
    run, _ = _fake_docker(monkeypatch, nodes=("n1",))
    ctx = _docker_ctx() if ctx_kind == "docker" else _native_ctx()
    assert read_server_files(ctx, str(tmp_path / "rows.json"), run=run) == '{"id":1}'


def test_output_on_two_nodes_is_kept_apart(tmp_path, monkeypatch):
    from livetest.striimfile import read_server_files
    (tmp_path / "rows.00.json").write_text('{"id":1}')
    run, _ = _fake_docker(monkeypatch, nodes=("n1", "n2"))
    # Both "nodes" see the same local directory here, so the file arrives twice.
    assert read_server_files(_docker_ctx(), str(tmp_path / "rows.json"), run=run) == \
        '{"id":1}\n{"id":1}'


@pytest.mark.parametrize("ctx_kind", ["docker", "native"])
def test_missing_output_reads_empty(tmp_path, monkeypatch, ctx_kind):
    from livetest.striimfile import read_server_files, clear_server_files
    run, _ = _fake_docker(monkeypatch, nodes=("n1",))
    ctx = _docker_ctx() if ctx_kind == "docker" else _native_ctx()
    for path in (tmp_path / "rows.json", tmp_path / "absent-dir" / "rows.json"):
        assert read_server_files(ctx, str(path), run=run) == ""
        clear_server_files(ctx, str(path), run=run)


def test_docker_reads_and_clears_on_both_nodes(tmp_path, monkeypatch):
    from livetest.striimfile import read_server_files, clear_server_files
    _populate(tmp_path, ["rows.00.json"])
    run, calls = _fake_docker(monkeypatch, nodes=("n1", "n2"))
    # Both "nodes" see the same local directory here, so the content arrives twice.
    assert read_server_files(_docker_ctx(), str(tmp_path / "rows.json"), run=run) == \
        "rows.00.json\nrows.00.json\n"
    assert {c[2] for c in calls} == {"n1", "n2"}
    calls.clear()
    clear_server_files(_docker_ctx(), str(tmp_path / "rows.json"), run=run)
    assert {c[2] for c in calls} == {"n1", "n2"}
    assert not (tmp_path / "rows.00.json").exists()


@pytest.mark.parametrize("fn", ["read", "clear"])
def test_docker_missing_container_is_an_error_not_zero_rows(tmp_path, monkeypatch, fn):
    from livetest.striimfile import read_server_files, clear_server_files, StriimFileError
    _populate(tmp_path, ["rows.00.json"])
    run, _ = _fake_docker(monkeypatch, nodes=("n1", "n2"), dead=("n2",))
    call = read_server_files if fn == "read" else clear_server_files
    with pytest.raises(StriimFileError, match="n2.*No such container"):
        call(_docker_ctx(), str(tmp_path / "rows.json"), run=run)


@pytest.mark.parametrize("fn", ["read", "clear"])
def test_docker_command_failure_on_a_selected_file_is_an_error(tmp_path, monkeypatch, fn):
    # A listing that succeeds followed by a cat/rm that fails (permissions) must not read as
    # zero rows or a clean directory.
    from livetest import striimfile
    from livetest.striimfile import read_server_files, clear_server_files, StriimFileError
    monkeypatch.setattr(striimfile.stack, "app_nodes", lambda: ("n1",))
    listed = str(tmp_path / "rows.00.json")

    def run(argv):
        script = argv[argv.index("-c") + 1]
        if script.lstrip().startswith(("cat", "rm", "for f in")):
            return _subprocess.CompletedProcess(argv, 1, "", f"{listed}: Permission denied\n")
        return _subprocess.CompletedProcess(argv, 0, listed + "\n", "")
    call = read_server_files if fn == "read" else clear_server_files
    with pytest.raises(StriimFileError, match="Permission denied"):
        call(_docker_ctx(), str(tmp_path / "rows.json"), run=run)


@pytest.mark.parametrize("fn", ["read", "clear"])
def test_native_permission_failure_is_an_error(tmp_path, monkeypatch, fn):
    from livetest.striimfile import read_server_files, clear_server_files, StriimFileError
    import os as _os
    _populate(tmp_path, ["rows.00.json"])

    def deny(*a, **k):
        raise PermissionError(13, "Permission denied", str(tmp_path))
    monkeypatch.setattr(_os, "scandir", deny)
    call = read_server_files if fn == "read" else clear_server_files
    with pytest.raises(StriimFileError, match="Permission denied"):
        call(_native_ctx(), str(tmp_path / "rows.json"))


# Review R1: the end anchor must cover BOTH rollover shapes. With it on only one, a legacy
# numeric prefix accepted any tail -- another run's file, a backup, or a name carrying shell
# metacharacters into the Docker command line.
_FOREIGN = ["rows.json.00.bak", "rows.json.01-other-run", "rows.json.02;echo REVIEW_MARKER",
            "rows.00.json.bak", "rows.00.json-other-run"]


def test_output_names_anchors_both_rollover_shapes():
    from livetest.striimfile import _output_names
    assert _output_names("rows.json", _FOREIGN + ["rows.00.json", "rows.json.03"]) == \
        ["rows.00.json", "rows.json.03"]
    assert _output_names("out", ["out.00.bak", "out.01-other-run", "out.02"]) == ["out.02"]


@pytest.mark.parametrize("ctx_kind", ["docker", "native"])
def test_read_and_clear_leave_numeric_prefixed_foreign_files_alone(tmp_path, monkeypatch, ctx_kind):
    from livetest.striimfile import read_server_files, clear_server_files
    _populate(tmp_path, _FOREIGN + ["rows.00.json"])
    run, _ = _fake_docker(monkeypatch, nodes=("n1",))
    ctx = _docker_ctx() if ctx_kind == "docker" else _native_ctx()
    path = str(tmp_path / "rows.json")
    assert read_server_files(ctx, path, run=run) == "rows.00.json\n"
    clear_server_files(ctx, path, run=run)
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted(_FOREIGN)


# Review R2: an output directory behind an unsearchable ancestor is not "no output yet". Docker
# must raise like native does, while a genuinely absent directory still reads as empty.
@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0,
                    reason="root ignores directory permissions")
@pytest.mark.parametrize("fn", ["read", "clear"])
@pytest.mark.parametrize("ctx_kind", ["docker", "native"])
def test_unsearchable_ancestor_is_an_error_not_missing_output(tmp_path, monkeypatch, ctx_kind, fn):
    from livetest.striimfile import read_server_files, clear_server_files, StriimFileError
    locked = tmp_path / "locked"
    (locked / "out").mkdir(parents=True)
    _populate(locked / "out", ["rows.00.json"])
    run, _ = _fake_docker(monkeypatch, nodes=("n1",))
    ctx = _docker_ctx() if ctx_kind == "docker" else _native_ctx()
    call = read_server_files if fn == "read" else clear_server_files
    locked.chmod(0)
    try:
        with pytest.raises(StriimFileError, match="Permission denied"):
            call(ctx, str(locked / "out" / "rows.json"), run=run)
    finally:
        locked.chmod(0o755)
    assert (locked / "out" / "rows.00.json").exists()
