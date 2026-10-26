"""`seed_when: post_start` is refused beside `matrix:`, and a reader case can never be an expect_error case."""
from __future__ import annotations

import pytest

from inttest.manifest import ManifestError, load_manifest

HEAD = ("name: c\npurpose: p\nop: {jar: java/OpenProcessors/X}\n"
        "properties: {Mode: a}\n")


def _load(tmp_path, body):
    (tmp_path / "out.json").write_text("[]")
    (tmp_path / "test.yaml").write_text(HEAD + body)
    return load_manifest(tmp_path / "test.yaml")


def test_post_start_with_a_matrix_is_refused_at_load(tmp_path):
    with pytest.raises(ManifestError, match="cannot be combined with 'matrix:'"):
        _load(tmp_path, "matrix: {Batch: ['1', '2']}\n"
                        "source: {max_ticks: 5, seed_when: post_start}\n"
                        "assert:\n  data:\n    - match: out.json\n")


def test_pre_start_with_a_matrix_still_loads(tmp_path):
    m = _load(tmp_path, "matrix: {Batch: ['1', '2']}\n"
                        "source: {max_ticks: 5}\n"
                        "assert:\n  data:\n    - match: out.json\n")
    assert len(m.permutations()) == 2


def test_a_reader_case_cannot_be_an_expect_error_case(tmp_path):
    # Why _expect_error needs no post-start seed hook: no source case ever reaches it.
    with pytest.raises(ManifestError):
        _load(tmp_path, "source: {max_ticks: 5, seed_when: post_start}\n"
                        "assert:\n  expect_error: refused for a reason\n")
    with pytest.raises(ManifestError):
        _load(tmp_path, "source: {max_ticks: 5, seed_when: post_start}\n"
                        "assert:\n  expect_error: refused for a reason\n  data:\n    - match: out.json\n")
