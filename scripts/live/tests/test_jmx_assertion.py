import pytest

from livetest.assertions import AssertionFailed
from livetest.assertions.jmx import (
    JmxSpecError, assert_jmx, exporter_endpoints, parse_jmx_specs, parse_samples, render_row_keys,
    select_bean,
)
from livetest.resultschema import SCHEMA_VERSION, validate

# Lines as jmx_prometheus_javaagent 0.16.1 renders them with the stack's rule-less config,
# captured from that jar serving an MXBean (Hits long, HitRate double, GateRunning boolean; a
# String attribute is not exported at all), its domain renamed to com.example.cache.
EXPORT = r'''# HELP com_example_cache_LookupOp_Hits Hits (com.example.cache<type=LookupOp, name="NS.ProductEnrich"><>Hits)
# TYPE com_example_cache_LookupOp_Hits untyped
com_example_cache_LookupOp_Hits{name="\"NS.a,b=\\\"c\\\"\"",} 9.0
com_example_cache_LookupOp_Hits{name="\"NS.ProductEnrich\"",} 3.0
com_example_cache_LookupOp_Misses{name="\"NS.ProductEnrich\"",} 2.0
com_example_cache_LookupOp_HitRate{name="\"NS.ProductEnrich\"",} 60.0
com_example_cache_LookupOp_GateRunning{name="\"NS.ProductEnrich\"",} 0.0
com_example_cache_LookupOp_Hits{name="\"OTHER.ProductEnrich\"",} 7.0
jvm_threads_current 42.0
'''

A = "http://h:7071/metrics"
B = "http://h:7075/metrics"


DOMAIN = "com.example.cache"


def _spec(attrs, component="ProductEnrich", type_="LookupOp", domain=DOMAIN):
    return [{"bean": {"domain": domain, "type": type_, "component": component}, "attributes": attrs}]


def _fetcher(pages):
    """pages: {url: text | Exception | list-of-those consumed one per poll}."""
    pages = {u: (list(p) if isinstance(p, list) else [p]) for u, p in pages.items()}

    def fetch(url):
        seq = pages[url]
        page = seq.pop(0) if len(seq) > 1 else seq[0]
        if isinstance(page, Exception):
            raise page
        return page
    return fetch


# ---- spec parsing --------------------------------------------------------------------

def test_parse_ok():
    parse_jmx_specs(_spec({"Hits": 3, "Misses": {"min": 1}, "GateRunning": False,
                           "HitRate": {"min": 50, "max": 70.5}}))


def test_parse_refuses_clocks_by_camelcase_word():
    for clock in ("ProbeMaxOverrunMillis", "LastEventTime", "CommitLatency", "EventAgeNanos",
                  "NodeUptime"):   # same word list as the integration tier
        with pytest.raises(JmxSpecError, match="CLOCK"):
            parse_jmx_specs(_spec({clock: 0}))
    parse_jmx_specs(_spec({"GateTimeoutsPassThrough": 0}))   # Timeouts is a count, not Time


def test_parse_refuses_bad_shapes():
    bad = [
        [],                                                           # nothing to assert
        [{"bean": {"domain": "d", "type": "X", "component": "C"}}],     # no attributes
        [{"bean": {"domain": "d", "type": "X", "component": "C"}, "attributes": {}}],
        [{"bean": {"domain": "d", "type": "X"}, "attributes": {"Hits": 1}}],   # no component
        [{"bean": {"domain": "d", "type": "", "component": "C"}, "attributes": {"Hits": 1}}],
        [{"bean": "X", "attributes": {"Hits": 1}}],
        [{"bean": {"domain": "d", "type": "X", "component": "C"}, "attributes": {"Hits": 1}, "node": 1}],
        _spec({"Hits": "3"}),                                         # strings are not exported
        _spec({"Hits": [3]}),
        _spec({"Hits": {"min": 3, "max": 1}}),
        _spec({"Hits": {"at_least": 3}}),
        _spec({"Hits": {"min": True}}),
        _spec({"Hits": {}}),
        _spec({"bad-name": 1}),
    ]
    for raw in bad:
        with pytest.raises(JmxSpecError):
            parse_jmx_specs(raw)


def test_parse_requires_the_mbean_domain():
    # No default: the domain is the one the plugin registers its MBean under.
    with pytest.raises(JmxSpecError, match="domain"):
        parse_jmx_specs([{"bean": {"type": "X", "component": "C"}, "attributes": {"Hits": 1}}])
    for domain in ("", "  ", "a:b", "a*", 3):
        with pytest.raises(JmxSpecError, match="domain"):
            parse_jmx_specs(_spec({"Hits": 1}, domain=domain))


def test_the_spec_domain_selects_the_bean():
    other = ('com_acme_ops_LookupOp_Hits{name="\\"NS.ProductEnrich\\"",} 5.0\n'
             'com_acme_ops_LookupOp_Misses{name="\\"NS.ProductEnrich\\"",} 0.0\n')
    fetch = _fetcher({A: EXPORT + other})
    records = assert_jmx(_spec({"Hits": 5}, domain="com.acme-ops"), "NS", timeout=0, poll=0,
                         endpoints=[A], fetch=fetch)
    assert records[0]["target"] == 'com.acme-ops:type=LookupOp,name="NS.ProductEnrich"'
    assert_jmx(_spec({"Hits": 3}), "NS", timeout=0, poll=0, endpoints=[A], fetch=fetch)
    with pytest.raises(AssertionFailed, match="no com_other_ lines at all"):
        assert_jmx(_spec({"Hits": 3}, domain="com.other"), "NS", timeout=0, poll=0,
                   endpoints=[A], fetch=fetch)


# ---- parsing and selection -----------------------------------------------------------

def test_samples_unescape_the_quoted_objectname_value():
    samples = parse_samples(EXPORT)
    assert len(samples) == 7
    assert select_bean(samples, DOMAIN, "LookupOp", "NS.ProductEnrich") == {
        "Hits": (3.0, 'com_example_cache_LookupOp_Hits{name="\\"NS.ProductEnrich\\"",} 3.0'),
        "Misses": (2.0, samples[2][3]),
        "HitRate": (60.0, samples[3][3]),
        "GateRunning": (0.0, samples[4][3]),
    }
    # A name with the characters ObjectName.quote escapes still round-trips.
    assert select_bean(samples, DOMAIN, "LookupOp", 'NS.a,b="c"') == {"Hits": (9.0, samples[0][3])}
    assert select_bean(samples, DOMAIN, "OtherOp", "NS.ProductEnrich") == {}


def test_endpoints_follow_the_stack_env():
    env = {"STRIIM_URL": "http://localhost:9180", "SLT_STRIIM_JMX_HOST_PORT": "7171",
           "SLT_STRIIM_NODE_JMX_HOST_PORT": "7175"}
    assert exporter_endpoints(env) == ["http://localhost:7171/metrics",
                                       "http://localhost:7175/metrics"]
    assert exporter_endpoints({}) == ["http://localhost:7071/metrics",
                                      "http://localhost:7075/metrics"]


# ---- the assertion ---------------------------------------------------------------------

def test_passes_on_the_jvm_that_has_the_bean_and_returns_a_valid_record():
    fetch = _fetcher({A: "jvm_threads_current 1.0\n", B: EXPORT})
    records = assert_jmx(_spec({"Hits": 3, "Misses": {"min": 1, "max": 2}, "GateRunning": False}),
                         "NS", timeout=5, poll=0, endpoints=[A, B], fetch=fetch)
    assert len(records) == 1 and records[0]["status"] == "passed"
    assert records[0]["type"] == "jmx"
    assert records[0]["target"] == 'com.example.cache:type=LookupOp,name="NS.ProductEnrich"'
    validate({"schema_version": SCHEMA_VERSION, "tests": [{
        "name": "t", "nodeid": "t", "status": "passed", "topology": "single", "services": [],
        "duration": 0.0, "skip_reason": None, "assertions": records}]})


def test_true_matches_one_and_a_dotted_component_is_taken_as_qualified():
    page = EXPORT.replace('GateRunning{name="\\"NS.ProductEnrich\\"",} 0.0',
                          'GateRunning{name="\\"NS.ProductEnrich\\"",} 1.0')
    assert_jmx(_spec({"GateRunning": True}, component="NS.ProductEnrich"), "IGNORED",
               timeout=0, poll=0, endpoints=[A], fetch=_fetcher({A: page}))


def test_polls_until_the_counters_catch_up():
    early = EXPORT.replace("_Hits{name=\"\\\"NS.ProductEnrich\\\"\",} 3.0",
                           "_Hits{name=\"\\\"NS.ProductEnrich\\\"\",} 1.0")
    assert early != EXPORT
    fetch = _fetcher({A: [early, early, EXPORT]})
    assert_jmx(_spec({"Hits": 3}), "NS", timeout=10, poll=0, endpoints=[A], fetch=fetch)


def test_mismatch_fails_naming_the_value_and_the_exporter_line():
    with pytest.raises(AssertionFailed) as e:
        assert_jmx(_spec({"Hits": 4, "Misses": 2}), "NS", timeout=0, poll=0, endpoints=[A],
                   fetch=_fetcher({A: EXPORT}))
    msg = str(e.value)
    assert "Hits: shows 3.0, expected 4" in msg
    assert 'com_example_cache_LookupOp_Hits{name="\\"NS.ProductEnrich\\"",} 3.0' in msg
    assert "Misses" not in msg
    rec = e.value.records[0]
    assert rec["status"] == "failed"
    assert rec["actual"]["rows"] == [{"attribute": "Hits", "value": "3.0"},
                                     {"attribute": "Misses", "value": "2.0"}]


def test_bound_failure_and_missing_attribute():
    with pytest.raises(AssertionFailed, match=r"Misses: shows 2.0, expected >= 5"):
        assert_jmx(_spec({"Misses": {"min": 5}}), "NS", timeout=0, poll=0, endpoints=[A],
                   fetch=_fetcher({A: EXPORT}))
    with pytest.raises(AssertionFailed, match=r"Label: not exported .*exported: \['GateRunning'"):
        assert_jmx(_spec({"Label": 1}), "NS", timeout=0, poll=0, endpoints=[A],
                   fetch=_fetcher({A: EXPORT}))


def test_bean_not_found_lists_the_nearby_lines_and_never_passes():
    with pytest.raises(AssertionFailed) as e:
        assert_jmx(_spec({"Hits": 3}, component="Missing"), "NS", timeout=0, poll=0,
                   endpoints=[A], fetch=_fetcher({A: EXPORT}))
    msg = str(e.value)
    assert 'bean com.example.cache:type=LookupOp,name="NS.Missing" not found' in msg
    assert "OTHER.ProductEnrich" in msg          # the nearby com_example_cache lines
    assert e.value.records[0]["status"] == "failed"
    with pytest.raises(AssertionFailed, match="no com_example_cache_ lines at all"):
        assert_jmx(_spec({"Hits": 3}), "NS", timeout=0, poll=0, endpoints=[A],
                   fetch=_fetcher({A: "jvm_threads_current 1.0\n"}))


def test_unreachable_endpoints_are_named():
    fetch = _fetcher({A: ConnectionRefusedError("refused"), B: ConnectionRefusedError("refused")})
    with pytest.raises(AssertionFailed) as e:
        assert_jmx(_spec({"Hits": 3}), "NS", timeout=0, poll=0, endpoints=[A, B], fetch=fetch)
    msg = str(e.value)
    assert "not found on any endpoint" in msg and "unreachable" in msg
    assert A in msg and B in msg and "ConnectionRefusedError" in msg


def test_one_unreachable_endpoint_does_not_hide_the_bean_on_the_other():
    fetch = _fetcher({A: ConnectionRefusedError("refused"), B: EXPORT})
    assert_jmx(_spec({"Hits": 3}), "NS", timeout=0, poll=0, endpoints=[A, B], fetch=fetch)


def test_a_bean_on_two_jvms_is_refused_as_ambiguous():
    with pytest.raises(AssertionFailed, match="exported by 2 JVMs"):
        assert_jmx(_spec({"Hits": 3}), "NS", timeout=0, poll=0, endpoints=[A, B],
                   fetch=_fetcher({A: EXPORT, B: EXPORT}))


def test_status_probe_runs_every_poll():
    calls = []
    assert_jmx(_spec({"Hits": 3}), "NS", timeout=5, poll=0, endpoints=[A],
               fetch=_fetcher({A: EXPORT}), status_probe=lambda: calls.append(1))
    assert calls == [1]


# ---- rows: one entry of a Map attribute ------------------------------------------------
# An MXBean Map<String, Long> attribute, as the 0.16.1 exporter renders it: one line per entry,
# the entry's key in a `key` label (captured from a real plugin's per-app counts map).

MAP_EXPORT = r'''com_example_cache_LookupOp_Hits{name="\"NS.ProductEnrich\"",} 3.0
com_example_cache_LookupOp_CountsByApp{name="\"NS.ProductEnrich\"",key="NS.orders",} 3.0
com_example_cache_LookupOp_CountsByApp{name="\"NS.ProductEnrich\"",key="NS.billing",} 2.0
com_example_cache_LookupOp_CountsByApp{name="\"OTHER.ProductEnrich\"",key="NS.orders",} 9.0
'''


def _rows_spec(rows, attrs=None):
    spec = {"bean": {"domain": DOMAIN, "type": "LookupOp", "component": "ProductEnrich"}, "rows": rows}
    if attrs is not None:
        spec["attributes"] = attrs
    return [spec]


def test_rows_select_each_map_entry_by_its_key():
    fetch = _fetcher({A: MAP_EXPORT})
    records = assert_jmx(_rows_spec({"CountsByApp": {"NS.orders": 3, "NS.billing": {"max": 2}}},
                                    attrs={"Hits": 3}),
                         "NS", timeout=0, poll=0, endpoints=[A], fetch=fetch)
    assert records[0]["status"] == "passed"
    assert [r["attribute"] for r in records[0]["actual"]["rows"]] == [
        "Hits", "CountsByApp[NS.orders]", "CountsByApp[NS.billing]"]


def test_rows_fail_on_a_wrong_value_and_name_the_entry():
    fetch = _fetcher({A: MAP_EXPORT})
    with pytest.raises(AssertionFailed, match=r"CountsByApp\[NS.billing\]: shows 2.0, expected 3"):
        assert_jmx(_rows_spec({"CountsByApp": {"NS.billing": 3}}), "NS", timeout=0, poll=0,
                   endpoints=[A], fetch=fetch)


def test_rows_fail_on_an_absent_key_and_list_the_keys_there_are():
    fetch = _fetcher({A: MAP_EXPORT})
    with pytest.raises(AssertionFailed, match=r"CountsByApp\[NS\.other\]: not exported .*NS\.billing"):
        assert_jmx(_rows_spec({"CountsByApp": {"NS.other": 1}}), "NS", timeout=0, poll=0,
                   endpoints=[A], fetch=fetch)


def test_a_map_entry_never_answers_for_a_plain_attribute():
    fetch = _fetcher({A: MAP_EXPORT})
    with pytest.raises(AssertionFailed, match="CountsByApp: not exported"):
        assert_jmx(_spec({"CountsByApp": 3}), "NS", timeout=0, poll=0, endpoints=[A], fetch=fetch)


def test_parse_rows_shapes():
    parse_jmx_specs(_rows_spec({"CountsByApp": {"NS.a": 1}}))
    bad = [
        _rows_spec({}),                                   # nothing to assert
        _rows_spec({"CountsByApp": {}}),
        _rows_spec({"CountsByApp": [1]}),
        _rows_spec({"CountsByApp": {"": 1}}),
        _rows_spec({"CountsByApp": {"NS.a": "1"}}),
        _rows_spec({"LastSeenTimeByApp": {"NS.a": 1}}),   # a clock, as for attributes
        _rows_spec({"bad-name": {"NS.a": 1}}),
    ]
    for raw in bad:
        with pytest.raises(JmxSpecError):
            parse_jmx_specs(raw)


def test_row_keys_are_rendered_and_a_collision_is_refused():
    spec = _rows_spec({"CountsByApp": {"${NS}.orders": 3}})[0]
    render_row_keys(spec, lambda k: k.replace("${NS}", "NS"))
    assert spec["rows"] == {"CountsByApp": {"NS.orders": 3}}
    clash = _rows_spec({"CountsByApp": {"${NS}.a": 1, "NS.a": 2}})[0]
    with pytest.raises(JmxSpecError, match="both render to 'NS.a'"):
        render_row_keys(clash, lambda k: k.replace("${NS}", "NS"))
