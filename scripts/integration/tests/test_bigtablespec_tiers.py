"""The two tiers load `setup: bigtable:` with the same code: livetest/bigtablespec.py is a copy of
inttest/bigtablespec.py, so `inttest` imports no `livetest`. This keeps the copy from drifting."""
from pathlib import Path

from inttest import bigtablespec as integration_tier
from livetest import bigtablespec as live_tier


def _body(module) -> str:
    text = Path(module.__file__).read_text()
    return text[text.index('"""', 3) + 3:]     # everything after the module docstring


def test_the_live_spec_is_the_integration_spec_below_its_docstring():
    assert _body(live_tier) == _body(integration_tier)
