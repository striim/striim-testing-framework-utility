"""The integration tier's BigtableAdmin is the live tier's code, copied so `inttest` imports no
`livetest`. The live tier's tests (scripts/live/tests/test_bigtableadmin*.py) cover that code once;
this keeps the copy from drifting, which is how the two readiness gates once came to differ."""
from pathlib import Path

from inttest import bigtableadmin as integration_tier
from livetest import bigtableadmin as live_tier


def _body(module) -> str:
    text = Path(module.__file__).read_text()
    end = text.index('"""', 3) + 3                 # everything after the module docstring
    return text[end:]


def test_the_integration_admin_is_the_live_admin_below_its_docstring():
    assert _body(integration_tier) == _body(live_tier)
