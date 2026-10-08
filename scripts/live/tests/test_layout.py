"""Hermetic tests for livetest.layout, the services overlay and state-dir adapter over
livetest.paths (ported from the legacy framework repo's test_layout.py).

Covers: call-time resolution (set_roots AFTER importing livetest.plugin), the defaults
(paths.services_dir() last, paths.state_dir()), the services overlay (precedence, root
expansion, canonicalization, base-dir refusal), the single-valued state dir, set-but-missing
keys raising PathConfigError, planned_state_dir and in_install_tree.
"""
from __future__ import annotations

import os

import pytest

from livetest import layout, paths

NO_DOTENV = {}   # every call passes dotenv explicitly, so a real <project root>/.env never leaks in


@pytest.fixture(autouse=True)
def _clean_layout():
    layout._reset()
    yield
    layout._reset()


def _fake_service(sroot, name="wsvc"):
    """A fake services root: <sroot>/services/<name>/service.yaml."""
    svc = sroot / "services" / name
    svc.mkdir(parents=True)
    (svc / "service.yaml").write_text(f"name: {name}\nisolation: docker\n")
    return svc


# ---------------------------------------------------------------------------
# Call-time resolution: set_roots AFTER the plugin import takes effect.
# ---------------------------------------------------------------------------

def test_set_roots_after_plugin_import_takes_effect(tmp_path):
    import livetest.plugin  # noqa: F401  (imported FIRST)

    sroot = tmp_path / "consumer"
    svc = _fake_service(sroot, "hnsvc")
    state = tmp_path / "state"

    layout.set_roots(services=[sroot], state=state)

    got = layout.services_roots(env={}, dotenv=NO_DOTENV)
    assert got[0] == (sroot / "services").resolve()
    assert got[0] / "hnsvc" == svc.resolve()
    assert got[-1] == paths.services_dir({}, NO_DOTENV)
    assert layout.state_dir(env={}, dotenv=NO_DOTENV) == state.resolve()


# ---------------------------------------------------------------------------
# Defaults: nothing configured falls back to livetest.paths.
# ---------------------------------------------------------------------------

def test_defaults_are_the_paths_defaults():
    assert layout.services_roots(env={}, dotenv=NO_DOTENV) == (paths.services_dir({}, NO_DOTENV),)
    assert layout.state_dir(env={}, dotenv=NO_DOTENV) == paths.state_dir({}, NO_DOTENV)
    assert layout.builtin_services({}, NO_DOTENV) == paths.services_dir({}, NO_DOTENV)


def test_services_and_state_keys_are_honoured(tmp_path):
    svc, st = tmp_path / "svc", tmp_path / "st"
    svc.mkdir(); st.mkdir()
    env = {"SLT_SERVICES_DIR": str(svc), "SLT_STATE_DIR": str(st)}
    assert layout.services_roots(env=env, dotenv=NO_DOTENV) == (svc.resolve(),)
    assert layout.state_dir(env=env, dotenv=NO_DOTENV) == st.resolve()
    # .env is the second layer, exactly as in livetest.paths
    assert layout.state_dir(env={}, dotenv={"SLT_STATE_DIR": str(st)}) == st.resolve()


@pytest.mark.parametrize("key,call", [
    ("SLT_SERVICES_DIR", lambda env: layout.services_roots(env=env, dotenv=NO_DOTENV)),
    ("SLT_STATE_DIR", lambda env: layout.state_dir(env=env, dotenv=NO_DOTENV)),
])
def test_set_but_missing_key_raises_path_config_error(key, call, tmp_path):
    with pytest.raises(paths.PathConfigError, match=key):
        call({key: str(tmp_path / "nope")})


def test_slt_services_roots_is_no_longer_read(tmp_path):
    extra = tmp_path / "extra"
    extra.mkdir()
    got = layout.services_roots(env={"SLT_SERVICES_ROOTS": str(extra)}, dotenv=NO_DOTENV)
    assert got == (paths.services_dir({}, NO_DOTENV),)


# ---------------------------------------------------------------------------
# Services overlay: ordering, expansion, canonicalization, base-dir refusal.
# ---------------------------------------------------------------------------

def test_prepend_order_preserved_base_last(tmp_path):
    a, b, m = [tmp_path / n for n in ("a", "b", "m")]
    for p in (a, b, m):
        p.mkdir()
    layout.set_roots(services=[a, b])
    layout.set_manifest_roots(services=[m])
    got = layout.services_roots(env={}, dotenv=NO_DOTENV)
    assert got == (a.resolve(), b.resolve(), m.resolve(), paths.services_dir({}, NO_DOTENV))


def test_services_entry_expands_to_its_services_tree(tmp_path):
    plain, flat, nested = tmp_path / "plain", tmp_path / "flat", tmp_path / "nested"
    plain.mkdir()
    (flat / "services").mkdir(parents=True)
    (nested / "scripts" / "live" / "services").mkdir(parents=True)
    layout.set_roots(services=[plain, flat, nested])
    got = layout.services_roots(env={}, dotenv=NO_DOTENV)
    assert got[:3] == (plain.resolve(), (flat / "services").resolve(),
                       (nested / "scripts" / "live" / "services").resolve())


def test_configuring_the_base_dir_raises():
    # Naming the base services dir would hoist it out of its guaranteed-last slot.
    layout.set_roots(services=[paths.services_dir({}, NO_DOTENV)])
    with pytest.raises(layout.LayoutError, match="cannot be configured"):
        layout.services_roots(env={}, dotenv=NO_DOTENV)


def test_canonicalization_collapses_duplicates(tmp_path):
    base = tmp_path / "root"
    base.mkdir()
    link = tmp_path / "link"
    link.symlink_to(base)
    layout.set_roots(services=[base, str(base) + "/", link])
    got = layout.services_roots(env={}, dotenv=NO_DOTENV)
    assert got == (base.resolve(), paths.services_dir({}, NO_DOTENV))


# ---------------------------------------------------------------------------
# state_dir.
# ---------------------------------------------------------------------------

def test_state_dir_rejects_a_list(tmp_path):
    with pytest.raises(layout.LayoutError, match="SINGLE-VALUED"):
        layout.set_roots(state=[tmp_path / "a", tmp_path / "b"])
    with pytest.raises(layout.LayoutError, match="SINGLE-VALUED"):
        layout.set_manifest_roots(state=[tmp_path / "a", tmp_path / "b"])


def test_configured_state_dir_is_created(tmp_path):
    explicit, manifest = tmp_path / "explicit", tmp_path / "manifest"
    layout.set_manifest_roots(state=manifest)
    assert layout.state_dir(env={}, dotenv=NO_DOTENV) == manifest.resolve()
    assert manifest.is_dir()
    layout.set_roots(state=explicit)
    assert layout.state_dir(env={}, dotenv=NO_DOTENV) == explicit.resolve()
    assert explicit.is_dir()


def test_state_precedence_explicit_manifest_key(tmp_path):
    explicit, manifest, env_dir = tmp_path / "explicit", tmp_path / "manifest", tmp_path / "env"
    env_dir.mkdir()
    env = {"SLT_STATE_DIR": str(env_dir)}
    assert layout.state_dir(env=env, dotenv=NO_DOTENV) == env_dir.resolve()
    layout.set_manifest_roots(state=manifest)
    assert layout.state_dir(env=env, dotenv=NO_DOTENV) == manifest.resolve()
    layout.set_roots(state=explicit)
    assert layout.state_dir(env=env, dotenv=NO_DOTENV) == explicit.resolve()


# ---------------------------------------------------------------------------
# planned_state_dir and in_install_tree.
# ---------------------------------------------------------------------------

def test_planned_state_dir_precedence_and_no_create(tmp_path):
    explicit, manifest, env_dir = tmp_path / "explicit", tmp_path / "manifest", tmp_path / "env"
    env = {"SLT_STATE_DIR": str(env_dir)}
    assert layout.planned_state_dir(env={}, dotenv=NO_DOTENV) is None     # nothing configured
    assert layout.planned_state_dir(env=env, dotenv=NO_DOTENV) == env_dir.resolve()
    assert layout.planned_state_dir(env={}, dotenv=env) == env_dir.resolve()
    layout.set_manifest_roots(state=manifest)
    assert layout.planned_state_dir(env=env, dotenv=NO_DOTENV) == manifest.resolve()
    layout.set_roots(state=explicit)
    assert layout.planned_state_dir(env=env, dotenv=NO_DOTENV) == explicit.resolve()
    assert not explicit.exists() and not manifest.exists() and not env_dir.exists()


def test_in_install_tree(tmp_path):
    assert layout.in_install_tree(tmp_path / "venv" / "lib" / "python3.11" / "site-packages" / "x")
    assert layout.in_install_tree(tmp_path / "usr" / "lib" / "python3" / "dist-packages")
    assert not layout.in_install_tree(tmp_path / "checkout" / "scripts" / "live")


def test_no_checkout_marker_is_consulted():
    # The legacy checkout marker file and the wheel/checkout mode split
    # are gone: the framework clone and a test repo's checkout resolve the same way.
    assert not hasattr(layout, "checkout_repo_root")
    assert not hasattr(layout, "builtin_live")
    assert "gold-standard" not in open(layout.__file__).read()
