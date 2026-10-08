"""Fail-closed, pre-flight-only Kafka registration recovery.

The caller must hold maintenance admission for the whole context lifetime. Its guard
revalidates the lease, exact identities and absence of consumers before EACH start.
No environment flag grants that authority. Docker mutations remain in compose-up's
wrapped call; workers and fault tests have no active context.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time

_ACTIVE = ContextVar('preflight_recovery', default=None)


class Refused(RuntimeError):
    pass


def current_context():
    return _ACTIVE.get()


@contextmanager
def preflight_context(context):
    token = _ACTIVE.set(context)
    try:
        yield
    finally:
        _ACTIVE.reset(token)


def registration_signature(text):
    if re.search(r'KeeperException\$NodeExistsException[^\n]*?/brokers/ids/1(?![\w/])', text):
        return True
    # v7.6.1 logs the path/owner mismatch before throwing a pathless exception.
    diagnostic = re.search(
        r"\bERROR Error while creating ephemeral at /brokers/ids/1, node already exists and owner "
        r"'(0x[0-9a-fA-F]+)' does not match current session '(0x[0-9a-fA-F]+)'", text)
    if not diagnostic or int(diagnostic[1], 16) == 0 or int(diagnostic[1], 16) == int(diagnostic[2], 16):
        return False
    traceback = re.search(
        r'KeeperException\$NodeExistsException:[^\n]*\n((?:[ \t]+at [^\n]*\n?)+)',
        text[diagnostic.end():])
    return bool(traceback and
                'kafka.zk.KafkaZkClient$CheckedEphemeral.getAfterNodeExists(' in traceback[1] and
                'kafka.zk.KafkaZkClient.registerBroker(' in traceback[1])


def parse_owner(text, status=0):
    # A failed shell/client command is NOT proof of an absent node.
    if status not in (0, 1) or 'SyncConnected' not in text:
        raise Refused('ZooKeeper protocol unavailable')
    owners = re.findall(r'^ephemeralOwner = (0x[0-9a-fA-F]+|[0-9]+)\s*$', text, re.M)
    absent = re.search(r'^Node does not exist: /brokers/ids/1\s*$', text, re.M)
    if re.search(r'(?i)NoAuth|AuthFailed|authentication failed|ConnectionLoss|connection refused|'
                 r'command not found|ERROR|Exception', text):
        raise Refused('ZooKeeper stat probe failed')
    if len(owners) == 1 and not absent and status == 0:
        return int(owners[0], 16 if owners[0].startswith('0x') else 10)
    if absent and not owners:
        return None
    raise Refused('missing or conflicting ZooKeeper stat evidence')


def _redact(text, limit=4096):
    return re.sub(r'(?i)(password|secret|token|licen[cs]e_key|product_key)(\s*[=:]\s*)\S+',
                  r'\1\2[redacted]', str(text))[:limit]


@dataclass
class RecoveryContext:
    evidence_dir: Path
    run_id: str
    operation_id: str
    lane: str
    maintenance_guard: object = None
    cancelled: object = lambda: False
    clock: object = time.monotonic
    sleep: object = time.sleep
    log: object = print
    expiry_seconds: float = 120
    readiness_seconds: float = 180
    sequence: int = 0
    used: bool = False
    started: float = field(init=False)
    deadline: float = field(init=False)
    original_error: str = ''
    identity: dict = field(default_factory=dict)

    def __post_init__(self):
        self.evidence_dir = Path(self.evidence_dir)
        if any((self.evidence_dir / name).exists() for name in
               ('preflight-healing.jsonl', 'preflight-summary.json')):
            raise ValueError('recovery evidence directory already used; require a fresh job directory')
        if not all((self.run_id, self.operation_id, self.lane)):
            raise ValueError('recovery requires run, operation and lane identity')
        if not 0 < self.expiry_seconds <= 120 or not 0 < self.readiness_seconds <= 180:
            raise ValueError('K1 budgets may only be lowered')
        self.started = self.clock()
        # Initial builds are separate from K1's 300-second episode. The initial
        # command gets its own 900-second deadline when it begins.
        self.deadline = self.started + 900

    def check(self):
        if self.cancelled():
            raise Refused('cancelled')
        if self.clock() >= self.deadline:
            raise Refused('deadline exhausted')

    def timeout(self):
        self.check()
        return min(5, self.deadline - self.clock())

    def pause(self, seconds):
        # Check cancellation at least once per second even during backoff.
        end = min(self.deadline, self.clock() + seconds)
        while self.clock() < end:
            self.check()
            self.sleep(min(1, end - self.clock()))
        self.check()

    def emit(self, event, action='', error='', readiness=None):
        self.evidence_dir.mkdir(parents=True, exist_ok=True)
        self.sequence += 1
        record = dict(version=1, event=event, event_id=f'{self.operation_id}:k1:{self.sequence}',
                      sequence=self.sequence, run_id=self.run_id, operation_id=self.operation_id,
                      lane=self.lane, service='kafka', classification='kafka_registration_conflict',
                      attempt=1, max_attempts=1, action=action, identity=self.identity,
                      evidence_refs=['compose-first-failure.log', 'kafka-first-failure.log',
                                     'preflight-healing.jsonl'],
                      elapsed=self.clock() - self.started, remaining=max(0, self.deadline-self.clock()),
                      utc=datetime.now(timezone.utc).isoformat(), original_error=self.original_error,
                      final_error=_redact(error), readiness=readiness)
        # Flush and fsync intent before allowing any mutation. Write failure propagates.
        with (self.evidence_dir / 'preflight-healing.jsonl').open('a', encoding='utf-8') as f:
            f.write(json.dumps(record) + '\n')
            f.flush()
            os.fsync(f.fileno())
        self.log(f'preflight recovery: {event} kafka {action} {_redact(error)}'.strip())

    def summary(self, healed, error=''):
        # Finalize only terminal verified outcomes; interrupted JSONL alone is not success.
        target = self.evidence_dir / 'preflight-summary.json'
        temp = target.with_suffix('.json.tmp')
        with temp.open('w', encoding='utf-8') as f:
            json.dump(dict(version=1, run_id=self.run_id, operation_id=self.operation_id,
                           lane=self.lane, service='kafka', healed=healed,
                           original_error=self.original_error, final_error=_redact(error)), f)
            f.flush()
            os.fsync(f.fileno())
        temp.replace(target)


def recover_kafka(ctx, evidence, since, original_error):
    if ctx.used:
        raise Refused('K1 single attempt already consumed')
    ctx.used = True
    ctx.started = ctx.clock()
    ctx.deadline = ctx.started + ctx.expiry_seconds
    ctx.original_error = _redact(original_error)
    ctx.evidence_dir.mkdir(parents=True, exist_ok=True)
    with (ctx.evidence_dir / 'compose-first-failure.log').open('x', encoding='utf-8') as f:
        f.write(_redact(original_error, 65536))
        f.flush()
        os.fsync(f.fileno())
    ctx.emit('attempt_failed', 'initial compose')
    expiry_phase = True
    try:
        ctx.check()
        ctx.identity = evidence.snapshot()
        if not evidence.registration_failure(since):
            raise Refused('missing fresh broker registration signature')
        owner = evidence.owner()
        if owner is None:
            raise Refused('initial ephemeral owner not observed')
        if not owner:
            raise Refused('persistent broker registration')
        ctx.emit('classified')
        delay = 5
        while True:
            ctx.emit('waiting', 'natural registration expiry')
            ctx.pause(min(delay, ctx.deadline - ctx.clock()))
            delay = min(20, delay * 2)
            ctx.check()
            observed = evidence.owner()
            if observed is None:
                break
            if observed != owner:
                raise Refused('broker registration owner changed')
        ctx.check()
        expiry_phase = False
        ctx.deadline = min(ctx.started + 300, ctx.clock() + ctx.readiness_seconds)
        if evidence.snapshot() != ctx.identity:
            raise Refused('container/image/config/mount identity changed')
        if evidence.owner() is not None:
            raise Refused('broker registration reappeared')
        def start(action, command, stopped):
            ctx.check()
            if evidence.snapshot(stopped=stopped) != ctx.identity:
                raise Refused('container/image/config/mount identity changed')
            if ctx.maintenance_guard is None or ctx.maintenance_guard(ctx.identity) is not True:
                raise Refused('exclusive maintenance authority unavailable')
            ctx.check()
            ctx.emit('attempt_started', action)
            ctx.check()
            command()
        ctx.log('Kafka broker registration expired; starting existing broker (recovery 1/1)')
        start('start existing broker', evidence.start_broker, True)
        if evidence.registry_needs_start():
            start('start existing registry', evidence.start_registry, False)
        while True:
            ctx.check()
            if evidence.snapshot(stopped=False) != ctx.identity:
                raise Refused('container/image/config/mount identity changed')
            if evidence.ready():
                ctx.check()
                ctx.emit('healed', readiness={'broker': True, 'registry': True, 'zookeeper': True})
                ctx.summary(True)
                return True
            ctx.pause(min(5, ctx.deadline - ctx.clock()))
    except (Refused, OSError, subprocess.SubprocessError, TimeoutError, ValueError, KeyError) as exc:
        reason = str(exc)
        if 'deadline' in reason:
            reason = ('natural expiry not observed' if expiry_phase
                      else 'complete readiness deadline exhausted')
            event = 'exhausted'
        else:
            event = 'cancelled' if 'cancelled' in reason else 'refused'
        ctx.emit(event, error=reason)
        ctx.summary(False, reason)
        raise Refused(reason) from exc


class DockerEvidence:
    """Pinned Kafka profile only. Resolve names from effective compose, verify by ID.

    Missing registry or unsupported profiles refuse; creating new dependencies is outside
    this first recovery adapter. All successful recovery commands preserve existing mounts.
    """
    roles = {'broker': 'slt-kafka', 'zookeeper': 'slt-zookeeper', 'registry': 'slt-schema-registry'}

    def __init__(self, ctx, defn, env, run=None):
        self.ctx, self.env = ctx, env
        self.run = run or subprocess.run
        self.compose = ['docker', 'compose', '-f', str(defn.dir / defn.compose)]
        self.profile = None
        self.containers = {}

    @staticmethod
    def digest(value):
        return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()

    def command(self, argv, *, allow_failure=False, with_status=False):
        result = self.run(argv, capture_output=True, text=True, env=self.env,
                          timeout=self.ctx.timeout())
        self.ctx.check()
        text = (result.stdout or '') + (result.stderr or '')
        if len(text.encode()) > 65536:
            raise Refused('diagnostic output exceeds 64 KiB')
        if with_status:
            return result.returncode, text
        if result.returncode and not allow_failure:
            raise Refused('diagnostic/action command failed: ' + ' '.join(argv[:3]))
        return None if result.returncode else text

    def snapshot(self, stopped=True):
        effective = json.loads(self.command(self.compose + ['config', '--format', 'json']))
        if self.profile is None:
            if set(effective['services']) != set(self.roles.values()):
                raise Refused('unsupported Kafka service profile')
            pinned = {'broker': 'confluentinc/cp-kafka:7.6.1',
                      'registry': 'confluentinc/cp-schema-registry:7.6.1',
                      'zookeeper': 'confluentinc/cp-zookeeper:7.6.1'}
            for role, svc in self.roles.items():
                if effective['services'][svc].get('image') != pinned[role]:
                    raise Refused('unsupported Kafka image')
            self.profile = effective
            self.profile_digest = self.digest(effective)
        if self.digest(effective) != self.profile_digest:
            raise Refused('effective compose identity changed')
        hashes = dict(line.split() for line in self.command(
            self.compose + ['config', '--hash', '*']).splitlines())
        if set(hashes) != set(self.roles.values()) or not all(hashes.values()):
            raise Refused('missing effective compose config hashes')
        names = [self.profile['services'][svc]['container_name'] for svc in self.roles.values()]
        containers = json.loads(self.command(['docker', 'container', 'inspect', *names]))
        if len(containers) != 3:
            raise Refused('missing Kafka dependency evidence')
        found, identity = {}, {}
        for role, svc in self.roles.items():
            config = self.profile['services'][svc]
            matches = [c for c in containers if c['Name'].lstrip('/') == config['container_name']]
            if len(matches) != 1:
                raise Refused('missing/ambiguous container')
            c = matches[0]
            labels = c['Config']['Labels']
            if (labels.get('com.docker.compose.project') != self.profile['name'] or
                    labels.get('com.docker.compose.service') != svc or
                    labels.get('com.docker.compose.oneoff', 'False').lower() != 'false' or
                    labels.get('com.docker.compose.config-hash') != hashes.get(svc) or
                    c['Config']['Image'] != config['image']):
                raise Refused('foreign container or unsupported image/config')
            expected_image = json.loads(self.command(['docker', 'image', 'inspect', config['image']]))
            if len(expected_image) != 1 or expected_image[0]['Id'] != c['Image']:
                raise Refused('resolved image identity mismatch')
            if c['State']['OOMKilled']:
                raise Refused('OOM killed dependency')
            actual = dict(v.split('=', 1) for v in c['Config']['Env'])
            if any(actual.get(k) != str(v) for k, v in config.get('environment', {}).items()):
                raise Refused('effective environment identity changed')
            found[role] = c
            identity[role] = dict(id=c['Id'], image=c['Image'], config=self.digest(c['Config']),
                                  mounts=c['Mounts'], project=self.profile['name'], service=svc,
                                  profile_digest=self.profile_digest)
        broker = found['broker']
        actual_env = dict(v.split('=', 1) for v in broker['Config']['Env'])
        expected_env = self.profile['services'][self.roles['broker']]['environment']
        if (str(expected_env.get('KAFKA_BROKER_ID')) != '1' or
                actual_env.get('KAFKA_BROKER_ID') != '1' or
                actual_env.get('KAFKA_ZOOKEEPER_CONNECT') != expected_env.get('KAFKA_ZOOKEEPER_CONNECT')):
            raise Refused('effective broker identity mismatch')
        if stopped and (broker['State']['Status'] != 'exited' or broker['State']['Running']):
            raise Refused('broker is not exited')
        if not stopped and not broker['State']['Running']:
            raise Refused('broker exited after single start')
        if not found['zookeeper']['State']['Running']:
            raise Refused('ZooKeeper is not running')
        # Inspect inventory as well: an extra project member or broker using the same
        # registration endpoint invalidates the diagnosis, even if its name differs.
        ids = self.command(['docker', 'ps', '-aq']).split()
        if not ids:
            raise Refused('missing Docker inventory')
        inventory = json.loads(self.command(['docker', 'container', 'inspect', *ids]))
        known = {c['Id'] for c in found.values()}
        for c in inventory:
            if c['Id'] in known:
                continue
            labels = c['Config'].get('Labels') or {}
            extra_env = dict(v.split('=', 1) for v in (c['Config'].get('Env') or []))
            if (labels.get('com.docker.compose.project') == self.profile['name'] or
                (extra_env.get('KAFKA_BROKER_ID') == '1' and
                 extra_env.get('KAFKA_ZOOKEEPER_CONNECT') == actual_env['KAFKA_ZOOKEEPER_CONNECT'])):
                raise Refused('competing project container/broker')
        self.containers = found
        return identity

    def registration_failure(self, since):
        c = self.containers['broker']
        started = datetime.fromisoformat(c['State']['StartedAt'].replace('Z', '+00:00'))
        if started < since:
            return False
        logs = self.command(['docker', 'logs', '--timestamps', '--since', since.isoformat(),
                             '--tail', '200', c['Id']])
        fresh = []
        for line in logs.splitlines():
            stamp, _, message = line.partition(' ')
            try:
                if datetime.fromisoformat(stamp.replace('Z', '+00:00')) >= since:
                    fresh.append(message)
            except ValueError:
                raise Refused('missing log timestamp')
        text = '\n'.join(fresh)
        # Retain only this bounded, redacted startup tail, never Config.Env.
        with (self.ctx.evidence_dir / 'kafka-first-failure.log').open('x', encoding='utf-8') as f:
            f.write(_redact(text, 65536))
            f.flush()
            os.fsync(f.fileno())
        return registration_signature(text)

    def owner(self):
        status, text = self.command(['docker', 'exec', self.containers['zookeeper']['Id'],
                                    'zookeeper-shell', 'localhost:2181', 'stat', '/brokers/ids/1'],
                                   with_status=True)
        return parse_owner(text, status)

    def start_broker(self):
        self.command(['docker', 'start', self.containers['broker']['Id']])

    def registry_needs_start(self):
        state = self.containers['registry']['State']
        if state['Running']:
            return False
        if state['Status'] not in ('created', 'exited'):
            raise Refused('unsupported registry state')
        return True

    def start_registry(self):
        self.command(['docker', 'start', self.containers['registry']['Id']])

    def ready(self):
        if not self.containers['registry']['State']['Running']:
            raise Refused('registry exited after single start')
        # Nonzero readiness during normal startup may be polled; malformed ZK evidence
        # and diagnostic failures remain refusals. Never accept State.Running alone.
        if not self.owner():
            return False
        broker = self.command(['docker', 'exec', self.containers['broker']['Id'],
                               'kafka-broker-api-versions', '--bootstrap-server', 'localhost:9092'],
                              allow_failure=True)
        registry = self.command(['docker', 'exec', self.containers['registry']['Id'],
                                 'curl', '-fsS', 'http://localhost:8081/subjects'], allow_failure=True)
        try:
            subjects = json.loads(registry or '')
        except ValueError:
            return False
        block = re.search(r'^[^\n]*\(id: 1 rack: [^\n)]*\) -> \(\s*\n(.*?)^\)\s*$',
                          broker or '', re.M | re.S)
        return bool(block and 'ERROR:' not in block[1] and
                    re.search(r'^\s*\w+\(\d+\): \d+(?: to \d+)? \[usable: \d+\]',
                              block[1], re.M)) and isinstance(subjects, list)
