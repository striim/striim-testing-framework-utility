from pathlib import Path

import pytest

from livetest.assertions.expected import load_expected_text
from livetest.substitute import SubstitutionError


def test_load_expected_text_passthrough_without_tokens(tmp_path):
    p = tmp_path / "x.csv"
    p.write_text("id,msg\n1,hello\n")
    assert load_expected_text(p, None) == "id,msg\n1,hello\n"


def test_load_expected_text_passthrough_when_no_placeholders(tmp_path):
    p = tmp_path / "x.csv"
    p.write_text("id,msg\n1,hello\n")
    assert load_expected_text(p, {"TID": "abc"}) == "id,msg\n1,hello\n"


def test_load_expected_text_renders_known_token(tmp_path):
    p = tmp_path / "x.csv"
    p.write_text("path\nimages/prod/QASOURCE.${TID}BLOB1_TABLE/DATA1_1.png\n")
    out = load_expected_text(p, {"TID": "objectwriter_folder_prefix_"})
    assert out == "path\nimages/prod/QASOURCE.objectwriter_folder_prefix_BLOB1_TABLE/DATA1_1.png\n"


def test_load_expected_text_fails_loudly_on_undefined_token(tmp_path):
    # A golden with a literal, UNDEFINED ${...} must fail loudly, not silently pass
    # (spec P1.2 unit-test requirement) -- catches an author typo or a token that
    # simply isn't in the tokens dict passed at assertion time.
    p = tmp_path / "x.csv"
    p.write_text("path\n${NOT_A_REAL_TOKEN}\n")
    with pytest.raises(SubstitutionError, match="NOT_A_REAL_TOKEN"):
        load_expected_text(p, {"TID": "abc"})
