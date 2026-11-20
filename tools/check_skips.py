#!/usr/bin/env python3
"""Fail when a pytest run skipped more tests than its budget allows.

    python tools/check_skips.py junit.xml --max 40

A skip is green, so a test that stopped running because a tool, file or setting went missing goes
unseen unless something counts them. This reads the run's JUnit XML (`pytest --junitxml`), lists
every skip with its reason, and exits 1 when there are more than `--max`. An xfail is not a skip.
Exit 2 when the report is missing or unreadable: no report is never a pass.
"""
from __future__ import annotations

import argparse
import sys
import xml.etree.ElementTree as ET
from pathlib import Path


def skips(report: Path) -> list[tuple[str, str]]:
    """(test id, reason) for each skipped test case; xfails excluded."""
    found = []
    for case in ET.parse(report).getroot().iter("testcase"):
        el = case.find("skipped")
        if el is None or el.get("type") == "pytest.xfail":
            continue
        name = f"{case.get('classname', '')}::{case.get('name', '')}".lstrip(":")
        found.append((name, el.get("message") or ""))
    return found


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("report", type=Path, help="the JUnit XML pytest wrote")
    p.add_argument("--max", type=int, required=True, help="the most skips this suite may have")
    args = p.parse_args(argv)
    try:
        found = skips(args.report)
    except (OSError, ET.ParseError) as e:
        print(f"check_skips: cannot read {args.report}: {e}", file=sys.stderr)
        return 2
    for name, reason in found:
        print(f"SKIPPED {name}: {reason}")
    over = len(found) > args.max
    print(f"{len(found)} skipped (budget {args.max})" + (": more than the budget; a test that should run did not, "
          "or the budget needs raising in the same pull request that adds the skip" if over else ""))
    return 1 if over else 0


if __name__ == "__main__":
    sys.exit(main())
