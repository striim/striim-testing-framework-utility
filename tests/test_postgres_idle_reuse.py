"""Bring-up must reconcile kept Postgres role policy without restarting it."""
from types import SimpleNamespace
from pathlib import Path
import pytest

@pytest.mark.parametrize('tier', ['live', 'integration'])
def test_kept_stack_applies_changed_budget(monkeypatch, tier):
    from livetest import services as live
    from inttest import services as integration
    mod = live if tier == 'live' else integration
    key = 'SLT_PG_IDLE_TRANSACTION_TIMEOUT_MS' if tier == 'live' else 'INT_PG_IDLE_TRANSACTION_TIMEOUT_MS'
    calls = []
    monkeypatch.setattr(mod.subprocess, 'run', lambda argv, **kw: calls.append(argv) or SimpleNamespace(returncode=0))
    if tier == 'live':
        monkeypatch.setenv('SLT_KEEP_SERVICES', '1')
        monkeypatch.setattr(live, '_preflight_profile', lambda d: None)
        monkeypatch.setattr(live, '_compose_env', lambda: dict(__import__('os').environ))
        bring_up = lambda: live._default_compose_up(SimpleNamespace(name='postgres', dir=Path('/service'), compose='compose.yaml'))
    else:
        monkeypatch.setattr(integration, '_checked_compose_file', lambda s: Path('/service/compose.yaml'))
        monkeypatch.setattr(integration, '_compose_up_env', lambda s: dict(__import__('os').environ))
        bring_up = lambda: integration.compose_up('postgres', None)
    for value in ('600000', '1800000'):
        monkeypatch.setenv(key, value)
        bring_up()
        assert 'exec' in calls[-1]
        sql = calls[-1][-1]
        assert f"'{value}ms'" in sql
        assert sql.count('ALTER ROLE') == 2
        assert 'IN DATABASE' in sql
    assert not any('down' in c for c in calls)

@pytest.mark.parametrize('tier', ['live', 'integration'])
@pytest.mark.parametrize('value', ['10min', '0', '-1', '2147483648', '99999999999'])
def test_invalid_budget_never_runs_compose(monkeypatch, tier, value):
    from livetest import services as live
    from inttest import services as integration
    mod = live if tier == 'live' else integration
    key = 'SLT_PG_IDLE_TRANSACTION_TIMEOUT_MS' if tier == 'live' else 'INT_PG_IDLE_TRANSACTION_TIMEOUT_MS'
    monkeypatch.setenv(key, value)
    monkeypatch.setenv('SLT_KEEP_SERVICES', '1')
    calls = []
    monkeypatch.setattr(mod.subprocess, 'run', lambda argv, **kw: calls.append(argv) or SimpleNamespace(returncode=0))
    monkeypatch.setattr(live, '_preflight_profile', lambda d: None)
    monkeypatch.setattr(live, '_compose_env', lambda: dict(__import__('os').environ))
    monkeypatch.setattr(integration, '_checked_compose_file', lambda s: Path('/service/compose.yaml'))
    monkeypatch.setattr(integration, '_compose_up_env', lambda s: dict(__import__('os').environ))
    with pytest.raises(ValueError, match=f'invalid-postgres-idle-transaction-budget: {key}'):
        if tier == 'live':
            live._default_compose_up(SimpleNamespace(name='postgres', dir=Path('/service'), compose='compose.yaml'))
        else:
            integration.compose_up('postgres', None)
    assert calls == []
