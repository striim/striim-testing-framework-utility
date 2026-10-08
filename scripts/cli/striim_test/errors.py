"""C5 exit codes and their aggregation across tiers and runners."""
from __future__ import annotations

OK = 0            # success
FAILED = 1        # at least one selected case/suite failed
CONFIG = 2        # configuration / manifest / schema / provenance error
INFRA = 3         # unavailable infrastructure (a selected item skipped, a prerequisite failed)
CANCELLED = 4     # cancelled / interrupted / timeout
NO_TESTS = 5      # no tests selected (explicit non-success)

_PRECEDENCE = (CONFIG, CANCELLED, INFRA, FAILED)


class CliError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def aggregate(codes) -> int:
    """2 > 4 > 3 > 1; all 5 -> 5; otherwise (0s, possibly with 5s) -> 0."""
    codes = list(codes)
    for c in _PRECEDENCE:
        if c in codes:
            return c
    if not codes or all(c == NO_TESTS for c in codes):
        return NO_TESTS
    return OK
