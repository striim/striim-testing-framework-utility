"""An unknown service.yaml key is refused when the definition loads.

`load_service` read the keys it knew and ignored the rest: `live_overide_env:` loaded with no
override, so a service meant to use an existing instance silently ran in container mode.
"""
from __future__ import annotations

import inspect
import re
from pathlib import Path

import pytest
import yaml

from livetest import registry
from livetest.registry import RegistryError, load_service

REPO = Path(__file__).resolve().parents[3]


def _service(tmp_path, **extra) -> Path:
    svc = tmp_path / "mine"
    svc.mkdir()
    (svc / "service.yaml").write_text(yaml.safe_dump({
        "name": "mine", "isolation": "none",
        "docker_defaults": {"host": "localhost", "port": 1}, "provides": {"MINE": "{host}"}, **extra}))
    return tmp_path


def test_a_misspelt_key_is_refused_naming_it_and_the_file(tmp_path):
    root = _service(tmp_path, live_overide_env="SLT_MINE_HOST")
    with pytest.raises(RegistryError) as e:
        load_service("mine", services_dir=root)
    msg = str(e.value)
    assert str(root / "mine" / "service.yaml") in msg
    assert "unknown key" in msg and "'live_overide_env'" in msg and "'live_override_env'" in msg


def test_every_unknown_key_is_named_at_once(tmp_path):
    root = _service(tmp_path, pre_up_timout=5, requierd_files=["x"])
    with pytest.raises(RegistryError, match=r"\['pre_up_timout', 'requierd_files'\]"):
        load_service("mine", services_dir=root)


def test_the_allowed_set_is_what_the_loader_reads():
    # Derived from the code: the keys load_service and its shared hook_policy read.
    src = inspect.getsource(registry.load_service) + inspect.getsource(registry.hook_policy) \
        + inspect.getsource(registry._live_defaults)
    read = set(re.findall(r"""raw(?:\.get\(|\[)['"]([a-z_]+)['"]""", src))
    assert read == registry.SERVICE_KEYS


def test_every_shipped_service_loads():
    names = registry.all_services()
    assert len(names) >= 10
    for name in names:
        assert load_service(name).name == name


def test_the_every_key_table_lists_exactly_the_keys_the_loader_accepts():
    doc = (REPO / "docs" / "YOUR-OWN-SERVICES.md").read_text()
    table = doc[doc.index("Every key a `service.yaml` can hold:"):doc.index("**A definition with neither")]
    assert set(re.findall(r"^\| `([a-z_]+)` \|", table, re.M)) == registry.SERVICE_KEYS


def test_a_key_the_table_documents_loads(tmp_path):
    root = _service(tmp_path, live_override_env="SLT_MINE_HOST", pre_up_check="always",
                    unavailable_policy="fail", pre_up_timeout=30, python_module_hint="pip install x",
                    required_files=["deps/a"], required_files_env={"MINE_DEPS": "deps"},
                    pre_up_env=[{"name": "MINE_INPUT", "type": "string"}])
    d = load_service("mine", services_dir=root)
    assert (d.live_override_env, d.pre_up_check, d.unavailable_policy, d.pre_up_timeout) == \
        ("SLT_MINE_HOST", "always", "fail", 30.0)
