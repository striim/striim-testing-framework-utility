"""Unknown keys inside assertion specs and block entries are refused at load.

`assert.data`, `diff`, `file`, `gcs` and `json` specs used to be parsed only in the assertion
phase, after provisioning and deploy, and none of the five parsers looked for keys it did not
read: a misspelt `exact`, `multiset` or `stable_seconds` quietly fell back to the weaker set
compare, and `{target: t, min_rows: 1, macth: ...}` passed on one row. The same held inside
`ddl`, `seed`, `server_files`, `generate`, `concurrent` and `stop_start_cycle` entries, where
`wen: post_start` made a CDC seed run before deploy.
"""
from __future__ import annotations

import textwrap

import pytest

from livetest.assertions.data import DataSpecError, parse_data_specs
from livetest.assertions.diff import DiffSpecError, parse_diff_specs
from livetest.assertions.file import FileSpecError, parse_file_specs
from livetest.assertions.gcs import GcsSpecError, parse_gcs_specs
from livetest.assertions.json import JsonSpecError, parse_json_specs
from livetest.manifest import ManifestError, load_manifest

BASE = "name: t\npurpose: p\ntql: app.tql\n"


def _case(tmp_path, body: str, files=("app.tql",)):
    d = tmp_path / "case"
    d.mkdir(exist_ok=True)
    for f in files:
        (d / f).write_text("-- x\n")
    (d / "test.yaml").write_text(BASE + textwrap.dedent(body))
    return d / "test.yaml"


def _refused(tmp_path, body, *, files=("app.tql",)):
    with pytest.raises(ManifestError) as e:
        load_manifest(_case(tmp_path, body, files))
    return str(e.value)


# ---- assert specs: misspelt keys ------------------------------------------------------------------

MISSPELT = [
    ("data", "{target: t, min_rows: 1, macth: expected/rows.csv}", "macth"),
    ("data", "{target: t, rows: 1, mtach: g.csv, kyes: [id]}", "kyes"),
    ("diff", "{source: a, target: b, exat: true}", "exat"),
    ("diff", "{source: a, target: b, foldnames: true}", "foldnames"),
    ("file", "{path: /o, events: 3, match: g.csv, multset: true}", "multset"),
    ("file", "{path: /o, match: g.csv, stable_second: 5}", "stable_second"),
    ("gcs", "{bucket: b, object: o, size: 3, sha265: '00'}", "sha265"),
    ("gcs", "{bucket: b, object: o, size: 3, content_hx: ff}", "content_hx"),
    ("json", "{target: t, db: spanner-google, key: id, column: c, match: g.json, strict: true}", "strict"),
]


@pytest.mark.parametrize("tier,spec,key", MISSPELT, ids=[f"{t}-{k}" for t, _, k in MISSPELT])
def test_a_misspelt_assert_spec_key_is_refused_at_load_naming_key_tier_and_case(tmp_path, tier, spec, key):
    msg = _refused(tmp_path, f"assert:\n  {tier}:\n    - {spec}\n")
    assert str(tmp_path / "case" / "test.yaml") in msg
    assert f"assert.{tier}[0]" in msg and repr(key) in msg and "unknown key" in msg


def test_the_index_named_is_the_specs_own_position_in_the_tier(tmp_path):
    msg = _refused(tmp_path, """\
        assert:
          data:
            - {target: a, min_rows: 1}
            - {target: b, min_rows: 1, macth: g.csv}
        """)
    assert "assert.data[1]" in msg and "'macth'" in msg


# ---- assert specs: incomplete specs ---------------------------------------------------------------

INCOMPLETE = [
    ("data", "{target: t}", "min_rows/rows/match"),
    ("data", "{min_rows: 1}", "target"),
    ("diff", "{source: a}", "target"),
    ("file", "{path: /tmp/x}", "match/min_events/events/distinct_events"),
    ("gcs", "{bucket: b}", "object"),
    ("json", "{target: t}", "db"),
]


@pytest.mark.parametrize("tier,spec,why", INCOMPLETE, ids=[f"{t}-{s}" for t, s, _ in INCOMPLETE])
def test_an_incomplete_assert_spec_is_refused_at_load(tmp_path, tier, spec, why):
    msg = _refused(tmp_path, f"assert:\n  {tier}:\n    - {spec}\n")
    assert f"assert.{tier}[0]" in msg and why in msg


def test_a_tier_that_is_not_a_list_is_refused_at_load(tmp_path):
    assert "'assert.gcs' must be a list" in _refused(tmp_path, "assert:\n  gcs: {bucket: b}\n")


# ---- assert specs: the parsers themselves --------------------------------------------------------

@pytest.mark.parametrize("parse,error,spec", [
    (parse_data_specs, DataSpecError, {"target": "t", "rows": 1, "mtach": "g.csv", "kyes": ["id"]}),
    (parse_file_specs, FileSpecError, {"path": "/o", "match": "g.csv", "multset": True, "stable_second": 5}),
    (parse_diff_specs, DiffSpecError, {"source": "a", "target": "b", "exat": True, "foldnames": True}),
    (parse_json_specs, JsonSpecError, {"target": "t", "db": "spanner", "column": "c", "match": "g.json",
                                       "key": "id", "strict": True}),
    (parse_gcs_specs, GcsSpecError, {"bucket": "b", "object": "o", "size": 3, "sha265": "00",
                                     "content_hx": "ffffff"}),
], ids=["data", "file", "diff", "json", "gcs"])
def test_each_parser_refuses_a_key_it_does_not_read(parse, error, spec):
    with pytest.raises(error, match="unknown key"):
        parse([spec])


def test_an_unknown_key_is_named_before_a_missing_one():
    # `tagret` is the reason `target` is missing; say so rather than only "needs a 'target'".
    with pytest.raises(DataSpecError, match="'tagret'"):
        parse_data_specs([{"tagret": "t", "min_rows": 1}])


# ---- assert specs: every key the assertion code reads still loads --------------------------------

def test_every_key_the_assertion_code_reads_loads(tmp_path):
    m = load_manifest(_case(tmp_path, """\
        assert:
          data:
            - {target: t, db: postgres-target, min_rows: 1}
            - {target: t, target_db: postgres-target, source_db: postgres-target, rows: 2, match: g.csv}
            - {target: '${KAFKA_TGT_TOPIC}', db: kafka, project: kafka_record, keys: [key.ID],
               rows: 1, ordered: true, match: g.csv}
          diff:
            - {source: a, target: b, db: postgres-source, exact: true, fold_names: true}
            - {source: a, source_db: postgres-source, target: b, target_db: postgres-target}
          file:
            - {path: /tmp/o, match: g.csv, events: 2, project: all, metadata: [OperationName],
               keys: [ID], multiset: true, stable_seconds: 5,
               order: [{before: {ID: '1'}, after: {ID: '2'}}]}
            - {path: /tmp/o, min_events: 1}
            - {path: /tmp/o, distinct_events: 2}
          gcs:
            - {bucket: b, object: o, size: 3, sha256: 'sha256:00', content_hex: ff}
          json:
            - {target: t, db: spanner-google, key: id, column: c, match: g.json}
        """))
    assert set(m.assert_) == {"data", "diff", "file", "gcs", "json"}


def test_loading_does_not_rewrite_the_specs(tmp_path):
    # parse_json_specs turns a string `key` into a list; the load-time check must not leak that
    # (or anything else) into the manifest the run reads.
    m = load_manifest(_case(tmp_path, """\
        assert:
          json:
            - {target: t, db: spanner-google, key: id, column: c, match: g.json}
        """))
    assert m.assert_["json"] == [{"target": "t", "db": "spanner-google", "key": "id", "column": "c",
                                  "match": "g.json"}]


def test_a_typed_exact_diff_spec_is_checked_for_unknown_keys_too(tmp_path):
    body = """\
        exact: {version: 1}
        assert:
          diff:
            - source_db: spanner-google
              source: s
              target_db: spanner-google
              target: t
              exact: {columns: {id: integer}}
              {extra}
        """
    load_manifest(_case(tmp_path, body.replace("{extra}", "keys: [id]")))
    msg = _refused(tmp_path, body.replace("{extra}", "fold_name: true"))
    assert "assert.diff[0]" in msg and "'fold_name'" in msg


def test_a_typed_exact_data_spec_is_checked_for_unknown_keys_too(tmp_path):
    body = """\
        exact: {version: 1}
        assert:
          data:
            - target: t
              match: g.csv
              exact: {columns: {id: integer}}
              {extra}
        """
    load_manifest(_case(tmp_path, body.replace("{extra}", "target_db: postgres-target"), files=("app.tql", "g.csv")))
    msg = _refused(tmp_path, body.replace("{extra}", "tagret_db: postgres-target"), files=("app.tql", "g.csv"))
    assert "assert.data[0]" in msg and "'tagret_db'" in msg


def test_nothing_and_raw_specs_keep_their_own_refusals(tmp_path):
    # These shapes already refused extra keys with their own codes; the generic check must not
    # mask them.
    msg = _refused(tmp_path, """\
        assert:
          file:
            - {path: '${OWNED_DIR}/out', raw: {files: {a: {bytes: 1, sha256: x}}}, match: g.csv}
        """)
    assert "invalid-raw-file-spec" in msg
    msg = _refused(tmp_path, """\
        assert:
          data:
            - {target: t, nothing: {window: 5s, where: {id: 1}}, min_rows: 1}
        """)
    assert "invalid-nothing-spec" in msg


TYPED_INCOMPLETE = [
    ("data", "{target_db: postgres-target, match: g.csv, exact: {columns: {id: integer}}}",
     "{target_db: postgres-target, target: t, match: g.csv, exact: {columns: {id: integer}}}", ["target"]),
    ("data", "{target_db: postgres-target, match: g.csv, exact: true}",
     "{target_db: postgres-target, target: t, match: g.csv, exact: true}", ["target"]),
    ("file", "{match: g.csv, exact: {columns: {id: integer}}}",
     "{path: '${OWNED_DIR}/out', match: g.csv, exact: {columns: {id: integer}}}", ["path"]),
    ("diff", "{db: postgres-source, exact: {}}",
     "{db: postgres-source, source: s, target: t, exact: {}}", ["source", "target"]),
    ("diff", "{db: postgres-source, source: s, exact: {columns: {id: integer}}}",
     "{db: postgres-source, source: s, target: t, exact: {columns: {id: integer}}}", ["target"]),
]


@pytest.mark.parametrize("tier,spec,complete,missing", TYPED_INCOMPLETE,
                         ids=[f"{t}-{'-'.join(m)}" for t, _, _, m in TYPED_INCOMPLETE])
def test_a_typed_exact_spec_missing_its_endpoint_is_refused_at_load(tmp_path, tier, spec, complete, missing):
    # The typed readers index spec["target"] / ["path"] / ["source"] after deploy; at load the
    # spec had only its keys checked, so the KeyError came after provisioning.
    body = "exact: {version: 1}\nassert:\n  " + tier + ":\n    - {spec}\n"
    files = ("app.tql", "g.csv")
    load_manifest(_case(tmp_path, body.replace("{spec}", complete), files))
    msg = _refused(tmp_path, body.replace("{spec}", spec), files=files)
    assert f"assert.{tier}[0]" in msg and f"needs {missing}" in msg


NOTHING_ELSEWHERE = [
    ("json", "{target: t, db: spanner-google, key: id, column: c, match: g.json, unorderd: [a], "
             "nothing: {window: 5s, where: {id: 1}}}"),
    ("gcs", "{bucket: b, object: o, size: 3, sha265: '00', nothing: {window: 5s, where: {id: 1}}}"),
    ("diff", "{source: a, target: b, nothing: {window: 5s, where: {id: 1}}}"),
    ("json", "{target: t, db: spanner-google, key: id, column: c, match: g.json, "
             "nothing: {window: 5s, where: {id: 1}}}"),
]


@pytest.mark.parametrize("tier,spec", NOTHING_ELSEWHERE, ids=["json-typo", "gcs-typo", "diff", "json"])
def test_nothing_outside_data_and_file_does_not_exempt_a_spec_from_its_checks(tmp_path, tier, spec):
    # nothing: is a data/file check (livetest.nothing validates only those tiers); on another
    # tier it skipped the spec's own key check, so `unorderd` or `sha265` loaded.
    msg = _refused(tmp_path, f"assert:\n  {tier}:\n    - {spec}\n")
    assert f"assert.{tier}[0]" in msg and "unknown key" in msg and "'nothing'" in msg


# ---- block entries (FW-80) ------------------------------------------------------------------------

def test_a_seed_entry_typo_is_refused(tmp_path):
    msg = _refused(tmp_path, """\
        seed:
          - {file: changes.sql, db: postgres-source, wen: post_start, aftr: 30s}
        assert: {smoke: true}
        """)
    assert "'seed'" in msg and "'aftr'" in msg and "'wen'" in msg


def test_a_ddl_entry_typo_is_refused(tmp_path):
    msg = _refused(tmp_path, """\
        ddl:
          - {file: target.sql, bd: postgres-target}
        assert: {smoke: true}
        """)
    assert "'ddl'" in msg and "'bd'" in msg


def test_a_ddl_entry_with_when_keeps_its_own_message(tmp_path):
    msg = _refused(tmp_path, """\
        ddl:
          - {file: target.sql, when: post_start}
        assert: {smoke: true}
        """)
    assert "which only 'seed' entries accept" in msg


def test_seed_and_ddl_entries_accept_every_key_the_loader_reads(tmp_path):
    with pytest.warns(DeprecationWarning):
        m = load_manifest(_case(tmp_path, """\
            ddl:
              - {file: a.sql, db: postgres-target, local: false}
              - {file: b.sql, target_db: postgres-target}
            seed:
              - {file: c.sql, db: postgres-source, when: post_start, after: 5s}
              - {file: d.sql, source_db: postgres-source}
            assert: {smoke: true}
            """))
    assert m.ddl_files == [("postgres-target", "a.sql"), ("postgres-target", "b.sql")]
    assert m.seed_files == [("postgres-source", "c.sql", "post_start", 5.0),
                            ("postgres-source", "d.sql", "pre_deploy", 0.0)]


def test_an_action_seed_and_stopped_seed_typo_is_refused(tmp_path):
    msg = _refused(tmp_path, """\
        action:
          - type: drop_recreate_app
            app: a
            seed: [{file: s.sql, db: postgres-source, aftr: 5s}]
        assert: {smoke: true}
        """)
    assert "action[0] seed" in msg and "'aftr'" in msg
    msg = _refused(tmp_path, """\
        action:
          - type: drop_recreate_app
            app: a
            stopped_seed: [{file: s.sql, bd: postgres-source}]
        assert: {smoke: true}
        """)
    assert "stopped_seed" in msg and "'bd'" in msg


def test_a_server_files_entry_typo_is_refused(tmp_path):
    msg = _refused(tmp_path, """\
        server_files:
          - {file: in.csv, dest: /tmp/in.csv, wen: post_start, laod: op}
        assert: {smoke: true}
        """)
    assert "'server_files'" in msg and "'laod'" in msg and "'wen'" in msg


def test_a_server_files_entry_accepts_every_key_the_loader_reads(tmp_path):
    m = load_manifest(_case(tmp_path, """\
        server_files:
          - {file: in.csv, dest: /tmp/in.csv, when: post_start}
          - {file: x.jar, dest: x.jar, load: op}
        assert: {smoke: true}
        """))
    assert m.server_files == [("in.csv", "/tmp/in.csv", "post_start", False),
                              ("x.jar", "x.jar", "pre_deploy", "open_processor")]


def test_a_stop_start_cycle_typo_is_refused(tmp_path):
    msg = _refused(tmp_path, """\
        action:
          - {type: stop_start_cycle, cycle: 5, stop_durration: 60}
        assert: {smoke: true}
        """)
    assert "action[0] 'stop_start_cycle'" in msg and "'cycle'" in msg and "'stop_durration'" in msg


def test_a_stop_start_cycle_accepts_every_key_the_loader_reads(tmp_path):
    m = load_manifest(_case(tmp_path, """\
        requires: [postgres]
        action:
          - type: stop_start_cycle
            cycles: 2
            delay_before_stop: 1
            stop_duration: 4
            concurrent: [{db: postgres-source, file: loop.sql, loop_interval: 2}]
        assert: {smoke: true}
        """))
    a, = m.action_specs
    assert (a["cycles"], a["delay_before_stop"], a["stop_duration"]) == (2, 1.0, 4.0)
    assert a["concurrent"] == [{"db": "postgres-source", "file": "loop.sql", "loop_interval": 2.0}]


def test_generate_and_concurrent_entry_typos_are_refused(tmp_path):
    msg = _refused(tmp_path, """\
        requires: [postgres]
        generate:
          - {kind: ggtrail, workload: workload.yaml, dest: /tmp/x, wen: pre_deploy}
        assert: {smoke: true}
        """, files=("app.tql", "workload.yaml"))
    assert "'generate'" in msg and "'wen'" in msg
    msg = _refused(tmp_path, """\
        requires: [postgres]
        action:
          - type: stop_start_cycle
            concurrent:
              - {db: postgres-source, file: loop.sql, loop_intervall: 30}
        assert: {smoke: true}
        """)
    assert "concurrent[0]" in msg and "'loop_intervall'" in msg


def test_a_service_outage_concurrent_typo_is_refused(tmp_path):
    msg = _refused(tmp_path, """\
        requires: [postgres]
        action:
          - type: service_outage
            service: postgres
            concurrent: [{db: postgres-source, file: loop.sql, interval: 1}]
        assert: {smoke: true}
        """)
    assert "concurrent[0]" in msg and "'interval'" in msg


def test_a_generate_entry_accepts_every_key_the_loader_reads(tmp_path):
    m = load_manifest(_case(tmp_path, """\
        generate:
          - {kind: ggtrail, workload: workload.yaml, dest: /tmp/x, when: pre_deploy}
        assert: {smoke: true}
        """, files=("app.tql", "workload.yaml")))
    g, = m.generate_specs
    assert (g["kind"], g["dest"], g["when"]) == ("ggtrail", "/tmp/x", "pre_deploy")
