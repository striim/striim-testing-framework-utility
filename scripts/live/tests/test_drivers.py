"""Service drivers (livetest.drivers): a consumer service's own Python, named in service.yaml."""
import sys
import types
import uuid

import pytest

from livetest import drivers, layout, registry
from livetest.registry import RegistryError, load_service


@pytest.fixture
def consumer(tmp_path):
    """A consumer services root with an `extdb` service whose driver has a unique module name."""
    module = f"extdb_driver_{uuid.uuid4().hex[:8]}"
    svc = tmp_path / "services" / "extdb"
    svc.mkdir(parents=True)
    (svc / "service.yaml").write_text(f"name: extdb\nisolation: none\ncompose: compose.yaml\ndriver: {module}\n")
    (svc / f"{module}_helper.py").write_text("VALUE = 'from the helper'\n")
    (svc / f"{module}.py").write_text(
        f"from {module}_helper import VALUE\n"
        "ENV_PATH_KEYS = ('SLT_EXTDB_HOME',)\n"
        "def unsupported_mode(mode):\n"
        "    return None if mode == 'docker' else 'docker only'\n")
    layout._reset()
    registry._REPORTED.clear()
    layout.set_roots(services=[tmp_path / "services"])
    yield types.SimpleNamespace(dir=svc, module=module)
    layout._reset()
    registry._REPORTED.clear()
    sys.modules.pop(module, None)
    sys.modules.pop(f"{module}_helper", None)


def test_service_yaml_names_the_driver(consumer):
    assert load_service("extdb").driver == consumer.module


def test_a_shipped_service_has_no_driver():
    defn = load_service("postgres")
    assert defn.driver is None
    assert drivers.load(defn) is None and drivers.hook(defn, "unavailable") is None


@pytest.mark.parametrize("raw", ["''", "my-driver", "1st", "[a]"])
def test_a_driver_must_be_a_module_name(tmp_path, raw):
    svc = tmp_path / "bad"
    svc.mkdir()
    (svc / "service.yaml").write_text(f"name: bad\nisolation: none\ndriver: {raw}\n")
    with pytest.raises(RegistryError, match="driver"):
        load_service("bad", services_dir=tmp_path)


def test_the_driver_loads_once_from_its_service_dir_with_its_siblings(consumer):
    path_before = list(sys.path)
    defn = load_service("extdb")
    module = drivers.load(defn)
    assert module.VALUE == "from the helper"
    assert drivers.load(defn) is module is sys.modules[consumer.module]
    assert sys.path == path_before                     # the service dir was on it for the import only
    assert drivers.hook(defn, "unsupported_mode")("native") == "docker only"
    assert drivers.hook(defn, "provision") is None


def test_a_name_already_loaded_from_elsewhere_is_refused(consumer, tmp_path):
    other = tmp_path / "elsewhere"
    other.mkdir()
    (other / f"{consumer.module}.py").write_text("")
    sys.path.insert(0, str(other))
    try:
        __import__(consumer.module)
    finally:
        sys.path.remove(str(other))
    with pytest.raises(drivers.DriverError, match="distinct module name"):
        drivers.load(load_service("extdb"))


def test_doctor_checks_the_path_settings_a_driver_declares(consumer, tmp_path):
    from striim_test import doctor
    assert "SLT_EXTDB_HOME" in doctor.driver_path_keys()
    checks, _ = doctor.check_env({"SLT_EXTDB_HOME": str(tmp_path / "missing")}, tmp_path)
    assert any("SLT_EXTDB_HOME" in c.message and "does not exist" in c.message for c in checks), checks


def test_a_consumer_registered_marker_names_its_service():
    from livetest.plugin import _registered_markers
    config = types.SimpleNamespace(getini=lambda name: ["live: x", "extdb: a consumer service", "depth_smoke"])
    assert _registered_markers(config) == {"live", "extdb", "depth_smoke"}
    assert _registered_markers(object()) == set()


def _driver_service(root, name, module, body):
    svc = root / name
    svc.mkdir(parents=True)
    (svc / "service.yaml").write_text(f"name: {name}\nisolation: none\ncompose: compose.yaml\ndriver: {module}\n")
    (svc / f"{module}.py").write_text(body)
    layout._reset()
    layout.set_roots(services=[root])
    return svc


@pytest.fixture
def clean_layout():
    yield
    layout._reset()
    registry._REPORTED.clear()


def test_doctor_knows_the_settings_a_driver_declares(tmp_path, clean_layout):
    from striim_test import doctor
    module = f"declared_driver_{uuid.uuid4().hex[:8]}"
    _driver_service(tmp_path / "services", "declared", module,
                    "ENV_PATH_KEYS = ('SLT_DECLARED_HOME',)\nENV_KEYS = ('SLT_DECLARED_LOCK_TIMEOUT',)\n")
    try:
        env = {"SLT_DECLARED_HOME": str(tmp_path), "SLT_DECLARED_LOCK_TIMEOUT": "30"}
        checks, _ = doctor.check_env(env, tmp_path)
        assert not [c.message for c in checks if c.status == doctor.FAIL], checks
    finally:
        sys.modules.pop(module, None)


def test_doctor_reports_a_driver_that_fails_to_load(tmp_path, clean_layout):
    from striim_test import doctor
    module = f"broken_driver_{uuid.uuid4().hex[:8]}"
    _driver_service(tmp_path / "services", "broken", module, "raise RuntimeError('driver init failed')\n")
    checks, _ = doctor.check_env({}, tmp_path)
    failed = [c.message for c in checks if c.status == doctor.FAIL]
    assert any("broken" in m and module in m and "driver init failed" in m for m in failed), checks


def test_a_driver_name_without_a_module_in_the_service_dir_is_refused(tmp_path, clean_layout):
    _driver_service(tmp_path / "services", "builtin", "sys", "")
    (tmp_path / "services" / "builtin" / "sys.py").unlink()
    with pytest.raises(drivers.DriverError, match="builtin"):
        drivers.load(load_service("builtin"))
