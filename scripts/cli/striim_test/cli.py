"""striim-test command surface (C5 ``list`` and ``run``, and ``doctor``). The implemented parser
and its ``--help`` output are the executable contract.

    striim-test list     [--targets <path>] [--tier <t>] [--suite <s>]
    striim-test run      [PATH] [--targets <path>] [--tier <t>] [--suite <s>] [--case <id>...]
                         [--parallel] [--keep-resources] [--dry-run]
    striim-test doctor   [--targets <path>] [--case <path>...]

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


def build_parser() -> _Parser:
    p = _Parser(prog="striim-test", description="Run the Striim testing framework's test tiers.")
    sub = p.add_subparsers(dest="command", required=True, metavar="{list,run,doctor,fetch}")

    f = sub.add_parser("fetch", help="download and verify a runnable example bundle")
    f.add_argument("--gcs-prefix", required=True, metavar="gs://BUCKET/TOOL/RELEASE")
    f.add_argument("--destination", required=True, metavar="DIR")
    f.add_argument("--endpoint", metavar="URL", help="explicit GCS emulator endpoint (anonymous credentials)")

    ls = sub.add_parser("list", help="offline: collect and print selected case IDs")
    _targets(ls)
    ls.add_argument("--tier", metavar="TIER")
    ls.add_argument("--suite", metavar="SUITE")

    r = sub.add_parser("run", help="run selected tiers and runners")
    r.add_argument("path", nargs="?", metavar="PATH",
                   help="run only the cases under this case dir, test.yaml or folder of cases "
                        "(relative to the working directory; it must lie in a case root)")
    _targets(r)
    r.add_argument("--tier", metavar="TIER")
    r.add_argument("--suite", metavar="SUITE")
    r.add_argument("--case", metavar="ID", action="extend", nargs="+")
    r.add_argument("--parallel", action="store_true")
    r.add_argument("--keep-resources", action="store_true")
    r.add_argument("--dry-run", action="store_true")

    d = sub.add_parser("doctor", help="check .env, Striim and the services the selected cases "
                                      "require; one line per check")
    _targets(d)
    d.add_argument("--case", metavar="PATH", action="extend", nargs="+",
                   help="a case dir, its test.yaml, or a dir of cases; the services they "
                        "require: are checked")
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
