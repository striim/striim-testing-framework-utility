"""A malformed sibling test.yaml must not sink the pre-flight, and must not vanish silently either.

A resource survey found 248 of 599 live tests never provisioned: their manifests were skipped by the
pre-flight's name scan without a word, so their OP/UDF jars and services were missing from the unions
and CREATE TARGET failed later ("pt is null"). A skip is still right -- one bad file must not stop
every other test -- but it names the file and the error.
"""
from livetest import preflight


def _cases(tmp_path):
    good = tmp_path / "op" / "fooop" / "fooop-good"
    good.mkdir(parents=True)
    (good / "app.tql").write_text("CREATE APPLICATION ${APP};\nEND APPLICATION ${APP};\n")
    (good / "test.yaml").write_text(
        "name: fooop-good\npurpose: a loadable case\ntql: app.tql\nrequires: [postgres]\n"
        "assert:\n  smoke: true\n")
    bad = tmp_path / "op" / "fooop" / "fooop-bad"
    bad.mkdir(parents=True)
    (bad / "test.yaml").write_text("name: fooop-bad\npurpose: [unclosed\n")
    return bad / "test.yaml"


def test_manifests_for_loads_the_rest_and_names_the_skipped_file(tmp_path, monkeypatch):
    bad = _cases(tmp_path)
    monkeypatch.setattr(preflight, "_CASES", tmp_path)
    logs = []
    monkeypatch.setattr(preflight, "_log", logs.append)
    found = preflight.manifests_for(["fooop-good"])
    assert [m.name for m in found] == ["fooop-good"]
    assert any(m.startswith("WARNING: skipped unloadable manifest") and str(bad) in m for m in logs), logs


def test_known_test_ids_names_the_skipped_file(tmp_path, monkeypatch):
    bad = _cases(tmp_path)
    monkeypatch.setattr(preflight, "_CASES", tmp_path)
    logs = []
    monkeypatch.setattr(preflight, "_log", logs.append)
    assert preflight.known_test_ids() == {"fooop-good"}
    assert any(m.startswith("WARNING: skipped unloadable manifest") and str(bad) in m for m in logs), logs


def test_known_test_ids_does_not_report_a_set_only_match(tmp_path, monkeypatch):
    # The name scan loads every manifest on disk; the warning belongs to the cases a run selects.
    import warnings

    from livetest.manifest import CardinalityWarning
    case = tmp_path / "op" / "fooop" / "fooop-setonly"
    case.mkdir(parents=True)
    (case / "app.tql").write_text("CREATE APPLICATION ${APP};\nEND APPLICATION ${APP};\n")
    (case / "test.yaml").write_text(
        "name: fooop-setonly\ntql: app.tql\nassert:\n  data:\n    - {target: s.t, match: rows.csv}\n")
    monkeypatch.setattr(preflight, "_CASES", tmp_path)
    with warnings.catch_warnings(record=True) as seen:
        warnings.simplefilter("always")
        assert preflight.known_test_ids() == {"fooop-setonly"}
    assert not [w for w in seen if issubclass(w.category, CardinalityWarning)]
