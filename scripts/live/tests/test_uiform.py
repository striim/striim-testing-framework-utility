import pytest

from livetest import uiform
from livetest.uiform import PLACEHOLDER, UiFormError, split, ui_object, ui_properties

TEMPLATE = {"propertyMap": [
    {"name": "Url", "type": "java.lang.String", "required": True},
    {"name": "Workers", "type": "java.lang.Integer", "required": True},
    {"name": "Flag", "type": "java.lang.Boolean", "required": False},
    {"name": "Repeat", "type": "java.lang.String", "required": False},
    {"name": "Size", "type": "java.lang.Integer", "required": False},
    {"name": "Note", "type": "java.lang.String", "required": False},
    {"name": "Query", "type": "java.lang.String", "required": False, "conditionalRequired": "(p)=>!p.Tables"},
]}


def test_ui_properties_as_measured_on_5_4_0_6c():
    stored = {"Url": "", "Workers": None, "Flag": None, "Repeat": 300, "Size": 50, "Note": None, "Query": None,
              "Password": {"encrypted": "x"}, "adapterName": "X"}
    assert ui_properties(stored, TEMPLATE) == {
        "Url": PLACEHOLDER, "Workers": 1, "Flag": False, "Repeat": "300", "Size": 50, "Query": "",
        "Password": {"encrypted": "x"}, "adapterName": "X"}


def test_split_at_first_deploy_and_names_the_apps_created_before_it():
    tql = ("CREATE NAMESPACE n; USE n;\nCREATE OR REPLACE APPLICATION a;\nEND APPLICATION a;\n"
           "USE m;\nCREATE APPLICATION b;\nEND APPLICATION b;\n"
           "DEPLOY APPLICATION a;\nSTART APPLICATION a;\nCREATE APPLICATION c;\n")
    head, tail, apps = split(tql)
    assert tail.startswith("DEPLOY APPLICATION a;") and "CREATE APPLICATION c" in tail
    assert apps == [("n", "a"), ("m", "b")]
    assert split("USE n;\nCREATE APPLICATION a;\n") == ("USE n;\nCREATE APPLICATION a;\n", "", [("n", "a")])
    assert split("USE n;\nCREATE OR REPLACE APPLICATION q.a;\nDEPLOY APPLICATION q.a;")[2] == [("q", "a")]


class FakeRmi:
    def __init__(self):
        self.reads = []

    def read(self, oid, kind):
        self.reads.append(oid)
        return TEMPLATE


def test_ui_object_open_processor_fields_and_changes():
    obj = {"name": "op", "type": "OPENPROCESSOR", "from": ["n.STREAM.s"],
           "adapter": {"handler": "Global.PROPERTYTEMPLATE.X", "properties": {"Flag": None, "Note": None, "Size": 5}}}
    rmi, templates = FakeRmi(), {}
    sent, changes = ui_object(rmi, obj, templates)
    assert sent["from"] == "n.STREAM.s" and sent["enrichment"] == [] and sent["version"] == 0
    assert sent["adapter"]["properties"] == {"Flag": False, "Size": 5}
    assert changes == ["op.Flag: None -> False"]
    ui_object(rmi, obj, templates)
    assert rmi.reads == ["Global.PROPERTYTEMPLATE.X"]   # one template read per handler


class FakeClient:
    def __init__(self):
        self.deployed = []

    def deploy_tql(self, tql):
        self.deployed.append(tql)


def test_unset_deploys_as_before(monkeypatch):
    monkeypatch.delenv("SLT_UI_FORM", raising=False)
    client = FakeClient()
    assert uiform.deploy(client, "USE n; CREATE APPLICATION a; DEPLOY APPLICATION a;") == []
    assert client.deployed == ["USE n; CREATE APPLICATION a; DEPLOY APPLICATION a;"]


def test_replay_runs_between_create_and_deploy(monkeypatch):
    monkeypatch.setenv("SLT_UI_FORM", "replay")
    calls = []
    monkeypatch.setattr(uiform, "replay", lambda url, token, apps: calls.append(apps) or ["c: saved unchanged"])
    client = FakeClient()
    client.api = type("Api", (), {"url_base": "http://h:9080", "auth_token": "t"})()
    lines = uiform.deploy(client, "USE n;\nCREATE APPLICATION a;\nDEPLOY APPLICATION a;\n")
    assert client.deployed == ["USE n;\nCREATE APPLICATION a;\n", "DEPLOY APPLICATION a;\n"]
    assert calls == [[("n", "a")]] and lines == ["[slt] ui-form replay: c: saved unchanged"]


def test_unknown_mode_is_refused(monkeypatch):
    monkeypatch.setenv("SLT_UI_FORM", "dense")
    with pytest.raises(UiFormError):
        uiform.deploy(FakeClient(), "")


def test_an_unknown_id_is_refused_not_read_as_empty(monkeypatch):
    rmi = object.__new__(uiform.Rmi)
    rmi.token = "t"
    monkeypatch.setattr(uiform.Rmi, "call", lambda self, *a: [{"noField": None}])
    with pytest.raises(UiFormError, match="no APPLICATION n.APPLICATION.a"):
        rmi.read("n.APPLICATION.a", "APPLICATION")


def test_an_app_with_nothing_to_save_fails(monkeypatch):
    rmi = object.__new__(uiform.Rmi)
    monkeypatch.setattr(uiform.Rmi, "read", lambda self, oid, kind: {"id": oid, "objects": {"STREAM": ["n.STREAM.s"]}})
    with pytest.raises(UiFormError, match="no component to save"):
        uiform.components(rmi, "n", "a")


def test_ui_form_with_no_app_before_deploy_fails(monkeypatch):
    monkeypatch.setenv("SLT_UI_FORM", "replay")
    client = FakeClient()
    with pytest.raises(UiFormError, match="no CREATE APPLICATION"):
        uiform.deploy(client, "DEPLOY APPLICATION n.a;")
    assert client.deployed == []
