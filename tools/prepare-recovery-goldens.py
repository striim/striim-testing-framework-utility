#!/usr/bin/env python3
"""Copy the reviewed recovery golden to its sample and regression consumers.

The source has header id,msg and exactly (n, row-n) for n=1..5000, derived
from the input generate_series, never from observed output.
"""
from pathlib import Path


def prepare(root):
    source = root / "samples/live/14-progress-recovery/expected/rows.csv"
    data = source.read_bytes()
    for parent, names in (
        ("samples/live", ("15-fault-evidence", "16-recovery-sentinel", "17-one-node-failover")),
        ("scripts/live/regression/framework", ("framework-progress-recovery", "framework-fault-evidence",
                                               "framework-recovery-sentinel", "framework-one-node-failover")),
    ):
        for name in names:
            target = root / parent / name / "expected/rows.csv"
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists() and target.read_bytes() != data:
                raise SystemExit(f"Refusing to overwrite a modified golden: {target}")
            target.write_bytes(data)


if __name__ == "__main__":
    prepare(Path(__file__).resolve().parents[1])
