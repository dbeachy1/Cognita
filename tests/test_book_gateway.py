from __future__ import annotations

import json
import hashlib
import asyncio

import httpx
import pytest
from argon2 import PasswordHasher

from cognita.config import CognitaConfig
from cognita.engine_local import LocalEngineHost
from cognita.auth_policy import AuthenticationPolicyStore, CredentialPolicyStore
from cognita.proxy import _forward_headers
from cognita.gateway import _suppress_wire_bodies
from cognita.connectors import ConnectorStore, PUBLIC_CONTRACT_VERSION
from cognita.gateway import create_gateway_app
from cognita.registry import Project, Registry
from cognita.store import SchemaVersionMismatch, Store
from cognita.tokens import generate_token, hash_token
from cognita.retrieval import RetrievalCore
from cognita.books.state import ProjectState

from retrieval_fakes import HashEmbedder, OverlapReranker
from test_book_service import _fixture


def _rpc(method: str, params=None, msg_id=1):
    return {"jsonrpc": "2.0", "id": msg_id, "method": method, "params": params or {}}


@pytest.fixture
def host(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    _fixture(docs, bound=True)
    ProjectState.initialize(docs)
    registry = Registry(tmp_path / "registry.yaml")
    registry.add(Project(
        name="fixture", documents_dir=docs, data_dir=tmp_path / "data",
        token_sha256=hash_token(generate_token()),
    ))
    store = Store("postgresql://unavailable.invalid/fixture", embedding_dimensions=32)
    # The datasource remains unopened; this models an index outage without
    # requiring a database process. Book and project-file routes must still
    # return source-side facts.
    store.schema_error = SchemaVersionMismatch("synthetic Postgres outage")
    core = RetrievalCore(store, HashEmbedder(32), OverlapReranker())
    return LocalEngineHost(CognitaConfig(), registry, core)


async def _post(host, message, *, headers=None):
    transport = httpx.ASGITransport(app=host.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://fixture") as client:
        return await client.post("/engine/fixture/mcp", json=message, headers=headers)


@pytest.mark.asyncio
async def test_gateway_catalog_and_source_reads_work_during_postgres_outage(host):
    listed = await _post(host, _rpc("tools/list"))
    names = {item["name"] for item in listed.json()["result"]["tools"]}
    assert {
        "audiobook_inspect_chapter", "audiobook_prepare_chapter",
        "audiobook_get_chapter", "audiobook_find_chunk", "book_get_index_status",
        "set_folder_indexing", "list_project_files", "read_project_file",
    }.issubset(names)

    response = await _post(host, _rpc("tools/call", {
        "name": "audiobook_inspect_chapter",
        "arguments": {
            "project": "fixture", "chapter_id": "ch1",
            "prose_filepath": "Chapters/1/chapter.docx",
            "tagged_filepath": "Chapters/1/chapter_audio-tags.docx",
        },
    }))
    assert response.status_code == 200
    result = response.json()["result"]
    payload = json.loads(result["content"][0]["text"])
    assert payload["status"] == "success"
    assert payload["data"]["speech_text"] == "hello"

    status = await _post(host, _rpc("tools/call", {
        "name": "book_get_index_status", "arguments": {"project": "fixture", "limit": 1},
    }, msg_id=7))
    status_payload = json.loads(status.json()["result"]["content"][0]["text"])
    assert status_payload["status"] == "success"
    assert status_payload["data"]["entries"][0]["index_state"] == "blocked"
    assert status_payload["data"]["entries"][0]["error"]["code"] == "index_unavailable"

    files = await _post(host, _rpc("tools/call", {
        "name": "list_project_files",
        "arguments": {"project": "fixture", "path": "Chapters/1"},
    }, msg_id=2))
    file_payload = json.loads(files.json()["result"]["content"][0]["text"])
    assert file_payload["status"] == "success"
    assert {item["path"] for item in file_payload["data"]["entries"]} >= {
        "Chapters/1/chapter.docx", "Chapters/1/chapter_audio-tags.docx",
    }
    assert (host.registry.get("fixture").documents_dir / ".cognita-storage").exists()


@pytest.mark.asyncio
async def test_prepare_receipt_is_principal_scoped_and_permission_precedes_replay(host, monkeypatch):
    inspected = await _post(host, _rpc("tools/call", {
        "name": "audiobook_inspect_chapter",
        "arguments": {
            "project": "fixture", "chapter_id": "ch1",
            "prose_filepath": "Chapters/1/chapter.docx",
            "tagged_filepath": "Chapters/1/chapter_audio-tags.docx",
        },
    }))
    inspect_payload = json.loads(inspected.json()["result"]["content"][0]["text"])
    assert inspect_payload["status"] == "success", inspect_payload
    view = inspect_payload["data"]
    documents = host.registry.get("fixture").documents_dir
    prose = (documents / "Chapters/1/chapter.docx").read_bytes()
    tagged = (documents / "Chapters/1/chapter_audio-tags.docx").read_bytes()
    args = {
        "project": "fixture", "operation_id": "one-prepare", "chapter_id": "ch1",
        "document_view_id": view["document_view_id"],
        "expected_prose_sha256": hashlib.sha256(prose).hexdigest(),
        "expected_tagged_sha256": hashlib.sha256(tagged).hexdigest(),
        "expected_manifest_revision": None,
        "scope": {"kind": "test", "authorization_id": "test-auth"},
        "speech_selection_confirmed": True,
        "request_limit": {"value": 100, "unit": "unicode_codepoints"},
        "expected_settings_sha256": None, "production_target": None,
        "chunks": [{"chunk_id": "chunk-1", "start": 0, "end": 5, "request_spec": None}],
        "publish_bookmarks_to_working_tagged_docx": False,
    }
    message = _rpc("tools/call", {"name": "audiobook_prepare_chapter", "arguments": args})
    owner_headers = {"x-cognita-principal-id": "alice"}
    first = await _post(host, message, headers=owner_headers)
    first_payload = json.loads(first.json()["result"]["content"][0]["text"])
    assert first_payload["status"] == "success" and first_payload["replayed"] is False

    replay = await _post(host, message, headers=owner_headers)
    replay_payload = json.loads(replay.json()["result"]["content"][0]["text"])
    assert replay_payload["status"] == "success" and replay_payload["replayed"] is True

    monkeypatch.setattr(host, "_connector_write_denial", lambda *_args, **_kwargs: {
        "reason": "read_only", "message": "The connector is read-only.",
    })
    denied = await _post(host, message, headers={
        **owner_headers, "x-cognita-connector-id": "read-only-connector",
    })
    denied_payload = json.loads(denied.json()["result"]["content"][0]["text"])
    assert denied_payload["status"] == "error" and denied_payload["reason"] == "read_only"

    monkeypatch.setattr(host, "_connector_write_denial", lambda *_args, **_kwargs: None)
    other_owner = await _post(host, message, headers={"x-cognita-principal-id": "bob"})
    other_payload = json.loads(other_owner.json()["result"]["content"][0]["text"])
    assert other_payload["status"] == "error" and other_payload["reason"] == "stale_manifest"


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", [
    "audiobook_record_generation", "audiobook_import_audio", "audiobook_build",
    "audiobook_commit_build", "audiobook_cancel_job",
])
async def test_read_only_connector_rejects_book_mutations_before_arguments_or_jobs(host, monkeypatch, tool):
    """Book reservations must not be reachable through a read-only connector."""
    monkeypatch.setattr(host, "_connector_write_denial", lambda *_args, **_kwargs: {
        "reason": "read_only", "message": "The connector is read-only.",
    })
    arguments = {
        "audiobook_record_generation": {"project": "fixture", "operation_id": "readonly-record", "change": {
            "kind": "reserve", "chapter_id": "ch1", "snapshot_id": "snapshot", "chunk_id": "chunk",
            "expected_manifest_revision": 1, "request": {"prompt_sha256": "a" * 64,
                "spec": {"provider": "synthetic", "route": "fixture", "model_id": "model", "voice_id": "voice", "parameters": {}, "context_fields": {}}},
        }},
        "audiobook_import_audio": {"project": "fixture", "operation_id": "readonly-import",
            "generation_record_id": "generation", "expected_generation_revision": 1,
            "source": {"kind": "project_file", "filepath": "Audiobook/source.wav", "expected_sha256": "a" * 64},
            "provenance": "native_generation"},
        "audiobook_build": {"project": "fixture", "operation_id": "readonly-build", "expected_head_revision": None,
            "input": {"kind": "chapter", "chapter_id": "ch1", "snapshot_id": "snapshot", "expected_manifest_revision": 1,
                      "request_plan_sha256": "a" * 64, "takes": []}, "mode": "production_pcm", "outputs": {"master": True},
            "gaps": [], "metadata": {"title": "Fixture", "author": "Fixture", "edition": "test"}},
        "audiobook_commit_build": {"project": "fixture", "operation_id": "readonly-commit", "build_id": "build",
            "expected_head_revision": None, "intent": "accept_candidate", "acceptance": {"actor": "fixture",
                "accepted_at": "2026-10-05T00:00:00+00:00", "listening_review": "passed", "notes": []}},
        "audiobook_cancel_job": {"project": "fixture", "operation_id": "readonly-cancel", "job_id": "job",
            "expected_job_revision": 1},
    }[tool]
    response = await _post(host, _rpc("tools/call", {
        "name": tool, "arguments": arguments,
    }), headers={"x-cognita-connector-id": "read-only-connector"})
    payload = json.loads(response.json()["result"]["content"][0]["text"])
    assert payload["status"] == "error" and payload["reason"] == "read_only"


@pytest.mark.asyncio
async def test_gateway_imports_completed_synthetic_raw_pcm_and_reports_durable_job(host):
    # Direct LocalEngineHost calls without trusted forwarded identity are the
    # explicit local maintenance path; the authenticated combined-gateway
    # context has a separate end-to-end source-import proof.
    headers = None
    inspected = await _post(host, _rpc("tools/call", {
        "name": "audiobook_inspect_chapter", "arguments": {
            "project": "fixture", "chapter_id": "ch1",
            "prose_filepath": "Chapters/1/chapter.docx",
            "tagged_filepath": "Chapters/1/chapter_audio-tags.docx",
        },
    }), headers=headers)
    view = json.loads(inspected.json()["result"]["content"][0]["text"])["data"]
    documents = host.registry.get("fixture").documents_dir
    prose = (documents / "Chapters/1/chapter.docx").read_bytes()
    tagged = (documents / "Chapters/1/chapter_audio-tags.docx").read_bytes()
    spec = {"provider": "synthetic", "route": "fixture", "model_id": "model",
            "voice_id": "voice", "parameters": {}, "context_fields": {}}
    prepared_response = await _post(host, _rpc("tools/call", {
        "name": "audiobook_prepare_chapter", "arguments": {
            "project": "fixture", "operation_id": "gateway-prepare-import", "chapter_id": "ch1",
            "document_view_id": view["document_view_id"],
            "expected_prose_sha256": hashlib.sha256(prose).hexdigest(),
            "expected_tagged_sha256": hashlib.sha256(tagged).hexdigest(),
            "expected_manifest_revision": None, "scope": {"kind": "test", "authorization_id": "test-auth"},
            "speech_selection_confirmed": True,
            "request_limit": {"value": 100, "unit": "unicode_codepoints"},
            "expected_settings_sha256": None, "production_target": None,
            "chunks": [{"chunk_id": "gateway-raw", "start": 0, "end": 5, "request_spec": spec}],
            "publish_bookmarks_to_working_tagged_docx": False,
        },
    }), headers=headers)
    prepared = json.loads(prepared_response.json()["result"]["content"][0]["text"])["data"]
    reserved_response = await _post(host, _rpc("tools/call", {
        "name": "audiobook_record_generation", "arguments": {
            "project": "fixture", "operation_id": "gateway-reserve-import", "change": {
                "kind": "reserve", "chapter_id": "ch1", "snapshot_id": prepared["snapshot_id"],
                "chunk_id": "gateway-raw", "expected_manifest_revision": prepared["manifest_revision"],
                "request": {"prompt_sha256": prepared["chunks"][0]["prompt_sha256"], "spec": spec},
            },
        },
    }), headers=headers)
    generation = json.loads(reserved_response.json()["result"]["content"][0]["text"])["data"]["generation"]
    for operation_id, state, revision, extra in (
        ("gateway-submit-import", "submitted", 1, {
            "provider_ids": {"generation_ids": ["synthetic-gateway"]},
            "provider_response_metadata": {"format": "synthetic raw s16le"},
        }),
        ("gateway-complete-import", "completed", 2, {}),
    ):
        response = await _post(host, _rpc("tools/call", {
            "name": "audiobook_record_generation", "arguments": {
                "project": "fixture", "operation_id": operation_id, "change": {
                    "kind": "update", "generation_record_id": generation["generation_record_id"],
                    "expected_generation_revision": revision, "state": state, **extra,
                },
            },
        }), headers=headers)
        generation = json.loads(response.json()["result"]["content"][0]["text"])["data"]["generation"]
    samples = b"\x00\x00\x01\x00\xff\xff\x02\x00"
    source = documents / "Audiobook/Chapters/1/gateway.pcm"
    source.parent.mkdir(parents=True)
    source.write_bytes(samples)
    imported = await _post(host, _rpc("tools/call", {
        "name": "audiobook_import_audio", "arguments": {
            "project": "fixture", "operation_id": "gateway-import-raw",
            "generation_record_id": generation["generation_record_id"],
            "expected_generation_revision": generation["generation_revision"],
            "source": {"kind": "project_file", "filepath": "Audiobook/Chapters/1/gateway.pcm",
                       "expected_sha256": hashlib.sha256(samples).hexdigest()},
            "provenance": "native_generation",
            "source_format": {"container": "raw_pcm", "encoding": "signed_integer",
                              "sample_rate_hz": 8000, "channels": 1, "storage_bits": 16,
                              "valid_bits": 16, "endianness": "little", "interleaving": "interleaved",
                              "provider_format_evidence": "synthetic raw s16le"},
        },
    }), headers=headers)
    job = json.loads(imported.json()["result"]["content"][0]["text"])["data"]
    final = None
    for _ in range(40):
        read = await _post(host, _rpc("tools/call", {
            "name": "audiobook_get_job", "arguments": {"project": "fixture", "job_id": job["job_id"]},
        }), headers=headers)
        final = json.loads(read.json()["result"]["content"][0]["text"])["data"]
        if final["state"] in {"succeeded", "failed", "cancelled"}:
            break
        await asyncio.sleep(0.01)
    assert final is not None
    assert final["error"] is None, final["error"]
    assert final["state"] == "succeeded"
    assert final["result"]["take"]["media"]["canonical_sample_sha256"] == hashlib.sha256(samples).hexdigest()
    take = final["result"]["take"]
    built_response = await _post(host, _rpc("tools/call", {
        "name": "audiobook_build", "arguments": {
            "project": "fixture", "operation_id": "gateway-build", "expected_head_revision": None,
            "input": {"kind": "chapter", "chapter_id": "ch1", "snapshot_id": prepared["snapshot_id"],
                      "expected_manifest_revision": prepared["manifest_revision"],
                      "request_plan_sha256": prepared["request_plan_sha256"],
                      "takes": [{"chunk_id": "gateway-raw", "take_id": take["take_id"],
                                 "request_sha256": take["request_sha256"]}]},
            "mode": "production_pcm", "outputs": {"master": True}, "gaps": [],
            "metadata": {"title": "Fixture", "author": "Fixture", "edition": "test"},
        },
    }), headers=headers)
    build_job = json.loads(built_response.json()["result"]["content"][0]["text"])["data"]
    for _ in range(40):
        read = await _post(host, _rpc("tools/call", {
            "name": "audiobook_get_job", "arguments": {"project": "fixture", "job_id": build_job["job_id"]},
        }), headers=headers)
        build_final = json.loads(read.json()["result"]["content"][0]["text"])["data"]
        if build_final["state"] in {"succeeded", "failed", "cancelled"}:
            break
        await asyncio.sleep(0.01)
    assert build_final["state"] == "succeeded", build_final
    committed_response = await _post(host, _rpc("tools/call", {
        "name": "audiobook_commit_build", "arguments": {
            "project": "fixture", "operation_id": "gateway-commit", "build_id": build_final["result"]["build_id"],
            "expected_head_revision": None, "intent": "accept_candidate",
            "acceptance": {"actor": "fixture", "accepted_at": "2026-10-05T00:00:00+00:00",
                           "listening_review": "passed", "notes": ["synthetic"]},
        },
    }), headers=headers)
    commit_payload = json.loads(committed_response.json()["result"]["content"][0]["text"])
    assert commit_payload["status"] == "success" and commit_payload["data"]["head_revision"] == 1


@pytest.mark.asyncio
async def test_authenticated_workspace_import_reserves_before_controlled_transfer(host, tmp_path):
    from pathlib import Path
    from cognita.books.sources import StagedAudioSource

    docs = host.registry.get("fixture").documents_dir
    connector_store = ConnectorStore(tmp_path / "connectors.yaml")
    connector_config = connector_store.create(
        expected_revision=0, name="Book import", project_names=["fixture"],
    )
    connector = connector_config.connectors[0]
    host.connector_store = connector_store
    auth = AuthenticationPolicyStore(tmp_path / "authentication.yaml", project_names=["fixture"])
    auth.mutate_global(
        expected_revision=0, oauth_enabled=False, static_key_action="generate",
    )
    credential_store = CredentialPolicyStore(
        tmp_path / "credentials-v2.json", master_key_dir=tmp_path / "master-keys",
        admin_password_hash=PasswordHasher().hash("fixture-admin"),
    )
    _credential, token = credential_store.add_credential(
        "combined", connector.id, "Book importer", surface_slug=connector.slug,
        password="fixture-admin",
    )

    samples = b"\x00\x00\x01\x00\xff\xff\x02\x00"
    digest = hashlib.sha256(samples).hexdigest()

    class ControlledBroker:
        def __init__(self):
            self.calls = []

        async def stage_book_workspace_source(
            self, principal, current_connector, project, path, expected_sha256,
            *, staging_root, connector_id, max_bytes, reserve_bytes,
        ):
            state = ProjectState.discover(project.documents_dir)
            active = state.unfinished_import_jobs()
            assert len(active) == 1 and active[0]["state"] == "running"
            assert "url" not in active[0]["payload"]
            assert principal.kind == "static_credential"
            assert principal.principal_id == principal.key_id
            assert current_connector.id == connector.id
            assert project.name == "fixture" and connector_id == connector.id
            assert path == "exports/gateway.pcm" and expected_sha256 == digest
            assert len(samples) <= max_bytes and reserve_bytes >= 0
            self.calls.append((principal, current_connector, project, path))
            stage = Path(staging_root) / ".cognita-book-source-controlled"
            stage.write_bytes(samples)
            return StagedAudioSource(stage, digest, len(samples), "workspace")

    broker = ControlledBroker()
    config = CognitaConfig(
        registry_path=host.registry.path, connectors_path=connector_store.path,
        data_root=tmp_path, public_base_url="https://cognita.example",
    )
    app = create_gateway_app(
        config, host.registry, engine=host, connector_store=connector_store,
        authentication_store=auth, workspace_service=object(), bridge_service=broker,
        credential_store=credential_store,
        credential_admission=credential_store.admission_status,
    )
    app.state.test_connector_slugs = {connector.id: connector.slug}

    async def gateway_post(message):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="https://t") as client:
            return await client.post(
                f"/mcp/connectors/{connector.slug}/mcp/v{PUBLIC_CONTRACT_VERSION}",
                json=message, headers={"Authorization": f"Bearer {token}"},
            )

    inspected = await gateway_post(_rpc("tools/call", {
        "name": "audiobook_inspect_chapter", "arguments": {
            "project": "fixture", "chapter_id": "ch1",
            "prose_filepath": "Chapters/1/chapter.docx",
            "tagged_filepath": "Chapters/1/chapter_audio-tags.docx",
        },
    }))
    inspect_payload = json.loads(inspected.json()["result"]["content"][0]["text"])
    assert inspect_payload["status"] == "success", inspect_payload
    view = inspect_payload["data"]
    prose = (docs / "Chapters/1/chapter.docx").read_bytes()
    tagged = (docs / "Chapters/1/chapter_audio-tags.docx").read_bytes()
    spec = {"provider": "synthetic", "route": "fixture", "model_id": "model",
            "voice_id": "voice", "parameters": {}, "context_fields": {}}
    prepared_response = await gateway_post(_rpc("tools/call", {
        "name": "audiobook_prepare_chapter", "arguments": {
            "project": "fixture", "operation_id": "authenticated-workspace-prepare",
            "chapter_id": "ch1", "document_view_id": view["document_view_id"],
            "expected_prose_sha256": hashlib.sha256(prose).hexdigest(),
            "expected_tagged_sha256": hashlib.sha256(tagged).hexdigest(),
            "expected_manifest_revision": None,
            "scope": {"kind": "test", "authorization_id": "test-auth"},
            "speech_selection_confirmed": True,
            "request_limit": {"value": 100, "unit": "unicode_codepoints"},
            "expected_settings_sha256": None, "production_target": None,
            "chunks": [{"chunk_id": "authenticated-workspace", "start": 0,
                        "end": 5, "request_spec": spec}],
            "publish_bookmarks_to_working_tagged_docx": False,
        },
    }))
    prepared = json.loads(prepared_response.json()["result"]["content"][0]["text"])["data"]
    reserved = await gateway_post(_rpc("tools/call", {
        "name": "audiobook_record_generation", "arguments": {
            "project": "fixture", "operation_id": "authenticated-workspace-reserve",
            "change": {"kind": "reserve", "chapter_id": "ch1",
                "snapshot_id": prepared["snapshot_id"],
                "chunk_id": "authenticated-workspace",
                "expected_manifest_revision": prepared["manifest_revision"],
                "request": {"prompt_sha256": prepared["chunks"][0]["prompt_sha256"], "spec": spec}},
        },
    }))
    generation = json.loads(reserved.json()["result"]["content"][0]["text"])["data"]["generation"]
    for operation_id, current_state, revision, extra in (
        ("authenticated-workspace-submit", "submitted", 1, {
            "provider_ids": {"generation_ids": ["synthetic-broker"]},
            "provider_response_metadata": {"format": "synthetic raw s16le"},
        }),
        ("authenticated-workspace-complete", "completed", 2, {}),
    ):
        response = await gateway_post(_rpc("tools/call", {
            "name": "audiobook_record_generation", "arguments": {
                "project": "fixture", "operation_id": operation_id,
                "change": {"kind": "update",
                    "generation_record_id": generation["generation_record_id"],
                    "expected_generation_revision": revision,
                    "state": current_state, **extra},
            },
        }))
        generation = json.loads(response.json()["result"]["content"][0]["text"])["data"]["generation"]

    imported = await gateway_post(_rpc("tools/call", {
        "name": "audiobook_import_audio", "arguments": {
            "project": "fixture", "operation_id": "authenticated-workspace-import",
            "generation_record_id": generation["generation_record_id"],
            "expected_generation_revision": generation["generation_revision"],
            "source": {"kind": "workspace", "path": "exports/gateway.pcm",
                       "expected_sha256": digest},
            "provenance": "native_generation",
            "source_format": {"container": "raw_pcm", "encoding": "signed_integer",
                "sample_rate_hz": 8000, "channels": 1, "storage_bits": 16,
                "valid_bits": 16, "endianness": "little", "interleaving": "interleaved",
                "provider_format_evidence": "synthetic raw s16le"},
        },
    }))
    assert imported.status_code == 200
    job = json.loads(imported.json()["result"]["content"][0]["text"])["data"]
    final = None
    for _ in range(50):
        read = await gateway_post(_rpc("tools/call", {
            "name": "audiobook_get_job", "arguments": {
                "project": "fixture", "job_id": job["job_id"],
            },
        }))
        final = json.loads(read.json()["result"]["content"][0]["text"])["data"]
        if final["state"] in {"succeeded", "failed", "cancelled"}:
            break
        await asyncio.sleep(0.01)
    assert final is not None and final["state"] == "succeeded", final
    assert final["result"]["take"]["bytes_sha256"] == digest
    assert len(broker.calls) == 1

    listed = await gateway_post(_rpc("tools/call", {
        "name": "list_project_files", "arguments": {
            "project": "fixture", "path": "Audiobook/Chapters/1/takes",
        },
    }))
    listing = json.loads(listed.json()["result"]["content"][0]["text"])
    assert listing["status"] == "success"
    assert any(item["type"] == "directory" for item in listing["data"]["entries"])
    read = await gateway_post(_rpc("tools/call", {
        "name": "read_project_file", "arguments": {
            "project": "fixture", "path": final["result"]["take"]["filepath"],
            "max_bytes": 1024,
        },
    }))
    read_payload = json.loads(read.json()["result"]["content"][0]["text"])
    assert read_payload["status"] == "success"

    mismatched_project = await gateway_post(_rpc("tools/call", {
        "name": "audiobook_inspect_chapter", "arguments": {
            "project": "other", "chapter_id": "ch1",
            "prose_filepath": "Chapters/1/chapter.docx",
            "tagged_filepath": "Chapters/1/chapter_audio-tags.docx",
        },
    }))
    mismatch_payload = json.loads(mismatched_project.json()["result"]["content"][0]["text"])
    assert mismatch_payload["status"] == "error"
    assert mismatch_payload["reason"] == "project_unavailable"

    readonly = connector_store.create(
        expected_revision=connector_config.revision, name="Read only",
        project_mode="selected", default_access=None,
        project_access={"fixture": "read"}, project_names=["fixture"],
    ).connectors[-1]
    app.state.test_connector_slugs[readonly.id] = readonly.slug
    _readonly_credential, readonly_token = credential_store.add_credential(
        "combined", readonly.id, "Read-only client", surface_slug=readonly.slug,
        password="fixture-admin",
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://t",
    ) as client:
        denied = await client.post(
            f"/mcp/connectors/{readonly.slug}/mcp/v{PUBLIC_CONTRACT_VERSION}",
            json=_rpc("tools/call", {
                "name": "audiobook_import_audio", "arguments": {
                    "project": "fixture", "operation_id": "readonly-attempt",
                    "generation_record_id": generation["generation_record_id"],
                    "expected_generation_revision": generation["generation_revision"],
                    "source": {"kind": "workspace", "path": "exports/gateway.pcm",
                               "expected_sha256": digest},
                    "provenance": "native_generation",
                },
            }), headers={"Authorization": f"Bearer {readonly_token}"},
        )
    denied_payload = json.loads(denied.json()["result"]["content"][0]["text"])
    assert denied_payload["status"] == "error" and denied_payload["reason"] == "read_only"
    assert len(broker.calls) == 1


def test_book_wire_capture_suppresses_source_and_malformed_bodies():
    for raw in (
        b'{"jsonrpc":"2.0","method":"tools/call","params":{"name":"read_project_file","arguments":{"content_base64":"c2VjcmV0"}}}',
        b'{"method":"batch","params":[{"method":"tools/call"}]}',
        b'{"jsonrpc":"2.0","method":"tools/call","params":{"name":"audiobook_import_audio"}}',
        b'{"method":"tools/call","params":{"name":"read_project_file"',
    ):
        assert _suppress_wire_bodies(raw) is True
    assert _suppress_wire_bodies(
        b'{"jsonrpc":"2.0","method":"tools/call","params":{"name":"search_knowledge"}}'
    ) is False


def test_proxy_replaces_caller_supplied_book_principal_with_authenticated_state():
    from starlette.requests import Request
    from types import SimpleNamespace

    scope = {
        "type": "http", "method": "POST", "path": "/mcp", "headers": [
            (b"x-cognita-principal-id", b"spoofed-owner"),
            (b"content-type", b"application/json"),
        ], "query_string": b"", "server": ("test", 80),
        "client": ("127.0.0.1", 1234), "scheme": "http", "http_version": "1.1",
    }
    request = Request(scope)
    request.state.cognita_principal = SimpleNamespace(principal_id="authenticated-user", key_id="key-1")
    headers = _forward_headers(request)
    assert headers["x-cognita-principal-id"] == "authenticated-user"
    assert "spoofed-owner" not in headers.values()
