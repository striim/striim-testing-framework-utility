"""Manifest-level validation of `sort_by:` — the contradictions worth catching at load.

⚠ Note the quoting in these fixtures: `sort_by: ["data[0]"]` and not `[data[0]]`. YAML flow
sequences treat `[` as structure, so an unquoted array path is a parse error rather than a
manifest error. Block style (`- data[0]`) needs no quotes, which is why the existing
ignore_fields cases never hit it.

Every check here fails BEFORE a container starts, which is the point: a case that sorts by a
field it also declared volatile would otherwise be stable in the run that authored its golden
and arbitrary afterwards.
"""
import pytest

from inttest.manifest import ManifestError, load_manifest


def write(tmp_path, assert_block):
    (tmp_path / "in.json").write_text("[]")
    (tmp_path / "out.json").write_text("[]")
    (tmp_path / "test.yaml").write_text(
        "name: c\npurpose: p\nop: {jar: java/OpenProcessors/X}\n" + assert_block)
    return tmp_path / "test.yaml"


def test_sort_by_is_accepted_and_normalized(tmp_path):
    m = load_manifest(write(tmp_path, """
assert:
  data:
    - input: in.json
      match: out.json
      sort_by: "data[0]"
"""))
    # Scalar courtesy, same as ddl:/requires:.
    assert m.assert_.data[0].sort_by == ("data[0]",)


def test_sorting_by_an_ignored_field_is_rejected(tmp_path):
    with pytest.raises(ManifestError) as e:
        load_manifest(write(tmp_path, """
assert:
  data:
    - input: in.json
      match: out.json
      sort_by: [metadata.TimeStamp]
      ignore_fields: [metadata.TimeStamp]
"""))
    assert "cannot be both" in str(e.value)


def test_sorting_by_a_field_project_excludes_is_rejected(tmp_path):
    with pytest.raises(ManifestError) as e:
        load_manifest(write(tmp_path, """
assert:
  data:
    - input: in.json
      match: out.json
      sort_by: ["data[0]"]
      project: ["data[1]"]
"""))
    assert "excludes from the comparison" in str(e.value)


def test_sort_by_may_combine_with_ignore_fields_on_different_paths(tmp_path):
    # The common shape: sort by a stable key, ignore a volatile one.
    m = load_manifest(write(tmp_path, """
assert:
  data:
    - input: in.json
      match: out.json
      sort_by: ["data[0]"]
      ignore_fields: [metadata.TimeStamp]
"""))
    assert m.assert_.data[0].sort_by == ("data[0]",)
    assert m.assert_.data[0].ignore_fields == ("metadata.TimeStamp",)


def test_a_malformed_sort_path_names_the_grammar(tmp_path):
    with pytest.raises(ManifestError) as e:
        load_manifest(write(tmp_path, """
assert:
  data:
    - input: in.json
      match: out.json
      sort_by: ["data[0"]
"""))
    assert "sort_by" in str(e.value)


def test_post_start_rejects_more_than_one_data_assertion(tmp_path):
    """⚠ Rejected at LOAD, because at run time it fails obscurely.

    Each `assert.data` entry drives the reader again in a fresh temp dir, and the seed is committed
    only for the first -- so the second blocks on the handshake, is released by a callback that
    returns immediately, and dies with "expected at least N events". A first attempt latched the
    callback, which only changed the diagnostic from ALREADY_EXISTS (naming the cause) to an
    event-count error (not naming it).
    """
    (tmp_path / "out.json").write_text("[]")
    (tmp_path / "test.yaml").write_text(
        "name: c\npurpose: p\nop: {jar: java/OpenProcessors/X}\n"
        "source: {max_ticks: 5, seed_when: post_start}\n"
        "assert:\n  data:\n    - match: out.json\n    - match: out.json\n")
    with pytest.raises(ManifestError) as e:
        load_manifest(tmp_path / "test.yaml")
    assert "exactly one 'assert.data' entry" in str(e.value)


def test_post_start_allows_exactly_one_data_assertion(tmp_path):
    (tmp_path / "out.json").write_text("[]")
    (tmp_path / "test.yaml").write_text(
        "name: c\npurpose: p\nop: {jar: java/OpenProcessors/X}\n"
        "source: {max_ticks: 5, seed_when: post_start}\n"
        "assert:\n  data:\n    - match: out.json\n")
    m = load_manifest(tmp_path / "test.yaml")
    assert m.source.seed_when == "post_start"


def test_pre_start_is_unaffected_by_the_assertion_limit(tmp_path):
    # The constraint is a property of committing DURING the run, not of reader cases generally.
    (tmp_path / "out.json").write_text("[]")
    (tmp_path / "test.yaml").write_text(
        "name: c\npurpose: p\nop: {jar: java/OpenProcessors/X}\n"
        "source: {max_ticks: 5}\n"
        "assert:\n  data:\n    - match: out.json\n    - match: out.json\n")
    m = load_manifest(tmp_path / "test.yaml")
    assert len(m.assert_.data) == 2
