from types import SimpleNamespace
import pytest
from livetest.assertions import AssertionFailed


def test_placement_requires_node_and_named_flow_and_refuses_unknown_keys():
    from livetest.node_fault import normalize_placement
    for spec in [{"flow": "N.flow"}, {"flow": "N.flow", "node": "primary", "extra": True},
                 {"flow": "N.flow", "node": "survivor"}]:
        with pytest.raises(ValueError, match="invalid-placement-spec"):
            normalize_placement([spec], has_node_fault=False)


def test_placement_exact_single_node_passes():
    from livetest.node_fault import assert_placement
    spec = [{"flow": "${NS}.${TID}flow", "node": "server_secondary"}]
    records = assert_placement(spec, client=None, ctx=SimpleNamespace(mode="native"),
        tokens={"NS": "N", "TID": ""}, timeout=1, read=lambda *args: {"server_secondary"})
    assert records[0]["type"] == "placement" and records[0]["status"] == "passed"


@pytest.mark.parametrize("actual", [{"server_primary"}, {"server_primary", "server_secondary"}, set()])
def test_placement_wrong_missing_or_duplicated_owner_fails(actual):
    from livetest.node_fault import assert_placement
    clock = [0]
    def sleep(seconds): clock[0] += seconds
    with pytest.raises(AssertionFailed, match="placement-mismatch"):
        assert_placement([{"flow": "${NS}.flow", "node": "server_secondary"}], client=None,
            ctx=SimpleNamespace(mode="native"), tokens={"NS": "N"}, timeout=1,
            read=lambda *args: set(actual), now=lambda: clock[0], sleep=sleep)


def test_survivor_is_the_other_app_node_not_the_restored_node():
    from livetest.node_fault import assert_placement
    nodes = {role: {"role": role, "server": "server_" + role} for role in ["primary", "secondary", "agent"]}
    target = SimpleNamespace(nodes=nodes, selected=nodes["primary"])
    records = assert_placement([{"flow": "${NS}.flow", "node": "survivor"}], client=None,
        ctx=SimpleNamespace(mode="docker"), tokens={"NS": "N"}, target=target,
        timeout=1, read=lambda *args: {"server_secondary"})
    assert records[0]["expected"]["rows"] == [{"node": "server_secondary"}]


@pytest.mark.parametrize('timeout', ['unknown', -1, 0, True])
def test_invalid_placement_timeout_has_stable_code(timeout):
    from livetest.node_fault import normalize_placement
    with pytest.raises(ValueError, match='invalid-placement-spec'):
        normalize_placement([{'flow': 'N.flow', 'node': 'primary', 'timeout': timeout}])
