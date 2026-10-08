from livetest.topology import parse_deployment_groups, Topology, topology_satisfies

def _resp(groups):
    # groups: {name: [uuids]}
    out = [{f"deploymentgroup{i}": {"name": n,
            "actualServers": [{"uuid": u, "name": ""} for u in uu]}}
           for i, (n, uu) in enumerate(groups.items(), 1)]
    return [{"command": "LIST DEPLOYMENTGROUPS", "executionStatus": "Success",
             "output": out, "responseCode": 200}]

def test_single_node():
    t = parse_deployment_groups(_resp({"default": ["u1"], "Agents": []}))
    assert t.has_cluster is False and t.has_agent is False
    assert t.groups["default"] == ["u1"]

def test_cluster_with_agent():
    t = parse_deployment_groups(_resp({"default": ["u1", "u2"], "Agents": ["a1"]}))
    assert t.has_cluster is True and t.has_agent is True

def test_ignores_empty_uuid_placeholders():
    raw = [{"command": "x", "output": [
        {"g": {"name": "Agents", "actualServers": [{"uuid": "", "name": ""},
                                                    {"uuid": "a1", "name": ""}]}}]}]
    t = parse_deployment_groups(raw)
    assert t.groups["Agents"] == ["a1"] and t.has_agent is True

def test_single_always_ok():
    ok, _ = topology_satisfies("single", parse_deployment_groups(_resp({"default": ["u1"]})))
    assert ok

def test_cluster_requires_nodes_and_agent():
    neither = parse_deployment_groups(_resp({"default": ["u1"], "Agents": []}))
    assert not topology_satisfies("cluster", neither)[0]
    nodes_no_agent = parse_deployment_groups(_resp({"default": ["u1", "u2"], "Agents": []}))
    assert not topology_satisfies("cluster", nodes_no_agent)[0]      # needs an agent too
    full = parse_deployment_groups(_resp({"default": ["u1", "u2"], "Agents": ["a1"]}))
    ok, why = topology_satisfies("cluster", full)
    assert ok and why == ""

def test_ignores_non_dict_group_values():
    raw = [{"command": "x", "output": [
        {"g1": None,
         "g2": {"name": "Agents", "actualServers": [{"uuid": "a1", "name": ""}]}}]}]
    t = parse_deployment_groups(raw)
    assert t.groups == {"Agents": ["a1"]}
    assert t.has_agent is True

def test_agent_is_alias_for_cluster():
    single = parse_deployment_groups(_resp({"default": ["u1"], "Agents": []}))
    assert not topology_satisfies("agent", single)[0]
    full = parse_deployment_groups(_resp({"default": ["u1", "u2"], "Agents": ["a1"]}))
    assert topology_satisfies("agent", full)[0]   # "agent" behaves like "cluster"
