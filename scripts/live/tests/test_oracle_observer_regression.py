"""The shipped observer case proves both probe tagging and writer visibility."""
from pathlib import Path
from livetest.manifest import load_manifest

ROOT = Path(__file__).resolve().parents[3]
CASE = ROOT/'scripts/live/regression/services/oracle/oracle-observer'


def test_regression_proves_tagged_probes_and_unfiltered_writer_logons():
    manifest = load_manifest(CASE/'test.yaml')
    checks = manifest.assert_['data']
    for suffix in ('PROBES', 'WRITERS'):
        assert any(c['target'].endswith(suffix) and c.get('min_rows') == 1 for c in checks)
    assert not any(c['target'].endswith('SESSIONS') for c in checks)
    assert manifest.disabled_parallel and 'PDB' in manifest.disabled_parallel
    trigger = (CASE/'observer_trigger.sql').read_text()
    assert "<> 'striim-test-observer'" not in trigger
    assert 'EXECUTE IMMEDIATE' in trigger and 'EXCEPTION WHEN OTHERS' in trigger
    views = (CASE/'ddl_target.sql').read_text()
    assert "WHERE PROGRAM = 'striim-test-observer'" in views
    assert "WHERE NVL(PROGRAM, '?') <> 'striim-test-observer'" in views


def test_documented_trigger_survives_missing_log_table():
    section = (ROOT/'docs/SERVICES.md').read_text().split('Then `observer_trigger.sql`',1)[1].split('```sql',1)[1].split('```',1)[0]
    assert 'EXECUTE IMMEDIATE' in section
    assert 'EXCEPTION WHEN OTHERS' in section and 'ROLLBACK;' in section
