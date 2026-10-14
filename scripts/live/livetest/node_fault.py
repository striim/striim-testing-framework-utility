"""Single Docker node faults and placement from Striim's live node metadata."""
from __future__ import annotations
import json
import math
import re
import shlex
import subprocess
import time
from urllib.parse import urlparse

from livetest import stack
from livetest.recovery import RecoveryError
from livetest.resultschema import build_assertion_result
from livetest.assertions import AssertionFailed

ROLES = {"primary", "secondary", "agent"}
_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_FLOW = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\.[A-Za-z_][A-Za-z0-9_]*\Z")
MAX_READ_CHARS = 65536


def _capture(reads, key, value):
    if reads is not None:
        from livetest.striim import redact
        text = redact(json.dumps(value, ensure_ascii=True, sort_keys=True))
        reads[key] = {"json": text[:MAX_READ_CHARS], "characters": len(text),
                      "truncated": len(text) > MAX_READ_CHARS}


def normalize(raw):
    if isinstance(raw, str) and raw in ROLES:
        return raw
    if isinstance(raw, dict) and set(raw) == {"hosting"} and isinstance(raw["hosting"], str) and raw["hosting"].strip():
        return dict(raw)
    raise ValueError("invalid-node-fault: node must be primary, secondary, agent or {hosting: named flow}")


def node_names(client, remaining, reads=None):
    raw = client.list_deployment_groups(timeout=remaining)
    _capture(reads, "deploymentGroups", raw)
    names = set()
    for item in raw[0].get("output", []) if isinstance(raw, list) and raw else []:
        for group in item.values():
            if not isinstance(group, dict): continue
            for server in group.get("actualServers", []):
                name = server.get("name")
                if not isinstance(name, str) or not _NAME.fullmatch(name):
                    raise ValueError("placement-node-name-unavailable")
                names.add(name)
    if not names:
        raise ValueError("placement-node-list-unavailable")
    return names


def deployed_flow(descriptions, flow):
    if not isinstance(descriptions, list) or not descriptions:
        raise ValueError("placement-node-description-unavailable")
    found = False
    for description in descriptions:
        objects = description.get("deployedObjects") if isinstance(description, dict) else None
        if not isinstance(objects, list): raise ValueError("placement-node-description-unavailable")
        for app in objects:
            components = app.get("components") if isinstance(app, dict) else None
            if not isinstance(components, list): raise ValueError("placement-components-unavailable")
            found |= any(c.get("entityType") == "FLOW" and c.get("name") == flow for c in components)
    return found


def flow_nodes(client, flow, remaining, reads=None):
    if not _FLOW.fullmatch(flow): raise ValueError("invalid-placement-flow")
    deadline = time.monotonic() + remaining
    names = node_names(client, remaining, reads)
    left = deadline - time.monotonic()
    if left <= 0: raise ValueError("placement-read-deadline")
    monitor = client.mon(flow, timeout=left)
    _capture(reads, "monitor", monitor)
    if monitor.get("status") != "RUNNING":
        return set()
    result = set()
    servers = {} if reads is not None else None
    if reads is not None:
        reads["servers"] = servers
    for name in sorted(names):
        left = deadline - time.monotonic()
        if left <= 0: raise ValueError("placement-read-deadline")
        descriptions = client.describe(name, timeout=left)
        _capture(servers, name, descriptions)
        if deployed_flow(descriptions, flow): result.add(name)
    if time.monotonic() > deadline: raise ValueError("placement-read-deadline")
    return result


def inventory(client, ctx, *, run=None, timeout=30):
    if ctx.mode != "docker": raise RecoveryError("invalid-node-fault: Docker mode required")
    deadline = time.monotonic() + timeout
    names = node_names(client, timeout)
    containers = {"primary": stack.striim_container(), "secondary": stack.app_nodes()[1], "agent": stack.agent_container()}
    result = {}
    for role, container in containers.items():
        left = deadline - time.monotonic()
        if left <= 0: raise RecoveryError("fault-node-inventory-deadline")
        call = run or (lambda argv: subprocess.run(argv, capture_output=True, text=True, timeout=left))
        r = call(["docker", "inspect", container])
        if r.returncode: raise RecoveryError("fault-node-inventory-unreadable")
        rows = json.loads(r.stdout)
        if len(rows) != 1: raise RecoveryError("fault-node-inventory-ambiguous")
        row = rows[0]
        if not row.get("State", {}).get("Running") or not row.get("Id"):
            raise RecoveryError("fault-node-not-running")
        env = row.get("Config", {}).get("Env", [])
        custom = []
        for entry in env:
            if entry.startswith("JAVA_SYSTEM_PROPERTIES="):
                custom += [v.split("=", 1)[1] for v in shlex.split(entry.split("=", 1)[1])
                           if v.startswith("-Dstriim.node.server.name=")]
        if custom:
            matches = set(custom) & names
        else:
            addresses = [v.get("IPAddress") for v in row.get("NetworkSettings", {}).get("Networks", {}).values()]
            prefixes = [("A" if role == "agent" else "S") + re.sub(r"[^A-Za-z0-9]+", "_", ip)
                        for ip in addresses if ip]
            matches = {name for name in names if any(name == p or name.startswith(p + "_") for p in prefixes)}
        if len(matches) != 1: raise RecoveryError("fault-node-unmapped: Docker node must map to one live Striim name")
        ports = row.get("NetworkSettings", {}).get("Ports", {}).get("9080/tcp") or []
        peer = None
        if ports and role != "agent":
            parsed = urlparse(ctx.url)
            host = parsed.hostname
            if not host: raise RecoveryError("fault-api-origin-unavailable")
            peer = f"{parsed.scheme}://{host}:{int(ports[0]['HostPort'])}"
        result[role] = {"role": role, "container": container, "id": row["Id"],
                        "server": next(iter(matches)), "url": peer}
    return result


def _reset_modules():
    from livetest import opregistry
    opregistry.clear()


def _wait_cluster(client, remaining):
    from livetest.striim_provision import wait_cluster_ready
    wait_cluster_ready(client, timeout=remaining)


class Target:
    def __init__(self, client, ctx, selection, *, app, nodes=None, run=None, record=None,
                 timeout=120, placement=None, wait_cluster=None):
        if ctx.mode != "docker": raise RecoveryError("invalid-node-fault: Docker mode required")
        self.client, self.ctx, self.app, self.timeout = client, ctx, app, timeout
        self.run, self.record = run, record
        self.nodes = nodes or inventory(client, ctx, run=run, timeout=timeout)
        self.refresh = nodes is None
        self._placement_reads = {}
        self.placement = placement or (lambda flow, left: flow_nodes(client, flow, left, self._placement_reads))
        self.wait_cluster = wait_cluster or _wait_cluster
        self.hosting = selection.get("hosting") if isinstance(selection, dict) else None
        if self.hosting:
            owners = self._owners(timeout, "owner-selection")
            candidates = [n for n in self.nodes.values() if n["server"] in owners]
            if len(owners) != 1 or len(candidates) != 1:
                self._refusal("fault-missed: hosting flow needs one running Docker owner", "owner-selection", owners)
                raise RecoveryError("fault-missed: hosting flow needs one running Docker owner")
            self.selected = candidates[0]
        else:
            self.selected = self.nodes[selection]
        peers = [n["url"] for role, n in self.nodes.items() if role != "agent" and role != self.selected["role"] and n["url"]]
        if not peers: raise RecoveryError("fault-fallback-unavailable: publish the surviving node API port")
        client.configure_failover(peers, app=app)
        self.needs_restore = False

    def _refusal(self, detail, phase, owners):
        if self.record:
            self.record(build_assertion_result(type="recovery", target=self.hosting, db=None,
                spec={"node": {"hosting": self.hosting}}, status="failed", detail=detail,
                expected={"kind": "count", "count": 1}, actual={"kind": "rows", "rows": [{
                    "phase": phase, "flow": self.hosting, "interrupted": False,
                    "owners": sorted(owners) if owners is not None else None,
                    "selected": getattr(self, "selected", {}).get("server"),
                    "dockerNodes": {role: {"server": node["server"], "containerId": node["id"]}
                                    for role, node in self.nodes.items()},
                    "reads": dict(self._placement_reads)}]}))

    def _owners(self, remaining, phase):
        self._placement_reads.clear()
        try:
            return self.placement(self.hosting, remaining)
        except Exception as error:
            self._refusal(str(error), phase, None)
            raise

    def check_owner(self, remaining):
        if self.hosting:
            owners = self._owners(remaining, "fault-boundary")
            if owners != {self.selected["server"]}:
                self._refusal("fault-missed: hosting flow moved before interruption", "fault-boundary", owners)
                raise RecoveryError("fault-missed: hosting flow moved before interruption")

    def _command(self, argv, remaining=None):
        r = (self.run or (lambda a: subprocess.run(a, capture_output=True, text=True,
            timeout=remaining or self.timeout)))(argv)
        if r.returncode: raise RecoveryError(f"node-fault-error: docker {argv[1]} rejected")
        return r

    def interrupt(self, mode):
        if mode not in ("kill", "stop", "restart"): raise RecoveryError("invalid-node-fault")
        node = self.selected
        self.needs_restore = True
        ok, detail = False, "ok"
        try:
            argv = ["docker", "kill", "-s", "KILL", node["id"]] if mode == "kill" else ["docker", mode, node["id"]]
            self._command(argv)
            state = self._command(["docker", "inspect", "--format", "{{.State.Running}}", node["id"]]).stdout.strip()
            if state != ("true" if mode == "restart" else "false"):
                raise RecoveryError("node-fault-error: requested container state not observed")
            if mode == "restart": self.needs_restore = False
            ok = True
        except Exception as e:
            detail = str(e)
            raise
        finally:
            if self.record:
                self.record(build_assertion_result(type="recovery", target=node["server"], db=None,
                    spec={"node": node["role"], "mode": mode}, status="passed" if ok else "failed", detail=detail,
                    expected={"kind": "count", "count": 1}, actual={"kind": "rows", "rows": [
                        {"node": node["server"], "role": node["role"], "containerId": node["id"],
                         "observedAt": time.monotonic(), "interrupted": ok}]}))

    def restore(self, expect_running=True, settle=0):
        deadline = time.monotonic() + self.timeout
        # A hosted server flow must demonstrably move while its owner is down.
        if expect_running and self.hosting and self.selected["role"] != "agent" and self.needs_restore:
            survivor = self.nodes["secondary" if self.selected["role"] == "primary" else "primary"]["server"]
            while True:
                left = deadline - time.monotonic()
                if left <= 0: raise RecoveryError("fault-node-no-failover")
                try:
                    if self.placement(self.hosting, left) == {survivor}: break
                except Exception:
                    pass
                time.sleep(min(.2, max(0, deadline - time.monotonic())))
        self.close(remaining=max(.05, deadline - time.monotonic()))
        if self.selected["role"] != "agent": _reset_modules()
        self.wait_cluster(self.client, max(.05, deadline - time.monotonic()))
        if self.refresh: self.nodes = inventory(self.client, self.ctx, run=self.run, timeout=max(.05, deadline - time.monotonic()))
        if expect_running:
            self.client.await_running(self.app, timeout=max(.05, deadline - time.monotonic()))
        status = self.client.current_status(self.app, timeout=max(.05, deadline - time.monotonic()))
        until = time.monotonic() + settle
        while time.monotonic() < until:
            if expect_running and status not in {"RUNNING", "COMPLETED"}:
                raise RecoveryError("fault-node-restore-status: " + status)
            time.sleep(min(.2, until - time.monotonic()))
            status = self.client.current_status(self.app, timeout=max(.05, until - time.monotonic()))
        return status

    def close(self, remaining=None):
        if self.needs_restore:
            self._command(["docker", "start", self.selected["id"]], remaining=remaining)
            self.needs_restore = False


def normalize_placement(raw, *, has_node_fault=False):
    from livetest.manifest import _parse_zeroable_duration, ManifestError
    if not isinstance(raw, list) or not raw: raise ValueError("invalid-placement-spec: nonempty list required")
    for s in raw:
        if not isinstance(s, dict) or set(s) - {"flow", "node", "timeout"} or not all(isinstance(s.get(k), str) and s[k] for k in ("flow", "node")):
            raise ValueError("invalid-placement-spec: flow and node required, unknown keys refused")
        if s["node"] == "survivor" and not has_node_fault: raise ValueError("invalid-placement-spec: survivor requires recover.node")
        if "timeout" in s:
            try:
                seconds = _parse_zeroable_duration(s["timeout"], "placement.timeout")
            except ManifestError as e:
                raise ValueError("invalid-placement-spec: positive timeout required") from e
            if not math.isfinite(seconds) or seconds <= 0: raise ValueError("invalid-placement-spec: positive timeout required")
    return raw


def assert_placement(specs, *, client, ctx, tokens, timeout, target=None, read=None, now=time.monotonic, sleep=time.sleep):
    from livetest.manifest import _parse_zeroable_duration
    from livetest.substitute import render
    records = []
    for spec in specs:
        flow = render(spec["flow"], tokens)
        if not flow.startswith(tokens["NS"] + ".") or not _FLOW.fullmatch(flow):
            raise ValueError("invalid-placement-flow: flow must belong to the case namespace")
        node = render(spec["node"], tokens)
        if node == "survivor":
            if target is None or target.selected["role"] == "agent": raise ValueError("invalid-placement-spec: no app-node survivor")
            node = target.nodes["secondary" if target.selected["role"] == "primary" else "primary"]["server"]
        elif node in ROLES:
            node = (target.nodes if target else inventory(client, ctx))[node]["server"]
        if not _NAME.fullmatch(node): raise ValueError("invalid-placement-node")
        deadline = now() + _parse_zeroable_duration(spec.get("timeout", timeout), "placement.timeout")
        actual, ok, detail = set(), False, "placement-mismatch"
        while now() < deadline:
            try:
                actual = (read or (lambda flow, left: flow_nodes(client, flow, left)))(flow, deadline - now())
                if not isinstance(actual, set): raise ValueError("placement-read-shape")
                ok = now() <= deadline and actual == {node}
            except Exception as e:
                detail = f"placement-read-error: {e}"
                break
            if ok: break
            sleep(max(0, min(.2, deadline - now())))
        records.append(build_assertion_result(type="placement", target=flow, db=None, spec=spec,
            status="passed" if ok else "failed", detail="ok" if ok else detail,
            expected={"kind": "rows", "rows": [{"node": node}]},
            actual={"kind": "rows", "rows": [{"node": n} for n in sorted(actual)]}))
        if not ok: raise AssertionFailed(detail, records)
    return records
