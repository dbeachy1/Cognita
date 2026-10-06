from __future__ import annotations

import asyncio
import hashlib
import threading
from types import SimpleNamespace
from uuid import uuid4

import pytest

from cognita.bridge import BridgeError, BridgeService
from cognita.connectors import PUBLIC_CONTRACT_VERSION, ConnectorDefinition
from cognita.workspace import WorkspaceMetadataStore


class _Workspace:
    def __init__(self):
        self.workspace_id = str(uuid4())
        self.lock = threading.RLock()
        self.rows: dict[str, dict] = {}
        self.trees: dict[str, list[dict]] = {}

    def _admit(self, principal, connector_id=None):
        return SimpleNamespace(workspace_id=self.workspace_id)

    def _queue(self, workspace_id):
        return self.lock

    def inventory(self, principal, paths, connector_id=None):
        return {path: self.rows[path] for path in paths if path in self.rows}

    def execute(self, principal, tool, arguments, connector_id=None):
        assert tool == "workspace_list_files"
        return {
            "status": "success",
            "data": {"entries": self.trees[arguments["path"]], "eof": True},
        }


class _Transfers:
    def __init__(self):
        self.received: dict[str, dict[str, bytearray]] = {}
        self.downloads: dict[str, bytes] = {}
        self.manifests: dict[str, dict] = {}

    def admit_transfer(self, manifest):
        self.manifests[manifest["transfer_id"]] = manifest
        self.received.setdefault(manifest["transfer_id"], {})

    def put_transfer_frame(self, transfer_id, path, offset, content, digest):
        assert hashlib.sha256(content).hexdigest() == digest
        self.received[transfer_id].setdefault(path, bytearray()).extend(content)

    def commit_transfer(self, transfer_id):
        return {"state": "committed"}

    def abort_transfer(self, transfer_id):
        return {"state": "aborted"}

    def get_transfer_content(self, transfer_id, path, offset, length):
        value = self.downloads[path]
        return value[offset:offset + length]


class _ReservedWorkspace(_Workspace):
    """Small durable-reservation stand-in for bridge concurrency tests."""

    def __init__(self, capacity: int):
        super().__init__()
        self.capacity = capacity
        self.held = 0
        self.released: list[str] = []
        self.reservations: dict[str, int] = {}
        self._reservation_lock = threading.RLock()

    def _reserve_admission(self, growth_bytes, *, request_key):
        with self._reservation_lock:
            if self.held + growth_bytes > self.capacity:
                error = RuntimeError("capacity busy")
                error.reason = "capacity_busy"
                raise error
            self.held += growth_bytes
            reservation_id = f"{request_key}:{len(self.reservations)}"
            self.reservations[reservation_id] = growth_bytes
            return reservation_id

    def release_growth(self, reservation_id):
        with self._reservation_lock:
            self.held -= self.reservations.pop(reservation_id, 0)
            self.released.append(reservation_id)

    def renew_growth(self, reservation_id):
        with self._reservation_lock:
            if reservation_id not in self.reservations:
                error = RuntimeError("capacity reservation expired")
                error.reason = "capacity_busy"
                raise error


class _BlockingTransfers(_Transfers):
    def __init__(self):
        super().__init__()
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def admit_transfer(self, manifest):
        super().admit_transfer(manifest)
        self.started.set()
        # Bounded: a test that never sets `release` fails with a TimeoutError
        # out of the transfer instead of hanging the run.
        await asyncio.wait_for(self.release.wait(), timeout=5)


class _FailingCommitTransfers(_Transfers):
    def commit_transfer(self, transfer_id):
        raise RuntimeError("commit failed")


class _CommitBlockingTransfers(_Transfers):
    def __init__(self):
        super().__init__()
        self.commit_started = asyncio.Event()
        self.release_commit = asyncio.Event()
        self.aborted = 0
        self.commit_calls = 0

    async def commit_transfer(self, transfer_id):
        self.commit_calls += 1
        self.commit_started.set()
        # Bounded for the same reason as _BlockingTransfers.admit_transfer.
        await asyncio.wait_for(self.release_commit.wait(), timeout=5)
        return {"state": "committed"}

    def abort_transfer(self, transfer_id):
        self.aborted += 1
        return super().abort_transfer(transfer_id)


class _FailingRenewWorkspace(_ReservedWorkspace):
    def __init__(self, capacity: int):
        super().__init__(capacity)
        self.renewals = 0
        # 2026-09-22: renewals used to fail from the SECOND one on. The first
        # is the bridge's synchronous pre-flight renewal; the second is the
        # heartbeat's first 5 ms tick, and whether that tick landed before or
        # after commit depended on how fast the machine ran the pre-commit
        # path. Now renewals fail only once the test arms this, after it has
        # seen commit start, so the failure is post-commit by construction.
        self.fail_renewals = False
        # Set when an armed renewal has been refused, so a test can wait for
        # the failure itself instead of sleeping and hoping the heartbeat got
        # there first.
        self.renewal_failed = asyncio.Event()

    def renew_growth(self, reservation_id):
        self.renewals += 1
        if self.fail_renewals:
            self.renewal_failed.set()
            error = RuntimeError("capacity reservation expired")
            error.reason = "capacity_busy"
            raise error
        return super().renew_growth(reservation_id)


class _ExpiringReservedWorkspace(_ReservedWorkspace):
    def __init__(self, capacity: int, ttl_seconds: float):
        super().__init__(capacity)
        self.ttl_seconds = ttl_seconds
        self.expires_at: dict[str, float] = {}
        self.renewals = 0

    def _expire(self):
        now = asyncio.get_running_loop().time()
        for reservation_id, expires_at in list(self.expires_at.items()):
            if expires_at <= now:
                self.held -= self.reservations.pop(reservation_id, 0)
                self.expires_at.pop(reservation_id, None)

    def _reserve_admission(self, growth_bytes, *, request_key):
        with self._reservation_lock:
            self._expire()
            reservation_id = super()._reserve_admission(
                growth_bytes, request_key=request_key
            )
            self.expires_at[reservation_id] = (
                asyncio.get_running_loop().time() + self.ttl_seconds
            )
            return reservation_id

    def renew_growth(self, reservation_id):
        with self._reservation_lock:
            self._expire()
            if reservation_id not in self.reservations:
                error = RuntimeError("capacity reservation expired")
                error.reason = "capacity_busy"
                raise error
            self.expires_at[reservation_id] = (
                asyncio.get_running_loop().time() + self.ttl_seconds
            )
            self.renewals += 1

    def release_growth(self, reservation_id):
        with self._reservation_lock:
            self.expires_at.pop(reservation_id, None)
        super().release_growth(reservation_id)


def _connector(**overrides):
    values = {
        "id": str(uuid4()), "name": "Bridge", "slug": "bridge", "workspace_enabled": True,
        "default_workspace_transfer": "allow", "default_access": "write",
    }
    values.update(overrides)
    return ConnectorDefinition(**values)


@pytest.fixture
def setup(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    project = SimpleNamespace(name="Project", documents_dir=docs, enabled=True, writable=True)
    workspace = _Workspace()
    transfers = _Transfers()
    service = BridgeService(workspace, transfers, staging_root=tmp_path / "staging")
    principal = SimpleNamespace(principal_id=str(uuid4()))
    return docs, project, workspace, transfers, service, principal


@pytest.mark.asyncio
async def test_bridge_rejects_retired_contract_generation(setup):
    _docs, project, _workspace, _transfers, service, principal = setup

    result = await service.execute(
        principal,
        _connector(),
        project,
        "copy_to_workspace",
        {"project": "Project", "paths": [], "destination": "build"},
        contract_version=PUBLIC_CONTRACT_VERSION - 1,
    )

    assert result == {
        "status": "error",
        "reason": "upgrade_required",
        "message": f"Bridge tools are available only on connector contract v{PUBLIC_CONTRACT_VERSION}.",
    }


def test_transfer_admission_reconciles_stale_job_and_reports_live_job(setup):
    _, _, workspace, _, service, _ = setup
    stale = {"job_id": "stale-job", "state": "running", "created_at": "2026-09-20T03:28:12+00:00"}
    workspace.metadata = SimpleNamespace(active_job=lambda workspace_id: stale)
    workspace.active_job_after_reconcile = lambda workspace_id: None
    service._check_workspace_transfer_ready(workspace.workspace_id)

    workspace.active_job_after_reconcile = lambda workspace_id: stale
    with pytest.raises(BridgeError) as blocked:
        service._check_workspace_transfer_ready(workspace.workspace_id)
    assert blocked.value.reason == "job_running"
    assert blocked.value.fields["job_id"] == "stale-job"
    assert blocked.value.fields["started_at"] == stale["created_at"]


@pytest.mark.asyncio
async def test_to_workspace_streams_binary_and_text_without_response_bodies(setup):
    docs, project, workspace, transfers, service, principal = setup
    (docs / "tree").mkdir()
    (docs / "tree" / "note.txt").write_text("hello", encoding="utf-8")
    (docs / "tree" / "blob.bin").write_bytes(b"\x00\xff\x01")

    receipt = await service.execute(
        principal, _connector(), project, "copy_to_workspace",
        {"project": "Project", "paths": ["tree"], "destination": "build"},
    )

    assert receipt["status"] == "success"
    assert receipt["bytes"] == 8
    assert "content" not in receipt
    transfer = next(iter(transfers.received.values()))
    assert bytes(transfer["build/tree/note.txt"]) == b"hello"
    assert bytes(transfer["build/tree/blob.bin"]) == b"\x00\xff\x01"
    assert not list((docs.parent / "staging").iterdir())


@pytest.mark.asyncio
async def test_to_workspace_accepts_unmeasured_usage_and_enforces_quota(setup):
    docs, project, workspace, transfers, service, principal = setup
    (docs / "new.txt").write_bytes(b"new")
    record = SimpleNamespace(quota_bytes=3, measured_apparent_bytes=None)
    workspace.metadata = SimpleNamespace(get=lambda workspace_id: record)

    receipt = await service.execute(
        principal, _connector(), project, "copy_to_workspace",
        {"project": "Project", "paths": ["new.txt"], "destination": "incoming"},
    )
    assert receipt["status"] == "success"
    assert bytes(next(iter(transfers.received.values()))["incoming/new.txt"]) == b"new"

    record.quota_bytes = 2
    with pytest.raises(BridgeError) as error:
        await service.execute(
            principal, _connector(), project, "copy_to_workspace",
            {"project": "Project", "paths": ["new.txt"], "destination": "too-small"},
        )
    assert error.value.reason == "quota_exceeded"


@pytest.mark.asyncio
async def test_to_workspace_rename_checks_each_candidate_and_commits_snapshot(setup):
    docs, project, workspace, transfers, service, principal = setup
    content = b"new"
    (docs / "result.txt").write_bytes(content)
    workspace.rows["result.txt"] = {
        "type": "file", "size": 3, "sha256": hashlib.sha256(b"old").hexdigest(),
    }
    workspace.rows["result (1).txt"] = {
        "type": "file", "size": 5, "sha256": hashlib.sha256(b"older").hexdigest(),
    }

    receipt = await service.execute(
        principal,
        _connector(),
        project,
        "copy_to_workspace",
        {"project": "Project", "paths": ["result.txt"], "conflict_policy": "rename"},
    )

    assert receipt["committed"] == ["result (2).txt"]
    transfer = next(iter(transfers.received.values()))
    assert bytes(transfer["result (2).txt"]) == content


@pytest.mark.asyncio
async def test_replace_requires_exact_destination_hash_and_creates_backup(setup):
    docs, project, workspace, transfers, service, principal = setup
    target = docs / "note.txt"
    target.write_text("old", encoding="utf-8")
    source = docs / "source.txt"
    source.write_text("new", encoding="utf-8")
    workspace.rows["source.txt"] = {"type": "file", "size": 3, "sha256": hashlib.sha256(b"old").hexdigest()}

    with pytest.raises(BridgeError) as exc:
        await service.execute(principal, _connector(), project, "copy_to_workspace", {
            "project": "Project", "paths": ["source.txt"], "conflict_policy": "replace",
            "expected_destination_hashes": {"source.txt": "0" * 64},
        })
    assert exc.value.reason == "stale_file"

    transfers.downloads["note.txt"] = b"old"
    workspace.rows["note.txt"] = {"type": "file", "size": 3, "sha256": hashlib.sha256(b"old").hexdigest()}
    receipt = await service.execute(principal, _connector(), project, "copy_from_workspace", {
        "project": "Project", "paths": ["note.txt"], "conflict_policy": "replace",
        "expected_destination_hashes": {"note.txt": hashlib.sha256(b"old").hexdigest()},
    })
    assert receipt["status"] == "success"


@pytest.mark.asyncio
async def test_copy_back_uses_staged_hash_verified_workspace_content(setup):
    docs, project, workspace, transfers, service, principal = setup
    content = b"binary\x00text"
    digest = hashlib.sha256(content).hexdigest()
    transfers.downloads["artifact.bin"] = content
    workspace.rows["artifact.bin"] = {"type": "file", "size": len(content), "sha256": digest}

    receipt = await service.execute(principal, _connector(), project, "copy_from_workspace", {
        "project": "Project", "paths": ["artifact.bin"], "destination": "out",
    })

    assert receipt["committed"] == ["out/artifact.bin"]
    assert receipt["bytes"] == len(content)
    assert (docs / "out" / "artifact.bin").read_bytes() == content

    # 13.0.2: a skipped transfer wrote nothing, so it reports 0 bytes — it used
    # to report the staged manifest's size beside file_count 0.
    skipped = await service.execute(principal, _connector(), project, "copy_from_workspace", {
        "project": "Project", "paths": ["artifact.bin"], "destination": "out", "conflict_policy": "skip",
    })
    assert skipped["skipped"] == ["out/artifact.bin"]
    assert (skipped["file_count"], skipped["bytes"], skipped["committed"]) == (0, 0, [])


@pytest.mark.asyncio
async def test_copy_back_directory_preserves_relative_structure(setup):
    docs, project, workspace, transfers, service, principal = setup
    content = b"nested"
    digest = hashlib.sha256(content).hexdigest()
    workspace.rows["tree"] = {"type": "directory", "size": 0}
    workspace.rows["tree/sub/result.bin"] = {
        "type": "file", "size": len(content), "sha256": digest,
    }
    workspace.trees["tree"] = [
        {"path": "/workspace/tree/sub", "type": "directory"},
        {"path": "/workspace/tree/sub/result.bin", "type": "file"},
    ]
    transfers.downloads["tree/sub/result.bin"] = content

    receipt = await service.execute(
        principal,
        _connector(),
        project,
        "copy_from_workspace",
        {"project": "Project", "paths": ["tree"], "destination": "published"},
    )

    assert receipt["committed"] == ["published/tree/sub/result.bin"]
    assert (docs / "published" / "tree" / "sub" / "result.bin").read_bytes() == content


@pytest.mark.asyncio
async def test_copy_back_rename_never_overwrites_existing_destination(setup):
    docs, project, workspace, transfers, service, principal = setup
    (docs / "result.txt").write_text("keep", encoding="utf-8")
    content = b"new"
    digest = hashlib.sha256(content).hexdigest()
    workspace.rows["result.txt"] = {
        "type": "file", "size": len(content), "sha256": digest,
    }
    transfers.downloads["result.txt"] = content

    receipt = await service.execute(
        principal,
        _connector(),
        project,
        "copy_from_workspace",
        {"project": "Project", "paths": ["result.txt"], "conflict_policy": "rename"},
    )

    assert (docs / "result.txt").read_text(encoding="utf-8") == "keep"
    assert receipt["committed"] == ["result (1).txt"]
    assert (docs / "result (1).txt").read_bytes() == content


@pytest.mark.asyncio
async def test_read_only_project_cannot_copy_back(setup):
    docs, project, workspace, transfers, service, principal = setup
    project.writable = False
    connector = _connector(default_access="read")
    with pytest.raises(BridgeError) as exc:
        await service.execute(principal, connector, project, "copy_from_workspace", {
            "project": "Project", "paths": ["artifact.txt"],
        })
    assert exc.value.reason in {"read_only", "project_forbidden"}


@pytest.mark.asyncio
async def test_concurrent_uploads_hold_durable_growth_reservations(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "first.bin").write_bytes(b"123456")
    (docs / "second.bin").write_bytes(b"abcdef")
    project = SimpleNamespace(name="Project", documents_dir=docs, enabled=True, writable=True)
    workspace = _ReservedWorkspace(capacity=6)
    transfers = _BlockingTransfers()
    service = BridgeService(workspace, transfers, staging_root=tmp_path / "staging")
    principal = SimpleNamespace(principal_id=str(uuid4()))

    first = asyncio.create_task(service.execute(
        principal, _connector(), project, "copy_to_workspace",
        {"project": "Project", "paths": ["first.bin"], "destination": "one"},
    ))
    await asyncio.wait_for(transfers.started.wait(), timeout=5)
    second = asyncio.create_task(service.execute(
        principal, _connector(), project, "copy_to_workspace",
        {"project": "Project", "paths": ["second.bin"], "destination": "two"},
    ))
    await asyncio.sleep(0)
    transfers.release.set()
    results = await asyncio.wait_for(
        asyncio.gather(first, second, return_exceptions=True), timeout=5
    )

    assert sum(isinstance(result, dict) and result["status"] == "success" for result in results) == 1
    errors = [result for result in results if isinstance(result, BridgeError)]
    assert len(errors) == 1 and errors[0].reason == "capacity_busy"
    assert workspace.held == 0
    assert len(workspace.released) == 1


@pytest.mark.asyncio
async def test_failed_upload_releases_durable_growth_reservation(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "payload.bin").write_bytes(b"payload")
    project = SimpleNamespace(name="Project", documents_dir=docs, enabled=True, writable=True)
    workspace = _ReservedWorkspace(capacity=7)
    transfers = _FailingCommitTransfers()
    service = BridgeService(workspace, transfers, staging_root=tmp_path / "staging")
    principal = SimpleNamespace(principal_id=str(uuid4()))

    with pytest.raises(BridgeError) as exc:
        await service.execute(
            principal, _connector(), project, "copy_to_workspace",
            {"project": "Project", "paths": ["payload.bin"], "destination": "incoming"},
        )

    assert exc.value.reason == "runtime_unavailable"
    assert workspace.held == 0
    assert len(workspace.released) == 1


@pytest.mark.asyncio
async def test_active_upload_renews_short_lived_growth_reservation(tmp_path):
    """An active upload keeps renewing its growth reservation, so a second
    upload that needs the same capacity is refused as busy, not admitted.

    On 2026-09-22, this test froze a
    full run at 14% for 20+ minutes. It used a real 20 ms TTL against a 5 ms
    renewal cadence and a 60 ms real sleep. Any stall of the event loop
    longer than 20 ms (a busy machine is enough) let the reservation expire
    before its renewal, so the second upload was ADMITTED instead of refused,
    blocked in _BlockingTransfers waiting for `release` -- which this test
    only sets after `second` has failed -- and `await second` never returned.
    A wall-clock race whose losing branch is a deadlock.

    Now nothing depends on a deadline: the TTL is long enough never to expire
    inside a test, the renewal is observed through an event instead of a
    sleep, and every await is bounded so a regression fails instead of
    hanging.
    """
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "payload.bin").write_bytes(b"payload")
    project = SimpleNamespace(name="Project", documents_dir=docs, enabled=True, writable=True)
    workspace = _ExpiringReservedWorkspace(capacity=7, ttl_seconds=30.0)
    renewed = asyncio.Event()
    real_renew = workspace.renew_growth

    def renew_growth(reservation_id):
        real_renew(reservation_id)
        renewed.set()

    workspace.renew_growth = renew_growth
    transfers = _BlockingTransfers()
    service = BridgeService(
        workspace,
        transfers,
        staging_root=tmp_path / "staging",
        growth_renewal_interval=0.005,
    )
    principal = SimpleNamespace(principal_id=str(uuid4()))

    first = asyncio.create_task(service.execute(
        principal, _connector(), project, "copy_to_workspace",
        {"project": "Project", "paths": ["payload.bin"], "destination": "one"},
    ))
    await asyncio.wait_for(transfers.started.wait(), timeout=5)
    await asyncio.wait_for(renewed.wait(), timeout=5)

    second = asyncio.create_task(service.execute(
        principal, _connector(), project, "copy_to_workspace",
        {"project": "Project", "paths": ["payload.bin"], "destination": "two"},
    ))
    with pytest.raises(BridgeError) as exc:
        await asyncio.wait_for(second, timeout=5)
    assert exc.value.reason == "capacity_busy"
    assert workspace.renewals > 0

    transfers.release.set()
    receipt = await asyncio.wait_for(first, timeout=5)
    assert receipt["status"] == "success"
    assert workspace.held == 0


@pytest.mark.asyncio
async def test_post_commit_renewal_failure_does_not_abort_committed_transfer(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "payload.bin").write_bytes(b"payload")
    project = SimpleNamespace(name="Project", documents_dir=docs, enabled=True, writable=True)
    workspace = _FailingRenewWorkspace(capacity=7)
    metadata_store = WorkspaceMetadataStore(tmp_path / "workspace.sqlite3")
    workspace.metadata = SimpleNamespace(
        idempotent=metadata_store.idempotent,
        save_idempotent=metadata_store.save_idempotent,
    )
    transfers = _CommitBlockingTransfers()
    service = BridgeService(
        workspace,
        transfers,
        staging_root=tmp_path / "staging",
        growth_renewal_interval=0.005,
    )
    principal = SimpleNamespace(principal_id=str(uuid4()))

    try:
        task = asyncio.create_task(service.execute(
            principal, _connector(), project, "copy_to_workspace",
            {
                "project": "Project", "paths": ["payload.bin"],
                "destination": "incoming", "idempotency_key": "commit-renewal-failure",
            },
        ))
        await asyncio.wait_for(transfers.commit_started.wait(), timeout=5)
        # Was a real 30 ms sleep betting the 5 ms heartbeat would fail its
        # second renewal first. Now arms the failure once commit is in flight
        # and waits on the refused renewal itself. The heartbeat keeps ticking
        # while commit is blocked, so the signal always comes; the timeout is
        # a hang guard only.
        workspace.fail_renewals = True
        await asyncio.wait_for(workspace.renewal_failed.wait(), timeout=5)
        transfers.release_commit.set()

        with pytest.raises(BridgeError) as exc:
            await asyncio.wait_for(task, timeout=5)
        assert exc.value.reason == "capacity_busy"
        assert exc.value.fields["committed"] is True
        assert exc.value.fields["receipt"]["status"] == "success"
        assert transfers.aborted == 0
        retry_transfers = _Transfers()
        retry_service = BridgeService(
            workspace, retry_transfers, staging_root=tmp_path / "staging-retry"
        )
        retry = await retry_service.execute(
            principal, _connector(), project, "copy_to_workspace",
            {
                "project": "Project", "paths": ["payload.bin"],
                "destination": "incoming", "idempotency_key": "commit-renewal-failure",
            },
        )
        assert retry == exc.value.fields["receipt"]
        assert transfers.commit_calls == 1
        assert retry_transfers.manifests == {}
        assert workspace.held == 0
    finally:
        metadata_store.close()


@pytest.mark.asyncio
async def test_post_commit_receipt_save_failure_requires_reconciliation(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "payload.bin").write_bytes(b"payload")
    project = SimpleNamespace(name="Project", documents_dir=docs, enabled=True, writable=True)
    workspace = _FailingRenewWorkspace(capacity=7)

    def fail_save(*args):
        raise OSError("metadata unavailable")

    workspace.metadata = SimpleNamespace(save_idempotent=fail_save)
    transfers = _CommitBlockingTransfers()
    service = BridgeService(
        workspace,
        transfers,
        staging_root=tmp_path / "staging",
        growth_renewal_interval=0.005,
    )
    principal = SimpleNamespace(principal_id=str(uuid4()))

    task = asyncio.create_task(service.execute(
        principal, _connector(), project, "copy_to_workspace",
        {
            "project": "Project", "paths": ["payload.bin"],
            "destination": "incoming", "idempotency_key": "receipt-save-failure",
        },
    ))
    await asyncio.wait_for(transfers.commit_started.wait(), timeout=5)
    # Was a real 30 ms sleep; now arms the renewal failure once commit is in
    # flight and waits on the refused renewal itself (hang guard only).
    workspace.fail_renewals = True
    await asyncio.wait_for(workspace.renewal_failed.wait(), timeout=5)
    transfers.release_commit.set()

    with pytest.raises(BridgeError) as exc:
        await asyncio.wait_for(task, timeout=5)
    assert exc.value.reason == "runtime_unavailable"
    assert exc.value.fields["committed"] is True
    assert exc.value.fields["receipt"]["status"] == "success"
    assert exc.value.fields["durable_replay"] is False
    assert exc.value.fields["retryable"] is False
    assert exc.value.fields["reconciliation_required"] is True
    assert exc.value.fields["prior_error"] == "capacity_busy"
    assert transfers.aborted == 0

    # The process-local receipt prevents a same-process retry from replaying a
    # broker commit, but the response does not claim crash-safe durable replay.
    retry = await service.execute(
        principal, _connector(), project, "copy_to_workspace",
        {
            "project": "Project", "paths": ["payload.bin"],
            "destination": "incoming", "idempotency_key": "receipt-save-failure",
        },
    )
    assert retry == exc.value.fields["receipt"]
    assert transfers.commit_calls == 1
    assert workspace.held == 0


@pytest.mark.asyncio
async def test_book_workspace_source_is_authorized_pinned_and_staged(setup, tmp_path):
    _docs, project, workspace, transfers, service, principal = setup
    content = b"native audio bytes\x00\x01"
    digest = hashlib.sha256(content).hexdigest()
    workspace.rows["exports/take.wav"] = {
        "type": "file", "size": len(content), "sha256": digest,
    }
    transfers.downloads["exports/take.wav"] = content
    leases, released = [], []
    workspace.metadata = SimpleNamespace(
        lease=lambda workspace_id, kind, seconds, owner: leases.append(
            (workspace_id, kind, seconds, owner)
        ) or f"lease:{owner}",
        release_lease=lambda lease_id: released.append(lease_id),
    )

    staged = await service.stage_book_workspace_source(
        principal, _connector(), project, "exports/take.wav", digest,
        staging_root=tmp_path / "book-stage",
    )
    assert staged.source_kind == "workspace"
    assert staged.bytes_sha256 == digest and staged.size_bytes == len(content)
    assert staged.staged_path.read_bytes() == content
    assert staged.staged_path.parent == (tmp_path / "book-stage").resolve()
    assert not any((tmp_path / "staging").iterdir())
    assert next(iter(transfers.manifests.values()))["direction"] == "from_workspace"
    assert leases and released == [f"lease:{leases[0][3]}"]

    with pytest.raises(BridgeError) as stale:
        await service.stage_book_workspace_source(
            principal, _connector(), project, "exports/take.wav", "0" * 64,
            staging_root=tmp_path / "book-stage",
        )
    assert stale.value.reason == "stale_file"
