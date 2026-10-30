import pytest

from livetest import uiform
from livetest.uiform import PLACEHOLDER, UiFormError, split, ui_object, ui_properties

TEMPLATE = {"propertyMap": [
    {"name": "Url", "type": "java.lang.String", "required": True},
    {"name": "Workers", "type": "java.lang.Integer", "required": True},
    {"name": "Flag", "type": "java.lang.Boolean", "required": False},
    {"name": "Repeat", "type": "java.lang.String", "required": False},
    {"name": "Size", "type": "java.lang.Integer", "required": False},
    {"name": "Note", "type": "java.lang.String", "required": False, "defaultValue": ""},
    {"name": "Bare", "type": "java.lang.String", "required": False},
    {"name": "Sep", "type": "java.lang.String", "required": False, "defaultValue": ","},
    {"name": "Query", "type": "java.lang.String", "required": False, "conditionalRequired": "(p)=>!p.Tables"},
]}


def test_ui_properties_as_measured_on_5_4_0_6c():
    stored = {"Url": "", "Workers": None, "Flag": None, "Repeat": 300, "Size": 50, "Note": None, "Query": None,
              "Password": {"encrypted": "x"}, "adapterName": "X"}
    assert ui_properties(stored, TEMPLATE) == {
        "Url": PLACEHOLDER, "Workers": 1, "Flag": False, "Repeat": "300", "Size": 50, "Query": "",
        "Password": {"encrypted": "x"}, "adapterName": "X"}
    # An empty optional field with an empty default is left out (TextCleanerOp ReplaceTo, 5.4.0.6C,
    # whose template says defaultValue ""); another default, or none, is not measured: sent as stored.
    assert ui_properties({"Note": "", "Sep": "", "Bare": "", "Query": ""}, TEMPLATE) == {
        "Sep": "", "Bare": "", "Query": ""}


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

    def template(self, oid):
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


def test_components_include_a_sub_flows_members(monkeypatch):
    store = {
        "n.APPLICATION.a": {"id": "n.APPLICATION.a", "objects": {"FLOW": ["n.FLOW.f"], "TARGET": ["n.TARGET.t"]}},
        "n.FLOW.f": {"id": "n.FLOW.f", "objects": {"SOURCE": ["n.SOURCE.s"], "STREAM": ["n.STREAM.x"]}},
        "n.TARGET.t": {"id": "n.TARGET.t", "adapter": {"handler": "h", "properties": {}}},
        "n.SOURCE.s": {"id": "n.SOURCE.s", "adapter": {"handler": "h", "properties": {}}},
        "n.STREAM.x": {"id": "n.STREAM.x"},
    }
    rmi = object.__new__(uiform.Rmi)
    monkeypatch.setattr(uiform.Rmi, "read", lambda self, oid, kind: store[oid])
    assert sorted(o["id"] for o in uiform.components(rmi, "n", "a")) == ["n.SOURCE.s", "n.TARGET.t"]


def test_a_password_change_is_reported_without_its_value():
    template = {"propertyMap": [{"name": "Password", "type": "com.webaction.security.Password", "required": True}]}
    obj = {"name": "t", "type": "TARGET", "adapter": {"handler": "h", "properties": {"Password": ""}}}
    _, changes = ui_object(FakeRmi(), obj, {"h": template})
    assert changes == ["t.Password: *** -> ***"]


def test_a_failed_save_is_a_ui_form_error_not_an_application_halt(monkeypatch):
    monkeypatch.setenv("SLT_UI_FORM", "replay")
    monkeypatch.setattr(uiform, "replay", lambda *a: (_ for _ in ()).throw(TimeoutError("socket")))
    client = FakeClient()
    client.api = type("Api", (), {"url_base": "http://h:9080", "auth_token": "t"})()
    with pytest.raises(UiFormError, match="the save failed: TimeoutError"):
        uiform.deploy(client, "USE n;\nCREATE APPLICATION a;\nDEPLOY APPLICATION a;\n")
    assert client.deployed == ["USE n;\nCREATE APPLICATION a;\n"]


def test_the_socket_is_closed_when_the_context_cannot_be_made(monkeypatch):
    import sys
    import types
    closed = []
    ws = types.SimpleNamespace(close=lambda: closed.append(True))
    monkeypatch.setitem(sys.modules, "websocket", types.SimpleNamespace(create_connection=lambda *a, **k: ws))
    monkeypatch.setattr(uiform.Rmi, "call", lambda self, *a: (_ for _ in ()).throw(UiFormError("refused")))
    with pytest.raises(UiFormError):
        uiform.Rmi("http://h:9080", "t")
    assert closed == [True]


def _rmi(answers, templates=()):
    """An Rmi whose READs answer from `answers` (id -> object) and whose template load returns
    `templates`, the way Striim does."""
    from livetest.uiform import Rmi
    rmi = Rmi.__new__(Rmi)
    rmi.token, rmi.reads, rmi.loads = "t", [], 0

    def call(cls, method, token, *params):
        if method == "getEntitiesByTypes":
            rmi.loads += 1
            return templates if isinstance(templates, (dict, type(None))) else {"PROPERTYTEMPLATE": list(templates)}
        query = params[2]
        rmi.reads.append(query["id"])
        return [answers.get(query["id"], {"noField": None})]
    rmi.call = call
    return rmi


def test_a_versioned_template_comes_from_the_one_template_load():
    kafka = {"id": "Global.PROPERTYTEMPLATE.KafkaWriter_2.1.0", "name": "KafkaWriter_2.1.0"}
    plain = {"id": "Global.PROPERTYTEMPLATE.KafkaWriter"}
    rmi = _rmi({}, [plain, kafka])
    assert rmi.template("Global.PROPERTYTEMPLATE.KafkaWriter_2.1.0") is kafka
    assert rmi.template("Global.PROPERTYTEMPLATE.KafkaWriter") is plain
    assert rmi.loads == 1 and rmi.reads == []


def test_an_unknown_template_names_the_id():
    rmi = _rmi({}, [{"id": "Global.PROPERTYTEMPLATE.KafkaWriter"}])
    with pytest.raises(UiFormError, match="no PROPERTYTEMPLATE Global.PROPERTYTEMPLATE.KafkaWriter_9.9.9$"):
        rmi.template("Global.PROPERTYTEMPLATE.KafkaWriter_9.9.9")


def test_a_direct_read_answered_with_another_id_is_refused():
    rmi = _rmi({"Global.TARGET.t": {"id": "Global.TARGET.other"}})
    with pytest.raises(UiFormError, match="no TARGET Global.TARGET.t$"):
        rmi.read("Global.TARGET.t", "TARGET")


@pytest.mark.parametrize("reply", [None, {}, {"PROPERTYTEMPLATE": None}])
def test_a_template_load_without_a_list_says_so(reply):
    rmi = _rmi({}, reply)
    with pytest.raises(UiFormError, match="getEntitiesByTypes returned no PROPERTYTEMPLATE list"):
        rmi.template("Global.PROPERTYTEMPLATE.X")
