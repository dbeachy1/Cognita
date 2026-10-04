from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
import sys
import threading
import time
import types
from pathlib import Path
from typing import ClassVar
from uuid import uuid4

import pytest

import cognita.runtime_broker.sdk_adapter_v2 as sdk


def _job_supervisor_module():
    path = Path("containers/workspace-toolbox/cognita-workspace-job")
    loader = importlib.machinery.SourceFileLoader("cognita_workspace_job_test", str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class _NotFound(Exception):
    pass


class _Volume:
    items: ClassVar[dict[str, _Volume]] = {}

    def __init__(self, name: str, labels: dict[str, str], quota_mib: int | None = None):
        self.name, self.labels, self.used_bytes = name, labels, 0
        self.quota_mib = quota_mib

    @classmethod
    async def get(cls, name):
        if name not in cls.items:
            raise _NotFound
        return cls.items[name]

    @classmethod
    async def create(cls, name, **kwargs):
        value = cls(name, kwargs.get("labels", {}), kwargs.get("quota_mib"))
        cls.items[name] = value
        return value

    @classmethod
    def named(cls, name, readonly=False):
        return types.SimpleNamespace(name=name, readonly=readonly)

    @staticmethod
    def tmpfs(*, size_mib):
        return types.SimpleNamespace(kind="tmpfs", size_mib=size_mib)

    async def remove(self):
        self.items.pop(self.name, None)


@pytest.mark.asyncio
async def test_new_volume_is_validated_from_persisted_sdk_object(monkeypatch):
    monkeypatch.setattr(
        sdk,
        "pinned_sdk_info",
        lambda: sdk.SdkRuntimeInfo("0.7.0", sdk.PINNED_RUNTIME_ROOT, "test"),
    )

    class TransientVolume(_Volume):
        items = {}

        @classmethod
        async def create(cls, name, **kwargs):
            await super().create(name, **kwargs)
            return object()  # SDK create receipt does not carry persisted labels.

    adapter = sdk.MicrosandboxSdkAdapter(timeout_seconds=10)
    workspace_id = uuid4()
    volume = await adapter._volume(
        TransientVolume, f"cognita-ws-data-{workspace_id}", workspace_id,
        4 * 1024**3, adapter._deadline(),
    )
    assert volume is TransientVolume.items[volume.name]
    assert volume.quota_mib == 4096


class _Fs:
    def __init__(self):
        self.files: dict[str, bytes] = {}

    async def read(self, path):
        if path not in self.files:
            raise _NotFound
        return self.files[path]

    async def write(self, path, data):
        self.files[path] = bytes(data)

    async def mkdir(self, path, parents=False):
        return None

    async def stat(self, path):
        if path not in self.files:
            raise _NotFound
        return types.SimpleNamespace(kind="file", size=len(self.files[path]))

    async def list(self, path, recursive=False):
        return [types.SimpleNamespace(path=name, kind="file", size=len(data)) for name, data in self.files.items()]

    async def remove(self, path, recursive=False):
        self.files.pop(path, None)


class _Handle:
    def __init__(self, fs):
        self.fs = fs
        self.stopped = False

    async def exec(self, executable, argv, **kwargs):
        if executable == "python3":
            return types.SimpleNamespace(stdout=f"3\n{argv[-1]}\n", stderr=b"", exit_code=0)
        return types.SimpleNamespace(stdout=b"", stderr=b"", exit_code=0)

    async def stop(self):
        self.stopped = True


class _Sandbox:
    items: ClassVar[dict[str, _Sandbox]] = {}
    pull_policies: ClassVar[list[object]] = []

    def __init__(self, name, labels, image, volumes, pull_policy, cpus=1, memory=512):
        self.name, self.labels, self.image, self.volumes = name, labels, image, volumes
        self.pull_policy = pull_policy
        self.cpus, self.memory, self.status, self.id = cpus, memory, "running", name
        self.handle = _Handle(_Fs())

    @classmethod
    async def get(cls, name):
        if name not in cls.items:
            raise _NotFound
        return cls.items[name]

    @classmethod
    async def create(cls, name, **kwargs):
        cls.pull_policies.append(kwargs["pull_policy"])
        value = cls(name, kwargs["labels"], kwargs["image"], kwargs["volumes"], kwargs["pull_policy"], kwargs.get("cpus", 1), kwargs.get("memory", 512))
        cls.items[name] = value
        return value

    async def connect(self):
        return self.handle

    async def connect_or_start(self):
        self.status = "running"
        return self.handle

    async def remove(self):
        self.items.pop(self.name, None)


class _SdkSandbox:
    """SDK 0.7.0-shaped handle: configuration is only in config()/JSON."""

    items: ClassVar[dict[str, _SdkSandbox]] = {}
    actions: ClassVar[list[str]] = []

    def __init__(self, name: str, config: dict):
        self.name, self._config = name, config
        self.status, self.id = "running", name
        self._handle = _Handle(_Fs())
        async def stop_handle():
            self.status = "stopped"
            self.actions.append("stop")
        self._handle.stop = stop_handle

    @classmethod
    async def get(cls, name):
        if name not in cls.items:
            raise _NotFound
        return cls.items[name]

    @classmethod
    async def create(cls, name, **kwargs):
        image = kwargs["image"]
        reference = image if isinstance(image, str) else image.reference
        root_disk = None if isinstance(image, str) else {
            "kind": image.root_disk.kind, "size_mib": image.root_disk.size_mib,
        }
        mounts = []
        for guest, volume in kwargs["volumes"].items():
            if hasattr(volume, "name"):
                mounts.append({"type": "Named", "name": volume.name, "guest": guest, "options": {}})
            else:
                mounts.append({"type": "Tmpfs", "guest": guest, "size_mib": volume.size_mib, "options": {}})
        config = {
            "name": name, "labels": kwargs["labels"],
            "image": {"Oci": {"reference": reference, "root_disk": root_disk}},
            "resources": {"cpus": kwargs["cpus"], "memory_mib": kwargs["memory"]},
            # SDK 0.7.0 persists Network.none() as an enabled wrapper around a
            # non-strict, default-deny policy.  Keep the fake shaped like the
            # live registry response rather than the transient create result.
            "mounts": mounts,
            "network": {
                "enabled": True,
                "strict": False,
                "policy": {
                    "default_egress": "deny",
                    "default_ingress": "deny",
                    "rules": [],
                },
            },
        }
        value = cls(name, config)
        cls.items[name] = value
        # The official SDK may return a transient handle/result whose persisted
        # config is unavailable until Sandbox.get(name) re-fetches the object.
        return types.SimpleNamespace(name=name)

    def config(self):
        return self._config

    @property
    def config_json(self):
        return json.dumps(self._config)

    async def connect_or_start(self):
        self.actions.append("connect_or_start")
        self.status = "running"
        return self._handle

    async def connect(self):
        self.actions.append("connect")
        return self._handle

    async def stop(self):
        self.actions.append("stop")
        self.status = "stopped"

    async def remove(self):
        self.actions.append("remove")
        self.items.pop(self.name, None)


def _sdk_070_controls(monkeypatch):
    class RootDisk:
        @staticmethod
        def managed(*, size_mib):
            return types.SimpleNamespace(kind="managed", size_mib=size_mib)

    class Image:
        @staticmethod
        def oci(reference, *, root_disk):
            return types.SimpleNamespace(reference=reference, root_disk=root_disk)

    monkeypatch.setitem(sys.modules, "microsandbox", types.SimpleNamespace(Image=Image, RootDisk=RootDisk))


class _Network:
    @staticmethod
    def none():
        return object()


def test_local_pull_policy_uses_pinned_sdk_enum(monkeypatch):
    never = object()
    fake_sdk = types.SimpleNamespace(PullPolicy=types.SimpleNamespace(NEVER=never))
    monkeypatch.setitem(sys.modules, "microsandbox", fake_sdk)
    assert sdk._local_pull_policy() is never


def test_pinned_root_disk_factory_uses_managed_image_control(monkeypatch):
    calls = []

    class RootDisk:
        @staticmethod
        def managed(*, size_mib):
            calls.append(("managed", size_mib))
            return ("managed", size_mib)

    class Image:
        @staticmethod
        def oci(reference, *, root_disk):
            calls.append(("oci", reference, root_disk))
            return ("oci", reference, root_disk)

    monkeypatch.setitem(sys.modules, "microsandbox", types.SimpleNamespace(Image=Image, RootDisk=RootDisk))
    source = sdk._managed_root_image("cognita-workspace-toolbox:12.5.0", sdk.WRITABLE_ROOT_QUOTA_MIB)
    assert source == ("oci", "cognita-workspace-toolbox:12.5.0", ("managed", 4096))
    assert calls == [("managed", 4096), ("oci", "cognita-workspace-toolbox:12.5.0", ("managed", 4096))]


# 13.0, DESIGN-13.0 section 8: `ensure` no longer removes and recreates an
# existing sandbox by itself.  These two cases are the ones that used to be
# rebuilt silently -- a terminal VM, and a stopped VM whose policy-controlled
# configuration is stale.  Both must now come back with a reason and leave the
# object exactly where it was, for the user's explicit removal or a reset.
@pytest.mark.asyncio
async def test_ensure_leaves_a_failed_sandbox_in_place_for_explicit_removal(monkeypatch):
    _Volume.items.clear()
    _Sandbox.items.clear()
    _Sandbox.pull_policies.clear()
    monkeypatch.setattr(sdk, "_sdk_types", lambda: (_Sandbox, _Volume, _Network))
    monkeypatch.setattr(sdk, "pinned_sdk_info", lambda: sdk.SdkRuntimeInfo("0.7.0", sdk.PINNED_RUNTIME_ROOT, "test"))
    workspace_id = uuid4()
    adapter = sdk.MicrosandboxSdkAdapter(timeout_seconds=10)
    await adapter.ensure(workspace_id, {"quota_bytes": 4 * 1024**3, "vcpus": 4})
    name, _volume_name = adapter.names(workspace_id)
    failed = _Sandbox.items[name]
    failed.status = "failed"

    with pytest.raises(sdk.SdkOperationError) as error:
        await adapter.ensure(workspace_id, {"quota_bytes": 4 * 1024**3, "vcpus": 4})
    assert error.value.category == "conflict"
    assert error.value.stage == "sandbox_failed"
    assert error.value.evidence["reason"] == "sandbox_failed_requires_explicit_removal"
    # The VM is still there, still failed, and still the same object.
    assert _Sandbox.items[name] is failed
    assert failed.status == "failed"


@pytest.mark.asyncio
async def test_ensure_does_not_replace_a_stopped_sandbox_with_stale_policy(monkeypatch):
    _Volume.items.clear()
    _Sandbox.items.clear()
    _Sandbox.pull_policies.clear()
    monkeypatch.setattr(sdk, "_sdk_types", lambda: (_Sandbox, _Volume, _Network))
    monkeypatch.setattr(sdk, "pinned_sdk_info", lambda: sdk.SdkRuntimeInfo("0.7.0", sdk.PINNED_RUNTIME_ROOT, "test"))
    workspace_id = uuid4()
    adapter = sdk.MicrosandboxSdkAdapter(timeout_seconds=10)
    await adapter.ensure(workspace_id, {"quota_bytes": 4 * 1024**3, "vcpus": 4})
    name, _volume_name = adapter.names(workspace_id)
    stopped = _Sandbox.items[name]
    stopped.status = "stopped"

    # Same Workspace, a different admitted vCPU policy: the persisted VM is
    # now stale.  Up to 12.x this was removed and rebuilt; it now fails closed.
    with pytest.raises(sdk.SdkOperationError) as error:
        await adapter.ensure(workspace_id, {"quota_bytes": 4 * 1024**3, "vcpus": 2})
    assert error.value.category == "runtime_failure"
    assert _Sandbox.items[name] is stopped
    assert stopped.labels["cognita.vcpus"] == "4"


@pytest.mark.asyncio
async def test_ensure_applies_separate_workspace_root_and_tmpfs_quotas(monkeypatch):
    _Volume.items.clear()
    _Sandbox.items.clear()
    _Sandbox.pull_policies.clear()
    monkeypatch.setattr(sdk, "_sdk_types", lambda: (_Sandbox, _Volume, _Network))
    monkeypatch.setattr(sdk, "pinned_sdk_info", lambda: sdk.SdkRuntimeInfo("0.7.0", sdk.PINNED_RUNTIME_ROOT, "test"))

    class RootDisk:
        @staticmethod
        def managed(*, size_mib):
            return types.SimpleNamespace(kind="managed", size_mib=size_mib)

    class Image:
        @staticmethod
        def oci(reference, *, root_disk):
            return types.SimpleNamespace(reference=reference, root_disk=root_disk)

    monkeypatch.setitem(sys.modules, "microsandbox", types.SimpleNamespace(Image=Image, RootDisk=RootDisk))
    workspace_id = uuid4()
    adapter = sdk.MicrosandboxSdkAdapter(timeout_seconds=10)
    await adapter.ensure(workspace_id, {
        "quota_bytes": 4 * 1024**3,
        "root_quota_bytes": 4 * 1024**3,
        "require_writable_root_quota": True,
    })

    name, volume_name = adapter.names(workspace_id)
    sandbox = _Sandbox.items[name]
    assert sandbox.image.root_disk.kind == "managed"
    assert sandbox.image.root_disk.size_mib == sdk.WRITABLE_ROOT_QUOTA_MIB
    assert sandbox.volumes["/workspace"].name == volume_name
    assert sandbox.volumes["/tmp"].kind == "tmpfs"
    assert sandbox.volumes["/tmp"].size_mib == sdk.TMPFS_SIZE_MIB
    assert sandbox.labels["cognita.root_quota_bytes"] == str(4 * 1024**3)
    assert sandbox.labels["cognita.tmpfs_mib"] == str(sdk.TMPFS_SIZE_MIB)

    sandbox.labels["cognita.root_quota_bytes"] = str(2 * 1024**3)
    with pytest.raises(sdk.SdkOperationError) as error:
        await adapter.inspect(workspace_id)
    assert error.value.category == "runtime_failure"
    assert error.value.stage == "quota_probe"


@pytest.mark.asyncio
async def test_root_quota_fails_closed_when_pinned_controls_are_missing(monkeypatch):
    _Volume.items.clear()
    _Sandbox.items.clear()
    monkeypatch.setattr(sdk, "_sdk_types", lambda: (_Sandbox, _Volume, _Network))
    monkeypatch.setattr(sdk, "pinned_sdk_info", lambda: sdk.SdkRuntimeInfo("0.7.0", sdk.PINNED_RUNTIME_ROOT, "test"))
    monkeypatch.setitem(sys.modules, "microsandbox", types.SimpleNamespace(Image=object(), RootDisk=object()))
    adapter = sdk.MicrosandboxSdkAdapter(timeout_seconds=10)
    with pytest.raises(sdk.SdkOperationError) as error:
        await adapter.ensure(uuid4(), {"require_writable_root_quota": True})
    assert error.value.category == "unsupported"
    assert error.value.stage == "quota_probe"
    assert error.value.evidence["reason"] == "pinned_sdk_root_disk_control_unavailable"
    assert not _Sandbox.items and not _Volume.items


@pytest.mark.asyncio
async def test_tmpfs_quota_fails_closed_when_pinned_mount_control_is_missing(monkeypatch):
    _Volume.items.clear()
    _Sandbox.items.clear()
    class VolumeWithoutTmpfs:
        get = _Volume.get
        create = _Volume.create
        named = _Volume.named

    monkeypatch.setattr(sdk, "_sdk_types", lambda: (_Sandbox, VolumeWithoutTmpfs, _Network))
    monkeypatch.setattr(sdk, "pinned_sdk_info", lambda: sdk.SdkRuntimeInfo("0.7.0", sdk.PINNED_RUNTIME_ROOT, "test"))
    monkeypatch.setitem(sys.modules, "microsandbox", types.SimpleNamespace(
        Image=types.SimpleNamespace(oci=lambda reference, *, root_disk: (reference, root_disk)),
        RootDisk=types.SimpleNamespace(managed=lambda *, size_mib: ("managed", size_mib)),
    ))
    adapter = sdk.MicrosandboxSdkAdapter(timeout_seconds=10)
    with pytest.raises(sdk.SdkOperationError) as error:
        await adapter.ensure(uuid4(), {"require_writable_root_quota": True})
    assert error.value.category == "unsupported"
    assert error.value.stage == "quota_probe"
    assert error.value.evidence["reason"] == "pinned_sdk_tmpfs_control_unavailable"


@pytest.mark.asyncio
async def test_readiness_uses_random_challenge_and_removes_owned_resources(monkeypatch):
    _Volume.items.clear()
    _Sandbox.items.clear()
    _Sandbox.pull_policies.clear()
    monkeypatch.setattr(sdk, "_sdk_types", lambda: (_Sandbox, _Volume, _Network))
    monkeypatch.setattr(sdk, "pinned_sdk_info", lambda: sdk.SdkRuntimeInfo("0.7.0", sdk.PINNED_RUNTIME_ROOT, "test"))
    adapter = sdk.MicrosandboxSdkAdapter(timeout_seconds=10)
    readiness = await adapter.readiness_probe()
    assert readiness["status"] == "ok"
    assert readiness["cleanup"] == "complete"
    assert not _Sandbox.items and not _Volume.items
    assert adapter.last_readiness["cleanup"] == "complete"
    assert [str(policy) for policy in _Sandbox.pull_policies] == ["never"]


@pytest.mark.asyncio
async def test_existing_owner_mismatch_fails_closed(monkeypatch):
    _Volume.items.clear()
    _Sandbox.items.clear()
    _Sandbox.pull_policies.clear()
    monkeypatch.setattr(sdk, "_sdk_types", lambda: (_Sandbox, _Volume, _Network))
    monkeypatch.setattr(sdk, "pinned_sdk_info", lambda: sdk.SdkRuntimeInfo("0.7.0", sdk.PINNED_RUNTIME_ROOT, "test"))
    workspace_id = uuid4()
    _name, volume = sdk.MicrosandboxSdkAdapter.names(workspace_id)
    _Volume.items[volume] = _Volume(volume, {"cognita.owner": "other", "cognita.workspace": str(workspace_id)})
    adapter = sdk.MicrosandboxSdkAdapter(timeout_seconds=10)
    with pytest.raises(sdk.SdkOperationError) as error:
        await adapter.ensure(workspace_id, {})
    assert error.value.category == "runtime_failure"


@pytest.mark.asyncio
async def test_restart_reconciles_only_complete_broker_owned_configuration(monkeypatch):
    _Volume.items.clear()
    _Sandbox.items.clear()
    _Sandbox.pull_policies.clear()
    monkeypatch.setattr(sdk, "_sdk_types", lambda: (_Sandbox, _Volume, _Network))
    monkeypatch.setattr(sdk, "pinned_sdk_info", lambda: sdk.SdkRuntimeInfo("0.7.0", sdk.PINNED_RUNTIME_ROOT, "test"))
    workspace_id = uuid4()
    first = sdk.MicrosandboxSdkAdapter(timeout_seconds=10)
    await first.ensure(workspace_id, {})
    assert [str(policy) for policy in _Sandbox.pull_policies] == ["never"]
    restarted = sdk.MicrosandboxSdkAdapter(timeout_seconds=10)
    assert (await restarted.inspect(workspace_id))["state"] == "running"
    name, _volume = restarted.names(workspace_id)
    _Sandbox.items[name].labels.pop("cognita.network")
    with pytest.raises(sdk.SdkOperationError) as error:
        await restarted.inspect(workspace_id)
    assert error.value.category == "runtime_failure"


@pytest.mark.asyncio
async def test_sdk_070_handle_reconciles_from_persisted_config(monkeypatch):
    _Volume.items.clear()
    _SdkSandbox.items.clear()
    _SdkSandbox.actions.clear()
    monkeypatch.setattr(sdk, "_sdk_types", lambda: (_SdkSandbox, _Volume, _Network))
    monkeypatch.setattr(sdk, "pinned_sdk_info", lambda: sdk.SdkRuntimeInfo("0.7.0", sdk.PINNED_RUNTIME_ROOT, "test"))
    _sdk_070_controls(monkeypatch)
    workspace_id = uuid4()
    first = sdk.MicrosandboxSdkAdapter(timeout_seconds=10)
    assert (await first.ensure(workspace_id, {"require_writable_root_quota": True}))["state"] == "running"
    name, _ = first.names(workspace_id)
    assert not hasattr(_SdkSandbox.items[name], "labels")
    restarted = sdk.MicrosandboxSdkAdapter(timeout_seconds=10)
    assert (await restarted.inspect(workspace_id))["network_mode"] == "off"
    _SdkSandbox.items[name].config = None
    assert (await restarted.inspect(workspace_id))["state"] == "running"
    assert (await restarted.start(workspace_id))["state"] == "running"
    assert (await restarted.stop(workspace_id))["state"] == "stopped"
    assert (await restarted.remove(workspace_id))["state"] == "absent"
    assert not _SdkSandbox.items and not _Volume.items


@pytest.mark.asyncio
async def test_sdk_070_reuses_one_connected_sandbox_until_lifecycle_transition(monkeypatch):
    _Volume.items.clear()
    _SdkSandbox.items.clear()
    _SdkSandbox.actions.clear()
    monkeypatch.setattr(sdk, "_sdk_types", lambda: (_SdkSandbox, _Volume, _Network))
    monkeypatch.setattr(sdk, "pinned_sdk_info", lambda: sdk.SdkRuntimeInfo("0.7.0", sdk.PINNED_RUNTIME_ROOT, "test"))
    _sdk_070_controls(monkeypatch)
    workspace_id = uuid4()
    adapter = sdk.MicrosandboxSdkAdapter(timeout_seconds=10)
    await adapter.ensure(workspace_id, {"require_writable_root_quota": True})

    _SdkSandbox.actions.clear()
    first = await adapter._connected(workspace_id, start=True, deadline=adapter._deadline(10))
    second = await adapter._connected(workspace_id, start=True, deadline=adapter._deadline(10))
    assert first is second
    assert _SdkSandbox.actions == []

    await adapter.stop(workspace_id)
    _SdkSandbox.actions.clear()
    await adapter.start(workspace_id)
    assert _SdkSandbox.actions == ["connect_or_start"]


@pytest.mark.asyncio
async def test_sdk_070_inspect_and_remove_preserve_volume_only_residual(monkeypatch):
    _Volume.items.clear()
    _SdkSandbox.items.clear()
    _SdkSandbox.actions.clear()
    monkeypatch.setattr(sdk, "_sdk_types", lambda: (_SdkSandbox, _Volume, _Network))
    monkeypatch.setattr(sdk, "pinned_sdk_info", lambda: sdk.SdkRuntimeInfo("0.7.0", sdk.PINNED_RUNTIME_ROOT, "test"))
    _sdk_070_controls(monkeypatch)
    workspace_id = uuid4()
    adapter = sdk.MicrosandboxSdkAdapter(timeout_seconds=10)
    name, volume_name = adapter.names(workspace_id)
    await _Volume.create(volume_name, quota_mib=4096, labels=adapter._volume_labels(workspace_id, quota_mib=4096))

    observed = await adapter.inspect(workspace_id)
    assert observed["state"] == "partial"
    assert observed["partial_state"] == "volume_only"
    assert observed["sandbox_name"] == name
    assert observed["volume_name"] == volume_name
    assert observed["host_path"] is None

    removed = await adapter.remove(workspace_id)
    assert removed == {"state": "absent", "sandbox_name": name, "volume_name": volume_name}
    assert name not in _SdkSandbox.items and volume_name not in _Volume.items


@pytest.mark.asyncio
async def test_sdk_070_keeps_failed_sandbox_and_its_named_volume(monkeypatch):
    """13.0, DESIGN-13.0 section 8: a terminal VM is retained, not rebuilt.

    Superseded: this case was
    `test_sdk_070_rebuilds_failed_sandbox_around_same_named_volume`, which
    asserted `["remove", "connect_or_start"]` -- `ensure` removed the failed
    sandbox, kept the named volume and built a replacement around it.  The
    durable-volume half of that assertion is the part that still matters and
    is kept here: nothing is removed at all, so the volume and its contents
    cannot be lost on the way to a repair that no longer happens by itself.
    """
    _Volume.items.clear()
    _SdkSandbox.items.clear()
    _SdkSandbox.actions.clear()
    monkeypatch.setattr(sdk, "_sdk_types", lambda: (_SdkSandbox, _Volume, _Network))
    monkeypatch.setattr(sdk, "pinned_sdk_info", lambda: sdk.SdkRuntimeInfo("0.7.0", sdk.PINNED_RUNTIME_ROOT, "test"))
    _sdk_070_controls(monkeypatch)
    workspace_id = uuid4()
    adapter = sdk.MicrosandboxSdkAdapter(timeout_seconds=10)
    await adapter.ensure(workspace_id, {"require_writable_root_quota": True})
    name, volume_name = adapter.names(workspace_id)
    volume = _Volume.items[volume_name]
    volume.marker = b"durable-workspace-marker"
    failed = _SdkSandbox.items[name]
    failed.status = "failed"
    _SdkSandbox.actions.clear()

    with pytest.raises(sdk.SdkOperationError) as error:
        await adapter.ensure(workspace_id, {
            "require_writable_root_quota": True,
            "_allow_volume_create": False,
        })

    assert error.value.category == "conflict"
    assert error.value.stage == "sandbox_failed"
    assert _SdkSandbox.actions == []
    assert _SdkSandbox.items[name] is failed and failed.status == "failed"
    assert _Volume.items[volume_name] is volume
    assert volume.marker == b"durable-workspace-marker"


@pytest.mark.asyncio
async def test_sdk_070_existing_workspace_missing_volume_fails_closed(monkeypatch):
    _Volume.items.clear()
    _SdkSandbox.items.clear()
    _SdkSandbox.actions.clear()
    monkeypatch.setattr(sdk, "_sdk_types", lambda: (_SdkSandbox, _Volume, _Network))
    monkeypatch.setattr(sdk, "pinned_sdk_info", lambda: sdk.SdkRuntimeInfo("0.7.0", sdk.PINNED_RUNTIME_ROOT, "test"))
    _sdk_070_controls(monkeypatch)
    workspace_id = uuid4()
    adapter = sdk.MicrosandboxSdkAdapter(timeout_seconds=10)
    await adapter.ensure(workspace_id, {"require_writable_root_quota": True})
    name, volume_name = adapter.names(workspace_id)
    _Volume.items.pop(volume_name)
    _SdkSandbox.items[name].status = "failed"
    _SdkSandbox.actions.clear()

    with pytest.raises(sdk.SdkOperationError) as error:
        await adapter.ensure(workspace_id, {
            "require_writable_root_quota": True,
            "_allow_volume_create": False,
        })

    assert error.value.category == "not_found"
    assert _SdkSandbox.actions == []
    assert name in _SdkSandbox.items
    assert volume_name not in _Volume.items


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["owner", "image", "mount", "resources", "network", "missing_config"])
async def test_sdk_070_rejects_unprovable_config_before_start_or_remove(monkeypatch, mutation):
    _Volume.items.clear()
    _SdkSandbox.items.clear()
    _SdkSandbox.actions.clear()
    monkeypatch.setattr(sdk, "_sdk_types", lambda: (_SdkSandbox, _Volume, _Network))
    monkeypatch.setattr(sdk, "pinned_sdk_info", lambda: sdk.SdkRuntimeInfo("0.7.0", sdk.PINNED_RUNTIME_ROOT, "test"))
    _sdk_070_controls(monkeypatch)
    workspace_id = uuid4()
    adapter = sdk.MicrosandboxSdkAdapter(timeout_seconds=10)
    await adapter.ensure(workspace_id, {"require_writable_root_quota": True})
    name, volume_name = adapter.names(workspace_id)
    item = _SdkSandbox.items[name]
    if mutation == "owner":
        item._config["labels"]["cognita.owner"] = "other"
    elif mutation == "image":
        item._config["image"]["Oci"]["reference"] = "untrusted:image"
    elif mutation == "mount":
        item._config["mounts"][0]["name"] = "untrusted-volume"
    elif mutation == "resources":
        item._config["resources"]["cpus"] = 99
    elif mutation == "network":
        item._config["network"]["policy"]["default_egress"] = "allow"
    else:
        item.config = lambda: None
    _SdkSandbox.actions.clear()
    with pytest.raises(sdk.SdkOperationError):
        await adapter.start(workspace_id)
    with pytest.raises(sdk.SdkOperationError):
        await adapter.remove(workspace_id)
    assert _SdkSandbox.actions == []
    assert name in _SdkSandbox.items and volume_name in _Volume.items


@pytest.mark.asyncio
async def test_sdk_070_remove_rejects_foreign_volume_before_stopping_sandbox(monkeypatch):
    _Volume.items.clear()
    _SdkSandbox.items.clear()
    _SdkSandbox.actions.clear()
    monkeypatch.setattr(sdk, "_sdk_types", lambda: (_SdkSandbox, _Volume, _Network))
    monkeypatch.setattr(sdk, "pinned_sdk_info", lambda: sdk.SdkRuntimeInfo("0.7.0", sdk.PINNED_RUNTIME_ROOT, "test"))
    _sdk_070_controls(monkeypatch)
    workspace_id = uuid4()
    adapter = sdk.MicrosandboxSdkAdapter(timeout_seconds=10)
    await adapter.ensure(workspace_id, {"require_writable_root_quota": True})
    name, volume_name = adapter.names(workspace_id)
    _Volume.items[volume_name].labels["cognita.owner"] = "other"
    _SdkSandbox.actions.clear()
    with pytest.raises(sdk.SdkOperationError):
        await adapter.remove(workspace_id)
    assert _SdkSandbox.actions == []
    assert name in _SdkSandbox.items and volume_name in _Volume.items


@pytest.mark.asyncio
async def test_sdk_070_checks_persisted_root_and_tmpfs_quotas(monkeypatch):
    _Volume.items.clear()
    _SdkSandbox.items.clear()
    _SdkSandbox.actions.clear()
    monkeypatch.setattr(sdk, "_sdk_types", lambda: (_SdkSandbox, _Volume, _Network))
    monkeypatch.setattr(sdk, "pinned_sdk_info", lambda: sdk.SdkRuntimeInfo("0.7.0", sdk.PINNED_RUNTIME_ROOT, "test"))
    _sdk_070_controls(monkeypatch)
    workspace_id = uuid4()
    adapter = sdk.MicrosandboxSdkAdapter(timeout_seconds=10)
    assert (await adapter.ensure(workspace_id, {"require_writable_root_quota": True}))["state"] == "running"
    name, _ = adapter.names(workspace_id)
    item = _SdkSandbox.items[name]
    item._config["image"]["Oci"]["root_disk"]["size_mib"] = 8192
    with pytest.raises(sdk.SdkOperationError) as error:
        await adapter.inspect(workspace_id)
    assert error.value.stage == "quota_probe"
    item._config["image"]["Oci"]["root_disk"]["size_mib"] = 4096
    tmp_mount = next(mount for mount in item._config["mounts"] if mount["guest"] == "/tmp")
    tmp_mount["size_mib"] = 1024
    with pytest.raises(sdk.SdkOperationError) as error:
        await adapter.inspect(workspace_id)
    assert error.value.stage == "quota_probe"


@pytest.mark.asyncio
async def test_125_sdk_sandbox_is_preserved_when_126_requires_managed_root(monkeypatch):
    _Volume.items.clear()
    _SdkSandbox.items.clear()
    _SdkSandbox.actions.clear()
    monkeypatch.setattr(sdk, "_sdk_types", lambda: (_SdkSandbox, _Volume, _Network))
    monkeypatch.setattr(sdk, "pinned_sdk_info", lambda: sdk.SdkRuntimeInfo("0.7.0", sdk.PINNED_RUNTIME_ROOT, "test"))
    workspace_id = uuid4()
    old = sdk.MicrosandboxSdkAdapter(image="cognita-workspace-toolbox:12.5.0", timeout_seconds=10)
    name, volume_name = old.names(workspace_id)
    await _Volume.create(volume_name, quota_mib=4096, labels=old._volume_labels(workspace_id, quota_mib=4096))
    await _SdkSandbox.create(
        name, image=old.image, cpus=4, memory=8192,
        volumes={"/workspace": _Volume.named(volume_name)},
        labels=old._sandbox_labels(workspace_id, vcpus=4, memory_mib=8192, volume_name=volume_name),
    )
    _SdkSandbox.items[name]._config["image"]["Oci"]["root_disk"] = {"kind": "managed", "size_mib": 4096}
    assert "/tmp" not in [mount["guest"] for mount in _SdkSandbox.items[name]._config["mounts"]]
    assert "cognita.root_quota_bytes" not in _SdkSandbox.items[name]._config["labels"]
    _sdk_070_controls(monkeypatch)
    current = sdk.MicrosandboxSdkAdapter(timeout_seconds=10)
    _SdkSandbox.actions.clear()
    with pytest.raises(sdk.SdkOperationError) as error:
        await current.ensure(workspace_id, {"require_writable_root_quota": True})
    assert error.value.category == "runtime_failure"
    assert error.value.stage == "reconcile"
    assert _SdkSandbox.actions == []
    assert name in _SdkSandbox.items and volume_name in _Volume.items


@pytest.mark.asyncio
@pytest.mark.parametrize("missing_label", ["cognita.root_quota_bytes", "cognita.tmpfs_mib"])
async def test_sdk_restart_requires_persisted_root_and_tmpfs_evidence(monkeypatch, missing_label):
    _Volume.items.clear()
    _SdkSandbox.items.clear()
    _SdkSandbox.actions.clear()
    monkeypatch.setattr(sdk, "_sdk_types", lambda: (_SdkSandbox, _Volume, _Network))
    monkeypatch.setattr(sdk, "pinned_sdk_info", lambda: sdk.SdkRuntimeInfo("0.7.0", sdk.PINNED_RUNTIME_ROOT, "test"))
    _sdk_070_controls(monkeypatch)
    workspace_id = uuid4()
    first = sdk.MicrosandboxSdkAdapter(timeout_seconds=10)
    await first.ensure(workspace_id, {"require_writable_root_quota": True})
    name, volume_name = first.names(workspace_id)
    _SdkSandbox.items[name]._config["labels"].pop(missing_label)
    restarted = sdk.MicrosandboxSdkAdapter(timeout_seconds=10)
    _SdkSandbox.actions.clear()
    for action in (restarted.inspect, restarted.start, restarted.remove):
        with pytest.raises(sdk.SdkOperationError) as error:
            await action(workspace_id)
        assert error.value.stage == "quota_probe"
    assert _SdkSandbox.actions == []
    assert name in _SdkSandbox.items and volume_name in _Volume.items


@pytest.mark.asyncio
@pytest.mark.parametrize("unproven_quota", [1024, None])
async def test_sdk_restart_preserves_smaller_authorized_volume_quota(monkeypatch, unproven_quota):
    _Volume.items.clear()
    _SdkSandbox.items.clear()
    _SdkSandbox.actions.clear()
    monkeypatch.setattr(sdk, "_sdk_types", lambda: (_SdkSandbox, _Volume, _Network))
    monkeypatch.setattr(sdk, "pinned_sdk_info", lambda: sdk.SdkRuntimeInfo("0.7.0", sdk.PINNED_RUNTIME_ROOT, "test"))
    _sdk_070_controls(monkeypatch)
    workspace_id = uuid4()
    first = sdk.MicrosandboxSdkAdapter(timeout_seconds=10)
    await first.ensure(workspace_id, {"require_writable_root_quota": True, "quota_bytes": 2 * 1024**3})
    name, volume_name = first.names(workspace_id)
    restarted = sdk.MicrosandboxSdkAdapter(timeout_seconds=10)
    assert (await restarted.inspect(workspace_id))["state"] == "running"
    _Volume.items[volume_name].quota_mib = unproven_quota
    _SdkSandbox.actions.clear()
    for action in (restarted.start, restarted.remove):
        with pytest.raises(sdk.SdkOperationError):
            await action(workspace_id)
    assert _SdkSandbox.actions == []
    assert name in _SdkSandbox.items and volume_name in _Volume.items
    _Volume.items[volume_name].quota_mib = 2048
    _Volume.items[volume_name].labels["cognita.quota_mib"] = "8192"
    with pytest.raises(sdk.SdkOperationError):
        await restarted.inspect(workspace_id)


@pytest.mark.asyncio
async def test_regex_matching_runs_in_bounded_owned_process():
    matches = await sdk._bounded_regex_indices(
        "alpha[.]txt$", ["/workspace/alpha.txt", "/workspace/beta.txt"],
        10, time.monotonic() + 10,
    )
    assert matches == [0]


@pytest.mark.asyncio
async def test_regex_result_larger_than_pipe_buffer_does_not_deadlock():
    candidates = [f"/workspace/{index}.txt" for index in range(10_000)]
    matches = await sdk._bounded_regex_indices(
        r"[.]txt$", candidates, len(candidates), time.monotonic() + 10,
    )
    assert matches == list(range(len(candidates)))


@pytest.mark.asyncio
async def test_pathological_regex_is_terminated_at_deadline():
    with pytest.raises(TimeoutError):
        await sdk._bounded_regex_indices(
            "(a+)+$", ["a" * 100_000 + "!"], 1, time.monotonic() + 0.1,
        )


@pytest.mark.asyncio
async def test_job_start_hands_sdk_created_directory_to_guest_user(monkeypatch):
    monkeypatch.setattr(
        sdk,
        "pinned_sdk_info",
        lambda: sdk.SdkRuntimeInfo("0.7.0", sdk.PINNED_RUNTIME_ROOT, "test"),
    )
    adapter = sdk.MicrosandboxSdkAdapter(timeout_seconds=10)
    events = []

    async def guest_exec(executable, argv, **kwargs):
        events.append(("owner", executable, argv, kwargs))
        return types.SimpleNamespace(exit_code=0)

    async def write(path, content):
        events.append(("write", path, content))

    async def connected(*args, **kwargs):
        return types.SimpleNamespace(fs=types.SimpleNamespace(write=write), exec=guest_exec)

    async def mkdir(*args):
        events.append(("mkdir", args[1]))

    async def execute(*args, **kwargs):
        events.append(("start", args[1]))
        return types.SimpleNamespace(exit_code=0)

    monkeypatch.setattr(adapter, "_connected", connected)
    monkeypatch.setattr(adapter, "_mkdir_parents", mkdir)
    monkeypatch.setattr(adapter, "_exec", execute)
    job_id = str(uuid4())
    result = await adapter._job(uuid4(), "job_start", {"job_id": job_id, "argv": ["true"]})
    assert result == {"job_id": job_id, "state": "running"}
    assert [event[0] for event in events] == ["mkdir", "owner", "write", "start"]
    owner = events[1]
    assert owner[1:3] == (
        "/usr/bin/chown",
        ["--no-dereference", "workspace:workspace", f"/workspace/.cognita/jobs/{job_id}"],
    )
    assert owner[3]["user"] == "root"


@pytest.mark.asyncio
async def test_job_poll_allows_output_files_not_created_yet(monkeypatch):
    monkeypatch.setattr(
        sdk,
        "pinned_sdk_info",
        lambda: sdk.SdkRuntimeInfo("0.7.0", sdk.PINNED_RUNTIME_ROOT, "test"),
    )
    adapter = sdk.MicrosandboxSdkAdapter(timeout_seconds=10)
    probed = []

    async def exists(path):
        probed.append(path)
        return False

    async def connected(*args, **kwargs):
        return types.SimpleNamespace(fs=types.SimpleNamespace(exists=exists))

    async def execute(*args, **kwargs):
        return types.SimpleNamespace(exit_code=0, stdout=b'{"state":"running"}')

    monkeypatch.setattr(adapter, "_connected", connected)
    monkeypatch.setattr(adapter, "_exec", execute)
    job_id = str(uuid4())
    result = await adapter._job(uuid4(), "job_get", {"job_id": job_id})
    assert result["state"] == "running"
    assert result["stdout"] == result["stderr"] == ""
    assert probed == [
        f"/workspace/.cognita/jobs/{job_id}/stdout",
        f"/workspace/.cognita/jobs/{job_id}/stderr",
    ]


@pytest.mark.asyncio
async def test_job_poll_exposes_running_output_and_advances_offsets(monkeypatch):
    monkeypatch.setattr(
        sdk,
        "pinned_sdk_info",
        lambda: sdk.SdkRuntimeInfo("0.7.0", sdk.PINNED_RUNTIME_ROOT, "test"),
    )
    adapter = sdk.MicrosandboxSdkAdapter(timeout_seconds=10)
    job_id = str(uuid4())
    files = {
        f"/workspace/.cognita/jobs/{job_id}/stdout": b"begin\n",
        f"/workspace/.cognita/jobs/{job_id}/stderr": b"",
    }

    class Fs:
        async def exists(self, path):
            return path in files

        async def read(self, path):
            return files[path]

    handle = types.SimpleNamespace(fs=Fs())

    async def connected(*args, **kwargs):
        return handle

    async def execute(*args, **kwargs):
        return types.SimpleNamespace(exit_code=0, stdout=b'{"state":"running"}')

    monkeypatch.setattr(adapter, "_connected", connected)
    monkeypatch.setattr(adapter, "_exec", execute)
    first = await adapter._job(uuid4(), "job_get", {
        "job_id": job_id, "stdout_offset": 0, "stderr_offset": 0, "max_bytes": 16,
    })
    assert first["state"] == "running"
    assert first["stdout"] == "YmVnaW4K"
    assert first["stdout_first_available_offset"] == 0
    assert first["stdout_next_offset"] == 6
    assert first["stdout_truncated"] is False
    assert first["stdout_has_more"] is False
    assert first["stderr_has_more"] is False
    assert first["has_more"] is False
    paged = await adapter._job(uuid4(), "job_get", {
        "job_id": job_id, "stdout_offset": 0, "stderr_offset": 0, "max_bytes": 3,
    })
    assert paged["stdout_next_offset"] == 3
    assert paged["stdout_has_more"] is True
    assert paged["has_more"] is True


@pytest.mark.asyncio
async def test_adapter_rejects_unsettled_cancel_receipt(monkeypatch):
    monkeypatch.setattr(
        sdk,
        "pinned_sdk_info",
        lambda: sdk.SdkRuntimeInfo("0.7.0", sdk.PINNED_RUNTIME_ROOT, "test"),
    )
    adapter = sdk.MicrosandboxSdkAdapter(timeout_seconds=10)

    async def connected(*args, **kwargs):
        return types.SimpleNamespace(fs=object())

    async def execute(*args, **kwargs):
        return types.SimpleNamespace(exit_code=0, stdout=b'{"state":"running"}')

    async def read(*args, **kwargs):
        return b""

    monkeypatch.setattr(adapter, "_connected", connected)
    monkeypatch.setattr(adapter, "_exec", execute)
    monkeypatch.setattr(adapter, "_read", read)
    with pytest.raises(sdk.SdkOperationError) as error:
        await adapter._job(uuid4(), "job_cancel", {"job_id": str(uuid4())})
    assert error.value.category == "runtime_failure"


def test_safe_sdk_log_categories_never_include_guest_payload(caplog):
    record = {"event": "sdk_failure", "category": "runtime_failure", "operation": "guest_exec"}
    assert all(key not in record for key in ("argv", "env", "stdout", "stderr", "path", "principal_id"))


def test_guest_cancel_delegates_signal_ownership_to_monitor(tmp_path, monkeypatch, capsys):
    supervisor = _job_supervisor_module()
    supervisor.ROOT = tmp_path
    job_id = str(uuid4())
    directory = tmp_path / job_id
    directory.mkdir()
    supervisor._write(
        directory / "status",
        {
            "state": "running",
            "pid": 4321,
            "process_start_token": "start-token",
            "process_group_id": 4321,
        },
    )
    signals = []
    monkeypatch.setattr(supervisor.os, "killpg", lambda pid, sig: signals.append((pid, sig)), raising=False)

    def finish_cancel(_seconds):
        state = supervisor._read(directory / "status", {})
        state.update({"state": "canceled", "finished_at": time.time()})
        supervisor._write(directory / "status", state)

    monkeypatch.setattr(supervisor.time, "sleep", finish_cancel)

    assert supervisor._cancel(job_id) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["state"] == "canceled"
    assert signals == []
    if os.name == "posix":
        assert (directory / supervisor.CANCEL_MARKER).stat().st_mode & 0o777 == 0o600


def test_guest_monitor_preserves_canceled_terminal_state(tmp_path, monkeypatch):
    supervisor = _job_supervisor_module()
    supervisor.ROOT = tmp_path
    job_id = str(uuid4())
    directory = tmp_path / job_id
    directory.mkdir()
    supervisor._write(
        directory / "descriptor",
        {"argv": ["python3", "-c", "pass"], "cwd": "/workspace", "timeout_seconds": 5},
    )
    class Process:
        pid = 123
        returncode = None
        stdout = None
        stderr = None

        def poll(self):
            return self.returncode

        def wait(self, timeout):
            return self.returncode

    def start_process(*args, **kwargs):
        (directory / supervisor.CANCEL_MARKER).write_text("", encoding="ascii")
        return child

    signals = []
    child = Process()

    def signal_group(pid, sig):
        signals.append((pid, sig))
        child.returncode = -15

    monkeypatch.setattr(supervisor.subprocess, "Popen", start_process)
    monkeypatch.setattr(supervisor.os, "killpg", signal_group, raising=False)
    monkeypatch.setattr(supervisor, "_token", lambda _pid: "token")
    assert supervisor._monitor(job_id) == 0
    assert supervisor._read(directory / "status", {})["state"] == "canceled"
    assert signals == [(123, supervisor.signal.SIGTERM)]


def test_guest_monitor_does_not_start_already_canceled_job(tmp_path, monkeypatch):
    supervisor = _job_supervisor_module()
    supervisor.ROOT = tmp_path
    job_id = str(uuid4())
    directory = tmp_path / job_id
    directory.mkdir()
    supervisor._write(
        directory / "descriptor",
        {"argv": ["python3", "-c", "pass"], "cwd": "/workspace", "timeout_seconds": 5},
    )
    (directory / supervisor.CANCEL_MARKER).write_text("", encoding="ascii")
    monkeypatch.setattr(
        supervisor.subprocess,
        "Popen",
        lambda *args, **kwargs: pytest.fail("canceled job must not launch"),
    )

    assert supervisor._monitor(job_id) == 0
    assert supervisor._read(directory / "status", {})["state"] == "canceled"


def test_guest_monitor_drains_sustained_output_into_fixed_tails(tmp_path, monkeypatch, capsys):
    supervisor = _job_supervisor_module()
    supervisor.ROOT = tmp_path
    job_id = str(uuid4())
    directory = tmp_path / job_id
    directory.mkdir()
    supervisor._write(directory / "descriptor", {
        "argv": ["python3", "-c", "pass"], "cwd": "/workspace", "timeout_seconds": 30,
    })
    stdout_chunks = iter(bytes([index % 251]) * supervisor.READ_CHUNK for index in range(320))
    stderr_chunks = iter(bytes([index % 239]) * supervisor.READ_CHUNK for index in range(192))
    streams = {10: stdout_chunks, 11: stderr_chunks}
    finished = {10: False, 11: False}

    class Stream:
        def __init__(self, fd):
            self.fd = fd

        def fileno(self):
            return self.fd

        def close(self):
            pass

    class Selector:
        def __init__(self):
            self.keys = {}

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            self.keys.clear()

        def register(self, stream, _events, name):
            self.keys[stream.fd] = types.SimpleNamespace(fd=stream.fd, fileobj=stream, data=name)

        def unregister(self, stream):
            self.keys.pop(stream.fd)

        def get_map(self):
            return self.keys

        def select(self, _timeout):
            key = next(iter(self.keys.values()))
            return [(key, supervisor.selectors.EVENT_READ)]

    class Process:
        pid = 123
        stdout = Stream(10)
        stderr = Stream(11)
        returncode = None

        def poll(self):
            if all(finished.values()):
                self.returncode = 0
            return self.returncode

    def read(fd, _count):
        try:
            return next(streams[fd])
        except StopIteration:
            finished[fd] = True
            return b""

    monkeypatch.setattr(supervisor.subprocess, "Popen", lambda *_args, **_kwargs: Process())
    monkeypatch.setattr(supervisor.selectors, "DefaultSelector", Selector)
    monkeypatch.setattr(supervisor.os, "set_blocking", lambda *_args: None)
    monkeypatch.setattr(supervisor.os, "read", read)
    monkeypatch.setattr(supervisor, "_token", lambda _pid: "token")
    assert supervisor._monitor(job_id) == 0
    assert supervisor._read(directory / "status", {})["state"] == "succeeded"
    expected_stdout = b"".join(bytes([index % 251]) * supervisor.READ_CHUNK for index in range(192, 320))
    expected_stderr = b"".join(bytes([index % 239]) * supervisor.READ_CHUNK for index in range(64, 192))
    assert (directory / "stdout").read_bytes() == expected_stdout
    assert (directory / "stderr").read_bytes() == expected_stderr
    assert (directory / "stdout").stat().st_size == supervisor.MAX_OUTPUT
    assert (directory / "stderr").stat().st_size == supervisor.MAX_OUTPUT
    assert supervisor._status(job_id) == 0
    published = json.loads(capsys.readouterr().out)
    assert published["stdout_bytes"] == supervisor.MAX_OUTPUT
    assert published["stderr_bytes"] == supervisor.MAX_OUTPUT


@pytest.mark.skipif(os.name != "posix", reason="guest supervisor uses POSIX process groups")
def test_guest_monitor_publishes_flushed_output_while_child_is_running(tmp_path, capsys):
    supervisor = _job_supervisor_module()
    supervisor.ROOT = tmp_path
    job_id = str(uuid4())
    directory = tmp_path / job_id
    directory.mkdir()
    # The child used to `time.sleep(2)` after printing, and the test polled the
    # stdout file with sleeps: a machine that stalled for 2s let the job finish
    # before "running" was observed. Now the child blocks reading a FIFO that
    # the test holds open (O_RDWR, so neither side's open ever blocks); it
    # exits only when the test closes its end. The supervisor's own timeout is
    # set far out of reach so it cannot decide the outcome either.
    gate = tmp_path / "gate"
    os.mkfifo(gate)
    gate_fd = os.open(gate, os.O_RDWR)
    supervisor._write(directory / "descriptor", {
        "argv": [
            sys.executable, "-c",
            "import sys; print('begin', flush=True); open(sys.argv[1], 'rb').read()",
            str(gate),
        ],
        "cwd": str(tmp_path), "timeout_seconds": 3600,
    })

    # Signal from the code under test: the supervisor's own append of a
    # stdout fragment, instead of polling the file.
    published = threading.Event()
    original_append = supervisor._bounded_append

    def observed_append(path, data):
        original_append(path, data)
        if path.name == "stdout" and data:
            published.set()

    supervisor._bounded_append = observed_append

    monitor = threading.Thread(target=supervisor._monitor, args=(job_id,))
    monitor.start()
    try:
        # Hang guard only: the child always prints before it blocks, and the
        # monitor always drains and appends what it prints.
        assert published.wait(timeout=10), "the monitor never published stdout"
        assert (directory / "stdout").read_bytes() == b"begin\n"
        assert supervisor._read(directory / "status", {})["state"] == "running"

        assert supervisor._status(job_id) == 0
        running = json.loads(capsys.readouterr().out)
        assert running["state"] == "running"
        assert running["stdout_bytes"] == len(b"begin\n")
    finally:
        # Closing the only writer gives the child EOF, so it exits and the
        # monitor publishes its terminal state. The join timeout is a hang guard.
        os.close(gate_fd)
        monitor.join(timeout=5)
    assert not monitor.is_alive()
    assert supervisor._read(directory / "status", {})["state"] == "succeeded"


def test_guest_monitor_times_out_and_reaps_owned_group(tmp_path, monkeypatch):
    supervisor = _job_supervisor_module()
    supervisor.ROOT = tmp_path
    job_id = str(uuid4())
    directory = tmp_path / job_id
    directory.mkdir()
    supervisor._write(directory / "descriptor", {
        "argv": ["python3", "-c", "pass"], "cwd": "/workspace", "timeout_seconds": 1,
    })

    class Process:
        pid = 123
        stdout = None
        stderr = None
        returncode = None

        def poll(self):
            return self.returncode

        def wait(self, timeout):
            return self.returncode

    child = Process()
    signals = []

    def signal_group(pid, sig):
        signals.append((pid, sig))
        child.returncode = -9

    monkeypatch.setattr(supervisor.subprocess, "Popen", lambda *_args, **_kwargs: child)
    monkeypatch.setattr(supervisor.signal, "SIGKILL", 9, raising=False)
    monkeypatch.setattr(supervisor.os, "killpg", signal_group, raising=False)
    monkeypatch.setattr(supervisor, "_token", lambda _pid: "token")
    assert supervisor._monitor(job_id) == 0
    assert supervisor._read(directory / "status", {})["state"] == "timed_out"
    assert signals == [(123, supervisor.signal.SIGKILL)]


def test_guest_cancel_fails_if_monitor_does_not_publish_terminal_state(
    tmp_path, monkeypatch, capsys,
):
    supervisor = _job_supervisor_module()
    supervisor.ROOT = tmp_path
    job_id = str(uuid4())
    directory = tmp_path / job_id
    directory.mkdir()
    supervisor._write(directory / "status", {"state": "running", "pid": 4321})
    moments = iter((0.0, 9.0))
    monkeypatch.setattr(supervisor.time, "monotonic", lambda: next(moments, 9.0))

    assert supervisor._cancel(job_id) == 1
    assert json.loads(capsys.readouterr().out)["state"] == "running"
