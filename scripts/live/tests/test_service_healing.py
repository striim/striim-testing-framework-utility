"""K1 policy tests: all time, Docker and protocol I/O are simulated."""
import json
from copy import deepcopy
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from livetest import service_healing as h


# Source-backed v7.6.1 getAfterNodeExists diagnostic plus registration traceback.
# https://github.com/confluentinc/kafka/blob/v7.6.1/core/src/main/scala/kafka/zk/KafkaZkClient.scala
REGISTRATION_LOG = """ERROR Error while creating ephemeral at /brokers/ids/1, node already exists and owner '0x2a' does not match current session '0x2b' (kafka.zk.KafkaZkClient$CheckedEphemeral)
ERROR [KafkaServer id=1] Fatal error during KafkaServer startup. Prepare to shutdown
org.apache.zookeeper.KeeperException$NodeExistsException: KeeperErrorCode = NodeExists
    at org.apache.zookeeper.KeeperException.create(KeeperException.java:126)
    at kafka.zk.KafkaZkClient$CheckedEphemeral.getAfterNodeExists(KafkaZkClient.scala:2186)
    at kafka.zk.KafkaZkClient$CheckedEphemeral.create(KafkaZkClient.scala:2127)
    at kafka.zk.KafkaZkClient.checkedEphemeralCreate(KafkaZkClient.scala:2094)
    at kafka.zk.KafkaZkClient.registerBroker(KafkaZkClient.scala:106)
    at kafka.server.KafkaServer.startup(KafkaServer.scala:365)
"""
API_VERSIONS = """localhost:9092 (id: 1 rack: null) -> (
    Produce(0): 0 to 9 [usable: 9],
    ApiVersions(18): 0 to 3 [usable: 3]
)
"""


class Clock:
    now = 0
    def __call__(self):
        return self.now
    def sleep(self, seconds):
        self.now += seconds


class Evidence:
    def __init__(self):
        self.identity = {'broker': {'id': 'b', 'image': 'sha:b', 'config': 'c', 'mounts': []},
                         'zookeeper': {'id': 'z', 'image': 'sha:z', 'config': 'c', 'mounts': []},
                         'registry': {'id': 'r', 'image': 'sha:r', 'config': 'c', 'mounts': []}}
        self.state = 'exited'
        self.oom = False
        self.signature = True
        self.owners = iter([42, None])
        self.last_owner = 42
        self.ready_values = iter([True])
        self.actions = []
        self.guards = []
        self.guard = True
        self.registry_stopped = True
    def snapshot(self, stopped=True):
        if stopped and (self.state != 'exited' or self.oom):
            raise h.Refused('broker not exited or OOM')
        return deepcopy(self.identity)
    def registration_failure(self, since):
        return self.signature
    def owner(self):
        self.last_owner = next(self.owners, self.last_owner)
        return self.last_owner
    def authorize(self, evidence):
        self.guards.append(evidence)
        return self.guard
    def start_broker(self):
        self.actions.append('broker')
        self.state = 'running'
    def start_registry(self):
        if self.registry_stopped:
            self.actions.append('registry')
    def registry_needs_start(self):
        return self.registry_stopped
    def ready(self):
        return next(self.ready_values, True)


@pytest.fixture
def episode(tmp_path):
    clock, evidence = Clock(), Evidence()
    ctx = h.RecoveryContext(tmp_path, 'run', 'operation', 'lane',
                            maintenance_guard=evidence.authorize, clock=clock, sleep=clock.sleep)
    return ctx, evidence, clock


def recover(episode):
    ctx, evidence, _ = episode
    return h.recover_kafka(ctx, evidence, datetime.now(timezone.utc), 'first failure')


def events(ctx):
    return [json.loads(s) for s in (ctx.evidence_dir / 'preflight-healing.jsonl').read_text().splitlines()]


def test_expiry_starts_once_and_retains_first_failure(episode):
    ctx, evidence, clock = episode
    assert recover(episode)
    assert evidence.actions == ['broker', 'registry']
    assert len(evidence.guards) == 2
    assert clock.now == 5
    records = events(ctx)
    assert [e['event'] for e in records] == ['attempt_failed', 'classified', 'waiting',
                                           'attempt_started', 'attempt_started', 'healed']
    assert all(e['original_error'] == 'first failure' for e in records)
    assert json.loads((ctx.evidence_dir / 'preflight-summary.json').read_text())['healed'] is True
    with pytest.raises(h.Refused):
        recover(episode)
    assert evidence.actions == ['broker', 'registry']


@pytest.mark.parametrize('owners,reason', [([42]*100, 'expiry'), ([0], 'persistent'),
                                         ([42, 43], 'owner'), ([None], 'ephemeral')])
def test_no_expiry_persistent_owner_change_or_missing_initial_proof(episode, owners, reason):
    ctx, evidence, clock = episode
    evidence.owners = iter(owners)
    with pytest.raises(h.Refused, match=reason):
        recover(episode)
    assert not evidence.actions
    assert clock.now <= 120
    assert not json.loads((ctx.evidence_dir / 'preflight-summary.json').read_text())['healed']


@pytest.mark.parametrize('field,value', [('state', 'running'), ('oom', True), ('signature', False),
                                        ('guard', False)])
def test_missing_evidence_active_oom_or_no_authority_never_starts(episode, field, value):
    ctx, evidence, clock = episode
    setattr(evidence, field, value)
    with pytest.raises(h.Refused):
        recover(episode)
    assert evidence.actions == []


def test_no_guard_diagnoses_but_refuses(episode):
    ctx, evidence, _ = episode
    ctx.maintenance_guard = None
    with pytest.raises(h.Refused, match='maintenance'):
        recover(episode)
    assert not evidence.actions
    assert any(e['event'] == 'classified' for e in events(ctx))


def test_cancel_between_classification_and_action(episode):
    ctx, evidence, clock = episode
    ctx.cancelled = lambda: clock.now >= 5
    with pytest.raises(h.Refused, match='cancelled'):
        recover(episode)
    assert not evidence.actions
    assert events(ctx)[-1]['event'] == 'cancelled'


def test_identity_change_during_wait_refuses(episode):
    ctx, evidence, clock = episode
    def sleep(seconds):
        clock.sleep(seconds)
        evidence.identity['broker']['image'] = 'changed'
    ctx.sleep = sleep
    with pytest.raises(h.Refused, match='identity'):
        recover(episode)
    assert not evidence.actions


def test_readiness_deadline_not_just_running(episode):
    ctx, evidence, clock = episode
    evidence.ready_values = iter([False]*100)
    with pytest.raises(h.Refused, match='readiness'):
        recover(episode)
    assert clock.now <= 300
    assert events(ctx)[-1]['event'] == 'exhausted'
    assert 'healed' not in [e['event'] for e in events(ctx)]


def test_probe_timeout_refuses_without_action(episode):
    ctx, evidence, _ = episode
    evidence.owner = lambda: (_ for _ in ()).throw(TimeoutError('probe timeout'))
    with pytest.raises(h.Refused, match='probe timeout'):
        recover(episode)
    assert not evidence.actions


def test_evidence_write_failure_prevents_mutation(episode, monkeypatch):
    ctx, evidence, _ = episode
    monkeypatch.setattr(ctx, 'emit', lambda *a, **k: (_ for _ in ()).throw(OSError('disk full')))
    with pytest.raises(OSError):
        recover(episode)
    assert not evidence.actions


@pytest.mark.parametrize('log,expected', [
    ('KeeperException$NodeExistsException: KeeperErrorCode = NodeExists for /brokers/ids/1', True),
    ('KeeperException$NodeExistsException: /unrelated', False),
    ('KeeperException$NodeExistsException: /brokers/ids/10', False),
    ('old KeeperException$NodeExistsException', False),
    ('/brokers/ids/1 already registered', False),
])
def test_exact_registration_signature(log, expected):
    assert h.registration_signature(log) is expected


def test_zk_stat_requires_protocol_and_explicit_absence():
    assert h.parse_owner('WATCHER::\nSyncConnected\nephemeralOwner = 0x2a') == 42
    assert h.parse_owner('SyncConnected\nNode does not exist: /brokers/ids/1') is None
    for text in ('connection refused', 'Node does not exist: /other', 'ephemeralOwner = 0x2a'):
        with pytest.raises(h.Refused):
            h.parse_owner(text)


@pytest.fixture
def docker_episode(tmp_path):
    from livetest.registry import load_service
    defn = load_service('kafka')
    clock = Clock()
    ctx = h.RecoveryContext(tmp_path, 'run', 'op', 'lane', clock=clock, sleep=clock.sleep)
    now = datetime.now(timezone.utc)
    role_images = {'slt-kafka': 'confluentinc/cp-kafka:7.6.1',
                   'slt-zookeeper': 'confluentinc/cp-zookeeper:7.6.1',
                   'slt-schema-registry': 'confluentinc/cp-schema-registry:7.6.1'}
    envs = {'slt-kafka': {'KAFKA_BROKER_ID': '1', 'KAFKA_ZOOKEEPER_CONNECT': 'lane-zk:2181'},
            'slt-zookeeper': {}, 'slt-schema-registry': {}}
    profile = {'name': 'lane-kafka', 'services': {svc: {'image': image,
               'container_name': f'lane-{svc}', 'environment': envs[svc]}
               for svc, image in role_images.items()}}
    containers = [dict(Id=svc+'-id', Name='/lane-'+svc, Image='sha:'+svc,
                       Config=dict(Image=image, Env=[f'{k}={v}' for k, v in envs[svc].items()],
                                   Labels={'com.docker.compose.project': 'lane-kafka',
                                           'com.docker.compose.service': svc,
                                           'com.docker.compose.config-hash': svc+'-hash'}),
                       State=dict(Status='running' if svc == 'slt-zookeeper' else 'exited',
                                  Running=svc == 'slt-zookeeper', OOMKilled=False,
                                  StartedAt=now.isoformat()),
                       Mounts=[{'Type': 'volume', 'Name': svc+'-data', 'Source': '/data/'+svc}])
                  for svc, image in role_images.items()]
    calls, mutations, guards = [], [], []
    reads = 0
    log = ''.join(now.isoformat()+' '+line+'\n' for line in REGISTRATION_LOG.splitlines())
    def run(argv, **kwargs):
        nonlocal reads
        calls.append((argv, kwargs))
        assert 0 < kwargs['timeout'] <= 5
        output, rc = '', 0
        if argv[:2] == ['docker', 'compose']:
            output = ('\n'.join(svc+' '+svc+'-hash' for svc in role_images)
                      if '--hash' in argv else json.dumps(profile))
        elif argv[:3] == ['docker', 'container', 'inspect']:
            output = json.dumps(containers)
        elif argv[:3] == ['docker', 'image', 'inspect']:
            svc = next(s for s, image in role_images.items() if image == argv[-1])
            output = json.dumps([{'Id': 'sha:'+svc}])
        elif argv[:2] == ['docker', 'ps']:
            output = '\n'.join(c['Id'] for c in containers)
        elif argv[:2] == ['docker', 'logs']:
            output = log
        elif argv[:2] == ['docker', 'start']:
            mutations.append(argv)
            c = next(c for c in containers if c['Id'] == argv[2])
            c['State'].update(Status='running', Running=True)
        elif 'zookeeper-shell' in argv:
            reads += 1
            present = reads == 1 or mutations
            output = ('SyncConnected\nephemeralOwner = 0x2a' if present
                      else 'SyncConnected\nNode does not exist: /brokers/ids/1')
            rc = 0 if present else 1
        elif 'kafka-broker-api-versions' in argv:
            output = API_VERSIONS
        elif 'curl' in argv:
            output = '[]'
        else:
            raise AssertionError(argv)
        return SimpleNamespace(stdout=output, stderr='', returncode=rc)
    adapter = h.DockerEvidence(ctx, defn, {}, run=run)
    def guard(identity):
        guards.append(deepcopy(identity))
        return True
    ctx.maintenance_guard = guard
    return SimpleNamespace(ctx=ctx, adapter=adapter, since=now, calls=calls, containers=containers,
                           profile=profile, mutations=mutations, guards=guards, run=run)


def test_real_adapter_command_history_preserves_mounts(docker_episode):
    d = docker_episode
    before = deepcopy([c['Mounts'] for c in d.containers])
    assert h.recover_kafka(d.ctx, d.adapter, d.since, 'initial NodeExists failure')
    assert d.mutations == [['docker', 'start', 'slt-kafka-id'],
                           ['docker', 'start', 'slt-schema-registry-id']]
    assert len(d.guards) == len(d.mutations)
    assert before == [c['Mounts'] for c in d.containers]
    commands = [argv for argv, _ in d.calls]
    assert not any(word in argv for argv in commands
                   for word in ('down', '-v', 'restart', 'rm', 'delete', 'up', '--build', '--force-recreate'))
    assert (d.ctx.evidence_dir / 'kafka-first-failure.log').exists()
    assert all('Env' not in json.dumps(event) for event in events(d.ctx))


@pytest.mark.parametrize('condition', ['foreign', 'active', 'oom', 'old', 'config', 'image', 'duplicate'])
def test_adapter_rejects_unsafe_identity_and_old_start(docker_episode, condition):
    d = docker_episode
    broker = d.containers[0]
    if condition == 'foreign':
        broker['Config']['Labels']['com.docker.compose.project'] = 'foreign'
    elif condition == 'active':
        broker['State'].update(Status='running', Running=True)
    elif condition == 'oom':
        broker['State']['OOMKilled'] = True
    elif condition == 'old':
        broker['State']['StartedAt'] = '2020-01-01T00:00:00Z'
    elif condition == 'config':
        broker['Config']['Labels']['com.docker.compose.config-hash'] = 'old-hash'
    elif condition == 'image':
        broker['Image'] = 'stale-image-id'
    elif condition == 'duplicate':
        extra = deepcopy(broker)
        extra.update(Id='duplicate', Name='/extra')
        d.containers.append(extra)
    with pytest.raises(h.Refused):
        h.recover_kafka(d.ctx, d.adapter, d.since, 'failed')
    assert not d.mutations


def test_old_log_cannot_supply_signature(docker_episode):
    d = docker_episode
    run = d.adapter.run
    def old_log(argv, **kw):
        if argv[:2] == ['docker', 'logs']:
            return SimpleNamespace(returncode=0, stderr='', stdout=(
                ''.join('2020-01-01T00:00:00Z '+line+'\n' for line in REGISTRATION_LOG.splitlines())))
        return run(argv, **kw)
    d.adapter.run = old_log
    with pytest.raises(h.Refused, match='fresh'):
        h.recover_kafka(d.ctx, d.adapter, d.since, 'failed')
    assert not d.mutations


@pytest.mark.parametrize('command', ['zookeeper-shell', 'kafka-broker-api-versions', 'curl'])
def test_all_protocols_must_pass(docker_episode, command):
    d = docker_episode
    run = d.adapter.run
    def failed_probe(argv, **kw):
        if command in argv:
            return SimpleNamespace(returncode=1, stderr='unavailable', stdout='')
        return run(argv, **kw)
    d.adapter.run = failed_probe
    with pytest.raises(h.Refused):
        h.recover_kafka(d.ctx, d.adapter, d.since, 'failed')
    assert not json.loads((d.ctx.evidence_dir / 'preflight-summary.json').read_text())['healed']


def test_guard_rechecked_before_registry_mutation(episode):
    ctx, evidence, _ = episode
    ctx.maintenance_guard = lambda identity: not evidence.actions
    with pytest.raises(h.Refused, match='maintenance'):
        recover(episode)
    assert evidence.actions == ['broker']


def test_cancel_after_intent_prevents_command(episode):
    ctx, evidence, _ = episode
    emit = ctx.emit
    def cancel_on_intent(event, *args, **kw):
        emit(event, *args, **kw)
        if event == 'attempt_started':
            ctx.cancelled = lambda: True
    ctx.emit = cancel_on_intent
    with pytest.raises(h.Refused, match='cancelled'):
        recover(episode)
    assert not evidence.actions


def test_fresh_job_directory_required(episode):
    ctx, _, _ = episode
    recover(episode)
    with pytest.raises(ValueError, match='fresh job'):
        h.RecoveryContext(ctx.evidence_dir, 'other-run', 'other-op', 'lane')


def test_failed_start_cannot_retry_or_claim_healed(episode):
    ctx, evidence, _ = episode
    def fail():
        evidence.actions.append('broker')
        raise h.Refused('start failed')
    evidence.start_broker = fail
    with pytest.raises(h.Refused, match='start failed'):
        recover(episode)
    assert evidence.actions == ['broker']
    assert events(ctx)[-1]['event'] == 'refused'
    assert not json.loads((ctx.evidence_dir / 'preflight-summary.json').read_text())['healed']


def test_persisted_evidence_redacts_secrets(episode):
    ctx, evidence, _ = episode
    h.recover_kafka(ctx, evidence, datetime.now(timezone.utc), 'token=private password=hidden')
    for path in ctx.evidence_dir.iterdir():
        assert 'private' not in path.read_text()
        assert 'hidden' not in path.read_text()


def test_v761_multiline_registration_signature():
    assert h.registration_signature(REGISTRATION_LOG)


@pytest.mark.parametrize('text', [
    REGISTRATION_LOG.replace('/brokers/ids/1,', '/brokers/ids/2,'),
    REGISTRATION_LOG.replace('/brokers/ids/1,', '/brokers/ids/10,'),
    REGISTRATION_LOG.replace('ERROR Error while', 'INFO Error while'),
    REGISTRATION_LOG.replace("owner '0x2a'", "owner '0x0'"),
    REGISTRATION_LOG.replace("session '0x2b'", "session '0x2a'"),
    REGISTRATION_LOG.replace('KeeperException$NodeExistsException', 'KeeperException$NoAuthException'),
    REGISTRATION_LOG.replace('KafkaZkClient.registerBroker', 'Unrelated.register'),
    'org.apache.zookeeper.KeeperException$NodeExistsException: KeeperErrorCode = NodeExists',
])
def test_multiline_lookalikes_are_unclassified(text):
    assert not h.registration_signature(text)


@pytest.mark.parametrize('response', [
    'Connection refused',
    'SyncConnected\nAuthentication failed',
    'SyncConnected\nKeeperErrorCode = NoAuth for /brokers/ids/1',
    'SyncConnected\nNode does not exist: /another',
    'SyncConnected\nephemeralOwner = 0x2a',
    'SyncConnected\nNode does not exist: /brokers/ids/1\nAuthentication failed',
])
def test_exit_one_stat_errors_never_mutate(docker_episode, response):
    d = docker_episode
    run = d.adapter.run
    def stat_error(argv, **kw):
        if 'zookeeper-shell' in argv:
            return SimpleNamespace(returncode=1, stdout=response, stderr='')
        return run(argv, **kw)
    d.adapter.run = stat_error
    with pytest.raises(h.Refused):
        h.recover_kafka(d.ctx, d.adapter, d.since, 'failed')
    assert not d.mutations


def test_exit_zero_api_error_cannot_heal_or_record_success(docker_episode, tmp_path):
    from livetest.services import ensure_provisioned_once
    d = docker_episode
    run = d.adapter.run
    def api_error(argv, **kw):
        if 'kafka-broker-api-versions' in argv:
            return SimpleNamespace(returncode=0, stderr='', stdout=(
                'localhost:9092 (id: 1 rack: null) -> ERROR: org.apache.kafka.common.errors.TimeoutException'))
        return run(argv, **kw)
    d.adapter.run = api_error
    registry_dir = tmp_path / 'registry'
    registry_dir.mkdir()
    with pytest.raises(h.Refused):
        ensure_provisioned_once('kafka', lambda: h.recover_kafka(d.ctx, d.adapter, d.since, 'failed'),
                                state_dir=registry_dir)
    assert not (registry_dir / '.slt-provision-registry.json').exists()
    assert not json.loads((d.ctx.evidence_dir / 'preflight-summary.json').read_text())['healed']
    assert 'healed' not in [event['event'] for event in events(d.ctx)]


@pytest.mark.parametrize('text', [API_VERSIONS.replace('id: 1', 'id: 2'),
                                API_VERSIONS.replace('[usable: 9]', '[unusable: UNSUPPORTED]')
                                            .replace('[usable: 3]', '[unusable: UNSUPPORTED]'),
                                API_VERSIONS.replace(')\n', ''),
                                'localhost:9092 (id: 1 rack: null) -> (\n)'])
def test_incomplete_or_wrong_broker_api_blocks_are_not_ready(docker_episode, text):
    d = docker_episode
    run = d.adapter.run
    def api_result(argv, **kw):
        if 'kafka-broker-api-versions' in argv:
            return SimpleNamespace(returncode=0, stderr='', stdout=text)
        return run(argv, **kw)
    d.adapter.run = api_result
    for c in d.containers:
        c['State'].update(Running=True, Status='running')
    d.adapter.snapshot(stopped=False)
    assert not d.adapter.ready()
