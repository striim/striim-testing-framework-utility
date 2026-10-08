import pytest
from livetest.substitute import render, missing_tokens, SubstitutionError

def test_render_replaces_tokens():
    out = render("USE ${NS}; app ${APP}", {"NS": "SLT_x", "APP": "SLT_xApp"})
    assert out == "USE SLT_x; app SLT_xApp"

def test_render_leaves_non_tokens_untouched():
    # a literal price string must survive; only ${...} is a token
    assert render("cost is $5 and ${N}", {"N": "1"}) == "cost is $5 and 1"

def test_missing_tokens_reported_sorted():
    assert missing_tokens("${B} ${A} ${A}", {}) == ["A", "B"]

def test_render_raises_on_missing():
    with pytest.raises(SubstitutionError, match="ORACLE_URL"):
        render("url ${ORACLE_URL}", {})
