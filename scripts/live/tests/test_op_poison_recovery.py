"""OP-loader poison recovery: decided from this case's log slice; restarts app nodes only on an owned stack."""

from types import SimpleNamespace

from livetest import plugin


def _cfg(ownership):
    infra = None if ownership is None else SimpleNamespace(ownership=ownership)
    return SimpleNamespace(_slt_infra=infra)


def test_exclusive_run_may_restart_app_nodes():
    assert plugin._op_poison_restart_refusal(_cfg("exclusive")) is None


def test_shared_and_undeclared_runs_refuse_the_restart():
    for mode in ("shared", None):
        why = plugin._op_poison_restart_refusal(_cfg(mode))
        assert why and "not restarted" in why and "--restart-app-nodes" in why
        assert f"SLT_INFRA_OWNERSHIP={mode or 'undeclared'}" in why

