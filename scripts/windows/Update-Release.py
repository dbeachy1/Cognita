#!/usr/bin/env python3
"""Bounded WSL-side image replacement; the Windows lifecycle owns publication.

This helper never prepares sources or controls the Compose system unit. Its
protected temporary snapshot is an owned rollback input, not installation state.
PowerShell always finishes it with either commit or rollback and cleanup.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import stat
import subprocess
import tempfile
import tarfile
import time
import uuid

ROOT = Path('/srv/cognita')
PROJECTION = Path('/run/cognita/source-identities.json')
PREFIX = 'cognita-windows-update-'
FILES = ('compose.yaml', 'compose.cpu.yaml', 'compose.workspace.yaml',
         'compose.cpu.images.yaml', 'compose.windows-projection.yaml')
CONFIG_FILES = ('cognita.yaml', 'registry.yaml', 'connectors.yaml',
                'authentication.yaml', 'acceleration.yaml', 'postgres.password',
                'postgres.dsn', 'broker.secret', 'admin_tls_certfile', 'admin_tls_keyfile')


def interrupted(signum, frame):
    raise InterruptedError('Owned update operation interrupted')


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def run(argv, *, timeout=120, env=None, check=True):
    """Own and reap a bounded process group without exposing child output."""
    with subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          text=True, env=env, start_new_session=True) as process:
        settled = False
        try:
            output, errors = process.communicate(timeout=timeout)
            settled = True
            result = subprocess.CompletedProcess(argv, process.returncode, output, errors)
            if check:
                require(result.returncode == 0, f'{Path(argv[0]).name} operation failed ({result.returncode})')
            return result
        finally:
            # A descendant can retain a pipe after the group leader exits. A
            # timeout still owns that entire session; poll() alone is insufficient.
            if not settled:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.communicate(timeout=10)
            process.wait(timeout=10)


def digest(path):
    value = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(8 * 1024**2), b''):
            value.update(block)
    return value.hexdigest()


def toolbox_engine_identity(archive, tag, config_digest):
    """Resolve Docker's tagged OCI descriptor separately from the SDK config.

    Classic Docker stores use the config digest as Id; containerd-backed stores
    expose the archive's tagged manifest/index descriptor instead. Verify its
    content-addressed graph reaches the already verified SDK config binding.
    """
    with tarfile.open(archive, 'r:') as saved:
        names = saved.getnames()
        require(len(names) == len(set(names)), 'Toolbox archive has duplicate members')
        if 'index.json' not in names:
            return None

        def read(name):
            info = saved.getmember(name)
            require(info.isfile() and info.size <= 1024 * 1024, 'Unsafe Toolbox OCI metadata')
            return saved.extractfile(info).read()

        index = json.loads(read('index.json'))
        descriptors = [item for item in index['manifests']
                       if item.get('annotations', {}).get('io.containerd.image.name')
                       in (tag, 'docker.io/library/' + tag)]
        require(len(descriptors) == 1, 'Toolbox OCI tag is missing or ambiguous')

        def reaches_config(descriptor, depth=0):
            identity = descriptor.get('digest', '')
            require(depth < 8 and re.fullmatch(r'sha256:[0-9a-f]{64}', identity),
                    'Invalid Toolbox OCI descriptor')
            data = read('blobs/sha256/' + identity.partition(':')[2])
            require(len(data) == descriptor['size'] and
                    'sha256:' + hashlib.sha256(data).hexdigest() == identity,
                    'Toolbox OCI descriptor bytes disagree')
            value = json.loads(data)
            if 'config' in value:
                return value['config'].get('digest') == config_digest
            children = value.get('manifests', [])
            require(0 < len(children) <= 32, 'Invalid Toolbox OCI index')
            return any(reaches_config(child, depth + 1) for child in children)

        require(reaches_config(descriptors[0]), 'Toolbox OCI tag differs from config binding')
        return descriptors[0]['digest']


def engine_matches_toolbox(value, descriptor, config_digest):
    observed = value.get('Descriptor')
    if observed:
        return descriptor is not None and observed.get('digest') == descriptor
    return value.get('Id') == config_digest


def update_phase(phase, operation, *args, **kwargs):
    """Emit only code-owned phase names and outcomes, never child diagnostics."""
    import sys
    print(f'COGNITA_UPDATE_PHASE {phase} started', file=sys.stderr, flush=True)
    try:
        result = operation(*args, **kwargs)
    except BaseException:
        print(f'COGNITA_UPDATE_PHASE {phase} failed', file=sys.stderr, flush=True)
        raise
    print(f'COGNITA_UPDATE_PHASE {phase} passed', file=sys.stderr, flush=True)
    return result


def metadata(path):
    result = {}
    for line in (path / 'release.txt').read_text().splitlines():
        key, sep, value = line.partition(':')
        if sep:
            require(key.strip() not in result, 'Duplicate release metadata')
            result[key.strip()] = value.strip()
    return result


def environment(data):
    values = {}
    for line in data.decode().splitlines():
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        key, sep, value = line.partition('=')
        require(sep and re.fullmatch(r'[A-Z][A-Z0-9_]*', key) and key not in values,
                'Installed Compose environment is ambiguous')
        values[key] = value
    return values


def release_environment(data, version):
    environment(data)  # Reject duplicates rather than changing just one copy.
    require(len(re.findall(rb'^COGNITA_VERSION=.*$', data, re.M)) == 1,
            'Installed release version entry is missing or ambiguous')
    return re.sub(rb'(?m)^COGNITA_VERSION=[^\r\n]*',
                  b'COGNITA_VERSION=' + version.encode(), data)


def atomic_write(path, data, template=None):
    """Replace one regular file while preserving its mode and numeric owner."""
    prior = (template or path).stat()
    require(not path.is_symlink(), 'Update destination is a symlink')
    temp = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.cognita-update-', delete=False) as stream:
            temp = Path(stream.name)
            os.fchmod(stream.fileno(), stat.S_IMODE(prior.st_mode))
            os.fchown(stream.fileno(), prior.st_uid, prior.st_gid)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        temp.replace(path)
    finally:
        if temp is not None:
            temp.unlink(missing_ok=True)


def file_fact(path, *, contents=True):
    require(path.is_file() and not path.is_symlink(), 'Required installed file is missing or unsafe')
    info = path.stat()
    fact = [info.st_dev, info.st_ino, info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)]
    if contents:
        fact.append(digest(path))
    return fact


def effective_mount(path):
    # Stacked/hidden mountinfo entries cannot establish which bind is visible.
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        match = re.search(r'^mnt_id:\s*(\d+)$', Path(f'/proc/self/fdinfo/{fd}').read_text(), re.M)
        require(match is not None, 'Effective source mount could not be identified')
        return int(match.group(1))
    finally:
        os.close(fd)


def source_facts(record, uid=None, gid=None):
    projection = json.loads(PROJECTION.read_text())
    require(projection.get('schema') == 1 and isinstance(projection.get('sources'), list),
            'Source projection is unavailable; explicit installer repair is required')
    seen = set()
    facts = {}
    root_mount = effective_mount(ROOT / 'sources')
    for row in projection['sources']:
        alias = row.get('alias', '')
        require(re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,31}', alias) and alias not in seen,
                'Source projection has an invalid or duplicate alias')
        seen.add(alias)
        if row.get('observation') != 'available':
            continue
        path = ROOT / 'sources' / alias
        require(path.is_dir() and not path.is_symlink(), 'Source alias is unavailable')
        info = path.stat()
        identity = row.get('identity')
        require(identity == {'device': info.st_dev, 'inode': info.st_ino},
                'Source projection disagrees with the effective alias')
        mount = effective_mount(path)
        if row.get('source_kind') != 'installation_ext4':
            require(mount != root_mount, 'Source alias is no longer mounted')
            if uid is not None:
                require((info.st_uid, info.st_gid) == (uid, gid),
                        'Source alias owner differs from the service identity')
        facts[alias] = [info.st_dev, info.st_ino, info.st_uid, info.st_gid, mount]
    topology = Path('/proc/self/mountinfo').read_text().splitlines()
    facts['topology'] = [line for line in topology if ' /srv/cognita/sources' in line]
    recorded = {row['alias'] for row in record['sources']}
    # Inspect directory names only; an unprojected live alias must never be
    # silently discarded by installer source regeneration.
    for path in (ROOT / 'sources').iterdir():
        if path.name not in seen and path.is_dir() and not path.is_symlink():
            require(effective_mount(path) == root_mount, 'Live source alias lacks a matching projection; explicit repair is required')
    manual = bool(seen - recorded)
    return {'manual': manual, 'aliases': facts, 'projection': file_fact(PROJECTION)}


def preservation_guard(record):
    recorded = {row['alias'] for row in record['sources']}
    existing = {item.name for item in (ROOT / 'sources').iterdir()} if (ROOT / 'sources').is_dir() else set()
    if not PROJECTION.exists():
        require(not (existing - recorded), 'Unrecorded source alias lacks its projection; explicit installer repair is required before bootstrap')
        return {'manual': False, 'verified': False}
    projected = json.loads(PROJECTION.read_text())
    manual = bool({row['alias'] for row in projected['sources']} - recorded)
    if manual:
        source_facts(record)  # Manual mappings cannot fall through to preparation.
        return {'manual': True, 'verified': True}
    try:
        source_facts(record)
        # Reuse the existing source identity owner read-only. A changed upstream
        # must still take its established preparation/refusal path, rather than
        # being mistaken for an unchanged healthy installation.
        import importlib.util
        spec = importlib.util.spec_from_file_location('cognita_update_sources', Path(__file__).with_name('Prepare-Sources.py'))
        helper = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(helper)
        for row in record['sources']:
            if row['source_kind'] != 'installation_ext4':
                observed = helper.canonical_identity(row['source_kind'], row['locator'], ROOT / 'sources' / row['alias'])
                require(observed == row['canonical_identity'], 'Recorded upstream identity changed')
        return {'manual': False, 'verified': True}
    except (OSError, RuntimeError, ValueError, KeyError):
        return {'manual': False, 'verified': False}


def compose_prefix(record):
    names = ['compose.yaml', 'compose.cpu.yaml']
    if record['mode'] == 'full':
        names.append('compose.workspace.yaml')
    names += ['compose.cpu.images.yaml', 'compose.windows-projection.yaml']
    prefix = ['docker', 'compose', '--project-name', record['compose_project'],
              '--env-file', str(ROOT / 'compose.env')]
    for name in names:
        prefix += ['--file', str(ROOT / name)]
    return prefix


class Update:
    def __init__(self, record, old_bundle, candidate, runner=run):
        self.record, self.old_bundle, self.candidate = record, Path(old_bundle), Path(candidate)
        self.old, self.new = metadata(self.old_bundle), metadata(self.candidate)
        self.run = runner
        self.env_bytes = (ROOT / 'compose.env').read_bytes()
        self.values = environment(self.env_bytes)
        self.version = self.values['COGNITA_VERSION']
        self.prefix = compose_prefix(record)
        self.snapshot = None

    def compose(self, *args, timeout=600, check=True):
        child_env = dict(os.environ)
        # Compose interpolates process variables before --env-file. Derive every
        # installed setting from its existing authority, including user ports.
        child_env.update(self.values)
        child_env['COGNITA_VERSION'] = self.version
        return self.run(self.prefix + list(args), timeout=timeout, env=child_env, check=check)

    def config(self):
        return json.loads(self.compose('config', '--format', 'json', timeout=120).stdout)

    def container(self, service, *, optional=False):
        value = self.compose('ps', '--all', '-q', service, timeout=60).stdout.strip()
        if not value and optional:
            return None
        require(re.fullmatch(r'[0-9a-f]{12,64}', value), 'Owned Compose container is missing or ambiguous')
        result = json.loads(self.run(['docker', 'inspect', value]).stdout)[0]
        labels = result['Config']['Labels']
        require(labels.get('com.docker.compose.project') == self.record['compose_project']
                and labels.get('com.docker.compose.service') == service,
                'Container ownership differs from the recorded project')
        return result

    def image(self, reference, expected, manifest=None):
        value = json.loads(self.run(['docker', 'image', 'inspect', reference]).stdout)[0]
        require(value['Id'] == expected, 'Selected image ID differs from the accepted bundle')
        if manifest is not None:
            labels = value['Config']['Labels']
            require(labels.get('org.opencontainers.image.version') == manifest['version']
                    and labels.get('org.opencontainers.image.revision') == manifest['commit'],
                    'Selected image OCI identity differs from the accepted bundle')
        return value

    def validate_ports(self, config):
        ports = config['services']['cognita'].get('ports', [])
        expected = {8675: self.record['mcp_port'], 8676: self.record['admin_port']}
        require(len(ports) == 2, 'Installed port mapping has unexpected listeners')
        for item in ports:
            require(item.get('host_ip') == '127.0.0.1'
                    and int(item['published']) == expected.get(int(item['target']))
                    and item.get('protocol', 'tcp') == 'tcp',
                    'Effective Compose ports differ from the recorded loopback ports')
        container = self.container('cognita', optional=True)
        if container:
            bindings = container['HostConfig'].get('PortBindings', {})
            require(set(bindings) == {'8675/tcp', '8676/tcp'}, 'Running container has unexpected published ports')
            for target, published in expected.items():
                require(bindings[f'{target}/tcp'] == [{'HostIp': '127.0.0.1', 'HostPort': str(published)}],
                        'Running container ports differ from the recorded owned listeners')

    def preserved(self):
        uid, gid = int(self.values['COGNITA_SERVICE_UID']), int(self.values['COGNITA_SERVICE_GID'])
        import pwd
        account = pwd.getpwnam('cognita-admin')
        require((account.pw_uid, account.pw_gid) == (uid, gid), 'Host service identity differs from installed Compose')
        for key, field in (('COGNITA_CONFIG_ROOT','config_root'), ('COGNITA_PROJECTS_ROOT','source_root'),
                           ('COGNITA_POSTGRES_DATA_ROOT','postgres_root'), ('COGNITA_MODEL_CACHE_ROOT','model_cache_root'),
                           ('COGNITA_TRANSFER_STAGING_ROOT','transfer_root'), ('COGNITA_WORKSPACE_DATA_ROOT','workspace_root')):
            require(self.values.get(key) == self.record[field], 'Installed storage root differs from owner record')
        require(self.values.get('COGNITA_RELEASE_TARGET') == 'windows', 'Installed release target is not Windows')
        pg = self.container('postgres')
        require(pg['Image'] == self.record['image_postgres'] and pg['State']['Running'],
                'Recorded PostgreSQL container/image is not running')
        config = self.config()
        self.validate_ports(config)
        files = {name: file_fact(ROOT / 'config' / name) for name in CONFIG_FILES
                 if (ROOT / 'config' / name).exists()}
        require(all(name in files for name in ('cognita.yaml', 'postgres.password', 'postgres.dsn', 'broker.secret')),
                'Installed configuration or credentials are missing')
        marker = ROOT / 'workspaces' / '.cognita-12-workspaces.json'
        require((marker.stat().st_uid, marker.stat().st_gid) == (uid, gid), 'Workspace capacity marker owner changed')
        require(Path('/dev/kvm').stat().st_gid == int(self.values['COGNITA_KVM_GID']), 'Host KVM group differs from installed access')
        devices = config['services']['workspace-runtime'].get('devices', [])
        runtime = config['services']['workspace-runtime']
        require(runtime['user'] == f'{uid}:{gid}' and config['services']['cognita']['user'] == f'{uid}:{gid}',
                'Effective service identity differs from the installed identity')
        require(str(self.values['COGNITA_KVM_GID']) in map(str, runtime['group_add'])
                and any(d.get('source') == '/dev/kvm' and d.get('target') == '/dev/kvm' for d in devices),
                'Effective KVM access differs from the installation')
        immutable_services = {}
        for service in ('cognita', 'workspace-runtime', 'postgres'):
            selected = dict(config['services'][service])
            selected.pop('image', None)
            selected.pop('build', None)
            selected['environment'] = dict(selected.get('environment', {}))
            selected['environment'].pop('COGNITA_VERSION', None)
            immutable_services[service] = selected
        return {'sources': source_facts(self.record, uid, gid), 'config': files,
                'marker': file_fact(marker), 'kvm': file_fact(Path('/dev/kvm'), contents=False)
                if Path('/dev/kvm').is_file() else self.device_fact(Path('/dev/kvm')),
                'pg': [pg['Id'], pg['Image'], pg['State']['StartedAt']],
                'services': immutable_services,
                'base': {name: file_fact(ROOT / name) for name in FILES if name != 'compose.cpu.images.yaml'},
                'startup': {str(path): file_fact(path) for path in (
                    Path('/etc/cognita-install-id'), Path('/etc/cognita-install-record-path'),
                    Path('/etc/systemd/system/cognita-compose.service'),
                    Path('/usr/local/libexec/cognita-session'), Path('/usr/local/libexec/cognita-prepare-sources'))}}

    @staticmethod
    def device_fact(path):
        info = path.stat()
        require(stat.S_ISCHR(info.st_mode), 'KVM device is unavailable')
        return [info.st_dev, info.st_ino, info.st_uid, info.st_gid, info.st_mode, info.st_rdev]

    def preflight(self):
        require(self.record['mode'] == self.new['bundle_mode'] == 'full', 'Windows update requires Full mode')
        require(self.new['image_ref_postgres'] == self.record['image_ref_postgres']
                and self.new['image_postgres'] == self.record['image_postgres'],
                'A PostgreSQL engine change is outside this maintenance update')
        for name in ('compose.yaml', 'compose.cpu.yaml', 'compose.workspace.yaml'):
            require((ROOT / name).read_bytes() == (self.candidate / name).read_bytes(),
                    'Base Compose changes require an explicit installer repair')
        selected = (ROOT / 'compose.cpu.images.yaml').read_bytes()
        require(selected in ((self.old_bundle / 'compose.cpu.full.images.yaml').read_bytes(),
                             (self.candidate / 'compose.cpu.full.images.yaml').read_bytes()),
                'Unrecognized partial release selection; refused adoption')
        require(self.version in (self.old['version'], self.new['version']),
                'Unrecognized partial release environment')
        # Deliberately precedes ordinary current-release validation: a killed
        # update may have replaced either service or selection file already.
        for service, key in (('cognita', 'cognita_cpu'), ('workspace-runtime', 'workspace_runtime')):
            container = self.container(service, optional=True)
            if container:
                choices = {self.old['image_' + key]: self.old, self.new['image_' + key]: self.new}
                require(container['Image'] in choices, 'Unrelated running image drift; update refused')
                self.image(container['Image'], container['Image'], choices[container['Image']])
        self.run(['systemctl', 'is-active', '--quiet', 'docker.service'])
        self.run(['systemctl', 'is-active', '--quiet', self.record['unit']])
        return self.preserved()

    def load_images(self):
        for name in ('cognita-cpu.tar', 'workspace-runtime.tar'):
            self.run(['docker', 'load', '--input', str(self.candidate / name)], timeout=3600)
        for key in ('cognita_cpu', 'workspace_runtime'):
            self.image(self.new['image_ref_' + key], self.new['image_' + key], self.new)
        self.image(self.new['image_ref_postgres'], self.record['image_postgres'])

    def runtime_command(self, *command, service='workspace-runtime', entrypoint=None):
        name = 'cognita-windows-operation-' + uuid.uuid4().hex
        registration = self.snapshot / 'oneoff' if self.snapshot is not None else None
        try:
            if registration:
                require(not registration.exists(), 'Owned Toolbox one-off is already registered')
                registration.write_text(name); registration.chmod(0o600)
            options = ['run', '--name', name, '--label', 'cognita.windows.update-owned=true',
                       '--rm', '--no-deps', '--pull', 'never']
            if entrypoint:
                options += ['--entrypoint', entrypoint]
            return self.compose(*options, service, *command, timeout=1800, check=False)
        finally:
            self.cleanup_oneoff(name)
            if registration:
                registration.unlink()

    def cleanup_oneoff(self, name):
        require(re.fullmatch(r'cognita-windows-operation-[0-9a-f]{32}', name), 'Invalid owned one-off name')
        observed = self.run(['docker', 'container', 'inspect', name], check=False)
        if observed.returncode == 0:
            value = json.loads(observed.stdout)[0]
            require(value['Config']['Labels'].get('cognita.windows.update-owned') == 'true'
                    and value['Config']['Labels'].get('com.docker.compose.project') == self.record['compose_project'],
                    'Toolbox one-off ownership could not be verified')
            self.run(['docker', 'container', 'rm', '--force', name], timeout=60)
        # A successful Engine query establishes absence even if inspect failed
        # because transport vanished, rather than mistaking a daemon error for it.
        remaining = self.run(['docker', 'container', 'ls', '--all', '--filter', f'name=^/{name}$', '--format', '{{.ID}}'])
        require(not remaining.stdout.strip(), 'Owned Toolbox one-off container cleanup failed')

    def loader(self, action, version):
        return self.runtime_command('python3', '-m', 'cognita.runtime_broker.image_cache', action,
                                    '--archive', f'/var/lib/cognita/toolbox-cache/toolbox-{version}.tar')

    def toolbox(self, bundle, manifest):
        cache = Path(self.values.get('COGNITA_TOOLBOX_IMAGE_CACHE_ROOT',
                                     self.values['COGNITA_WORKSPACE_DATA_ROOT'] + '/toolbox-cache'))
        archive = cache / f"toolbox-{manifest['toolbox_version']}.tar"
        require(cache.is_dir() and not cache.is_symlink(), 'Installed Toolbox cache is unavailable')
        accepted = bundle / 'toolbox.tar'
        if not archive.is_file() or archive.is_symlink() or digest(archive) != digest(accepted):
            require(not archive.is_symlink(), 'Toolbox cache archive is unsafe')
            template = archive if archive.exists() else ROOT / 'config' / 'broker.secret'
            # Stream the potentially large archive; do not buffer it in RAM.
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(dir=cache, prefix='.cognita-update-', delete=False) as stream:
                    temporary = Path(stream.name)
                    info = template.stat()
                    os.fchmod(stream.fileno(), 0o600)
                    os.fchown(stream.fileno(), info.st_uid, info.st_gid)
                    with accepted.open('rb') as source:
                        shutil.copyfileobj(source, stream, 8 * 1024**2)
                    stream.flush(); os.fsync(stream.fileno())
                require(digest(temporary) == digest(accepted), 'Staged Toolbox archive failed full checksum')
                temporary.replace(archive)
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
        require(self.loader('materialize-binding', manifest['toolbox_version']).returncode == 0,
                'Toolbox archive binding could not be materialized')
        binding = self.runtime_command('python3', '-c',
            "import json,sys; from pathlib import Path; "
            "from cognita.runtime_broker.image_cache import _verified_binding,TOOLBOX_TAG; "
            "print(json.dumps(_verified_binding(Path(sys.argv[1]),TOOLBOX_TAG)._asdict()))",
            f"/var/lib/cognita/toolbox-cache/toolbox-{manifest['toolbox_version']}.tar")
        require(binding.returncode == 0, 'Packaged Toolbox archive parser failed')
        parsed = json.loads(binding.stdout)
        tag = 'cognita-workspace-toolbox:' + manifest['toolbox_version']
        identity = parsed['config_digest']
        require(parsed['tag'] == tag and parsed['archive_digest'] == 'sha256:' + digest(accepted)
                and re.fullmatch(r'sha256:[0-9a-f]{64}', identity), 'Packaged Toolbox binding differs from candidate')
        descriptor = toolbox_engine_identity(accepted, tag, identity)
        def matches():
            observed = self.run(['docker', 'image', 'inspect', tag], check=False)
            return observed.returncode == 0 and engine_matches_toolbox(
                json.loads(observed.stdout)[0], descriptor, identity)
        if not matches():
            self.run(['docker', 'load', '--input', str(accepted)], timeout=3600)
        require(matches(), 'Docker Toolbox identity differs from the accepted archive')
        if self.loader('verify', manifest['toolbox_version']).returncode:
            require(self.loader('load', manifest['toolbox_version']).returncode == 0,
                    'Candidate Toolbox import failed')
        require(self.loader('verify', manifest['toolbox_version']).returncode == 0,
                'Candidate Microsandbox binding failed verification')

    def selection(self, bundle, manifest, env_bytes):
        atomic_write(ROOT / 'compose.cpu.images.yaml', (bundle / 'compose.cpu.full.images.yaml').read_bytes())
        atomic_write(ROOT / 'compose.env', release_environment(env_bytes, manifest['version']))
        self.version = manifest['version']
        self.values = environment((ROOT / 'compose.env').read_bytes())

    def start_pair(self, manifest):
        for service in ('workspace-runtime', 'cognita'):
            self.compose('up', '--detach', '--no-deps', '--no-build', '--pull', 'never', '--force-recreate', service)
            deadline = time.monotonic() + 120
            while True:
                container = self.container(service)
                if container['State'].get('Health', {}).get('Status') == 'healthy':
                    break
                require(time.monotonic() < deadline, f'{service} did not become healthy; use the explicit manual reset command only if needed')
                time.sleep(2)
            key = 'cognita_cpu' if service == 'cognita' else 'workspace_runtime'
            require(container['Image'] == manifest['image_' + key], 'Running service has a different accepted image')
            self.image(container['Image'], manifest['image_' + key], manifest)
            rendered = self.config()['services'][service]
            actual_env = dict(value.split('=', 1) for value in container['Config']['Env'])
            for key, value in rendered.get('environment', {}).items():
                if value is not None:
                    require(actual_env.get(key) == str(value), 'Running release environment differs from installed Compose')
            require(container['Config']['User'] == rendered['user'], 'Running service user differs from Compose')
            actual_mounts = {(m['Source'], m['Destination'], not m['RW']) for m in container['Mounts']}
            for volume in rendered.get('volumes', []):
                if volume['type'] == 'bind':
                    require((volume['source'], volume['target'], volume.get('read_only', False)) in actual_mounts,
                            'Running service bind mounts differ from installation')
            if service == 'workspace-runtime':
                require(set(map(str, container['HostConfig'].get('GroupAdd') or [])) == set(map(str, rendered['group_add'])),
                        'Running runtime KVM groups differ from Compose')
                require(any(d['PathOnHost'] == d['PathInContainer'] == '/dev/kvm'
                            for d in container['HostConfig'].get('Devices') or []), 'Running runtime lacks KVM device')

    def cleanup_workspace(self, workspace):
        try:
            require(self.runtime_command('-c', WORKSPACE_CLEANUP, workspace,
                    service='cognita', entrypoint='python').returncode == 0,
                    'Packaged Workspace cleanup client failed')
        except BaseException:
            raise RuntimeError(f'Owned update Workspace cleanup unverified: {workspace}') from None

    def status(self):
        """Report observed identity/readiness without adopting a partial selection."""
        observed = {service: self.container(service, optional=True)
                    for service in ('cognita', 'workspace-runtime', 'postgres')}
        identities = {}
        coherent = self.version == self.record['version']
        for service, key in (('cognita', 'cognita_cpu'), ('workspace-runtime', 'workspace_runtime'), ('postgres', 'postgres')):
            value = observed[service]
            labels = value['Config'].get('Labels', {}) if value else {}
            state = value['State'] if value else {}
            identities[service] = {'image': value['Image'] if value else None,
                                   'version': labels.get('org.opencontainers.image.version'),
                                   'commit': labels.get('org.opencontainers.image.revision'),
                                   'running': state.get('Running', False),
                                   'health': state.get('Health', {}).get('Status')}
            coherent = coherent and bool(value and value['Image'] == self.record['image_' + key] and state.get('Running'))
            if service != 'postgres':
                coherent = coherent and labels.get('org.opencontainers.image.version') == self.record['version'] \
                    and labels.get('org.opencontainers.image.revision') == self.record['commit'] \
                    and identities[service]['health'] == 'healthy'
            if service == 'workspace-runtime' and value:
                runtime_env = dict(item.split('=', 1) for item in value['Config']['Env'])
                identities[service]['release_environment'] = runtime_env.get('COGNITA_VERSION')
                coherent = coherent and runtime_env.get('COGNITA_VERSION') == self.record['version']
        config = self.config()
        try:
            self.validate_ports(config)
            ports_agree = True
        except RuntimeError:
            ports_agree = False
        selection_agrees = (ROOT / 'compose.cpu.images.yaml').read_bytes() == \
            (self.old_bundle / 'compose.cpu.full.images.yaml').read_bytes()
        return {'record_version': self.record['version'], 'record_commit': self.record['commit'],
                'mode': self.record['mode'], 'version': self.version, 'services': identities,
                'ports': [self.record['mcp_port'], self.record['admin_port']],
                'ports_agree': ports_agree, 'selection_agrees': selection_agrees,
                'coherent': bool(coherent and ports_agree and selection_agrees)}

    def verify(self, manifest, before, *, workspace=True, snapshot=None):
        require(self.preserved() == before, 'Preserved installation resources changed during update')
        for service, key in (('cognita', 'cognita_cpu'), ('workspace-runtime', 'workspace_runtime')):
            observed = self.container(service)
            require(observed['State']['Running'] and observed['State'].get('Health', {}).get('Status') == 'healthy'
                    and observed['Image'] == manifest['image_' + key], 'Current app/runtime image or readiness disagrees with the record')
            self.image(observed['Image'], manifest['image_' + key], manifest)
        app = self.container('cognita')
        uid = self.values['COGNITA_SERVICE_UID'] + ':' + self.values['COGNITA_SERVICE_GID']
        check = ("import json,urllib.request; "
                 "h=json.load(urllib.request.urlopen('http://127.0.0.1:8675/healthz',timeout=3)); "
                 f"assert h['status']=='ok' and h['version']=={manifest['version']!r} and h['workspace']['mode']=='full'")
        self.run(['docker', 'exec', '--user', uid, app['Id'], 'python', '-c', check])
        self.run(['docker', 'exec', '--user', uid, app['Id'], 'python', '-c',
                  WORKSPACE_CLIENT + BROKER_READINESS, 'readiness', manifest['version']], timeout=120)
        if workspace:
            owned = str(uuid.uuid4())
            require(snapshot is not None, 'Workspace proof requires its owned cleanup snapshot')
            (snapshot / 'workspace-id').write_text(owned)
            (snapshot / 'workspace-id').chmod(0o600)
            completed = False
            try:
                # Guest-side timeout owns the exec process independently of
                # Docker's client, which may disappear on host cancellation.
                self.run(['docker', 'exec', '--user', uid, app['Id'], 'timeout', '--signal=TERM',
                          '--kill-after=15s', '420s', 'python', '-c', WORKSPACE_PROOF, owned], timeout=600)
                completed = True
            finally:
                if not completed:
                    # Killing docker exec's client does not stop its container
                    # process. Stop only this replaced app before the independent
                    # packaged cleanup client, so a delayed RPC cannot recreate
                    # the synthetic sandbox after absence has been proved.
                    self.compose('stop', '--timeout', '30', 'cognita')
                self.cleanup_workspace(owned)
                (snapshot / 'workspace-id').unlink()
        require(self.preserved() == before, 'Workspace verification changed preserved installation resources')
        self.run(['systemctl', 'is-active', '--quiet', self.record['unit']])


WORKSPACE_CLIENT = r'''
import os, time, sys, signal
from pathlib import Path
from cognita.workspace import BrokerRuntimeClient
client=BrokerRuntimeClient(os.environ['COGNITA_WORKSPACE_RUNTIME_URL'],
    Path(os.environ['COGNITA_INTERNAL_BEARER_FILE']).read_text().strip(),timeout=90)
workspace=sys.argv[1]
'''
BROKER_READINESS = r'''
import json, urllib.request
assert client.health().get('runtime')=='ready'
# The existing health schema reports readiness, while the broker's existing
# OpenAPI info owns its reported application version. Preserve both contracts.
with urllib.request.urlopen(client.base_url.removesuffix('/v1')+'/openapi.json',timeout=10) as response:
    assert json.load(response)['info']['version']==sys.argv[2]
'''
WORKSPACE_CLEANUP = WORKSPACE_CLIENT + r'''
assert client.call(workspace,'remove',{}).get('state')=='absent'
assert client.call(workspace,'inspect',{}).get('state')=='absent'
print('owned Workspace and jobs absent')
'''
WORKSPACE_PROOF = WORKSPACE_CLIENT + r'''
def interrupt(signum,frame): raise InterruptedError('proof interrupted')
signal.signal(signal.SIGTERM,interrupt)
assert client.health().get('runtime')=='ready'
job=None
try:
    client.call(workspace,'ensure',{'create_volume_if_absent':True,'network':{'mode':'off'}})
    assert client.call(workspace,'inspect',{}).get('state') != 'absent'
    client.call(workspace,'fs_write',{'path':'/workspace/update-proof.txt','text':'Cognita update proof\n'})
    assert client.call(workspace,'fs_read',{'path':'/workspace/update-proof.txt'})['content']=='Cognita update proof\n'
    job=client.call(workspace,'job_start',{'argv':['python3','-c',"from pathlib import Path; assert Path('/workspace/update-proof.txt').read_text()=='Cognita update proof\\n'; print('verified')"], 'timeout_seconds':30})['job_id']
    end=time.monotonic()+60
    while True:
        result=client.call(workspace,'job_get',{'job_id':job})
        if result.get('state') not in ('queued','running'):
            assert result.get('state')=='succeeded' and result.get('exit_code')==0
            break
        assert time.monotonic()<end
        time.sleep(.5)
finally:
    # remove settles owned active jobs and proves both sandbox and volume absent.
    removed=client.call(workspace,'remove',{})
    assert removed.get('state')=='absent'
    assert client.call(workspace,'inspect',{}).get('state')=='absent'
print('packaged Workspace create/files/Python job/remove verification passed')
'''


def transaction_path(token):
    path = Path(token)
    require(path.parent == Path(tempfile.gettempdir()) and re.fullmatch(PREFIX + r'[A-Za-z0-9_]+', path.name)
            and not path.is_symlink(), 'Invalid owned update snapshot path')
    require(path.is_dir() and (path / 'owner').read_text() == 'cognita-windows-update-v1',
            'Owned update snapshot is missing')
    info = path.stat()
    require(info.st_uid == os.geteuid() and stat.S_IMODE(info.st_mode) == 0o700,
            'Owned update snapshot permissions changed')
    return path


def begin(update):
    before = update.preflight()
    path = Path(tempfile.mkdtemp(prefix=PREFIX))
    try:
        (path / 'owner').write_text('cognita-windows-update-v1')
        (path / 'env').write_bytes(update.env_bytes)
        # Always restore the prior recorded candidate, including partial retries.
        (path / 'snapshot.json').write_text(json.dumps({'record': update.record,
            'old_bundle': str(update.old_bundle), 'candidate': str(update.candidate), 'before': before}))
        for item in path.iterdir():
            item.chmod(0o600)
        return path
    except BaseException:
        shutil.rmtree(path)
        require(not path.exists(), 'Failed update snapshot cleanup')
        raise


def apply(update, path):
    update.snapshot = path
    snapshot = json.loads((path / 'snapshot.json').read_text())
    require(update.preserved() == snapshot['before'], 'Installation changed after update preflight')
    update_phase('apply-load-images', update.load_images)
    (path / 'selected').touch(mode=0o600)
    update.compose('stop', '--timeout', '30', 'cognita', 'workspace-runtime')
    update.selection(update.candidate, update.new, (path / 'env').read_bytes())
    update_phase('apply-toolbox', update.toolbox, update.candidate, update.new)
    update_phase('apply-start-pair', update.start_pair, update.new)
    update_phase('apply-verify', update.verify, update.new, snapshot['before'], snapshot=path)


def rollback(update, path):
    update.snapshot = path
    snapshot = json.loads((path / 'snapshot.json').read_text())
    if not (path / 'selected').exists():
        require(update.preserved() == snapshot['before'], 'Pre-selection preservation verification failed')
        return
    try:
        if (path / 'oneoff').exists():
            update.cleanup_oneoff((path / 'oneoff').read_text())
            (path / 'oneoff').unlink()
        if (path / 'workspace-id').exists():
            owned = (path / 'workspace-id').read_text()
            require(str(uuid.UUID(owned)) == owned, 'Invalid owned verification Workspace ID')
            update.compose('stop', '--timeout', '30', 'cognita')
            update.cleanup_workspace(owned)
            (path / 'workspace-id').unlink()
        update.compose('stop', '--timeout', '30', 'cognita', 'workspace-runtime')
        update.selection(update.old_bundle, update.old, (path / 'env').read_bytes())
        update_phase('rollback-toolbox', update.toolbox, update.old_bundle, update.old)
        update_phase('rollback-start-pair', update.start_pair, update.old)
        update_phase('rollback-verify', update.verify, update.old, snapshot['before'], workspace=False)
    except BaseException:
        update.compose('stop', '--timeout', '30', 'cognita', 'workspace-runtime')
        raise RuntimeError('Prior image pair could not be verified; application/runtime left stopped; PostgreSQL retained') from None


def cleanup(path):
    transaction_path(str(path))
    require(not (path / 'oneoff').exists() and not (path / 'workspace-id').exists(),
            f'Owned update cleanup remains unverified; protected snapshot retained at {path}')
    shutil.rmtree(path)
    require(not path.exists(), 'Owned update snapshot remains after cleanup')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('guard', 'ports', 'current', 'repair', 'begin', 'apply', 'rollback', 'cleanup', 'status', 'stop-pair'))
    parser.add_argument('--record', type=Path)
    parser.add_argument('--old-bundle', type=Path)
    parser.add_argument('--candidate', type=Path)
    parser.add_argument('--snapshot')
    args = parser.parse_args()
    if args.action == 'cleanup':
        cleanup(transaction_path(args.snapshot))
        return
    if args.snapshot:
        path = transaction_path(args.snapshot)
        saved = json.loads((path / 'snapshot.json').read_text())
        update = Update(saved['record'], saved['old_bundle'], saved['candidate'])
    else:
        record = json.loads(args.record.read_text())
        if args.action == 'guard':
            print(json.dumps(preservation_guard(record)))
            return
        if args.action == 'ports':
            values = environment((ROOT / 'compose.env').read_bytes())
            # Only the Compose parser is invoked; no application content is read.
            child_env = dict(os.environ); child_env.update(values)
            config = json.loads(run(compose_prefix(record) + ['config', '--format', 'json'], env=child_env).stdout)
            observer = object.__new__(Update)
            observer.record, observer.run, observer.prefix, observer.values = record, run, compose_prefix(record), values
            observer.version = values['COGNITA_VERSION']
            observer.validate_ports(config)
            return
        update = Update(record, args.old_bundle, args.candidate)
    if args.action == 'begin':
        print(begin(update))
    elif args.action == 'apply':
        apply(update, path)
        print('image replacement and packaged Workspace verification passed')
    elif args.action == 'rollback':
        rollback(update, path)
        print('prior image pair restored and verified')
    elif args.action == 'stop-pair':
        update.compose('stop', '--timeout', '30', 'cognita', 'workspace-runtime')
    elif args.action == 'repair':
        before = update.preflight()
        require(update.version == update.old['version'] and (ROOT / 'compose.cpu.images.yaml').read_bytes()
                == (update.old_bundle / 'compose.cpu.full.images.yaml').read_bytes(),
                'Install/Repair cannot adopt a partial image replacement; retry the canonical deployment')
        for key in ('cognita_cpu', 'workspace_runtime'):
            update.image(update.old['image_ref_' + key], update.old['image_' + key], update.old)
        ready = all(update.container(service, optional=True)
                    and update.container(service)['State'].get('Health', {}).get('Status') == 'healthy'
                    for service in ('cognita', 'workspace-runtime'))
        if not ready:
            update_phase('repair-start-pair', update.start_pair, update.old)
        update_phase(args.action + '-verify', update.verify, update.old, before, workspace=False)
        print('preserved current installation verified')
    elif args.action == 'current':
        before = update.preflight()
        require(update.version == update.old['version'], 'Current release environment differs from the owner record')
        update_phase(args.action + '-verify', update.verify, update.old, before, workspace=False)
        print('current image pair and broker readiness verified')
    elif args.action == 'status':
        print(json.dumps(update.status()))


if __name__ == '__main__':
    if hasattr(signal, 'SIGTERM'):
        signal.signal(signal.SIGTERM, interrupted)
    try:
        main()
    except Exception as exc:
        # Only our bounded diagnostic strings are public. Never print child
        # stdout/stderr, credential contents or preservation fingerprints.
        print('Windows update failed: ' + (str(exc) if isinstance(exc, RuntimeError) else type(exc).__name__), file=__import__('sys').stderr)
        raise SystemExit(1)
