"""Integration testing framework package for Striim operators.

This is the Phase 1 integration-test sibling of scripts/live/livetest, but targeted
at single OpenProcessor/UDF behavior against real Postgres/Oracle/Spanner services.
The pytest plugin (inttest.plugin) is loaded via pyproject.toml addopts.
"""

__version__ = "0.1.0"
__all__ = ["plugin"]
