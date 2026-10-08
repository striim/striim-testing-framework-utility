from __future__ import annotations
from dataclasses import dataclass, field

@dataclass
class Topology:
    groups: dict = field(default_factory=dict)
    has_cluster: bool = False
    has_agent: bool = False

def parse_deployment_groups(raw: list) -> Topology:
    groups: dict = {}
    output = []
    if isinstance(raw, list) and raw and isinstance(raw[0], dict):
        output = raw[0].get("output", []) or []
    for item in output:
        if not isinstance(item, dict):
            continue
        for _, g in item.items():
            if not isinstance(g, dict):
                continue
            name = g.get("name")
            if not name:
                continue
            uuids = [s.get("uuid") for s in g.get("actualServers", [])
                     if isinstance(s, dict) and s.get("uuid")]
            groups[name] = uuids
    return Topology(
        groups=groups,
        has_cluster=len(groups.get("default", [])) >= 2,
        has_agent=len(groups.get("Agents", [])) >= 1,
    )

def topology_satisfies(required: str, t: Topology) -> tuple[bool, str]:
    # Two topologies in this framework: "single" (native Mac Striim) and "cluster"
    # (the full Docker topology — >=2 nodes AND a registered agent, always provisioned
    # together). "agent" is accepted as a legacy alias for "cluster".
    if required == "single":
        return True, ""
    if required in ("cluster", "agent"):
        ok = t.has_cluster and t.has_agent
        return (ok, "" if ok else
                "requires the Striim cluster (>=2 nodes + a registered agent); "
                "resolved Striim is single-node")
    return False, f"unknown topology {required!r}"
