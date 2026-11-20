"""striim-test command surface (C5 ``list`` and ``run``, ``doctor`` and ``fetch``). The implemented
parser and its ``--help`` output are the executable contract.

    striim-test fetch    --gcs-prefix gs://<bucket>/<tool>/<release> --destination <dir> [--endpoint <url>]
    striim-test list     [--targets <path>] [--tier <t>] [--suite <s>]
    striim-test run      [PATH] [--targets <path>] [--tier <t>] [--suite <s>] [--case <id>...]
                         [--parallel] [--keep-resources] [--dry-run]
    striim-test doctor   [--targets <path>] [--case <path>...] [--strict]

``--case`` differs by subcommand: for ``run`` it is a case id (what ``list`` prints, the manifest
``name:``, or the case dir relative to the project root); for ``doctor`` it is a path.

With no project manifest (no ``--targets``, no ``GOLD_TARGETS``) both run over this clone's own
suites, located by the SLT_* path keys.

Unknown subcommands, flags or choices return 2 (never ``sys.exit`` from library code). Parsing
uses only the standard library; engine provenance is checked before any engine import.
"""
from __future__ import annotations

import argparse
import sys

from striim_test.errors import CANCELLED, CONFIG, CliError


class _Exit(Exception):
    def __init__(self, status: int):
        super().__init__(status)
        self.status = status


class _Parser(argparse.ArgumentParser):
    def __init__(self, *args, **kwargs):
        kwargs.setdefault("allow_abbrev", False)
        super().__init__(*args, **kwargs)

    def error(self, message):
        raise CliError(CONFIG, f"{message}\n{self.format_usage().rstrip()}")

    def exit(self, status=0, message=None):
        if message:
            sys.stderr.write(message)
        raise _Exit(status)


def _targets(p):
    p.add_argument("--targets", metavar="PATH",
                   help="C1 project manifest (default: $GOLD_TARGETS; neither: this clone's "
                        "suites)")


def _tier_suite(p):
    p.add_argument("--tier", metavar="TIER",
                   help="only this tier: live, integration, perf, or a tier a project runner "
                        "declares (default: live and integration)")
    p.add_argument("--suite", metavar="SUITE",
                   help="only the cases under this folder, relative to the project root and inside "
                        "a tier's case root; or the name of a project runner")


def build_parser() -> _Parser:
    p = _Parser(prog="striim-test", description="Run the Striim testing framework's test tiers.")
    sub = p.add_subparsers(dest="command", required=True, metavar="{list,run,doctor,fetch}")

    f = sub.add_parser("fetch", help="download and verify a runnable example bundle")
    f.add_argument("--gcs-prefix", required=True, metavar="gs://BUCKET/TOOL/RELEASE",
                   help="where the bundle is published; its publisher gives it")
    f.add_argument("--destination", required=True, metavar="DIR",
                   help="an empty or missing folder to unpack the bundle into")
    f.add_argument("--endpoint", metavar="URL", help="explicit GCS emulator endpoint (anonymous credentials)")

    ls = sub.add_parser("list", help="offline: collect and print selected case IDs")
    _targets(ls)
    _tier_suite(ls)

    r = sub.add_parser("run", help="run selected tiers and runners")
    r.add_argument("path", nargs="?", metavar="PATH",
                   help="run only the cases under this case dir, test.yaml or folder of cases "
                        "(relative to the working directory; it must lie in a case root)")
    _targets(r)
    _tier_suite(r)
    r.add_argument("--case", metavar="ID", action="extend", nargs="+",
                   help="run only these cases, by id: what `list` prints, the manifest name:, or the "
                        "case dir relative to the project root (doctor's --case takes a path instead)")
    r.add_argument("--parallel", action="store_true",
                   help="run three tests at a time (pytest-xdist); not with the perf tier")
    r.add_argument("--keep-resources", action="store_true",
                   help="keep each test's app, tables and slots whatever the result "
                        "(SLT_KEEP_RESOURCES=1)")
    r.add_argument("--dry-run", action="store_true",
                   help="print what would run and deploy nothing (writes only a run record)")

    d = sub.add_parser("doctor", help="check .env, Striim and the services the selected cases "
                                      "require; one line per check")
    _targets(d)
    d.add_argument("--case", metavar="PATH", action="extend", nargs="+",
                   help="a case dir, its test.yaml, or a dir of cases; the services they "
                        "require: are checked")
    d.add_argument("--strict", action="store_true",
                   help="fail, rather than warn, on anything that would skip a selected case")
    return p


def _err(message: str) -> None:
    print(f"striim-test: error: {message}", file=sys.stderr)


def main(argv=None) -> int:
    try:
        args = build_parser().parse_args(argv)
    except _Exit as e:
        return e.status
    except CliError as e:
        _err(e.message)
        return e.code

    from striim_test import _bootstrap
    try:
        origins = _bootstrap.check_provenance()
    except _bootstrap.ProvenanceError as e:
        _err(str(e))
        return CONFIG

    try:
        if args.command == "fetch":
            from striim_test.fetch import cmd_fetch
            return cmd_fetch(args, origins)
        if args.command == "doctor":
            from striim_test.doctor import cmd_doctor
            return cmd_doctor(args, origins)
        if args.command == "list":
            from striim_test.dispatch import cmd_list
            return cmd_list(args, origins)
        from striim_test.dispatch import cmd_run
        return cmd_run(args, origins)
    except CliError as e:
        _err(e.message)
        return e.code
    except KeyboardInterrupt:
        _err("interrupted")
        return CANCELLED
