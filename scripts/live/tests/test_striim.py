import pytest
from livetest.striim import StriimClient, StriimError, striim_api

class FakeApi:
    def __init__(self):
        self.posted = None
        self.status_seq = []
        self.stopped = False
        self.undeployed = False
        self.posted_lines = []
        self.stopped_apps = []
        self.undeployed_apps = []
        self.list_applications_response = None
    def post_tungsten_file(self, path):
        self.posted = open(path).read()
    def post_tungsten_line(self, line, timeout=None):
        self.posted_lines.append(line)
        if line.strip().rstrip(";") == "LIST APPLICATIONS":
            return self.list_applications_response
        return []
    def status_application(self, app, timeout=None):
        return self.status_seq.pop(0)
    def stop_application(self, app):
        self.stopped = True
        self.stopped_apps.append(app)
    def undeploy_application(self, app):
        self.undeployed = True
        self.undeployed_apps.append(app)

def _client(api):
    c = StriimClient.__new__(StriimClient)
    c.api = api
    return c

def test_deploy_posts_tql(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    api = FakeApi()
    _client(api).deploy_tql("CREATE APPLICATION X;")
    assert api.posted == "CREATE APPLICATION X;"

def test_deploy_tql_raises_on_failed_statement(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    class Api:
        def post_tungsten_file(self, path):
            return [
                {"command": "CREATE APPLICATION X;", "executionStatus": "Success"},
                {"command": "CREATE SOURCE bad;", "executionStatus": "Failure",
                 "failureMessage": "boom"},
            ]
    c = StriimClient.__new__(StriimClient); c.api = Api()
    with pytest.raises(StriimError, match="CREATE SOURCE bad"):
        c.deploy_tql("CREATE APPLICATION X;\nCREATE SOURCE bad;")

def test_deploy_tql_succeeds_when_all_statements_ok(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    class Api:
        def post_tungsten_file(self, path):
            return [{"command": "CREATE APPLICATION X;", "executionStatus": "Success"}]
    c = StriimClient.__new__(StriimClient); c.api = Api()
    c.deploy_tql("CREATE APPLICATION X;")  # must not raise

def test_await_running_succeeds_after_polls():
    api = FakeApi()
    api.status_seq = ["DEPLOYED", "STARTING", "RUNNING"]
    _client(api).await_running("NS.App", timeout=30, poll=0)  # poll=0: no real sleep

def test_await_running_raises_on_crash():
    api = FakeApi()
    api.status_seq = ["DEPLOYED", "CRASH"]
    with pytest.raises(StriimError, match="CRASH"):
        _client(api).await_running("NS.App", timeout=30, poll=0)

def test_await_running_raises_on_deploy_failed():
    # DEPLOY_FAILED must be treated as terminal — fail fast instead of polling out the timeout
    api = FakeApi()
    api.status_seq = ["DEPLOYED", "DEPLOY_FAILED"]
    with pytest.raises(StriimError, match="DEPLOY_FAILED"):
        _client(api).await_running("NS.App", timeout=30, poll=0)

def test_teardown_never_raises():
    api = FakeApi()
    def boom(app):
        raise RuntimeError("network down")
    api.stop_application = boom
    _client(api).teardown("NS.App")  # must not raise
    assert api.undeployed is True     # still attempts undeploy

def test_teardown_drops_app_and_namespace():
    api = FakeApi()
    _client(api).teardown("SLT_x.xApp", namespace="SLT_x")
    assert api.stopped is True and api.undeployed is True
    assert any("DROP APPLICATION SLT_x.xApp FORCE" in l for l in api.posted_lines)
    assert any("DROP NAMESPACE SLT_x CASCADE FORCE" in l for l in api.posted_lines)

def test_teardown_still_drops_when_stop_and_undeploy_raise():
    api = FakeApi()
    def boom(app):
        raise RuntimeError("network down")
    api.stop_application = boom
    api.undeploy_application = boom
    _client(api).teardown("SLT_x.xApp", namespace="SLT_x")  # must not raise
    # a failed stop/undeploy must NOT prevent the DROP cleanup
    assert any("DROP APPLICATION" in l for l in api.posted_lines)
    assert any("DROP NAMESPACE" in l for l in api.posted_lines)

# ---- teardown_namespace (multi-app namespace teardown) ----------------------
#
# Response shape verified live (STRIIM_PASS=striim, http://localhost:9080, admin):
#   [{"command": "LIST APPLICATIONS", "executionStatus": "Success",
#     "output": [{"application1": {"name": "SLT_hello_cluster.hello_clusterApp"}}],
#     "responseCode": 200}]
# i.e. a one-element list whose "output" is a list of {"applicationN": {"name": ...}}
# wrapper dicts. "LIST APPLICATIONS IN <ns>" is NOT valid syntax (confirmed live —
# it 400s), so every app must be listed and filtered client-side by name prefix.

def _list_apps_response(*names):
    return [{
        "command": "LIST APPLICATIONS", "executionStatus": "Success", "responseCode": 200,
        "output": [{f"application{i+1}": {"name": n}} for i, n in enumerate(names)],
    }]

def test_teardown_namespace_stops_and_undeploys_every_app_then_drops_namespace():
    api = FakeApi()
    api.list_applications_response = _list_apps_response(
        "SLT_x.x_producerApp", "SLT_x.x_readerApp", "SLT_y.otherApp")
    _client(api).teardown_namespace("SLT_x")
    assert set(api.stopped_apps) == {"SLT_x.x_producerApp", "SLT_x.x_readerApp"}
    assert set(api.undeployed_apps) == {"SLT_x.x_producerApp", "SLT_x.x_readerApp"}
    assert any(l.strip() == "LIST APPLICATIONS;" for l in api.posted_lines)
    assert any("DROP NAMESPACE SLT_x CASCADE FORCE" in l for l in api.posted_lines)
    # the other namespace's app must be left alone
    assert "SLT_y.otherApp" not in api.stopped_apps

def test_teardown_namespace_matches_case_insensitively():
    # Striim upper-cases namespaces; compare loosely rather than assume a case.
    api = FakeApi()
    api.list_applications_response = _list_apps_response("slt_x.someapp")
    _client(api).teardown_namespace("SLT_x")
    assert api.stopped_apps == ["slt_x.someapp"]
    assert api.undeployed_apps == ["slt_x.someapp"]

def test_teardown_namespace_with_no_apps_still_drops_namespace():
    api = FakeApi()
    api.list_applications_response = _list_apps_response()   # empty namespace/output
    _client(api).teardown_namespace("SLT_x")
    assert api.stopped_apps == [] and api.undeployed_apps == []
    assert any("DROP NAMESPACE SLT_x CASCADE FORCE" in l for l in api.posted_lines)

def test_teardown_namespace_never_raises_when_list_applications_fails():
    class Api(FakeApi):
        def post_tungsten_line(self, line, timeout=None):
            if line.strip().rstrip(";") == "LIST APPLICATIONS":
                raise RuntimeError("network down")
            return super().post_tungsten_line(line)
    api = Api()
    _client(api).teardown_namespace("SLT_x")   # must not raise
    assert any("DROP NAMESPACE SLT_x CASCADE FORCE" in l for l in api.posted_lines)

def test_teardown_namespace_one_apps_failure_does_not_block_the_others_or_the_drop():
    api = FakeApi()
    api.list_applications_response = _list_apps_response("SLT_x.aApp", "SLT_x.bApp")
    def flaky_stop(app):
        if app == "SLT_x.aApp":
            raise RuntimeError("HALT app returns non-2xx on stop")
        api.stopped_apps.append(app)
    api.stop_application = flaky_stop
    _client(api).teardown_namespace("SLT_x")   # must not raise
    assert api.stopped_apps == ["SLT_x.bApp"]              # a's failure didn't block b
    assert set(api.undeployed_apps) == {"SLT_x.aApp", "SLT_x.bApp"}  # undeploy still attempted for both
    assert any("DROP NAMESPACE SLT_x CASCADE FORCE" in l for l in api.posted_lines)

def test_apps_in_namespace_tolerates_shape_drift():
    # Defensive parsing beyond the observed live shape: a bare string item, and an
    # item exposing "name" directly (not nested under an "applicationN" wrapper).
    resp = [{"executionStatus": "Success", "output": [
        "SLT_x.bareStringApp",
        {"name": "SLT_x.directNameApp"},
        {"application3": {"name": "SLT_y.otherNamespaceApp"}},
    ]}]
    names = StriimClient._apps_in_namespace(resp, "SLT_x")
    assert set(names) == {"SLT_x.bareStringApp", "SLT_x.directNameApp"}

def test_apps_in_namespace_returns_empty_on_non_list_response():
    assert StriimClient._apps_in_namespace(None, "SLT_x") == []
    assert StriimClient._apps_in_namespace({"not": "a list"}, "SLT_x") == []


def test_current_status_wraps_unexpected_error_as_striim_error():
    api = FakeApi()
    def boom(app, timeout=None):
        raise KeyError("status")
    api.status_application = boom
    with pytest.raises(StriimError, match="status"):
        _client(api).current_status("NS.App")

def test_load_open_processor_posts_load_line_and_raises_on_failure():
    calls = []
    class _Api:
        def post_tungsten_line(self, line, timeout=None):
            calls.append(line)
            if "BOOM" in line:
                return [{"executionStatus": "Failure", "failureMessage": "nope"}]
            return [{"executionStatus": "Success"}]
    c = StriimClient.__new__(StriimClient); c.api = _Api()
    c.load_open_processor("FooOp-5.4.jar")
    assert calls == ['LOAD OPEN PROCESSOR "UploadedFiles/FooOp-5.4.jar";']
    with pytest.raises(StriimError):
        c.load_open_processor("BOOM-5.4.jar")


def test_list_deployment_groups_sends_semicolon():
    sent = {}
    class Api:
        def post_tungsten_line(self, line, timeout=None):
            sent["line"] = line
            return [{"command": line, "executionStatus": "Success", "output": []}]
    c = StriimClient.__new__(StriimClient); c.api = Api()
    out = c.list_deployment_groups()
    assert sent["line"].strip().endswith(";")
    assert isinstance(out, list)

def test_list_deployment_groups_raises_on_failure():
    class Api:
        def post_tungsten_line(self, line, timeout=None):
            return [{"command": line, "executionStatus": "Failure", "failureMessage": "boom"}]
    c = StriimClient.__new__(StriimClient); c.api = Api()
    with pytest.raises(StriimError):
        c.list_deployment_groups()


# --- checkpoint history: proof a checkpoint was actually recorded ------------------------
# APICheckpointHistoryCommandExecutor answers a JSON array when the app has recorded at least
# one checkpoint, or a CommandResponse(404, "... not available yet") when it has recorded
# none -- two different platform shapes for the same "nothing yet" fact. checkpoint_history()
# normalizes both to [] so a caller only ever compares "empty" vs "nonempty".

def test_checkpoint_history_returns_the_rows_when_the_platform_has_recorded_some():
    sent = {}
    rows = [{"serialNo": 1, "applicationName": "NS.App", "checkpointType": "PERIODIC"}]
    class Api:
        def post_tungsten_line(self, line, timeout=None):
            sent["line"] = line
            return [{"command": line, "executionStatus": "Success", "output": rows}]
    c = StriimClient.__new__(StriimClient); c.api = Api()
    assert c.checkpoint_history("NS.App") == rows
    assert sent["line"].strip() == "SHOW NS.App CHECKPOINT HISTORY;"

def test_checkpoint_history_normalizes_the_not_available_yet_failure_to_an_empty_list():
    class Api:
        def post_tungsten_line(self, line, timeout=None):
            return [{"command": line, "executionStatus": "Failure",
                     "failureMessage": "Checkpoint History for application NS.App is not "
                                        "available yet."}]
    c = StriimClient.__new__(StriimClient); c.api = Api()
    assert c.checkpoint_history("NS.App") == []

def test_checkpoint_history_raises_on_a_transport_failure():
    class Api:
        def post_tungsten_line(self, line, timeout=None):
            raise RuntimeError("boom")
    c = StriimClient.__new__(StriimClient); c.api = Api()
    with pytest.raises(StriimError):
        c.checkpoint_history("NS.App")


# --- what the server already has loaded -------------------------------------------------
# An OP's registration IS a PropertyTemplate in the MDR (StriimClassLoader.addJar puts one
# and removeJar removes it), so LIST PROPERTYTEMPLATES is the read-only, authoritative
# answer to "does this cluster already have this OP?". Without it the framework could only
# guess, which is why it used to wipe its registry every run and reload everything.

_LIB_RESPONSE = [{
    "command": "LIST LIBRARIES", "executionStatus": "Success",
    "output": [
        {"fileName": "ExampleMapOpV8G-5.4.jar"},
        {"fileName": "LookupOpV7E-5.4.jar"},
        {"fileName": "ExampleJsonUdfV1C-5.4.jar"},
    ],
    "responseCode": 200,
}]


def test_loaded_libraries_returns_jar_filenames_including_udfs():
    # Keyed on the jar FILENAME, which is what LOAD/UNLOAD act on and what the registry
    # records -- no @PropertyTemplate-name inference in between. UDF jars are listed too,
    # which is what lets both kinds be verified the same way.
    class Api:
        def post_tungsten_line(self, line, timeout=None):
            assert line.strip().rstrip(";") == "LIST LIBRARIES"
            return _LIB_RESPONSE
    c = StriimClient.__new__(StriimClient); c.api = Api()
    names = c.loaded_libraries()
    assert "examplemapopv8g-5.4.jar" in names
    assert "examplejsonudfv1c-5.4.jar" in names, "UDF jars must be verifiable too"


def test_loaded_libraries_raises_on_failure():
    # Callers decide what an unknown answer means; the client must not invent an empty set,
    # which would read as "nothing is loaded" and trigger a cluster-wide reload.
    class Api:
        def post_tungsten_line(self, line, timeout=None):
            return [{"executionStatus": "Failure", "failureMessage": "boom"}]
    c = StriimClient.__new__(StriimClient); c.api = Api()
    with pytest.raises(StriimError):
        c.loaded_libraries()


# --- LOAD first, UNLOAD only when the server says it must -------------------------------
# The old order was UNLOAD-then-LOAD unconditionally: a destroy-then-recreate of shared
# cluster state on every run, even for a byte-identical jar. LOAD first means the common
# case (nothing loaded at that name) never destroys anything.

def _op_api(already_loaded: bool):
    class Api:
        def __init__(self): self.lines = []
        def post_tungsten_line(self, line, timeout=None):
            self.lines.append(line)
            if line.startswith("LOAD OPEN PROCESSOR") and already_loaded \
                    and sum(l.startswith("UNLOAD") for l in self.lines) == 0:
                return [{"executionStatus": "Failure", "failureMessage":
                         "The file :FooOp-5.4.jar has already been loaded. Unload it and load again."}]
            return [{"executionStatus": "Success"}]
    return Api()


def test_idempotent_load_does_not_unload_when_nothing_is_loaded():
    api = _op_api(already_loaded=False)
    c = StriimClient.__new__(StriimClient); c.api = api
    assert c.load_open_processor_idempotent("FooOp-5.4.jar") == "loaded"
    assert not any(l.startswith("UNLOAD") for l in api.lines), \
        "a jar the server does not have must never be unloaded first"
    assert len(api.lines) == 1


def test_idempotent_load_unloads_only_when_the_server_reports_a_collision():
    api = _op_api(already_loaded=True)
    c = StriimClient.__new__(StriimClient); c.api = api
    assert c.load_open_processor_idempotent("FooOp-5.4.jar") == "reloaded"
    kinds = [l.split()[0] for l in api.lines]
    assert kinds == ["LOAD", "UNLOAD", "LOAD"]


def test_idempotent_load_propagates_a_non_collision_failure_without_unloading():
    # "invalid LOC header" is NOT a collision -- unloading on it would destroy a good
    # registration to chase an error that has nothing to do with one.
    class Api:
        def __init__(self): self.lines = []
        def post_tungsten_line(self, line, timeout=None):
            self.lines.append(line)
            return [{"executionStatus": "Failure",
                     "failureMessage": "ZipFile invalid LOC header (bad signature):"}]
    api = Api()
    c = StriimClient.__new__(StriimClient); c.api = api
    with pytest.raises(StriimError):
        c.load_open_processor_idempotent("FooOp-5.4.jar")
    assert not any(l.startswith("UNLOAD") for l in api.lines)




# --- a collision UNLOADs the OP's other builds, each made safe first ----------------------
# Striim's UNLOAD rewrites its own copy from UploadedFiles/<name>; before_unload must leave the
# loaded bytes there first (opartifacts.restore_loaded_copy). Reproduced on 5.4.0.6C: other
# bytes there poison every later LOAD of the name, or keep the OLD main class beside the new
# jar's other classes, across a restart.

NEW = "FooOp-bbbbbbbbbbbb-5.4.jar"
COLLISION = [{"executionStatus": "Failure", "failureMessage":
              "The file :x has already been loaded. Unload it and load again."}]


class _Api:
    """A fake Striim: LOAD collides until the registration's holder is UNLOADed successfully
    (`holders`: substrings of names whose successful UNLOAD frees it; default any)."""
    def __init__(self, listed=(), list_error=None, unload_fails=(), holders=None):
        self.lines, self.listed, self.list_error = [], list(listed), list_error
        self.unload_fails, self.holders, self.freed = set(unload_fails), holders, False

    def post_tungsten_line(self, line, timeout=None):
        self.lines.append(line)
        if line.startswith("LIST LIBRARIES"):
            if self.list_error:
                raise self.list_error
            return [{"executionStatus": "Success", "output": [{"fileName": n} for n in self.listed]}]
        if line.startswith("UNLOAD"):
            if any(n in line for n in self.unload_fails):
                return [{"executionStatus": "Failure", "failureMessage": "in use"}]
            if self.holders is None or any(h in line for h in self.holders):
                self.freed = True
            return [{"executionStatus": "Success"}]
        if line.startswith("LOAD OPEN PROCESSOR") and not self.freed:
            return COLLISION
        return [{"executionStatus": "Success"}]


def _client(api):
    c = StriimClient.__new__(StriimClient); c.api = api
    return c


def _run(api, jar=NEW, tag="bbbbbbbbbbbb"):
    order = []
    post = api.post_tungsten_line

    def record(line, timeout=None):
        if line.startswith(("LOAD OPEN", "UNLOAD OPEN")):
            # an abandoned LOAD/UNLOAD keeps running on the server: they are never timed out
            assert timeout is striim_api.NO_TIMEOUT, (line, timeout)
        if line.startswith("UNLOAD"):
            order.append(line.split('"')[1].split("/")[1])
        return post(line, timeout)
    api.post_tungsten_line = record
    result = _client(api).load_open_processor_idempotent(jar, tag, lambda n: order.append(f"restore {n}"))
    return result, order


def test_collision_unloads_the_other_builds_each_restored_first():
    # Striim's UNLOAD rewrites its copy from UploadedFiles even when it then refuses, so every
    # UNLOAD is preceded by a restore.
    api = _Api(["FooOp-aaaaaaaaaaaa-5.4.jar",
                "FooOp-5.4.jar",                    # the plain name is another build too
                "FooOpV2-cccccccccccc-5.4.jar",     # another OP
                "FooOp-Bar-dddddddddddd-5.4.jar",   # another OP sharing the prefix
                "FooOp-AAAAAAAAAAAA-5.2.jar"])      # another series
    result, order = _run(api)
    assert result == "reloaded"
    assert order == ["restore FooOp-aaaaaaaaaaaa-5.4.jar", "FooOp-aaaaaaaaaaaa-5.4.jar",
                     "restore FooOp-5.4.jar", "FooOp-5.4.jar"]
    assert api.lines[-1] == f'LOAD OPEN PROCESSOR "UploadedFiles/{NEW}";'


def test_a_listed_own_name_is_reloaded_after_the_other_builds_are_tried():
    # The own listing may be stale while another build holds the registration, so the other
    # builds go first; here the own name is the holder.
    api = _Api([NEW, "FooOp-aaaaaaaaaaaa-5.4.jar"], holders=[NEW])
    result, order = _run(api)
    assert result == "reloaded"
    assert order == ["restore FooOp-aaaaaaaaaaaa-5.4.jar", "FooOp-aaaaaaaaaaaa-5.4.jar",
                     f"restore {NEW}", NEW]


def test_a_stale_own_listing_does_not_hide_the_build_that_holds_the_registration():
    api = _Api([NEW, "FooOp-aaaaaaaaaaaa-5.4.jar"], holders=["aaaa"])
    result, order = _run(api)
    assert result == "reloaded" and order == ["restore FooOp-aaaaaaaaaaaa-5.4.jar",
                                              "FooOp-aaaaaaaaaaaa-5.4.jar"]


def test_a_transport_error_on_an_unload_is_noted_not_fatal():
    api = _Api(["FooOp-aaaaaaaaaaaa-5.4.jar", "FooOp-5.4.jar"], holders=["FooOp-5.4.jar"])
    post = api.post_tungsten_line

    def flaky(line, timeout=None):
        if line.startswith("UNLOAD") and "aaaa" in line:
            raise ConnectionError("reset")
        return post(line, timeout)
    api.post_tungsten_line = flaky
    result, _ = _run(api)
    assert result == "reloaded"


def test_a_failed_listing_falls_back_to_reloading_the_name_itself():
    api = _Api(list_error=ConnectionError("reset"))
    result, order = _run(api)
    assert result == "reloaded" and order == [f"restore {NEW}", NEW]
    assert [l.split()[0] for l in api.lines] == ["LOAD", "LIST", "LIST", "UNLOAD", "LOAD"]


def test_an_unidentifiable_holder_is_displaced_by_unloading_the_name_itself():
    # UNLOAD removes the OP's registration whichever jar holds it; a never-loaded content name
    # is safe to UNLOAD.
    api = _Api(["SomethingElse-5.4.jar"])
    result, order = _run(api)
    assert result == "reloaded" and order == [f"restore {NEW}", NEW]


def test_a_refused_unload_of_a_stale_listing_does_not_stop_the_load():
    # A listed build whose registration is already gone refuses its UNLOAD; the plain name
    # held the registration, so the LOAD goes ahead once that is unloaded.
    api = _Api(["FooOp-aaaaaaaaaaaa-5.4.jar", "FooOp-5.4.jar"], unload_fails=["aaaa"],
               holders=["FooOp-5.4.jar"])
    result, order = _run(api)
    assert result == "reloaded"
    assert api.lines[-1] == f'LOAD OPEN PROCESSOR "UploadedFiles/{NEW}";'


def test_when_nothing_frees_the_registration_the_error_names_the_refusals():
    api = _Api(["FooOp-aaaaaaaaaaaa-5.4.jar"], unload_fails=["aaaa", "bbbb"])
    with pytest.raises(StriimError, match="already been loaded.*UNLOAD refusals: .*aaaaaaaaaaaa.*in use"):
        _run(api)


def test_a_jar_without_a_tag_replaces_its_own_name():
    api = _Api([])
    result, order = _run(api, jar="Plain-5.4.jar", tag=None)
    assert result == "reloaded" and order == ["restore Plain-5.4.jar", "Plain-5.4.jar"]
    assert [l.split()[0] for l in api.lines] == ["LOAD", "UNLOAD", "LOAD"]


def test_library_file_names_keep_the_servers_case():
    api = _Api(["FooOp-AbC-5.4.jar"])
    assert _client(api).library_file_names() == ["FooOp-AbC-5.4.jar"]
    assert _client(api).loaded_libraries() == {"fooop-abc-5.4.jar"}


def test_a_failed_restore_skips_that_unload_and_says_so():
    # The broken copy's name is not UNLOADed, and no self-UNLOAD frees the OP behind its back.
    api = _Api(["FooOp-aaaaaaaaaaaa-5.4.jar", "FooOp-5.4.jar"], holders=["bbbb"])

    def restore(name):
        if name == "FooOp-5.4.jar":
            raise RuntimeError("not a complete zip")
    with pytest.raises(StriimError, match="FooOp-5.4.jar: not unloaded, restore failed"):
        _client(api).load_open_processor_idempotent(NEW, "bbbbbbbbbbbb", restore)
    unloads = [l for l in api.lines if l.startswith("UNLOAD")]
    assert unloads == ['UNLOAD OPEN PROCESSOR "UploadedFiles/FooOp-aaaaaaaaaaaa-5.4.jar";']


def test_a_non_collision_load_failure_still_names_the_refusals():
    class Api(_Api):
        def post_tungsten_line(self, line, timeout=None):
            if line.startswith("LOAD OPEN PROCESSOR") and any(l.startswith("UNLOAD") for l in self.lines):
                self.lines.append(line)
                return [{"executionStatus": "Failure", "failureMessage": "File copying failed"}]
            return super().post_tungsten_line(line)
    api = Api(["FooOp-aaaaaaaaaaaa-5.4.jar"], unload_fails=["aaaa"])
    with pytest.raises(StriimError, match="File copying failed.*UNLOAD refusals: .*aaaa.*in use"):
        _client(api).load_open_processor_idempotent(NEW, "bbbbbbbbbbbb", lambda n: None)


def test_a_holder_whose_restore_failed_is_not_freed_by_the_self_unload():
    api = _Api(["FooOp-5.4.jar"], holders=["FooOp-5.4.jar"])

    def restore(name):
        raise RuntimeError("not a complete zip")
    with pytest.raises(StriimError, match="already been loaded.*restore failed"):
        _client(api).load_open_processor_idempotent(NEW, "bbbbbbbbbbbb", restore)
    assert not [l for l in api.lines if l.startswith("UNLOAD")]


def test_library_listing_passes_its_timeout():
    seen = []
    class _Api:
        def post_tungsten_line(self, line, timeout=None):
            seen.append(timeout)
            return [{"executionStatus": "Success", "output": []}]
    c = _client(_Api())
    c.loaded_libraries(timeout=(1, 2))
    c.loaded_libraries()
    assert seen == [(1, 2), None]


def test_force_drop_does_not_resend_a_drop_that_timed_out(monkeypatch):
    import requests
    from livetest import striim
    monkeypatch.setattr(striim.time, "sleep", lambda _s: None)
    sent = []
    class _Api:
        def post_tungsten_line(self, line, timeout=None):
            sent.append(line)
            raise requests.ReadTimeout("slow")
    assert _client(_Api())._force_drop("NS.App") is None
    assert len(sent) == 1


def test_force_drop_retries_a_connect_timeout(monkeypatch):
    # A ConnectTimeout never reached the server, so the DROP is not running: retry it.
    import requests
    from livetest import striim
    monkeypatch.setattr(striim.time, "sleep", lambda _s: None)
    sent = []
    class _Api:
        def post_tungsten_line(self, line, timeout=None):
            sent.append(line)
            if len(sent) == 1:
                raise requests.ConnectTimeout("busy")
            return [{"executionStatus": "Success"}]
    assert _client(_Api())._force_drop("NS.App") is True
    assert len(sent) == 2


def test_udf_jar_load_and_unload_are_never_timed_out():
    import striim_api
    seen = []
    class _Api:
        def post_tungsten_line(self, line, timeout=None):
            seen.append((line.split()[0], timeout))
            return []
    _client(_Api()).load_jar("x.jar")
    assert seen == [("UNLOAD", striim_api.NO_TIMEOUT), ("LOAD", striim_api.NO_TIMEOUT)]


def test_from_url_fills_an_empty_shell_value_from_dotenv(monkeypatch):
    import striim_api
    from livetest import striim
    monkeypatch.setenv("STRIIM_API_TIMEOUT", "")
    monkeypatch.setattr(striim.paths, "setting", lambda k: "0" if k == "STRIIM_API_TIMEOUT" else None)
    monkeypatch.setattr(striim_api.StriimApi, "getAuthToken", lambda self, timeout=None: None)
    client = StriimClient.from_url("http://localhost:9080", "u", "p")
    assert client.api.default_timeout is None


def test_force_drop_treats_a_body_read_timeout_as_still_running(monkeypatch):
    # requests wraps a read timeout while reading the body in a ConnectionError.
    import requests, urllib3
    from livetest import striim
    monkeypatch.setattr(striim.time, "sleep", lambda _s: None)
    sent = []
    class _Api:
        def post_tungsten_line(self, line, timeout=None):
            sent.append(line)
            raise requests.ConnectionError(urllib3.exceptions.ReadTimeoutError(None, None, "slow"))
    assert _client(_Api())._force_drop("NS.App") is None
    assert len(sent) == 1


# --- follow-ups: poll timeouts, deploy timeout, teardown budget ----------------------------

def test_read_only_polls_use_the_poll_timeout():
    from livetest import striim
    seen = []
    class _Api:
        def post_tungsten_line(self, line, timeout=None):
            seen.append((line.split()[0], timeout))
            return [{"executionStatus": "Success", "output": {}}]
        def status_application(self, app, timeout=None):
            seen.append(("STATUS", timeout))
            return "RUNNING"
    c = _client(_Api())
    c.mon("NS.App"); c.describe("NS.App"); c.current_status("NS.App"); c._list_twice()
    assert seen == [(v, striim.POLL_TIMEOUT) for v in ("MON", "DESCRIBE", "STATUS", "LIST")]


def test_a_timed_out_deploy_is_a_clear_error_and_not_retried(tmp_path, monkeypatch):
    import requests
    monkeypatch.chdir(tmp_path)
    posts = []
    class _Api:
        def post_tungsten_file(self, path, timeout=None):
            posts.append(path)
            raise requests.ReadTimeout("read timed out")
    from livetest.striim import StriimTimeout
    with pytest.raises(StriimTimeout, match="may still be executing it. Not retried"):
        _client(_Api()).deploy_tql("CREATE APPLICATION X;")
    assert len(posts) == 1


def test_a_deploy_connection_error_is_not_relabelled(tmp_path, monkeypatch):
    import requests
    monkeypatch.chdir(tmp_path)
    class _Api:
        def post_tungsten_file(self, path, timeout=None):
            raise requests.ConnectionError("refused")
    with pytest.raises(requests.ConnectionError):
        _client(_Api()).deploy_tql("CREATE APPLICATION X;")


class _Clock:
    now = 0.0
    def __call__(self):
        return self.now


def test_teardown_skips_graceful_steps_after_the_budget_but_still_drops(monkeypatch):
    from livetest import striim
    api = FakeApi()
    clock = _Clock()
    monkeypatch.setattr(striim.time, "monotonic", clock)
    monkeypatch.setattr(striim.time, "sleep", lambda _s: None)
    def slow_stop(app):
        clock.now += striim.TEARDOWN_BUDGET + 1      # a stop that ate the whole budget
        api.stopped = True
    api.stop_application = slow_stop
    _client(api).teardown("SLT_x.xApp", namespace="SLT_x")
    assert api.stopped and not api.undeployed          # undeploy not started
    assert any("DROP APPLICATION SLT_x.xApp FORCE" in l for l in api.posted_lines)
    assert any("DROP NAMESPACE SLT_x CASCADE FORCE" in l for l in api.posted_lines)


def test_teardown_namespace_skips_graceful_steps_after_the_budget_but_drops_every_app(monkeypatch):
    from livetest import striim
    api = FakeApi()
    api.list_applications_response = _list_apps_response("SLT_x.aApp", "SLT_x.bApp")
    clock = _Clock()
    monkeypatch.setattr(striim.time, "monotonic", clock)
    monkeypatch.setattr(striim.time, "sleep", lambda _s: None)
    def slow_stop(app):
        clock.now += striim.TEARDOWN_BUDGET + 1
        api.stopped_apps.append(app)
    api.stop_application = slow_stop
    _client(api).teardown_namespace("SLT_x")
    assert api.stopped_apps == ["SLT_x.aApp"] and api.undeployed_apps == []
    assert {l.split()[2] for l in api.posted_lines if l.startswith("DROP APPLICATION")} == {
        "SLT_x.aApp", "SLT_x.bApp"}
    assert any("DROP NAMESPACE SLT_x CASCADE FORCE" in l for l in api.posted_lines)


def test_a_deploy_timeout_is_a_striim_timeout():
    from livetest.striim import StriimTimeout
    assert issubclass(StriimTimeout, StriimError)


def test_teardown_namespace_lists_apps_with_the_default_timeout():
    # A timed-out listing would skip every per-app FORCE drop.
    seen = []
    class _Api(FakeApi):
        def post_tungsten_line(self, line, timeout=None):
            if line.startswith("LIST"):
                seen.append(timeout)
            return super().post_tungsten_line(line, timeout)
    _client(_Api()).teardown_namespace("SLT_x")
    assert seen == [None]


def test_remaining_read_only_polls_use_the_poll_timeout():
    from livetest import striim
    seen = []
    class _Api:
        def post_tungsten_line(self, line, timeout=None):
            seen.append(timeout)
            return [{"executionStatus": "Success", "output": []}]
    c = _client(_Api())
    c.checkpoint_history("NS.App"); c.list_deployment_groups()
    assert seen == [striim.POLL_TIMEOUT] * 2


def test_the_teardown_budget_fits_inside_the_interrupt_alarm():
    from livetest import striim, plugin
    assert striim.TEARDOWN_BUDGET < plugin.INTERRUPT_TEARDOWN_TIMEOUT


def test_from_url_bounds_the_login(monkeypatch):
    from livetest import striim
    seen = []
    monkeypatch.setattr(striim_api.StriimApi, "getAuthToken",
                        lambda self, timeout=None: seen.append(timeout))
    StriimClient.from_url("http://localhost:9080", "u", "p")
    assert seen == [striim.POLL_TIMEOUT]
