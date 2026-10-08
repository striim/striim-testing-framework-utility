"""Unit tests for inttest.manifest's `target:` block and its `assert.target:`/`assert.acked:`
assertions (T2a) -- pure parsing tests, no Docker/Striim/pytest-plugin collection involved.
"""
from __future__ import annotations

import pytest

from inttest.manifest import (ManifestError, TargetSpec, load_manifest, target_to_wire)


def _write(tmp_path, text: str):
    test_dir = tmp_path / "some-test"
    test_dir.mkdir()
    manifest_path = test_dir / "test.yaml"
    manifest_path.write_text(text)
    return manifest_path, test_dir


BASE = """
name: writer-case
op:
  jar: java/OpenProcessors/JdbcSink
requires: [postgres]
"""

TARGET = BASE + """
target:
  input: input/events.json
assert:
  target:
    - query: SELECT ID FROM T ORDER BY ID
      match: expected/rows.json
"""


def test_absent_target_normalizes_to_none(tmp_path):
    # An ordinary in-stream case: it declares no `target:` and is unaffected by this block.
    path, _ = _write(tmp_path, """
name: op-case
op:
  jar: java/OpenProcessors/ExampleTransformOp
assert:
  data:
    - input: input/e.json
      match: expected/e.json
""")
    assert load_manifest(path).target is None


def test_target_block_normalizes(tmp_path):
    path, test_dir = _write(tmp_path, TARGET)
    m = load_manifest(path)
    assert isinstance(m.target, TargetSpec)
    assert m.target.input == "input/events.json"
    assert m.target.input_path == (test_dir / "input/events.json").resolve()
    assert m.target.restart_after is None
    assert m.target.positions is True
    assert m.target.distribution_id is None
    assert len(m.assert_.target) == 1
    assert m.assert_.target[0].db == "postgres-target"       # the default, not postgres-source
    assert m.assert_.target[0].match_path == (test_dir / "expected/rows.json").resolve()


def test_target_to_wire_shape(tmp_path):
    path, _ = _write(tmp_path, TARGET.replace(
        "  input: input/events.json",
        "  input: input/events.json\n  restart_after: 2\n  distribution_id: d1"))
    wire = target_to_wire(load_manifest(path).target)
    # `input` is deliberately NOT on the wire -- it becomes the request's own inputFile.
    # restartAfter is always a LIST on the wire, even for the bare-int spelling: the driver
    # then never has to branch on the two forms.
    assert wire == {"restartAfter": [2], "midRunAfter": None,
                    "positions": True, "distributionId": "d1"}


def test_restart_after_accepts_a_list_of_points(tmp_path):
    path, _ = _write(tmp_path, TARGET.replace(
        "  input: input/events.json",
        "  input: input/events.json\n  restart_after: [2, 4]"))
    m = load_manifest(path)
    assert m.target.restart_after == (2, 4)
    assert target_to_wire(m.target)["restartAfter"] == [2, 4]


def test_restart_after_rejects_an_empty_list(tmp_path):
    path, _ = _write(tmp_path, TARGET.replace(
        "  input: input/events.json",
        "  input: input/events.json\n  restart_after: []"))
    with pytest.raises(ManifestError, match="never recovers"):
        load_manifest(path)


def test_restart_after_rejects_a_repeated_ordinal(tmp_path):
    # A repeat cannot restart twice at one point -- the driver restarts AS it passes the ordinal,
    # so the second entry is unreachable and the case silently restarts once.
    path, _ = _write(tmp_path, TARGET.replace(
        "  input: input/events.json",
        "  input: input/events.json\n  restart_after: [2, 2]"))
    with pytest.raises(ManifestError, match="repeats an ordinal"):
        load_manifest(path)


def test_restart_after_rejects_a_descending_list(tmp_path):
    path, _ = _write(tmp_path, TARGET.replace(
        "  input: input/events.json",
        "  input: input/events.json\n  restart_after: [4, 2]"))
    with pytest.raises(ManifestError, match="must be ascending"):
        load_manifest(path)


def test_mid_run_normalizes(tmp_path):
    path, test_dir = _write(tmp_path, TARGET.replace(
        "  input: input/events.json",
        "  input: input/events.json\n  mid_run:\n    - after: 2\n      file: alter.sql"))
    m = load_manifest(path)
    assert len(m.target.mid_run) == 1
    step = m.target.mid_run[0]
    assert step.after == 2
    assert step.file == "alter.sql"
    # Defaults to the TARGET route, not postgres-source the way ddl:/seed: do -- this SQL acts on
    # the table the writer is writing.
    assert step.db == "postgres-target"
    assert step.path == (test_dir / "alter.sql").resolve()
    # Only the ordinals cross the wire; the driver gates, this side runs the SQL.
    assert target_to_wire(m.target)["midRunAfter"] == [2]


def test_mid_run_absent_is_empty_and_off_the_wire(tmp_path):
    path, _ = _write(tmp_path, TARGET)
    m = load_manifest(path)
    assert m.target.mid_run == ()
    assert target_to_wire(m.target)["midRunAfter"] is None


def test_mid_run_rejects_a_zero_ordinal(tmp_path):
    # 0 would mean "before the run", which is what ddl: already is.
    path, _ = _write(tmp_path, TARGET.replace(
        "  input: input/events.json",
        "  input: input/events.json\n  mid_run:\n    - after: 0\n      file: a.sql"))
    with pytest.raises(ManifestError, match="at least 1"):
        load_manifest(path)


def test_mid_run_allows_steps_sharing_an_ordinal_in_list_order(tmp_path):
    # One gate at 2; the callback runs both steps there, in list order.
    path, _ = _write(tmp_path, TARGET.replace(
        "  input: input/events.json",
        "  input: input/events.json\n  mid_run:\n    - after: 2\n      file: b.sql\n"
        "    - after: 2\n      file: a.sql\n      db: oracle-target"))
    target = load_manifest(path).target
    assert [(s.after, s.file, s.db) for s in target.mid_run] == [
        (2, "b.sql", "postgres-target"), (2, "a.sql", "oracle-target")]
    assert target_to_wire(target)["midRunAfter"] == [2, 2]


def test_mid_run_rejects_a_descending_list(tmp_path):
    path, _ = _write(tmp_path, TARGET.replace(
        "  input: input/events.json",
        "  input: input/events.json\n  mid_run:\n    - after: 4\n      file: a.sql\n"
        "    - after: 2\n      file: b.sql"))
    with pytest.raises(ManifestError, match="must be non-descending"):
        load_manifest(path)


def test_mid_run_rejects_an_unknown_key(tmp_path):
    path, _ = _write(tmp_path, TARGET.replace(
        "  input: input/events.json",
        "  input: input/events.json\n  mid_run:\n    - after: 2\n      file: a.sql\n"
        "      whenever: nope"))
    with pytest.raises(ManifestError, match="unknown key"):
        load_manifest(path)


def test_mid_run_requires_a_file(tmp_path):
    path, _ = _write(tmp_path, TARGET.replace(
        "  input: input/events.json",
        "  input: input/events.json\n  mid_run:\n    - after: 2"))
    with pytest.raises(ManifestError, match="file' is required"):
        load_manifest(path)


def test_target_to_wire_none_stays_none():
    assert target_to_wire(None) is None


def test_target_requires_input(tmp_path):
    path, _ = _write(tmp_path, BASE + """
target:
  restart_after: 2
assert:
  acked: 1
""")
    with pytest.raises(ManifestError, match="target.input' is required"):
        load_manifest(path)


def test_target_rejects_unknown_key(tmp_path):
    path, _ = _write(tmp_path, TARGET.replace(
        "  input: input/events.json", "  input: input/events.json\n  restart_afer: 2"))
    with pytest.raises(ManifestError, match="unknown key"):
        load_manifest(path)


def test_target_rejects_assert_data(tmp_path):
    # A target emits nothing, so a WAEvent comparison would assert against an empty list.
    path, _ = _write(tmp_path, BASE + """
target:
  input: input/events.json
assert:
  data:
    - input: input/events.json
      match: expected/events.json
""")
    with pytest.raises(ManifestError, match="no meaning for a 'target:' case"):
        load_manifest(path)


def test_restart_after_below_one_is_rejected(tmp_path):
    path, _ = _write(tmp_path, TARGET.replace(
        "  input: input/events.json", "  input: input/events.json\n  restart_after: 0"))
    with pytest.raises(ManifestError, match="at least 1"):
        load_manifest(path)


def test_restart_after_needs_positions(tmp_path):
    # With no positions there is nothing to checkpoint, so the restart would replay the whole
    # input and the case would be asserting idempotence rather than recovery.
    path, _ = _write(tmp_path, TARGET.replace(
        "  input: input/events.json",
        "  input: input/events.json\n  restart_after: 2\n  positions: false"))
    with pytest.raises(ManifestError, match="needs 'positions: true'"):
        load_manifest(path)


def test_target_and_source_are_mutually_exclusive(tmp_path):
    path, _ = _write(tmp_path, BASE + """
target:
  input: input/events.json
source:
  max_ticks: 3
assert:
  acked: 1
""")
    with pytest.raises(ManifestError, match="'target' and 'source' cannot both"):
        load_manifest(path)


def test_target_needs_requires(tmp_path):
    # The whole point of this tier for a writer is that the database is real.
    path, _ = _write(tmp_path, """
name: writer-case
op:
  jar: java/OpenProcessors/JdbcSink
target:
  input: input/events.json
assert:
  acked: 1
""")
    with pytest.raises(ManifestError, match="needs 'requires:'"):
        load_manifest(path)


def test_target_needs_an_assertion(tmp_path):
    path, _ = _write(tmp_path, BASE + """
target:
  input: input/events.json
assert:
  smoke: false
""")
    with pytest.raises(ManifestError, match="must declare at least one assertion"):
        load_manifest(path)


def test_assert_target_needs_a_target_block(tmp_path):
    path, _ = _write(tmp_path, """
name: op-case
op:
  jar: java/OpenProcessors/ExampleTransformOp
assert:
  target:
    - query: SELECT 1 ORDER BY 1
      match: expected/rows.json
""")
    with pytest.raises(ManifestError, match="needs a 'target:' block"):
        load_manifest(path)


def test_assert_acked_needs_a_target_block(tmp_path):
    path, _ = _write(tmp_path, """
name: op-case
op:
  jar: java/OpenProcessors/ExampleTransformOp
assert:
  acked: 3
""")
    with pytest.raises(ManifestError, match="only a target acknowledges"):
        load_manifest(path)


def test_assert_target_query_without_order_by_is_rejected(tmp_path):
    # Rows are compared in order and a database may return them in any order it likes.
    path, _ = _write(tmp_path, BASE + """
target:
  input: input/events.json
assert:
  target:
    - query: SELECT ID FROM T
      match: expected/rows.json
""")
    with pytest.raises(ManifestError, match="no ORDER BY"):
        load_manifest(path)


def test_assert_target_rejects_an_unknown_route(tmp_path):
    path, _ = _write(tmp_path, TARGET.replace(
        "      match: expected/rows.json", "      db: notaroute\n      match: expected/rows.json"))
    with pytest.raises(ManifestError):
        load_manifest(path)


def test_assert_acked_rejects_a_negative_count(tmp_path):
    path, _ = _write(tmp_path, TARGET + "  acked: -1\n")
    with pytest.raises(ManifestError, match="non-negative integer"):
        load_manifest(path)


def test_restart_and_replay_counts_normalize(tmp_path):
    # These two are what make a recovery case about recovery: the rows and the ack count are
    # identical whether or not the restart happened.
    path, _ = _write(tmp_path, BASE + """
target:
  input: input/events.json
  restart_after: 3
assert:
  acked: 5
  restarts: 1
  replayed: 3
""")
    m = load_manifest(path)
    assert (m.assert_.restarts, m.assert_.replayed) == (1, 3)


def test_restart_count_needs_a_target_block(tmp_path):
    path, _ = _write(tmp_path, """
name: op-case
op:
  jar: java/OpenProcessors/ExampleTransformOp
assert:
  restarts: 1
""")
    with pytest.raises(ManifestError, match="needs a 'target:' block"):
        load_manifest(path)


def test_replayed_rejects_a_negative_count(tmp_path):
    path, _ = _write(tmp_path, BASE + """
target:
  input: input/events.json
assert:
  replayed: -1
""")
    with pytest.raises(ManifestError, match="non-negative integer"):
        load_manifest(path)


def test_restart_counts_alone_do_not_count_as_an_assertion(tmp_path):
    # `restarts`/`replayed` describe what the HARNESS did, not what the writer wrote or released.
    # A case asserting only those drives the writer and checks nothing about it.
    path, _ = _write(tmp_path, BASE + """
target:
  input: input/events.json
  restart_after: 2
assert:
  restarts: 1
""")
    with pytest.raises(ManifestError, match="do not count"):
        load_manifest(path)


def test_target_timezone_normalizes_and_reaches_the_wire(tmp_path):
    path, _ = _write(tmp_path, TARGET.replace(
        "  input: input/events.json", "  input: input/events.json\n  timezone: Asia/Tokyo"))
    m = load_manifest(path)
    assert m.target.timezone == "Asia/Tokyo"


def test_target_timezone_defaults_to_none(tmp_path):
    # Absent means "whatever the JVM would pick", which is every existing case's behaviour.
    path, _ = _write(tmp_path, TARGET)
    assert load_manifest(path).target.timezone is None


def test_target_rejects_an_unknown_timezone(tmp_path):
    # The JVM does NOT reject an unknown -Duser.timezone -- it silently falls back to GMT. A typo
    # would quietly turn a non-UTC case into a second UTC one that still passes and proves nothing,
    # which is the exact failure mode the paired UTC/Tokyo cases exist to avoid.
    path, _ = _write(tmp_path, TARGET.replace(
        "  input: input/events.json", "  input: input/events.json\n  timezone: Asia/Tokyoo"))
    with pytest.raises(ManifestError, match="not a known zone id"):
        load_manifest(path)


def test_target_rejects_an_empty_timezone(tmp_path):
    path, _ = _write(tmp_path, TARGET.replace(
        "  input: input/events.json", "  input: input/events.json\n  timezone: '  '"))
    with pytest.raises(ManifestError, match="non-empty IANA zone id"):
        load_manifest(path)


# ---------------------------------------------------------------------------
# §6.1a: the knob matrix. ONE case, every permutation, ONE shared expectation.
# ---------------------------------------------------------------------------

MATRIX = BASE + """
target:
  input: input/events.json
matrix:
  UseUpsert: ['true', 'false']
  CompactEvents: ['true', 'false']
assert:
  acked: 5
"""


def test_matrix_cross_products_in_declaration_order(tmp_path):
    path, _ = _write(tmp_path, MATRIX)
    perms = load_manifest(path).permutations()
    assert perms == [
        {"UseUpsert": "true", "CompactEvents": "true"},
        {"UseUpsert": "true", "CompactEvents": "false"},
        {"UseUpsert": "false", "CompactEvents": "true"},
        {"UseUpsert": "false", "CompactEvents": "false"},
    ], "order must follow the file, so a failure names something an author can find"


def test_three_flags_give_eight_permutations(tmp_path):
    path, _ = _write(tmp_path, MATRIX.replace(
        "  CompactEvents: ['true', 'false']",
        "  CompactEvents: ['true', 'false']\n  NormalizeColumnSet: ['true', 'false']"))
    assert len(load_manifest(path).permutations()) == 8


def test_no_matrix_is_one_empty_overlay(tmp_path):
    # A case without the block runs exactly once with exactly its own properties -- the existing
    # behaviour, expressed rather than special-cased at the call site.
    path, _ = _write(tmp_path, TARGET)
    assert load_manifest(path).permutations() == [{}]


def test_matrix_rejects_a_key_also_in_properties(tmp_path):
    # Which one wins is not something an author should have to know.
    path, _ = _write(tmp_path, BASE + """
properties:
  UseUpsert: 'true'
target:
  input: input/events.json
matrix:
  UseUpsert: ['true', 'false']
assert:
  acked: 5
""")
    with pytest.raises(ManifestError, match="in both 'properties:' and 'matrix:'"):
        load_manifest(path)


def test_matrix_rejects_a_single_value(tmp_path):
    path, _ = _write(tmp_path, MATRIX.replace("  UseUpsert: ['true', 'false']",
                                              "  UseUpsert: 'true'"))
    with pytest.raises(ManifestError, match="must be a LIST"):
        load_manifest(path)


def test_matrix_rejects_a_one_element_list(tmp_path):
    path, _ = _write(tmp_path, MATRIX.replace("  UseUpsert: ['true', 'false']",
                                              "  UseUpsert: ['true']"))
    with pytest.raises(ManifestError, match="at least two values"):
        load_manifest(path)


def test_matrix_rejects_repeated_values(tmp_path):
    # The same permutation would run twice and prove nothing the first run did not.
    path, _ = _write(tmp_path, MATRIX.replace("  UseUpsert: ['true', 'false']",
                                              "  UseUpsert: ['true', 'true']"))
    with pytest.raises(ManifestError, match="repeats"):
        load_manifest(path)


def test_matrix_works_for_an_openprocessor_case_too(tmp_path):
    # `matrix:` is not target-only: it overrides `properties:`, which every configured case has.
    path, _ = _write(tmp_path, """
name: op-case
op:
  jar: java/OpenProcessors/ExampleTransformOp
matrix:
  EnableRegex: ['true', 'false']
assert:
  data:
    - input: input/e.json
      match: expected/e.json
""")
    assert load_manifest(path).permutations() == [
        {"EnableRegex": "true"}, {"EnableRegex": "false"}]


def test_matrix_on_a_udf_case_is_refused(tmp_path):
    # A UDF is a bare static function with no `properties:`, so every permutation would be
    # identical -- N passes that look like they proved something.
    path, _ = _write(tmp_path, """
name: udf-case
udf:
  jar: java/UserDefinedFunctions/ExampleJsonUdf
  class: com.example.ExampleJsonUdf
  kind: jsonnode
  source: data[0]
  target: data[0]
  pipeline:
    - function: JSONGet
      args: ['$', 'a']
matrix:
  Foo: ['a', 'b']
assert:
  data:
    - input: input/e.json
      match: expected/e.json
""")
    with pytest.raises(ManifestError, match="no meaning for a 'udf:' case"):
        load_manifest(path)


# ---------------------------------------------------------------------------
# assert.expect_error: the drive must FAIL, and for the stated reason.
# ---------------------------------------------------------------------------


def test_expect_error_normalizes(tmp_path):
    path, _ = _write(tmp_path, BASE + """
target:
  input: input/events.json
assert:
  expect_error: 'matched 2 tables'
""")
    assert load_manifest(path).assert_.expect_error == "matched 2 tables"


def test_expect_error_rejects_a_substring_that_matches_anything(tmp_path):
    # A case that cannot tell the refusal it is testing from the harness failing to start is
    # worse than no case.
    for broad in ("Error", "exception", "failed"):
        sub = tmp_path / broad
        sub.mkdir()
        path, _ = _write(sub, BASE + f"""
target:
  input: input/events.json
assert:
  expect_error: '{broad}'
""")
        with pytest.raises(ManifestError, match="too broad"):
            load_manifest(path)


def test_expect_error_cannot_be_combined_with_success_assertions(tmp_path):
    # The drive is expected to fail, so there is nothing emitted to match and nothing written to
    # read back. Allowing both would let a case quietly assert the success path.
    path, _ = _write(tmp_path, BASE + """
target:
  input: input/events.json
assert:
  expect_error: 'matched 2 tables'
  acked: 5
""")
    with pytest.raises(ManifestError, match="cannot be combined"):
        load_manifest(path)


def test_expect_error_alone_satisfies_the_assertion_requirement(tmp_path):
    path, _ = _write(tmp_path, BASE + """
target:
  input: input/events.json
assert:
  expect_error: 'matched 2 tables'
""")
    assert load_manifest(path).assert_.expect_error is not None


# ---------------------------------------------------------------------------
# §48.6: variants — the CONNECTION axis. One case, every database type, one expectation.
# ---------------------------------------------------------------------------

VARIANTS = BASE + """
target:
  input: input/events.json
variants:
  postgres:
    db: postgres-target
    tokens: {V_URL: 'jdbc:postgresql://x', V_PROVIDER: Postgres}
  oracle:
    db: oracle-target
    tokens: {V_URL: 'jdbc:oracle:thin:@x', V_PROVIDER: Oracle}
assert:
  acked: 5
"""


def test_variants_normalize_in_declaration_order(tmp_path):
    m = load_manifest(_write(tmp_path, VARIANTS)[0])
    assert [v.name for v in m.variants] == ["postgres", "oracle"]
    assert m.variants[0].tokens["V_PROVIDER"] == "Postgres"
    assert m.variants[1].db == "oracle-target"


def test_runs_compose_variants_with_the_matrix(tmp_path):
    # The prize: 2 variants x 2 permutations = 4 runs of ONE authored case.
    m = load_manifest(_write(tmp_path, VARIANTS.replace(
        "variants:", "matrix:\n  UseUpsert: ['true', 'false']\nvariants:"))[0])
    runs = m.runs()
    assert len(runs) == 4
    assert [(v.name, o["UseUpsert"]) for v, o in runs] == [
        ("postgres", "true"), ("postgres", "false"),
        ("oracle", "true"), ("oracle", "false")]


def test_int_variant_narrows_runs_to_one_engine(tmp_path, monkeypatch):
    """INT_VARIANT is how you iterate on ONE engine without bringing up six databases.

    The live tier does this with pytest markers (`-m "live and teradata"`); this tier composes
    variants INSIDE one test, so a marker cannot reach them and an env filter is the only lever.
    """
    m = load_manifest(_write(tmp_path, VARIANTS)[0])
    assert [v.name for v, _ in m.runs()] == ["postgres", "oracle"], "unset runs every variant"

    monkeypatch.setenv("INT_VARIANT", "oracle")
    assert [v.name for v, _ in m.runs()] == ["oracle"]


def test_int_variant_naming_nothing_yields_no_runs(tmp_path, monkeypatch):
    """⚠ A name no case defines yields NO runs, never all of them.

    Falling back to the full matrix would read as "the filter worked" while running every engine
    -- the failure that makes a filter worse than none.
    """
    m = load_manifest(_write(tmp_path, VARIANTS)[0])
    monkeypatch.setenv("INT_VARIANT", "teradata")   # not a variant of this fixture
    assert m.runs() == []


def test_int_variant_does_not_touch_a_case_without_variants(tmp_path, monkeypatch):
    # A case with no variants: the filter is not its business, and must not silence it.
    m = load_manifest(_write(tmp_path, TARGET)[0])
    monkeypatch.setenv("INT_VARIANT", "teradata")
    assert m.runs() == [(None, {})]


def test_no_variants_is_one_run_with_none(tmp_path):
    # A case without the block runs exactly once, with no variant -- existing behaviour.
    m = load_manifest(_write(tmp_path, TARGET)[0])
    assert m.runs() == [(None, {})]


def test_a_variant_may_not_carry_a_columnmap(tmp_path):
    # THE guard. A variant varies the CONNECTION; COLUMNMAP changes the MAPPING, which writes
    # different rows and so needs its own golden -- destroying the one shared expectation that is
    # the whole construct.
    path, _ = _write(tmp_path, VARIANTS.replace(
        "{V_URL: 'jdbc:postgresql://x', V_PROVIDER: Postgres}",
        "{V_TABLE: 'qatarget.t COLUMNMAP(a=b)'}"))
    with pytest.raises(ManifestError, match="contains COLUMNMAP"):
        load_manifest(path)


def test_a_variant_may_not_carry_keycolumns(tmp_path):
    path, _ = _write(tmp_path, VARIANTS.replace(
        "{V_URL: 'jdbc:postgresql://x', V_PROVIDER: Postgres}",
        "{V_TABLE: 'qatarget.t KEYCOLUMNS(id)'}"))
    with pytest.raises(ManifestError, match="contains KEYCOLUMNS"):
        load_manifest(path)


def test_a_variant_may_not_carry_appendonly(tmp_path):
    # APPENDONLY changes WHAT is written (one row per operation), so it is a mapping
    # clause like the other three, not a connection detail a variant may vary.
    path, _ = _write(tmp_path, VARIANTS.replace(
        "{V_URL: 'jdbc:postgresql://x', V_PROVIDER: Postgres}",
        "{V_TABLE: 'qatarget.t APPENDONLY'}"))
    with pytest.raises(ManifestError, match="contains APPENDONLY"):
        load_manifest(path)


def test_the_mapping_guard_is_case_insensitive(tmp_path):
    # `columnmap(...)` is just as much a mapping as `COLUMNMAP(...)`.
    path, _ = _write(tmp_path, VARIANTS.replace(
        "{V_URL: 'jdbc:postgresql://x', V_PROVIDER: Postgres}",
        "{V_TABLE: 'qatarget.t columnmap(a=b)'}"))
    with pytest.raises(ManifestError, match="contains COLUMNMAP"):
        load_manifest(path)


def test_variants_are_refused_on_a_data_case_because_the_runner_ignores_them(tmp_path):
    # plugin.py loops over runs() in exactly two branches -- assert.expect_error and
    # target: -- while the assert.data branch loops over permutations() and renders every token
    # from the CASE-level map. So a data case could declare three variants, parse cleanly, have
    # runs() return three, and execute ONCE against whatever the case-level tokens named.
    # Declared and inert, in the harness that refuses that shape everywhere else.
    #
    # MEASURED before this was added: of the 74 corpus cases declaring variants, 73 are target
    # cases and ZERO are data cases, so this refuses nothing that exists. It would have caught
    # an unsupported first attempt at load rather than after a Docker run reported a single pass.
    path, _ = _write(tmp_path, """
name: op-case
op:
  jar: java/OpenProcessors/LookupOp
variants:
  postgres:
    db: postgres-source
    tokens: {V_URL: 'jdbc:postgresql://x'}
  oracle:
    db: oracle-source
    tokens: {V_URL: 'jdbc:oracle:thin:@x'}
assert:
  data:
    - input: input/e.json
      match: expected/e.json
""")
    with pytest.raises(ManifestError, match="not honoured for this case shape"):
        load_manifest(path)


def test_variants_are_still_ACCEPTED_on_a_target_case_theControl(tmp_path):
    # The control. The refusal must be about the case SHAPE, not about variants -- 73 shipped
    # cases depend on this staying legal, and a guard that took them out would be caught here
    # rather than by the whole corpus failing to collect.
    path, _ = _write(tmp_path, VARIANTS)
    m = load_manifest(path)
    assert [v.name for v in m.variants] == ["postgres", "oracle"]


def test_a_variant_may_not_carry_an_ec_style_column_list(tmp_path):
    # The guard was REAL for JdbcSink and decorative for everyone else: it knew
    # only that module's TQL clause names. LookupOp expresses the same mapping in its
    # config.json as keyColumnNames / valColumnNames / sourceColumnNames / columnNames, so a token
    # feeding a different key column into a config passed silently -- the guard's own docstring
    # says a different mapping needs its own golden, and for EC nothing enforced it.
    for key_name in ("keyColumnNames", "valColumnNames", "sourceColumnNames", "columnNames"):
        # _write makes a fixed subdirectory, so each iteration needs its own tmp root.
        root = tmp_path / key_name
        root.mkdir()
        path, _ = _write(root, VARIANTS.replace(
            "{V_URL: 'jdbc:postgresql://x', V_PROVIDER: Postgres}",
            "{V_CFG: '\"%s\": [\"OTHER_ID\"]'}" % key_name))
        with pytest.raises(ManifestError, match="contains COLUMNNAMES"):
            load_manifest(path)


def test_the_ec_clause_is_one_word_because_it_is_a_substring_of_all_four(tmp_path):
    # Why COLUMNNAMES is a single entry rather than four: it is a substring of every one of EC's
    # mapping keys. Spelling them out separately is how the fifth gets forgotten.
    for key_name in ("keyColumnNames", "valColumnNames", "sourceColumnNames", "columnNames"):
        assert "COLUMNNAMES" in key_name.upper(), key_name


def test_a_variant_may_still_name_a_column_LIST_that_is_not_a_mapping(tmp_path):
    # The control, and it is the one that matters: the guard must not refuse a legitimate
    # connection-axis token. Engines spell their column lists differently in DDL and in a SELECT,
    # and V_COLS is an existing, shipped token doing exactly that.
    path, _ = _write(tmp_path, VARIANTS.replace(
        "{V_URL: 'jdbc:postgresql://x', V_PROVIDER: Postgres}",
        "{V_COLS: 'id, name, note'}"))
    load_manifest(path)


def test_a_variant_may_name_a_plain_table(tmp_path):
    # The guard must not block the legitimate case: engines spell their tables differently.
    m = load_manifest(_write(tmp_path, VARIANTS.replace(
        "{V_URL: 'jdbc:postgresql://x', V_PROVIDER: Postgres}",
        "{V_TABLE: 'qatarget.customers'}"))[0])
    assert m.variants[0].tokens["V_TABLE"] == "qatarget.customers"


def test_variants_reject_unknown_keys(tmp_path):
    # A variant supplies a route, its DDL and token values -- it does not restructure the case.
    path, _ = _write(tmp_path, VARIANTS.replace(
        "    db: postgres-target\n", "    db: postgres-target\n    properties: {X: y}\n", 1))
    with pytest.raises(ManifestError, match="unknown key"):
        load_manifest(path)


def test_variants_need_at_least_two(tmp_path):
    path, _ = _write(tmp_path, BASE + """
target:
  input: input/events.json
variants:
  postgres:
    db: postgres-target
assert:
  acked: 5
""")
    with pytest.raises(ManifestError, match="at least two entries"):
        load_manifest(path)


def test_variants_require_a_db_route(tmp_path):
    path, _ = _write(tmp_path, VARIANTS.replace("    db: postgres-target\n", "", 1))
    with pytest.raises(ManifestError, match="needs a non-empty 'db' route"):
        load_manifest(path)


def test_a_variants_ddl_takes_the_variants_route_by_default(tmp_path):
    # The rule that keeps one engine's SQL off another engine's connection: a file that names no
    # route inherits the variant's, whatever `ddl:`'s own default would have been.
    test_dir = tmp_path / "some-test"
    test_dir.mkdir()
    (test_dir / "x.sql").write_text("select 1;")
    (test_dir / "test.yaml").write_text(VARIANTS.replace(
        "    db: oracle-target\n", "    db: oracle-target\n    ddl: [x.sql]\n", 1))
    m = load_manifest(test_dir / "test.yaml")
    assert [(f.file, f.db) for f in m.variants[1].ddl] == [("x.sql", "oracle-target")]


def test_a_variants_ddl_may_name_another_route_of_the_same_service(tmp_path):
    # ⚠ §84. The SOURCE schema and the TARGET schema of ONE engine -- what a case needs when its
    # fixture puts the same table name in both. This used to be accepted and then silently
    # DISCARDED, which is §98.1's shape: a route resolved at parse time and overwritten after.
    test_dir = tmp_path / "some-test"
    test_dir.mkdir()
    (test_dir / "x.sql").write_text("select 1;")
    (test_dir / "test.yaml").write_text(VARIANTS.replace(
        "    db: postgres-target\n",
        "    db: postgres-target\n    ddl:\n      - {file: x.sql, db: postgres-source}\n", 1))
    m = load_manifest(test_dir / "test.yaml")
    assert [(f.file, f.db) for f in m.variants[0].ddl] == [("x.sql", "postgres-source")]


def test_a_variants_ddl_may_not_name_another_engine(tmp_path):
    # The property the unconditional override was protecting: PostgreSQL DDL must not reach the
    # Oracle connection just because a file said so.
    test_dir = tmp_path / "some-test"
    test_dir.mkdir()
    (test_dir / "x.sql").write_text("select 1;")
    (test_dir / "test.yaml").write_text(VARIANTS.replace(
        "    db: postgres-target\n",
        "    db: postgres-target\n    ddl:\n      - {file: x.sql, db: oracle-target}\n", 1))
    with pytest.raises(ManifestError, match="never another engine"):
        load_manifest(test_dir / "test.yaml")


def test_an_explicit_assertion_db_beside_variants_is_refused(tmp_path):
    # Third instance of §100.4's shape, in the assertion path: plugin.py reads every assertion
    # through the VARIANT's route, so a `db:` here was accepted and silently overridden. Reading
    # through the variant is the DESIGN -- it is what lets one query reach five engines -- so the
    # key is refused rather than honoured.
    path, _ = _write(tmp_path, VARIANTS.replace(
        "assert:\n  acked: 5",
        "assert:\n  target:\n"
        "    - query: 'SELECT a FROM t ORDER BY 1'\n"
        "      db: postgres-source\n"
        "      match: expected/rows.json"))
    with pytest.raises(ManifestError, match="reads every assertion through the VARIANT"):
        load_manifest(path)


def test_an_assertion_without_a_db_is_fine_beside_variants(tmp_path):
    # The guard must not block the normal form, which is every fanned-out case in the repo.
    path, _ = _write(tmp_path, VARIANTS.replace(
        "assert:\n  acked: 5",
        "assert:\n  target:\n"
        "    - query: 'SELECT a FROM t ORDER BY 1'\n"
        "      match: expected/rows.json"))
    m = load_manifest(path)
    assert m.assert_.target[0].db_explicit is False


def test_a_variants_ddl_route_typo_is_a_manifest_error_not_a_traceback(tmp_path):
    # ⚠ parse_route raises RouteError, which IntYamlFile.collect does not catch. Without the
    # guard the author gets a traceback for a typo, where every other bad route in this file
    # gets a sentence.
    test_dir = tmp_path / "some-test"
    test_dir.mkdir()
    (test_dir / "x.sql").write_text("select 1;")
    (test_dir / "test.yaml").write_text(VARIANTS.replace(
        "    db: postgres-target\n",
        "    db: postgres-target\n    ddl:\n      - {file: x.sql, db: postgres-targt}\n", 1))
    with pytest.raises(ManifestError, match="postgres-targt"):
        load_manifest(test_dir / "test.yaml")


def test_case_level_seed_beside_variants_is_refused(tmp_path):
    # The same trap as the ddl rule below, one field over: a case-level seed: is rendered with
    # the VARIANT's tokens but runs through its own route, seeding one engine with another's
    # table names and failing nothing. §98.1's shape, closed before a case walks into it.
    test_dir = tmp_path / "some-test"
    test_dir.mkdir()
    (test_dir / "s.sql").write_text("select 1;")
    (test_dir / "test.yaml").write_text(VARIANTS.replace(
        "target:\n  input: input/events.json",
        "seed:\n  - file: s.sql\ntarget:\n  input: input/events.json"))
    with pytest.raises(ManifestError, match="cannot carry a case-level 'seed:'"):
        load_manifest(test_dir / "test.yaml")


def test_case_level_ddl_beside_variants_is_refused(tmp_path):
    # A shared ddl: would run one engine's SQL through every variant's connection.
    test_dir = tmp_path / "some-test"
    test_dir.mkdir()
    (test_dir / "x.sql").write_text("select 1;")
    (test_dir / "test.yaml").write_text(VARIANTS.replace(
        "target:\n  input: input/events.json",
        "ddl:\n  - file: x.sql\ntarget:\n  input: input/events.json"))
    with pytest.raises(ManifestError, match="puts its DDL on each VARIANT"):
        load_manifest(test_dir / "test.yaml")


# ---------------------------------------------------------------------------
# assert.monitor: what the platform is SHOWN. §43.24 records it seeing no throughput at all for
# a whole live run, with 558 unit tests and a green live run silent about it.
# ---------------------------------------------------------------------------


def test_monitor_normalizes_values_to_strings(tmp_path):
    path, _ = _write(tmp_path, BASE + """
target:
  input: input/events.json
assert:
  monitor:
    PROCESSED: 5
    TARGET_COMMIT_POSITION: 5
""")
    assert load_manifest(path).assert_.monitor == {
        "PROCESSED": "5", "TARGET_COMMIT_POSITION": "5"}


def test_monitor_refuses_a_clock_metric(tmp_path):
    # A case asserting a latency or a timestamp measures the machine it runs on -- a flake
    # authored on purpose, and the same mistake §6.1a refuses for performance.
    for clock in ("LAST_COMMIT_TIME", "LAST_IO_TIME", "COMMIT_LATENCY", "EXTERNAL_IO_LATENCY"):
        sub = tmp_path / clock
        sub.mkdir()
        path, _ = _write(sub, BASE + f"""
target:
  input: input/events.json
assert:
  monitor:
    {clock}: 0
""")
        with pytest.raises(ManifestError, match="is a CLOCK"):
            load_manifest(path)


def test_monitor_accepts_the_132_3_count_metrics(tmp_path):
    """§156. The §132.3 fields that are counts or shapes, not clocks."""
    path, _ = _write(tmp_path, BASE + """
target:
  input: input/events.json
assert:
  monitor:
    NUM_OF_EXCEPTIONS_IGNORED: 0
    OPERATION_METRICS: '{"Insert":3}'
    TABLE_INFO: '{"t":3}'
""")
    assert load_manifest(path).assert_.monitor == {
        "NUM_OF_EXCEPTIONS_IGNORED": "0",
        "OPERATION_METRICS": '{"Insert":3}',
        "TABLE_INFO": '{"t":3}',
    }


def test_monitor_refuses_last_write_age_as_a_clock(tmp_path):
    """
    §156. ⚠ LAST_WRITE_AGE is an AGE measured from a clock. Asserting "0.003 seconds since the
    last write" asserts how fast this machine ran, which is the flake the clock rule exists to
    refuse. It is still published for a human to read.
    """
    path, _ = _write(tmp_path, BASE + """
target:
  input: input/events.json
assert:
  monitor:
    LAST_WRITE_AGE: 0
""")
    with pytest.raises(ManifestError, match="is a CLOCK"):
        load_manifest(path)


def test_monitor_refuses_a_metric_the_writer_does_not_publish(tmp_path):
    path, _ = _write(tmp_path, BASE + """
target:
  input: input/events.json
assert:
  monitor:
    NOT_A_METRIC: 1
""")
    with pytest.raises(ManifestError, match="not a metric this writer publishes"):
        load_manifest(path)


def test_monitor_needs_a_target_block(tmp_path):
    path, _ = _write(tmp_path, """
name: op-case
op:
  jar: java/OpenProcessors/ExampleTransformOp
assert:
  monitor:
    PROCESSED: 1
""")
    with pytest.raises(ManifestError, match="needs a 'target:' block"):
        load_manifest(path)


def test_monitor_alone_satisfies_the_assertion_requirement(tmp_path):
    # Asserting what the platform is shown IS asserting something the writer did.
    path, _ = _write(tmp_path, BASE + """
target:
  input: input/events.json
assert:
  monitor:
    PROCESSED: 5
""")
    assert load_manifest(path).assert_.monitor == {"PROCESSED": "5"}


def test_monitor_cannot_be_combined_with_expect_error(tmp_path):
    # The drive is expected to fail, so there is nothing published to compare.
    path, _ = _write(tmp_path, BASE + """
target:
  input: input/events.json
assert:
  expect_error: 'matched 2 tables'
  monitor:
    PROCESSED: 5
""")
    with pytest.raises(ManifestError, match="cannot be combined"):
        load_manifest(path)

# ---------------------------------------------------------------------------
# assert.expect_log: what the operator LOGGED on a run that SUCCEEDS.
# ---------------------------------------------------------------------------


def test_expect_log_accepts_a_list(tmp_path):
    path, _ = _write(tmp_path, BASE + """
target:
  input: input/events.json
assert:
  acked: 1
  expect_log:
    - 'is a SQL Server driver property'
    - 'passed to the driver and ignored'
""")
    assert load_manifest(path).assert_.expect_log == [
        "is a SQL Server driver property", "passed to the driver and ignored"]


def test_expect_log_accepts_a_bare_string(tmp_path):
    # One line is the common case; requiring a list for it would be ceremony.
    path, _ = _write(tmp_path, BASE + """
target:
  input: input/events.json
assert:
  acked: 1
  expect_log: 'set-based apply path backed out'
""")
    assert load_manifest(path).assert_.expect_log == ["set-based apply path backed out"]


def test_expect_log_defaults_to_empty(tmp_path):
    # Absent means "assert nothing about the log", not "assert the log is empty".
    path, _ = _write(tmp_path, BASE + """
target:
  input: input/events.json
assert:
  acked: 1
""")
    assert load_manifest(path).assert_.expect_log == []


def test_expect_log_rejects_a_substring_that_matches_anything(tmp_path):
    # ⚠ The same guard expect_error carries. A case that cannot tell the behaviour it is about
    # from the operator logging ANYTHING is worse than no case -- and every run logs something.
    for broad in ("warn", "WARNING", "info", "error"):
        sub = tmp_path / broad
        sub.mkdir()
        path, _ = _write(sub, BASE + f"""
target:
  input: input/events.json
assert:
  expect_log: '{broad}'
""")
        with pytest.raises(ManifestError, match="too broad"):
            load_manifest(path)


def test_expect_log_rejects_blank_and_non_string_entries(tmp_path):
    for bad, sub_name in (("'   '", "blank"), ("7", "number"), ("[]", "emptylist")):
        sub = tmp_path / sub_name
        sub.mkdir()
        path, _ = _write(sub, BASE + f"""
target:
  input: input/events.json
assert:
  expect_log: {bad}
""")
        with pytest.raises(ManifestError):
            load_manifest(path)


def test_expect_log_alone_is_refused(tmp_path):
    """⚠ DELIBERATE, not an oversight in the at-least-one-assertion check.

    A log-only case proves the operator SAID something without proving it DID anything. The
    warning this key exists for is emitted on a run that otherwise succeeds, so "warned" only
    means "warned while working" when the working part is asserted too.
    """
    path, _ = _write(tmp_path, BASE + """
target:
  input: input/events.json
assert:
  expect_log: 'is a SQL Server driver property'
""")
    with pytest.raises(ManifestError, match="proves what the operator SAID"):
        load_manifest(path)


# --- assert.exception_store ------------------------------------------------

def test_assert_exception_store_normalizes_to_lists_of_ordinals(tmp_path):
    path, _ = _write(tmp_path, TARGET + "  exception_store: [[2, 3], [5]]\n")
    m = load_manifest(path)
    assert m.assert_.exception_store == [[2, 3], [5]]


def test_assert_exception_store_empty_list_is_an_assertion(tmp_path):
    # `[]` says NOTHING reached the store, which is what a case with no ignorable rows asserts.
    path, _ = _write(tmp_path, TARGET + "  exception_store: []\n")
    assert load_manifest(path).assert_.exception_store == []


def test_assert_exception_store_needs_a_target_block(tmp_path):
    path, _ = _write(tmp_path, """
name: op-case
op:
  jar: java/OpenProcessors/ExampleTransformOp
assert:
  exception_store: [[1]]
""")
    with pytest.raises(ManifestError, match="only a target skips rows"):
        load_manifest(path)


def test_assert_exception_store_rejects_a_flat_list(tmp_path):
    # One notification carries a LIST of ordinals (a folded row has several); a flat list is the
    # commonest way to write the wrong thing.
    path, _ = _write(tmp_path, TARGET + "  exception_store: [2, 3]\n")
    with pytest.raises(ManifestError, match="list of lists of 1-based input ordinals"):
        load_manifest(path)


def test_assert_exception_store_rejects_a_zero_ordinal(tmp_path):
    path, _ = _write(tmp_path, TARGET + "  exception_store: [[0]]\n")
    with pytest.raises(ManifestError, match="1-based"):
        load_manifest(path)


def test_assert_exception_store_alone_is_refused(tmp_path):
    # It says what the writer did NOT write; a case asserting only that drives the writer and
    # checks nothing it did -- the same rule as restarts/replayed. It supplements target:/acked:.
    path, _ = _write(tmp_path, BASE + """
target:
  input: input/events.json
assert:
  exception_store: []
""")
    with pytest.raises(ManifestError, match="must declare at least one assertion"):
        load_manifest(path)


def test_assert_exception_store_cannot_combine_with_expect_error(tmp_path):
    # A failed drive writes no run report, so there is nothing for the store assertion to read
    # -- and the plugin returns after expect_error, so it would be silently ignored.
    path, _ = _write(tmp_path, BASE + """
target:
  input: input/events.json
assert:
  expect_error: boom
  exception_store: [[1]]
""")
    with pytest.raises(ManifestError, match="cannot be combined"):
        load_manifest(path)


# --- assert.monitor shape metrics: COMMIT_LAG ------------------------------------------

def test_monitor_commit_lag_is_a_shape_metric_taking_table_names(tmp_path):
    # Its values are clocks; what a case asserts is WHICH tables carry a lag.
    path, _ = _write(tmp_path, BASE + """
target:
  input: input/events.json
assert:
  monitor:
    COMMIT_LAG: ['${V_TABLE}', 'QATARGET.OTHER']
""")
    assert load_manifest(path).assert_.monitor == {"COMMIT_LAG": ["${V_TABLE}", "QATARGET.OTHER"]}


def test_monitor_commit_lag_refuses_a_scalar(tmp_path):
    # A number here would be the machine's clock against the fixture's -- the clock flake.
    path, _ = _write(tmp_path, BASE + """
target:
  input: input/events.json
assert:
  monitor:
    COMMIT_LAG: 250
""")
    with pytest.raises(ManifestError, match="SHAPE metric"):
        load_manifest(path)
