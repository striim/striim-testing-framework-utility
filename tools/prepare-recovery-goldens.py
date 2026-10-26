#!/usr/bin/env python3
"""Check that every recovery case carries the reviewed recovery golden.

The source has header id,msg and exactly (n, row-n) for n=1..5000, derived
from the input generate_series, never from observed output. The copies are
committed so each case is self-contained; review only the source.
--write copies the source over the others after a reviewed change to it.
"""
import sys
from pathlib import Path

SOURCE = "samples/live/14-progress-recovery/expected/rows.csv"
COPIES = (
    ("samples/live", ("15-fault-evidence", "16-recovery-sentinel", "17-one-node-failover")),
    ("scripts/live/regression/framework", ("framework-progress-recovery", "framework-fault-evidence",
                                           "framework-recovery-sentinel", "framework-one-node-failover")),
)


def targets(root):
    return [root / parent / name / "expected/rows.csv" for parent, names in COPIES for name in names]


def check(root):
    """Return the copies that are missing or differ from the source."""
    data = (root / SOURCE).read_bytes()
    return [t for t in targets(root) if not t.is_file() or t.read_bytes() != data]


def write(root):
    data = (root / SOURCE).read_bytes()
    for target in targets(root):
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)


def main(argv):
    root = Path(__file__).resolve().parents[1]
    if argv == ["--write"]:
        write(root)
        return 0
    if argv:
        print("usage: prepare-recovery-goldens.py [--write]", file=sys.stderr)
        return 2
    bad = check(root)
    for t in bad:
        print(f"differs from {SOURCE}: {t.relative_to(root)}", file=sys.stderr)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
