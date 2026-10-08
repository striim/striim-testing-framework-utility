"""Verify: the command surface is ``list`` and ``run`` with their C5 options, plus ``doctor`` and ``fetch``; both work from an
unrelated working directory; unknown commands and options are exit 2 before any run directory."""
import re

import pytest

from _clikit import run_cli

SURFACE = {
    "list": {"--targets", "--tier", "--suite"},
    "run": {"--targets", "--tier", "--suite", "--case", "--parallel", "--keep-resources",
            "--dry-run"},
    "doctor": {"--case", "--targets"},
    "fetch": {"--gcs-prefix", "--destination", "--endpoint"},
}


def _options(help_text: str) -> set:
    return set(re.findall(r"(?<![\w-])(--[a-z][a-z-]*)", help_text)) - {"--help"}


def test_help_lists_the_surface(elsewhere):
    root = run_cli(["--help"], cwd=elsewhere)
    assert root.rc == 0, root.stderr
    m = re.search(r"\{([a-z,]+)\}", root.stdout)
    assert m and set(m.group(1).split(",")) == set(SURFACE)
    for command, expected in SURFACE.items():
        r = run_cli([command, "--help"], cwd=elsewhere)
        assert r.rc == 0, r.stderr
        assert _options(r.stdout) == expected, (command, r.stdout)


def test_commands_from_unrelated_cwd(project, elsewhere):
    t = ["--targets", project]
    r = run_cli(["list", "--tier", "live", *t], cwd=elsewhere)
    assert r.rc == 0 and "live:cases/live/alpha::alpha" in r.ids(), r.stderr
    r = run_cli(["run", "--case", "alpha", "--dry-run", *t], cwd=elsewhere)
    assert r.rc == 0 and r.ids() == ["live:cases/live/alpha::alpha"], r.stderr


@pytest.mark.parametrize("argv", [
    ["validate"], ["services", "list"], ["identity"],          # not ported
    ["list", "--no-such-flag"], ["run", "--tier"], ["lis"],
], ids=["validate", "services", "identity", "unknown-flag", "missing-value", "abbreviation"])
def test_unknown_commands_and_options_are_exit_2(elsewhere, argv):
    r = run_cli(argv, cwd=elsewhere)
    assert r.rc == 2 and "striim-test: error:" in r.stderr, (argv, r.stderr)
    assert r.run_dir is None
