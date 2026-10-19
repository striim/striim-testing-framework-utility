"""Disposable Postgres budgets apply to data roles in their test database only."""
from pathlib import Path
import re
import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]

@pytest.mark.parametrize('tier,setting,service', [
    ('live', 'SLT_PG_IDLE_TRANSACTION_TIMEOUT_MS', 'slt-postgres'),
    ('integration', 'INT_PG_IDLE_TRANSACTION_TIMEOUT_MS', 'int-postgres'),
])
def test_compose_policy_changes_do_not_restart_kept_service(tier, setting, service):
    text = (ROOT / 'scripts' / tier / 'services/postgres/compose.yaml').read_text()
    command = yaml.safe_load(text)['services'][service]['command']
    assert setting not in ' '.join(command)
    # This custom input must not set a server-wide idle timeout for unrelated users.
    assert not re.search(r'(?<![a-z_])idle_in_transaction_session_timeout=', ' '.join(command))

@pytest.mark.parametrize('file', [
    'live/services/postgres/init-qausers.sql',
    'integration/services/postgres/sql/init.sql',
])
def test_init_sets_only_data_role_database_defaults(file):
    text = (ROOT / 'scripts' / file).read_text()
    assert "current_setting('slt.idle_transaction_timeout_ms', true)" in text
    assert "IF budget IS NULL THEN" in text
    assert "ARRAY['qasource', 'qatarget']" in text
    assert 'ALTER ROLE %I IN DATABASE %I SET idle_in_transaction_session_timeout = %L' in text
    assert 'current_database()' in text
    assert 'invalid-postgres-idle-transaction-budget' in text
    assert '2147483647' in text
    assert not re.search(r'ALTER\s+(?:SYSTEM|DATABASE)', text, re.I)

