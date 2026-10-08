"""test.yaml `example:` resolves under SLT_PROJECT_ROOT, end to end."""
from tests.test_path_sites import _run


def test_example_resolves_under_project_root(tmp_path):
    proj = tmp_path / "proj"
    (proj / "java" / "Foo").mkdir(parents=True)
    # tql is read relative to source_dir (manifest.py:927), i.e. under the example dir
    (proj / "java" / "Foo" / "app.tql").write_text("CREATE APPLICATION x;\nEND APPLICATION x;\n")
    case = proj / "cases" / "c1"
    case.mkdir(parents=True)
    (case / "test.yaml").write_text("name: c1\nexample: java/Foo\ntql: app.tql\n"
                                     "assert:\n  smoke: true\n")
    out = _run("importlib.import_module('livetest.manifest').load_manifest("
               f"__import__('pathlib').Path(r'{case / 'test.yaml'}')).source_dir",
               {"SLT_PROJECT_ROOT": str(proj)}, tmp_path)
    assert out == (proj / "java" / "Foo").resolve()
