"""A consumer's example:, jar: and ggtrail content resolves in the consumer at call time.

The console loads manifests in-process with GOLD_TARGETS set and SLT_PROJECT_ROOT unset.
The engine used to fix its project root at import (`_REPO = paths.project_root()`), which is
then the framework clone, so an `example:` case read its tql/ddl/seed from the wrong repo.
"""
import pytest

_EXAMPLE_TQL = "-- consumer example tql\nCREATE APPLICATION ${APP};\nEND APPLICATION ${APP};\n"


@pytest.fixture
def consumer(tmp_path, monkeypatch):
    from livetest import layout, project
    root = tmp_path / "consumer"
    ex = root / "examples" / "orders"
    ex.mkdir(parents=True)
    (ex / "app.tql").write_text(_EXAMPLE_TQL)
    (ex / "ddl.sql").write_text("CREATE TABLE ${TID}orders (id int);\n")
    (ex / "seed.sql").write_text("INSERT INTO ${TID}orders VALUES (1);\n")
    case = root / "tests" / "live" / "orders-from-example"
    case.mkdir(parents=True)
    (case / "test.yaml").write_text(
        "name: orders-from-example\npurpose: an example-backed case\nexample: examples/orders\n"
        "tql: app.tql\nddl: ddl.sql\nseed: seed.sql\nrequires: [postgres]\nassert:\n  smoke: true\n")
    (root / "gold-targets.yaml").write_text(
        "schemaVersion: 1\ntargets: []\nsuites:\n  live: tests/live\n")
    monkeypatch.delenv("SLT_PROJECT_ROOT", raising=False)
    monkeypatch.setenv("GOLD_TARGETS", str(root / "gold-targets.yaml"))
    project.load_and_activate()
    yield root
    monkeypatch.delenv("GOLD_TARGETS")
    layout._reset()
    project.load_and_activate()


def test_an_example_case_loads_its_files_from_the_consumer(consumer):
    from livetest import manifest
    man = manifest.load_manifest(consumer / "tests" / "live" / "orders-from-example" / "test.yaml")
    assert man.source_dir == (consumer / "examples" / "orders").resolve()
    assert (man.source_dir / "app.tql").read_text() == _EXAMPLE_TQL
    for name in ("ddl.sql", "seed.sql"):
        assert (man.source_dir / name).is_file()


def test_jar_modules_and_the_ggtrail_harness_resolve_in_the_consumer(consumer):
    from inttest import opartifacts as int_opartifacts
    from livetest import opartifacts
    from livetest.ggtrail import harness
    assert opartifacts._root() == consumer.resolve()
    assert int_opartifacts._root() == consumer.resolve()
    assert int_opartifacts._common_dir() == consumer.resolve() / "java" / "OpenProcessors" / "OpenProcessorCommon"
    assert harness._harness_dir() == consumer.resolve() / "tools" / "ggtrail-harness"
