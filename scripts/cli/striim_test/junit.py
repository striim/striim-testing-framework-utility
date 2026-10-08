"""JUnit XML facts for the C2 runner protocol. Well-formedness and counts only; freshness is
the caller's check. Comprehensive product report parsers are phase 3."""
from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass, field


class JunitError(Exception):
    def __init__(self, reason: str, detail: str):
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


@dataclass
class ReportFacts:
    tests: int
    failures: int
    errors: int
    skipped: int
    failing: list = field(default_factory=list)
    skipped_ids: list = field(default_factory=list)


def parse_junit(path) -> ReportFacts:
    try:
        root = ET.parse(path).getroot()
    except (ET.ParseError, OSError) as e:
        raise JunitError("report-malformed", f"{path}: {e}") from None
    if root.tag == "testsuite":
        suites = [root]
    elif root.tag == "testsuites":
        suites = list(root.iter("testsuite"))
    else:
        raise JunitError("report-malformed",
                         f"{path}: root element <{root.tag}> is not <testsuites>/<testsuite>")
    facts = ReportFacts(0, 0, 0, 0)
    for case in root.iter("testcase"):
        cid = f"{case.get('classname', '')}::{case.get('name', '')}"
        facts.tests += 1
        if case.find("failure") is not None:
            facts.failures += 1
            facts.failing.append(cid)
        elif case.find("error") is not None:
            facts.errors += 1
            facts.failing.append(cid)
        elif case.find("skipped") is not None:
            facts.skipped += 1
            facts.skipped_ids.append(cid)
    declared = [s.get("tests") for s in suites]
    if suites and all(d is not None for d in declared):
        try:
            total = sum(int(d) for d in declared)
        except ValueError:
            raise JunitError("report-malformed", f"{path}: non-integer tests attribute") from None
        if total != facts.tests:
            raise JunitError("report-malformed", f"{path}: declares {total} tests but contains "
                                                 f"{facts.tests} <testcase> (truncated?)")
    return facts
