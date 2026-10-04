#!/usr/bin/env python3
"""Verify ownership and prepare the one shared bind root before Docker starts."""
from __future__ import annotations

import base64
import json
import os
import re
import stat
import subprocess
import sys
import time
from pathlib import Path, PurePosixPath

try:
    import pwd
except ImportError:  # Windows-hosted unit tests do not mount sources.
    pwd = None

RECORD_PATH = Path('/etc/cognita-install-record-path')
MARKER_PATH = Path('/etc/cognita-install-id')
ROOT = Path('/srv/cognita/sources')
MOUNT_ROOT = Path('/mnt/cognita-upstreams')
PROJECTION = Path('/run/cognita/source-identities.json')
ALIAS = re.compile(r'^[a-z0-9][a-z0-9_-]{0,31}$')
BIND_CHANGES = False


def run(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(args, text=True, capture_output=True, check=False, timeout=30)
    if check and result.returncode:
        raise RuntimeError(f'{Path(args[0]).name} failed ({result.returncode})')
    return result


def mount_identity(path: Path) -> str:
    """Return an opaque filesystem identity without exposing the source locator."""
    info = os.statvfs(path)
    st = path.stat()
    identity = f'{info.f_fsid:x}:{st.st_dev:x}'
    if path == ROOT / 'cognita-self-test':
        identity += f':{st.st_ino:x}'
    return identity


def runtime_identity(path: Path) -> dict[str, int]:
    """Return the exact stat facts consumed by source_mount_guard schema 1."""
    st = path.stat()
    return {'device': int(st.st_dev), 'inode': int(st.st_ino)}


class SourceIdentityMismatch(RuntimeError):
    """A substituted upstream must be detached while the healthy stack stays up."""


def windows_volume_identity(locator: str) -> str:
    """Observe the Windows volume GUID and serial, independent of WSL mount numbers."""
    # Native volume APIs also handle mount-point paths. Drive letters and DrvFS
    # device numbers can be reassigned or change when WSL reconnects a volume.
    literal = locator.replace("'", "''")
    script = r"""
$ErrorActionPreference = 'Stop'
Add-Type -TypeDefinition @'
using System;
using System.Text;
using System.Runtime.InteropServices;
public static class CognitaVolumeIdentity {
    [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true)]
    public static extern bool GetVolumePathName(string file, StringBuilder path, uint length);
    [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true)]
    public static extern bool GetVolumeNameForVolumeMountPoint(string path, StringBuilder name, uint length);
    [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true)]
    public static extern bool GetVolumeInformation(string path, StringBuilder name, uint size, out uint serial, out uint max, out uint flags, StringBuilder fs, uint fsSize);
}
'@
$path = [Text.StringBuilder]::new(32768)
$guid = [Text.StringBuilder]::new(32768)
$fs = [Text.StringBuilder]::new(256)
$label = [Text.StringBuilder]::new(256)
[uint32]$serial=0; [uint32]$maximum=0; [uint32]$flags=0
if(-not [CognitaVolumeIdentity]::GetVolumePathName('__LOCATOR__',$path,$path.Capacity)) { throw 'volume path unavailable' }
if(-not [CognitaVolumeIdentity]::GetVolumeNameForVolumeMountPoint($path.ToString(),$guid,$guid.Capacity)) { throw 'volume GUID unavailable' }
if(-not [CognitaVolumeIdentity]::GetVolumeInformation($path.ToString(),$label,$label.Capacity,[ref]$serial,[ref]$maximum,[ref]$flags,$fs,$fs.Capacity)) { throw 'volume serial unavailable' }
if($fs.ToString() -cne 'NTFS') { throw 'selected source is not NTFS' }
Write-Output ($guid.ToString().ToLowerInvariant() + ':' + $serial.ToString('x8'))
""".replace('__LOCATOR__', literal)
    encoded = base64.b64encode(script.encode('utf-16-le')).decode('ascii')
    result = run('/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe',
                 '-NoProfile', '-NonInteractive', '-EncodedCommand', encoded, check=False)
    identity = result.stdout.strip().lower()
    if result.returncode or not re.fullmatch(r'\\\\\?\\volume\{[0-9a-f-]{36}\}\\:[0-9a-f]{8}', identity):
        raise RuntimeError('Windows NTFS volume identity could not be verified')
    return identity


def canonical_identity(kind: str, locator: str, upstream: Path) -> str:
    """Bind the owner-side identity to the upstream volume or named SMB share."""
    if kind == 'ntfs':
        return f'ntfs:{windows_volume_identity(locator)}'
    parts = locator.replace('\\', '/').split('/')
    if len(parts) < 5 or not parts[2] or not parts[3]:
        raise RuntimeError('SMB source configuration is invalid')
    share = f'//{parts[2]}/{parts[3]}'.casefold()
    filesystem = run('findmnt', '--noheadings', '--target', str(upstream), '--output', 'FSTYPE').stdout.strip()
    if not filesystem or not re.fullmatch(r'[a-zA-Z0-9_.-]+', filesystem):
        raise RuntimeError('SMB source filesystem type could not be verified')
    return f'smb:{share}:{filesystem}:{mount_identity(upstream)}'


def projection_row(alias: str, source_kind: str, observation: str,
                   identity: dict[str, int]) -> dict:
    return {
        'alias': alias,
        'source_kind': source_kind,
        'observation': observation,
        'identity': identity,
    }


def unavailable_source(row: dict) -> dict:
    """Project the last verified runtime facts for a known absent source."""
    identity = row.get('last_runtime_identity')
    if (not isinstance(identity, dict) or set(identity) != {'device', 'inode'}
            or type(identity['device']) is not int or identity['device'] < 0
            or type(identity['inode']) is not int or identity['inode'] < 0):
        raise RuntimeError(f"initial identity capture failed for alias {row.get('alias', '')}")
    return projection_row(row['alias'], row['source_kind'], 'unavailable', identity)


def installation_record_file() -> Path:
    """Resolve the absolute install-record path stored in the root-owned pointer."""
    try:
        pointer = RECORD_PATH.read_text(encoding='utf-8').strip()
    except FileNotFoundError:
        raise RuntimeError('installation record pointer is missing') from None
    except (OSError, UnicodeError):
        raise RuntimeError('installation record pointer is unreadable or malformed') from None
    if not pointer or '\x00' in pointer or '\n' in pointer or '\r' in pointer:
        raise RuntimeError('installation record pointer is empty or malformed')
    if not PurePosixPath(pointer).is_absolute():
        raise RuntimeError('installation record pointer must name an absolute path')
    record_file = Path(pointer)
    try:
        record_info = record_file.stat()
    except FileNotFoundError:
        raise RuntimeError('installation record target is missing or not a file') from None
    except OSError:
        raise RuntimeError('installation record target is unreadable') from None
    if not stat.S_ISREG(record_info.st_mode):
        raise RuntimeError('installation record target is missing or not a file')
    return record_file


def read_owned_record() -> dict:
    if not MARKER_PATH.is_file():
        raise RuntimeError('installation ownership marker is missing')
    record_file = installation_record_file()
    try:
        record = json.loads(record_file.read_text(encoding='utf-8'))
    except OSError:
        raise RuntimeError('installation record target is unreadable') from None
    except (UnicodeError, json.JSONDecodeError):
        raise RuntimeError('installation record target contains invalid JSON') from None
    if not isinstance(record, dict):
        raise RuntimeError('installation record target must contain a JSON object')
    marker = MARKER_PATH.read_text(encoding='ascii').strip()
    if not record.get('installation_id') or marker != record['installation_id']:
        raise RuntimeError('installation owner marker does not match')
    if record.get('distro') != 'Cognita-Windows' or record.get('source_root') != str(ROOT):
        raise RuntimeError('installation resource identity does not match')
    return record


def write_owned_record(record: dict) -> None:
    """Atomically update install metadata without replacing its path pointer."""
    record_file = installation_record_file()
    temp_record = record_file.with_name(record_file.name + '.tmp')
    try:
        temp_record.write_text(json.dumps(record, separators=(',', ':')) + '\n', encoding='utf-8')
        temp_record.replace(record_file)
    finally:
        temp_record.unlink(missing_ok=True)


def wait_for_drvfs(seconds: int = 30) -> None:
    until = time.monotonic() + seconds
    while time.monotonic() < until:
        if Path('/mnt/c').is_dir():
            return
        time.sleep(.25)
    raise RuntimeError('WSL C: DrvFS mount did not become available')


def bind_source(source: Path, target: Path) -> None:
    global BIND_CHANGES
    if not source.is_dir():
        raise FileNotFoundError
    if target.is_mount():
        current = runtime_identity(target)
        expected = runtime_identity(source)
        if current != expected:
            raise RuntimeError('an existing source alias is mounted from a different upstream identity')
        return
    target.mkdir(parents=True, exist_ok=True)
    run('mount', '--bind', str(source), str(target))
    if runtime_identity(target) != runtime_identity(source):
        raise RuntimeError('new source alias bind does not match its verified upstream identity')
    BIND_CHANGES = True


def unbind_source(target: Path) -> None:
    global BIND_CHANGES
    if target.is_mount():
        result = run('umount', str(target), check=False)
        if result.returncode:
            raise RuntimeError('a stale source alias could not be detached')
        BIND_CHANGES = True


def prepare_source(row: dict, alias_target: Path) -> dict:
    alias = row.get('alias', '')
    kind = row.get('source_kind', '')
    if not ALIAS.fullmatch(alias) or (alias_target.name != alias):
        raise RuntimeError('source alias is invalid')
    if kind not in {'ntfs', 'smb', 'installation_ext4'}:
        raise RuntimeError(f'unsupported source kind for alias {alias}')
    if kind == 'installation_ext4':
        alias_target.mkdir(parents=True, exist_ok=True)
        if pwd is not None:
            service = pwd.getpwnam('cognita-admin')
            os.chown(alias_target, service.pw_uid, service.pw_gid)
            alias_target.chmod(0o750)
        identity = mount_identity(alias_target)
        expected = row.get('canonical_identity')
        if expected and expected != identity:
            raise RuntimeError('reserved Self-Test source identity changed')
        if not expected:
            row['canonical_identity'] = identity
        facts = runtime_identity(alias_target)
        row['last_runtime_identity'] = facts
        return projection_row(alias, kind, 'available', facts)

    locator = row.get('locator', '')
    if not locator:
        raise RuntimeError(f'source configuration is incomplete for alias {alias}')
    if kind == 'ntfs':
        converted = run('wslpath', '-a', locator.replace('\\', '/'), check=False)
        upstream = Path(converted.stdout.strip()) if converted.returncode == 0 else Path('/__missing__')
    elif kind == 'smb':
        share = locator.split('\\')[2:4]
        if len(share) != 2:
            raise RuntimeError(f'SMB source configuration is invalid for alias {alias}')
        mountpoint = MOUNT_ROOT / alias
        mountpoint.mkdir(parents=True, exist_ok=True)
        normalized_share = '//' + '/'.join(share)
        if not mountpoint.is_mount():
            result = run('mount', '-t', 'drvfs', normalized_share, str(mountpoint), check=False)
            if result.returncode:
                if not row.get('canonical_identity'):
                    raise RuntimeError(f'initial identity capture failed for alias {alias}')
                unbind_source(alias_target)
                return unavailable_source(row)
        suffix = locator.replace('\\', '/').split('/', 4)
        relative = '/'.join(suffix[4:])
        upstream = mountpoint / relative
    else:
        raise RuntimeError(f'unsupported source kind for alias {alias}')

    if not upstream.is_dir():
        if not row.get('canonical_identity'):
            raise RuntimeError(f'initial identity capture failed for alias {alias}')
        unbind_source(alias_target)
        if kind == 'smb' and mountpoint.is_mount():
            # A disconnected DrvFS share can leave an old mountpoint behind.
            # Detach only this recorded alias so Repair can mount it afresh.
            result = run('umount', str(mountpoint), check=False)
            if result.returncode:
                raise RuntimeError('a disconnected SMB source mount could not be detached')
        return unavailable_source(row)
    expected = row.get('canonical_identity')
    try:
        current_identity = canonical_identity(kind, locator, upstream)
    except RuntimeError:
        if not expected:
            raise
        unbind_source(alias_target)
        raise SourceIdentityMismatch(
            f'configured source identity could not be verified for alias {alias}; the alias is unavailable. '
            'Restore source access and rerun Repair.') from None
    if expected and expected != current_identity:
        unbind_source(alias_target)
        raise SourceIdentityMismatch(
            f'configured source identity changed for alias {alias}; the alias is unavailable. '
            'Restore the recorded source or explicitly reconfigure that source before Repair.')
    if not expected:
        # Initial install captures identity only from a present upstream.
        row['canonical_identity'] = current_identity
    bind_source(upstream, alias_target)
    facts = runtime_identity(alias_target)
    row['last_runtime_identity'] = facts
    return projection_row(alias, kind, 'available', facts)


def prepare() -> bool:
    global BIND_CHANGES
    BIND_CHANGES = False
    record = read_owned_record()
    wait_for_drvfs()
    ROOT.mkdir(parents=True, exist_ok=True)
    # A self-bind gives rslave a stable shared parent before dockerd starts.
    if not ROOT.is_mount():
        run('mount', '--bind', str(ROOT), str(ROOT))
    run('mount', '--make-rshared', str(ROOT))
    observations = []
    mismatches = []
    identity_changed = False
    seen: set[str] = set()
    for row in record.get('sources', []):
        alias = row.get('alias', '')
        if alias.casefold() in seen:
            raise RuntimeError('duplicate source alias in owner record')
        seen.add(alias.casefold())
        target = ROOT / alias
        prior_identity = row.get('canonical_identity')
        prior_runtime_identity = row.get('last_runtime_identity')
        try:
            observations.append(prepare_source(row, target))
        except SourceIdentityMismatch as exc:
            # Publish a complete, validated unavailable observation and detach
            # only this alias. A running app independently sees the child unmount.
            observations.append(unavailable_source(row))
            mismatches.append(str(exc))
        identity_changed |= (prior_identity != row.get('canonical_identity')
                             or prior_runtime_identity != row.get('last_runtime_identity'))
    if identity_changed:
        # install.json remains the one lifecycle authority. DrvFS can traverse
        # the fixed host directory, while this file stays owner/admin-only;
        # replacement is same-directory and atomic.
        write_owned_record(record)
    PROJECTION.parent.mkdir(parents=True, exist_ok=True)
    projection_text = json.dumps({'schema': 1, 'sources': observations}, separators=(',', ':')) + '\n'
    projection_changed = BIND_CHANGES or not PROJECTION.is_file() or PROJECTION.read_text(encoding='utf-8') != projection_text
    if projection_changed:
        temporary = PROJECTION.with_suffix('.tmp')
        try:
            temporary.write_text(projection_text, encoding='utf-8')
            # The app runs as the nonroot service UID. This projection contains no
            # locator, credential, bundle path, or installation identity.
            temporary.chmod(0o644)
            temporary.replace(PROJECTION)
        finally:
            temporary.unlink(missing_ok=True)
    if mismatches:
        raise SourceIdentityMismatch('; '.join(mismatches))
    return projection_changed


if __name__ == '__main__':
    try:
        projection_changed = prepare()
    except Exception as exc:  # Diagnostics deliberately omit all paths and locators.
        detail = str(exc) if isinstance(exc, RuntimeError) else type(exc).__name__
        print(f'cognita source preparation failed: {detail}', file=sys.stderr)
        raise SystemExit(1)
    count = len(json.loads(PROJECTION.read_text())['sources'])
    print(f'cognita source preparation complete: {count} aliases projection_changed={str(projection_changed).lower()}')
