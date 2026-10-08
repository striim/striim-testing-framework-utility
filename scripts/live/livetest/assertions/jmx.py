from __future__ import annotations

import os
import re
import time
import urllib.request
from urllib.parse import urlparse

from livetest.assertions import AssertionFailed
from livetest.resultschema import build_assertion_result

# `assert.jmx:` reads a plugin's MBean attributes the way an operator's Prometheus does: from
# the jmx_prometheus_javaagent (0.16.1, no rules) every Striim JVM in the Docker stack runs on
# container port 7071. The exporter's default naming turns
#
#     com.example.cache:type=FooOp,name="NS.ProductEnrich"   attribute Hits
#
# into this line -- the ObjectName-quoted value, quotes included, escaped again as a
# Prometheus label (measured against the same jar and config, see the tests):
#
#     com_example_cache_FooOp_Hits{name="\"NS.ProductEnrich\"",} 3.0
#
# A boolean attribute is exported as 1.0/0.0; a String attribute is not exported at all.
# The domain is the one the plugin registers its MBean under; a spec must name it.

# The app-group JVMs' exporter host ports, by the compose variable that publishes each one.
# The agent's is left out: nothing the harness deploys runs an OP on the agent.
ENDPOINT_PORT_VARS = (("SLT_STRIIM_JMX_HOST_PORT", "7071"),
                      ("SLT_STRIIM_NODE_JMX_HOST_PORT", "7075"))

# CamelCase words that make an attribute a clock or a duration: a case asserting one measures
# the machine. `Timeouts` is a count and is a different word from `Time`.
CLOCK_WORDS = frozenset({
    "Millis", "Micros", "Nanos", "Seconds", "Secs", "Ms", "Latency", "Time", "Timestamp",
    "Age", "Duration", "Elapsed", "Uptime"})

_ATTR = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_WORD = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|\d+")
_SAMPLE = re.compile(r"([A-Za-z_:][A-Za-z0-9_:]*)(?:\{(.*)\})?\s+(\S+)(?:\s+\S+)?\s*\Z")
_UNSAFE = re.compile(r"[^A-Za-z0-9:]+")
_LABEL = re.compile(r'\s*([A-Za-z_][A-Za-z0-9_]*)="((?:[^"\\]|\\.)*)"\s*,?')


class JmxSpecError(Exception):
    pass


def _clock_word(attr: str) -> str | None:
    return next((w for w in _WORD.findall(attr) if w.capitalize() in CLOCK_WORDS), None)


def domain_prefix(domain: str) -> str:
    """The metric-name prefix the exporter gives a domain's beans: each run of characters other
    than letters, digits and ':' (underscores included) becomes one '_'."""
    return _UNSAFE.sub("_", domain + "_")


def _check_value(label: str, want) -> None:
    if isinstance(want, dict):
        if not want or set(want) - {"min", "max"} or not all(
                isinstance(v, (int, float)) and not isinstance(v, bool)
                for v in want.values()):
            raise JmxSpecError(
                f"'assert.jmx' {label}: a bound is {{min, max}} with numeric "
                f"values, got {want!r}")
        if "min" in want and "max" in want and want["min"] > want["max"]:
            raise JmxSpecError(f"'assert.jmx' {label}: min > max in {want!r}")
    elif not isinstance(want, (int, float)):
        # bool is an int subclass, so it lands here only when it is not one.
        raise JmxSpecError(
            f"'assert.jmx' {label}: expected a number, a boolean or "
            f"{{min, max}}, got {want!r} (the exporter does not export strings)")


def _check_name(name) -> None:
    if not isinstance(name, str) or not _ATTR.match(name):
        raise JmxSpecError(f"'assert.jmx' attribute name {name!r} is not an identifier")
    word = _clock_word(name)
    if word:
        raise JmxSpecError(
            f"'assert.jmx' attribute {name!r} is a CLOCK ({word!r}), and a case that "
            f"asserts one is measuring the machine it runs on; assert a count instead")


def parse_jmx_specs(raw) -> list[dict]:
    """`assert.jmx:` is a list of {bean: {domain, type, component}, attributes, rows}. `domain` is
    the ObjectName's domain, the one the plugin registers its MBean under. `type` is the
    ObjectName's `type=` key (the plugin's module, e.g. FooOp); `component` is the
    TQL component name, qualified by the test's namespace at run time (a name with a dot is
    taken as already qualified). `attributes` maps an attribute to a number or boolean compared
    exactly, or to {min, max} bounds (either or both, inclusive). `rows` maps a Map attribute
    (an MXBean Map, exported one line per entry with the entry's key in a `key` label) to
    {entry key: expected value}, in the same value forms. At least one of the two is needed."""
    if not isinstance(raw, list) or not raw:
        raise JmxSpecError("'assert.jmx' must be a non-empty list of specs")
    for spec in raw:
        if not isinstance(spec, dict):
            raise JmxSpecError(f"jmx spec must be a mapping: {spec!r}")
        unknown = set(spec) - {"bean", "attributes", "rows"}
        if unknown:
            raise JmxSpecError(f"jmx spec has unknown key(s) {sorted(unknown)}; "
                               f"expected 'bean:' with 'attributes:' and/or 'rows:'")
        bean = spec.get("bean")
        if isinstance(bean, dict) and "domain" not in bean:
            raise JmxSpecError(
                f"jmx spec 'bean' needs 'domain:', the MBean domain the plugin registers its "
                f"bean under (there is no default), got {bean!r}")
        if not isinstance(bean, dict) or set(bean) != {"domain", "type", "component"} or not all(
                isinstance(bean[k], str) and bean[k] for k in ("domain", "type", "component")):
            raise JmxSpecError(
                f"jmx spec 'bean' must be {{domain: <MBean domain>, type: <ObjectName type>, "
                f"component: <TQL component name>}}, got {bean!r}")
        domain = bean["domain"]
        if domain != domain.strip() or any(c in domain for c in ':,=*?"\n'):
            raise JmxSpecError(f"jmx spec 'bean.domain' {domain!r} is not an MBean domain")
        attrs = spec.get("attributes", {})
        rows = spec.get("rows", {})
        if not isinstance(attrs, dict) or not isinstance(rows, dict) or not (attrs or rows):
            raise JmxSpecError("jmx spec needs a non-empty 'attributes:' mapping of "
                               "attribute -> expected value, or 'rows:' of "
                               "Map attribute -> {entry key: expected value}")
        for name, want in attrs.items():
            _check_name(name)
            _check_value(f"attribute {name!r}", want)
        for name, entries in rows.items():
            _check_name(name)
            if not isinstance(entries, dict) or not entries:
                raise JmxSpecError(f"'assert.jmx' rows {name!r}: expected a non-empty mapping of "
                                   f"entry key -> expected value, got {entries!r}")
            for key, want in entries.items():
                if not isinstance(key, str) or not key:
                    raise JmxSpecError(f"'assert.jmx' rows {name!r}: entry key {key!r} is not a "
                                       f"non-empty string")
                _check_value(f"rows {name}[{key}]", want)
    return raw


def render_row_keys(spec: dict, render_key) -> None:
    """Token-render each `rows:` entry key in place. Two keys that render to the same string would
    silently drop one expectation, so that is refused."""
    for attr, entries in spec.get("rows", {}).items():
        rendered = {}
        for key, want in entries.items():
            r = render_key(key)
            if r in rendered:
                raise JmxSpecError(f"'assert.jmx' rows {attr!r}: entry keys {key!r} and another "
                                   f"both render to {r!r}")
            rendered[r] = want
        spec["rows"][attr] = rendered


def exporter_endpoints(env=None) -> list[str]:
    """The exporter URLs of this stack's app-group JVMs: STRIIM_URL's host, and the host
    ports compose publishes -- so a prefixed stack on its own ports is read, not the default."""
    env = os.environ if env is None else env
    host = urlparse(env.get("STRIIM_URL") or "http://localhost:9080").hostname or "localhost"
    return [f"http://{host}:{env.get(var) or default}/metrics"
            for var, default in ENDPOINT_PORT_VARS]


def _fetch(url: str) -> str:
    with urllib.request.urlopen(url, timeout=5) as r:
        return r.read().decode("utf-8", "replace")


def _unescape_label(v: str) -> str:
    return re.sub(r"\\(.)", lambda m: "\n" if m.group(1) == "n" else m.group(1), v)


def _unquote_objectname(v: str) -> str:
    # ObjectName.quote: surrounding quotes, with \\ \" \* \? \n escaped inside.
    if len(v) >= 2 and v[0] == '"' and v[-1] == '"':
        return re.sub(r"\\(.)", lambda m: "\n" if m.group(1) == "n" else m.group(1), v[1:-1])
    return v


def parse_samples(text: str) -> list[tuple[str, dict, float, str]]:
    """(metric, labels, value, line) for every sample line of Prometheus text format."""
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = _SAMPLE.match(line)
        if not m:
            continue
        try:
            value = float(m.group(3))
        except ValueError:
            continue
        labels = {k: _unescape_label(v) for k, v in _LABEL.findall(m.group(2) or "")}
        out.append((m.group(1), labels, value, line))
    return out


def qualified(component: str, ns: str) -> str:
    return component if "." in component else f"{ns}.{component}"


def select_bean(samples, domain: str, bean_type: str, name: str) -> dict[str, tuple[float, str]]:
    """{attribute: (value, line)} for the bean `<domain>:type=<bean_type>,name="<name>"`."""
    prefix = domain_prefix(domain) + bean_type + "_"
    found = {}
    for metric, labels, value, line in samples:
        if metric.startswith(prefix) and "key" not in labels \
                and _unquote_objectname(labels.get("name", "")) == name:
            found[metric[len(prefix):]] = (value, line)
    return found


def select_rows(samples, domain: str, bean_type: str, name: str) -> dict[str, dict]:
    """{Map attribute: {entry key: (value, line)}} for the same bean: the exporter writes one line
    per Map entry, the entry's key in a `key` label."""
    prefix = domain_prefix(domain) + bean_type + "_"
    found = {}
    for metric, labels, value, line in samples:
        if metric.startswith(prefix) and "key" in labels \
                and _unquote_objectname(labels.get("name", "")) == name:
            found.setdefault(metric[len(prefix):], {})[labels["key"]] = (value, line)
    return found


def _render(want) -> str:
    if isinstance(want, dict):
        return " and ".join(f"{'>=' if k == 'min' else '<='} {v}" for k, v in want.items())
    return str(want)


def _matches(want, got: float) -> bool:
    if isinstance(want, dict):
        return want.get("min", got) <= got <= want.get("max", got)
    return got == float(want)   # True/False compare as 1.0/0.0, as the exporter renders them


def _evaluate(spec: dict, ns: str, scraped: dict[str, list], errors: dict[str, str]):
    domain, bean_type = spec["bean"]["domain"], spec["bean"]["type"]
    name = qualified(spec["bean"]["component"], ns)
    oname = f'{domain}:type={bean_type},name="{name}"'
    prefix = domain_prefix(domain)
    wanted = list(spec.get("attributes", {}).items()) + [
        (f"{a}[{k}]", w) for a, entries in spec.get("rows", {}).items() for k, w in entries.items()]
    hits = {url: (select_bean(s, domain, bean_type, name), select_rows(s, domain, bean_type, name))
            for url, s in scraped.items()}
    hits = {url: found for url, found in hits.items() if found[0] or found[1]}
    expected = {"kind": "rows", "rows": [{"attribute": a, "value": _render(w)} for a, w in wanted]}
    if len(hits) != 1:
        if hits:
            detail = (f"{oname} is exported by {len(hits)} JVMs ({sorted(hits)}); a per-node "
                      f"bean cannot be asserted as one value")
        else:
            nearby = [line for s in scraped.values() for _m, _l, _v, line in s
                      if line.startswith(prefix)][:20]
            detail = (f"bean {oname} not found on {sorted(scraped) or 'any endpoint'}"
                      + (f"; unreachable: {errors}" if errors else "")
                      + (f"; {prefix} lines seen: {nearby}" if nearby
                         else f"; no {prefix} lines at all"))
        return False, detail, oname, expected, {"kind": "rows", "rows": []}
    url, (attrs, maps) = next(iter(hits.items()))
    misses, rows = [], []

    def check(label, want, found, exported):
        if found is None:
            misses.append(f"{label}: not exported by {oname} at {url} (exported: {exported})")
            rows.append({"attribute": label, "value": "<absent>"})
            return
        got, line = found
        rows.append({"attribute": label, "value": str(got)})
        if not _matches(want, got):
            misses.append(f"{label}: shows {got}, expected {_render(want)} -- {line}")

    for attr, want in spec.get("attributes", {}).items():
        check(attr, want, attrs.get(attr), sorted(attrs))
    for attr, entries in spec.get("rows", {}).items():
        exported = sorted(maps.get(attr, {}))
        for key, want in entries.items():
            check(f"{attr}[{key}]", want, maps.get(attr, {}).get(key),
                  f"{attr} keys {exported}" if exported else sorted(maps))
    return (not misses), "; ".join(misses) or None, oname, expected, {"kind": "rows", "rows": rows}


def assert_jmx(specs: list[dict], ns: str, timeout: int, poll: float = 2.0, endpoints=None,
               fetch=_fetch, status_probe=None, progress=None) -> list[dict]:
    """Polls every exporter endpoint until each spec's bean shows every expected attribute,
    or the deadline expires. An endpoint that does not answer is reported, not skipped
    silently; a bean found nowhere is a failure, never a pass."""
    endpoints = exporter_endpoints() if endpoints is None else endpoints
    deadline = time.monotonic() + timeout
    while True:
        if status_probe:
            status_probe()
        scraped, errors = {}, {}
        for url in endpoints:
            try:
                scraped[url] = parse_samples(fetch(url))
            except Exception as e:
                errors[url] = f"{type(e).__name__}: {e}"
        results = [_evaluate(s, ns, scraped, errors) for s in specs]
        detail = next((d for ok, d, *_ in results if not ok), None)
        if detail is None or time.monotonic() >= deadline:
            records = [
                build_assertion_result(type="jmx", status="passed" if ok else "failed",
                                       spec=s, target=oname, db=None,
                                       detail="ok" if ok else d, expected=exp, actual=act)
                for s, (ok, d, oname, exp, act) in zip(specs, results)
            ]
            if detail is None:
                return records
            raise AssertionFailed(f"jmx assertion not satisfied within {timeout}s: {detail}",
                                  records)
        if progress:
            try:
                progress(timeout - (deadline - time.monotonic()), float(timeout))
            except Exception:
                pass
        time.sleep(poll or 0.05)
