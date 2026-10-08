"""The per-case lifecycle block (C7.2) is validated at load, before any
provisioning. Wrong versions, unknown keys or kinds, invalid combinations, zero counts, unsupported
routes and paths outside ${OWNED_DIR} are rejected with the field named."""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from livetest import lifecycle
from livetest.lifecycle import LifecycleSpecError, parse_manifest_block
from livetest.manifest import ManifestError, load_manifest

SRC = {"db": "postgres-source", "table": "${PG_SOURCE_SCHEMA}.${TID}src"}
TGT = {"db": "postgres-target", "table": "${PG_TARGET_SCHEMA}.${TID}tgt"}
SENTINEL = {"db": "postgres-source", "insert": "sentinel_insert.sql", "delete": "sentinel_delete.sql",
            "observe": {"db": "postgres-target", "table": "${PG_TARGET_SCHEMA}.${TID}tgt", "key": "id"}}
CONTRACT_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "lifecycle-contract"


def initial(**over):
    block = {"version": 1, "mode": "initial-load", "sink": "db",
             "readiness": {"kind": "baseline-landed", "source": SRC, "target": TGT},
             "completion": {"kind": "source-count", "source": SRC, "target": TGT}}
    block.update(over)
    return block


def cdc(**over):
    block = {"version": 1, "mode": "cdc", "sink": "db", "readiness": {"kind": "sentinel"},
             "completion": {"kind": "sentinel"}, "sentinel": SENTINEL}
    block.update(over)
    return block


def parse(block, **raw):
    return parse_manifest_block({"lifecycle": block, **raw}, "cases/lc/test.yaml")


def rejected(block, match, **raw):
    with pytest.raises(LifecycleSpecError, match=match) as ei:
        parse(block, **raw)
    assert "cases/lc/test.yaml: lifecycle:" in str(ei.value)
    return ei.value


def _manifest(tmp_path, **raw):
    case = tmp_path / "lc"
    case.mkdir()
    (case / "app.tql").write_text("CREATE APPLICATION ${APP};\nEND APPLICATION ${APP};\n")
    doc = {"name": "lc", "purpose": "hermetic", "tql": "app.tql", "requires": ["postgres"],
           "assert": {"smoke": True}, **raw}
    (case / "test.yaml").write_text(yaml.safe_dump(doc, sort_keys=False))
    return case / "test.yaml"


def test_legacy_manifest_loads_as_legacy(tmp_path):
    m = load_manifest(_manifest(tmp_path))
    assert m.lifecycle is None
    state = lifecycle.State.for_manifest(m)
    assert state.mode == "legacy" and state.ready["kind"] == "legacy" and state.completion["kind"] == "legacy"
    assert state.ready["witness"] == "app RUNNING (not capture readiness)"
    assert not state.ready_satisfied()


def test_valid_initial_load_baseline_landed():
    spec = parse(initial())
    assert (spec.mode, spec.sink, spec.readiness["kind"], spec.completion["kind"]) == \
        ("initial-load", "db", "baseline-landed", "source-count")
    assert spec.readiness["source"] == SRC and spec.readiness["target"] == TGT
    assert spec.stability_s == 5.0 and spec.readiness_s == 60.0 and spec.completion_s == 120.0
    assert spec.reset == "owned" and spec.sentinel is None
    timed = parse(initial(stability="500ms", deadlines={"readiness": "2m", "completion": 30}), timeout=90)
    assert timed.stability_s == 0.5 and timed.readiness_s == 120.0 and timed.completion_s == 30.0
    assert parse(initial(stability=0)).stability_s == 0.0


def test_valid_cdc_sentinel_db_sink():
    spec = parse(cdc())
    assert spec.uses_sentinel() and spec.sentinel["observe"]["key"] == "id"


def test_valid_cdc_source_progress_postgres():
    spec = parse(cdc(readiness={"kind": "source-progress", "db": "postgres-source"},
                     completion={"kind": "row-count", "db": "postgres-target", "table": TGT["table"], "expect": 3},
                     sentinel=None) | {})
    assert spec.readiness == {"kind": "source-progress", "db": "postgres-source"}


def test_valid_cdc_file_sink_file_lines():
    block = {"version": 1, "mode": "cdc", "sink": "file",
             "readiness": {"kind": "source-progress", "db": "postgres-source"},
             "completion": {"kind": "file-lines", "path": "${OWNED_DIR}/out.csv", "lines": 3}}
    spec = parse(block, **{"assert": {"file": [{"path": "${OWNED_DIR}/out.csv", "min_events": 3}]},
                           "server_files": [{"file": "in.csv", "dest": "${OWNED_DIR}/in/in.csv"}]})
    assert spec.sink == "file" and spec.completion["lines"] == 3


def _without(block, key):
    return {k: v for k, v in block.items() if k != key}


def test_block_without_version_rejected():
    rejected(_without(initial(), "version"), "'version' is required")


def test_version_2_rejected():
    rejected(initial(version=2), "version 2 is not supported")
    rejected(initial(version=True), "version True is not supported")


def test_unknown_key_rejected():
    rejected(initial(timeout=10), r"unknown key\(s\) \['timeout'\]")
    rejected(initial(readiness={"kind": "baseline-landed", "source": SRC, "target": TGT, "poll": 1}),
             "readiness kind baseline-landed has unknown key")


def test_unknown_kind_rejected():
    rejected(initial(readiness={"kind": "source-complete"}), "unknown readiness kind 'source-complete'")
    rejected(initial(completion={"kind": "eventually"}), "unknown completion kind 'eventually'")


def test_initial_load_with_sentinel_readiness_rejected():
    rejected(initial(readiness={"kind": "sentinel"}, sentinel=SENTINEL), "mode initial-load requires readiness kind baseline-landed")


def test_initial_load_readiness_other_kind_rejected():
    rejected(initial(readiness={"kind": "source-progress", "db": "postgres-source"}),
             "mode initial-load requires readiness kind baseline-landed, got 'source-progress'")


def test_cdc_with_baseline_landed_rejected():
    rejected(cdc(readiness={"kind": "baseline-landed", "source": SRC, "target": TGT}),
             "baseline-landed is for mode initial-load")


def test_file_sink_with_sentinel_rejected():
    rejected(cdc(sink="file", completion={"kind": "file-lines", "path": "${OWNED_DIR}/o", "lines": 1}),
             "sink file cannot use a sentinel kind")


def test_row_count_zero_rejected():
    rejected(cdc(readiness={"kind": "source-progress", "db": "postgres-source"}, sentinel=None,
                 completion={"kind": "row-count", "db": "postgres-target", "table": TGT["table"], "expect": 0}),
             "completion.expect is 0: zero is never a completion witness")


def test_file_lines_zero_rejected():
    block = {"version": 1, "mode": "cdc", "sink": "file",
             "readiness": {"kind": "source-progress", "db": "postgres-source"},
             "completion": {"kind": "file-lines", "path": "${OWNED_DIR}/out.csv", "lines": 0}}
    rejected(block, "completion.lines is 0: zero is never a completion witness")


def test_sentinel_without_block_rejected():
    rejected(cdc(sentinel=None), "requires the sentinel block")
    rejected(initial(sentinel=SENTINEL), "no readiness or completion kind uses it")


def test_sentinel_observe_table_without_tid_rejected():
    bad = {**SENTINEL, "observe": {**SENTINEL["observe"], "table": "qatarget.shared_tgt"}}
    rejected(cdc(sentinel=bad), r"observe.table must contain \$\{TID\}")


def test_source_progress_non_postgres_rejected():
    rejected(cdc(readiness={"kind": "source-progress", "db": "mysql-source"}),
             "source-progress db 'mysql-source' is an unsupported lifecycle route")


def test_non_postgres_route_rejected_for_lifecycle_probe_or_sentinel():
    rejected(cdc(readiness={"kind": "source-progress", "db": "postgres-source"}, sentinel=None,
                 completion={"kind": "row-count", "db": "oracle-target", "table": "QATARGET.${TID_ORACLE}T", "expect": 1}),
             "completion.db 'oracle-target' is an unsupported lifecycle route")
    rejected(cdc(sentinel={**SENTINEL, "db": "mssql-source"}), "sentinel.db 'mssql-source' is an unsupported")
    rejected(initial(readiness={"kind": "baseline-landed", "source": {"db": "spanner-google", "table": "t"},
                                "target": TGT}),
             "readiness.source.db 'spanner-google' is an unsupported lifecycle route")


def test_reset_not_owned_rejected():
    rejected(initial(reset="schema"), "reset must be owned, got 'schema'")


def test_checkpoint_continuation_rejected():
    rejected(initial(reset="checkpoint-continuation"), "checkpoint-continuation is reserved")


def test_lifecycle_file_paths_must_start_with_owned_dir():
    block = {"version": 1, "mode": "cdc", "sink": "file",
             "readiness": {"kind": "source-progress", "db": "postgres-source"},
             "completion": {"kind": "file-lines", "path": "${OWNED_DIR}/out.csv", "lines": 3}}
    rejected(block, r"assert.file\[0\].path '/opt/striim/\$\{NS\}-out' must start with \$\{OWNED_DIR\}/",
             **{"assert": {"file": [{"path": "/opt/striim/${NS}-out", "min_events": 1}]}})
    rejected(block, r"server_files\[0\].dest '/tmp/in.csv' must start with",
             server_files=[{"file": "in.csv", "dest": "/tmp/in.csv"}])
    rejected({**block, "completion": {"kind": "file-lines", "path": "/tmp/out.csv", "lines": 3}},
             "completion.path '/tmp/out.csv' must start with")
    # a `load: true` entry's dest is an uploaded jar name, not a server path
    assert parse(block, server_files=[{"file": "x.jar", "dest": "x.jar", "load": True}]) is not None


def test_legacy_case_paths_unrestricted(tmp_path):
    raw = {"assert": {"file": [{"path": "/opt/striim/${NS}-out", "min_events": 1}]},
           "server_files": [{"file": "in.csv", "dest": "/tmp/in.csv"}]}
    assert parse_manifest_block(raw, "cases/legacy/test.yaml") is None
    (tmp_path / "in.csv").write_text("x\n")
    m = load_manifest(_manifest(tmp_path, **raw))
    assert m.lifecycle is None and m.server_files[0][1] == "/tmp/in.csv"


def test_rejection_is_manifest_error_from_load_manifest(tmp_path):
    path = _manifest(tmp_path, lifecycle=initial(version=2))
    with pytest.raises(ManifestError, match=r"lifecycle: version 2 is not supported") as ei:
        load_manifest(path)
    assert str(path) in str(ei.value)
    ok = load_manifest(_manifest(tmp_path / "ok", lifecycle=initial())) if (tmp_path / "ok").mkdir() is None else None
    assert isinstance(ok.lifecycle, lifecycle.LifecycleSpec)


@pytest.mark.parametrize("name", sorted(p.name for p in CONTRACT_FIXTURES.glob("lifecycle.*.yaml")))
def test_contract_fixtures_parse_as_documented(name):
    raw = yaml.safe_load((CONTRACT_FIXTURES / name).read_text())
    if ".valid-" in name:
        assert isinstance(parse_manifest_block(raw, name), lifecycle.LifecycleSpec)
    else:
        with pytest.raises(LifecycleSpecError):
            parse_manifest_block(raw, name)


# ---------------------------------------------------------------- code review r1: R4 finite durations, R5 containment

@pytest.mark.parametrize("field", ["stability", "readiness", "completion"])
@pytest.mark.parametrize("text", [".inf", ".nan"])
def test_nonfinite_durations_rejected(field, text):
    value = yaml.safe_load(text)                      # YAML's own spelling of infinity / not-a-number
    over = {"stability": value} if field == "stability" else {"deadlines": {field: value}}
    rejected(cdc(**over), "must be a finite duration")


@pytest.mark.parametrize("bad", ["${OWNED_DIR}/../foreign/out.csv", "${OWNED_DIR}/a/../../x.csv",
                                 "${OWNED_DIR}/./out.csv", "${OWNED_DIR}//out.csv"])
def test_owned_dir_paths_reject_traversal_and_empty_segments(bad):
    block = cdc(sink="file", readiness={"kind": "source-progress", "db": "postgres-source"},
                completion={"kind": "file-lines", "path": bad, "lines": 1}, sentinel=None)
    rejected(block, "must stay inside")
    rejected(cdc(sink="file", readiness={"kind": "source-progress", "db": "postgres-source"},
                 completion={"kind": "file-lines", "path": "${OWNED_DIR}/out.csv", "lines": 1}, sentinel=None),
             "must stay inside", **{"assert": {"file": [{"path": bad}]}})


def test_unsupported_route_explains_supported_routes_without_planning_ids():
    block = initial(readiness={"kind": "baseline-landed", "source": {**SRC, "db": "oracle-source"}, "target": TGT})
    error = rejected(block, "unsupported lifecycle route")
    assert "postgres-source" in str(error)
    assert "task" not in str(error) and "follow-up" not in str(error)
