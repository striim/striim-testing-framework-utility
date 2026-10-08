"""Unit tests for inttest.plugin.render_config_file -- token-rendering a ConfigFile's
CONTENT (not just its path) onto a per-test temp copy, so a config-driven OP
can carry ${TID}-style isolation in an embedded
identifier (e.g. a table name) the same way ddl:/seed: SQL already can via dbroutes.

Pure unit tests: no Docker, no DB, no pytest-plugin collection involved -- each test
writes a small inline config.json into tmp_path and calls render_config_file() directly.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from inttest.plugin import render_config_file
from inttest.tokens import SubstitutionError


def _write_config(tmp_path, text: str) -> Path:
    test_dir = tmp_path / "some-test"
    test_dir.mkdir()
    config_path = test_dir / "config.json"
    config_path.write_text(text)
    return config_path


def test_renders_tokens_onto_a_temp_copy(tmp_path):
    original = _write_config(
        tmp_path,
        json.dumps({"tables": [{"tableName": "${TID}PRODUCTS", "columns": []}]}),
    )
    original_bytes = original.read_bytes()

    rendered_path = render_config_file(str(original), {"TID": "it_abc123_"})

    assert rendered_path != str(original), "a tokenized config must be rendered onto a NEW path"
    rendered = json.loads(Path(rendered_path).read_text())
    assert rendered["tables"][0]["tableName"] == "it_abc123_PRODUCTS"
    # The checked-in fixture itself must be untouched -- rendering happens onto a
    # copy, never in place.
    assert original.read_bytes() == original_bytes


def test_returns_original_path_unchanged_when_no_tokens_present(tmp_path):
    original = _write_config(
        tmp_path,
        json.dumps({"tables": [{"tableName": "LITERAL_PRODUCTS", "columns": []}]}),
    )

    rendered_path = render_config_file(str(original), {"TID": "it_abc123_"})

    # No ${...} anywhere in the file -- must be a pure passthrough, not even a
    # byte-identical temp copy, so a config's own relative sibling references
    # (e.g. a bootstrap CSV path) keep resolving next to the ORIGINAL file.
    assert rendered_path == str(original)


def test_preserves_original_filename_in_the_temp_copy(tmp_path):
    original = _write_config(tmp_path, json.dumps({"table": "${TID}T"}))

    rendered_path = render_config_file(str(original), {"TID": "it_"})

    assert Path(rendered_path).name == "config.json"
    assert Path(rendered_path).parent != original.parent


def test_missing_token_raises_substitution_error(tmp_path):
    original = _write_config(tmp_path, json.dumps({"table": "${UNDEFINED_TOKEN}T"}))

    with pytest.raises(SubstitutionError, match="UNDEFINED_TOKEN"):
        render_config_file(str(original), {"TID": "it_"})


def test_rendered_content_is_still_valid_json(tmp_path):
    # A tokenized config.json must stay syntactically valid JSON after rendering --
    # a naive substitution that dropped or mangled surrounding quotes would corrupt
    # every config-driven OP's config file, not just fail loudly.
    original = _write_config(
        tmp_path,
        json.dumps(
            {
                "lookups": [
                    {
                        "lookupName": "L",
                        "layers": [{
                            "tableName": "${TID}LOOKUP",
                            "keyColumnNames": ["ID"],
                            "valColumnNames": ["V"],
                        }],
                    }
                ]
            }
        ),
    )

    rendered_path = render_config_file(str(original), {"TID": "it_xyz_"})

    parsed = json.loads(Path(rendered_path).read_text())
    assert parsed["lookups"][0]["layers"][0]["tableName"] == "it_xyz_LOOKUP"


def test_dest_dir_used_instead_of_a_fresh_tempdir(tmp_path):
    # inttest.perf.run_measured_iteration passes its own per-iteration scratch
    # directory as dest_dir, rather than letting this function mint a second,
    # separately-torn-down tempfile.mkdtemp per iteration.
    original = _write_config(tmp_path, json.dumps({"table": "${TID}T"}))
    dest_dir = tmp_path / "iter-scratch"
    dest_dir.mkdir()

    rendered_path = render_config_file(str(original), {"TID": "it_"}, dest_dir=dest_dir)

    rendered = Path(rendered_path)
    assert rendered.parent == dest_dir
    assert rendered.name == "config.json"
    assert json.loads(rendered.read_text()) == {"table": "it_T"}


def test_dest_dir_ignored_when_no_tokens_present(tmp_path):
    # The no-tokens fast path returns the original path unchanged regardless of
    # dest_dir -- no copy is ever made, into dest_dir or anywhere else.
    original = _write_config(tmp_path, json.dumps({"table": "LITERAL_T"}))
    dest_dir = tmp_path / "iter-scratch"
    dest_dir.mkdir()

    rendered_path = render_config_file(str(original), {"TID": "it_"}, dest_dir=dest_dir)

    assert rendered_path == str(original)
    assert list(dest_dir.iterdir()) == []


def test_repeated_token_in_one_file_all_substituted(tmp_path):
    # The same ${TID} token typically appears in more than one place in a real
    # multi-layer config (e.g. a chained lookup referencing the same isolated table
    # from two layers) -- every occurrence must be rendered, not just the first.
    original = _write_config(
        tmp_path,
        json.dumps({"a": "${TID}FOO", "b": "${TID}BAR", "c": "${TID}FOO"}),
    )

    rendered_path = render_config_file(str(original), {"TID": "it_"})

    parsed = json.loads(Path(rendered_path).read_text())
    assert parsed == {"a": "it_FOO", "b": "it_BAR", "c": "it_FOO"}
