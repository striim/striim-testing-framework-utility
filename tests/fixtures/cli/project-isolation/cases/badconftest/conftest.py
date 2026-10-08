# Negative control: a consumer conftest that injects a legacy engine path and
# imports from it. striim-test's guard must detect the mixed provenance and fail the run.
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / ".decoy"))
import inttest  # noqa: E402,F401  (the decoy; the live tier has not imported the real one)
