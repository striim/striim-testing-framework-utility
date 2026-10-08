from livetest.services import _classify_compose_phase

def test_classify_compose_phase():
    assert _classify_compose_phase(" slt-postgres Pulling ") == "pulling image"
    assert _classify_compose_phase("Step 3/10 : RUN apt-get ...") == "building image"
    assert _classify_compose_phase(" Container slt-postgres  Creating") == "creating container"
    assert _classify_compose_phase(" Container slt-postgres  Started") == "starting container"
    assert _classify_compose_phase(" Container slt-postgres  Waiting") == "waiting for healthy"
    assert _classify_compose_phase(" Container slt-postgres  Healthy") == "waiting for healthy"
    assert _classify_compose_phase("random noise line") is None


def test_zookeeper_healthcheck_checks_a_protocol_response():
    import yaml
    from livetest.registry import load_service
    defn = load_service('kafka')
    config = yaml.safe_load((defn.dir / defn.compose).read_text())
    check = config['services']['slt-zookeeper']['healthcheck']['test']
    assert check[0] == 'CMD-SHELL'
    assert 'imok' in check[1] and 'grep' in check[1]
    environment = config['services']['slt-zookeeper']['environment']
    assert environment['KAFKA_OPTS'] == '-Dzookeeper.4lw.commands.whitelist=ruok'
    assert 'ZOOKEEPER_4LW_COMMANDS_WHITELIST' not in environment


def test_preflight_command_streams_phases_and_returns_first_failure(tmp_path):
    import sys
    from livetest.services import _run_preflight_compose
    from livetest.service_healing import RecoveryContext
    ctx = RecoveryContext(tmp_path, 'run', 'op', 'lane')
    progress = []
    result = _run_preflight_compose(
        [sys.executable, '-c', "print('Container broker Waiting'); print('first failure'); exit(1)"],
        {}, ctx, 'kafka', lambda *args: progress.append(args))
    assert result.returncode == 1
    assert 'first failure' in result.stdout
    assert progress == [('kafka', 'waiting for healthy')]


def test_preflight_initial_cancellation_terminates_and_waits_owned_command(tmp_path):
    import sys
    import pytest
    from livetest.services import _run_preflight_compose, ServiceError
    from livetest.service_healing import RecoveryContext
    ctx = RecoveryContext(tmp_path, 'run', 'op', 'lane', cancelled=lambda: True)
    with pytest.raises(ServiceError, match='cancelled'):
        _run_preflight_compose([sys.executable, '-c', 'import time; time.sleep(60)'],
                               {}, ctx, 'kafka', None)
    assert '"healed": false' in (tmp_path / 'preflight-summary.json').read_text()


def test_initial_compose_deadline_is_bounded(tmp_path):
    import sys
    import pytest
    from livetest.services import _run_preflight_compose, ServiceError
    from livetest.service_healing import RecoveryContext
    class Clock:
        calls = 0
        def __call__(self):
            self.calls += 1
            return 0 if self.calls < 4 else 901
    clock = Clock()
    ctx = RecoveryContext(tmp_path, 'run', 'op', 'lane', clock=clock)
    with pytest.raises(ServiceError, match='deadline'):
        _run_preflight_compose([sys.executable, '-c', 'import time; time.sleep(60)'],
                               {}, ctx, 'kafka', None)
