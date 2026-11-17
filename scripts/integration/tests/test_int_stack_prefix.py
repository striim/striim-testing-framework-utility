"""INT_STACK_PREFIX is validated as SLT_STACK_PREFIX is.

The prefix names compose projects, container names and the state files in the state directory. It was
used as given, so `../x` reached a filename (`.../x-int-session.lock`) and a trailing newline reached
compose. Both now raise, naming the setting, before anything is created.
"""
from __future__ import annotations

import os
import sys

import pytest

from inttest import cli, services

COMPOSE = """\
services:
  pg:
    container_name: ${INT_STACK_PREFIX:+${INT_STACK_PREFIX}-}int-postgres
"""


@pytest.mark.parametrize("bad", ["alt\n", "../x", "a/b", "Alt", "-alt", "alt_x", "alt stack", " alt"])
def test_an_invalid_int_stack_prefix_is_refused_by_state_name(bad):
    with pytest.raises(services.StackPrefixError, match="INT_STACK_PREFIX"):
        services.state_name(".int-session.lock", {"INT_STACK_PREFIX": bad})


@pytest.mark.parametrize("bad", ["alt\n", "../x"])
def test_an_invalid_int_stack_prefix_is_refused_by_declared_containers(tmp_path, bad):
    compose = tmp_path / "compose.yaml"
    compose.write_text(COMPOSE)
    with pytest.raises(services.StackPrefixError, match="INT_STACK_PREFIX"):
        services.declared_containers([compose], env={"INT_STACK_PREFIX": bad})


@pytest.mark.parametrize("good, name", [("alt", ".alt-int-session.lock"), ("x1-b2", ".x1-b2-int-session.lock"),
                                        ("", ".int-session.lock"), ("  ", ".int-session.lock")])
def test_a_valid_or_blank_int_stack_prefix_scopes_as_before(good, name):
    assert services.state_name(".int-session.lock", {"INT_STACK_PREFIX": good}) == name


def test_declared_containers_keeps_a_valid_prefix(tmp_path):
    compose = tmp_path / "compose.yaml"
    compose.write_text(COMPOSE)
    assert services.declared_containers([compose], env={"INT_STACK_PREFIX": "alt"}) == ["alt-int-postgres"]


# --- the setting as the CLI and a session resolve it ------------------------------------------

@pytest.fixture
def _restore_environ():
    """`inttest.cli.main` puts the effective settings into this process's environment, as a session's
    configure does; monkeypatch does not see keys added that way."""
    saved = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(saved)


def _main(monkeypatch, *argv):
    monkeypatch.setattr(sys, "argv", ["inttest.cli", *argv])
    return cli.main()


def test_the_cli_compose_lock_is_the_sessions_when_the_prefix_is_set_only_in_dotenv(tmp_path, monkeypatch,
                                                                                  _restore_environ):
    """A session merges .env into its environment at configure, so its compose lock and its containers both
    carry .env's INT_STACK_PREFIX. `start` from a shell without it must take that same lock, or the two run
    `compose up` on one project at once."""
    from inttest import paths
    # The hermetic conftest blanks the .env layer; this test needs a real one, in tmp_path.
    monkeypatch.setattr(paths, "dotenv_values", lambda env=None: paths.read_dotenv(paths.dotenv_path(env)))
    root = tmp_path / "project"
    root.mkdir()
    (root / ".env").write_text("INT_STACK_PREFIX=ec\n")
    monkeypatch.setenv("SLT_PROJECT_ROOT", str(root))
    monkeypatch.delenv("INT_STACK_PREFIX", raising=False)
    seen = {}
    monkeypatch.setattr(cli, "cmd_up", lambda targets: seen.update(lock=cli._lock_file()) or 0)
    assert _main(monkeypatch, "start", "postgres") == 0
    compose = tmp_path / "compose.yaml"
    compose.write_text(COMPOSE)
    assert services.declared_containers([compose]) == ["ec-int-postgres"]     # what compose up names
    assert seen["lock"].name == ".ec-int-compose.lock"
