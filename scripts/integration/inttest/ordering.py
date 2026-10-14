"""Longest-first ordering for a parallel (xdist) run, from the durations of earlier runs.

xdist hands cases to workers in collection order, so a long case collected late starts late and
the run waits for it alone at the end: measured on striim-field, a 24-minute Teradata case
scheduled near the end of a 100-case lane stretched the lane from ~39 to 48 minutes. Ordering by
known duration, longest first, starts the long cases while there is still other work to overlap.

Durations are recorded per node id from each case's call phase and kept in a JSON file in the
state directory; the newest run's value wins. A case with no history keeps its place after the
known ones, except that one needing Teradata goes first among them: on the local VM those are the
long cases.
"""
from __future__ import annotations

import json
from pathlib import Path

FILE_NAME = "integration-durations.json"


def load(path: Path) -> dict:
    """The recorded durations, {node id: seconds}; empty when absent or unreadable."""
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return {str(k): float(v) for k, v in data.items() if isinstance(v, (int, float))}


def save(path: Path, recorded: dict) -> None:
    """Merges this run's durations into the file; a failure to write never fails the run."""
    if not recorded:
        return
    merged = load(path)
    merged.update(recorded)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(merged, indent=0, sort_keys=True))
        tmp.replace(path)
    except OSError:
        pass


def longest_first(items: list, durations: dict, needs_teradata) -> list:
    """`items` reordered: known durations longest first, then Teradata cases, then the rest.

    Stable within each group, so cases with equal or no history keep collection order.
    """
    def key(indexed):
        index, item = indexed
        seconds = durations.get(item.nodeid)
        if seconds is not None:
            return (0, -seconds, index)
        return (1 if needs_teradata(item) else 2, 0.0, index)
    return [item for _, item in sorted(enumerate(items), key=key)]
