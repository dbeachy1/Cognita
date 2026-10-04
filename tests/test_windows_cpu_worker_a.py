"""Focused regression checks for Worker A's Windows CPU delivery path."""
from __future__ import annotations

import importlib.util
import hashlib
import json
import sys
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parents[1]


def _load_script(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def release(tmp_path, monkeypatch):
    module = _load_script("worker_a_release", REPO / "scripts" / "release.py")
    # From 14.2.0 the build and deploy paths fetch the EasyOCR weights into the target's model
    # cache (fetch_ocr_weights, located through target_models_root, which reads a real env file).
    # These tests exercise the Windows delivery path, not the weights, so neither may touch the
    # network or this machine's env files. raising=False keeps the fixture valid before 14.2.0.
    monkeypatch.setattr(module, "target_models_root", lambda *_args: tmp_path / "models",
                        raising=False)
    monkeypatch.setattr(module, "fetch_ocr_weights",
                        lambda models_root, _log: models_root / "easyocr", raising=False)
    return module


@pytest.fixture
def log(release, tmp_path):
    logger = release.Log(tmp_path / "release.log")
    try:
        yield logger
    finally:
        logger.close()


def test_core_and_full_compose_inputs_are_separate_and_dependency_is_removed():
    core = (REPO / "compose.yaml").read_text(encoding="utf-8")
    workspace = (REPO / "compose.workspace.yaml").read_text(encoding="utf-8")
    assert "workspace-runtime:" not in core
    assert "cognita_broker_secret" not in core
    assert "COGNITA_WORKSPACE_" not in core
    assert "workspace-runtime:" in workspace
    assert "COGNITA_WORKSPACE_RUNTIME_URL: http://workspace-runtime:8080/v1" in workspace
    assert "depends_on:" not in workspace
    assert "propagation: rslave" in core


def test_checkout_file_order_matches_the_core_and_full_contract(release, tmp_path):
    assert [path.name for path in release.checkout_compose_files(tmp_path, "cpu", "core")] == [
        "compose.yaml", "compose.cpu.yaml",
    ]
    assert [path.name for path in release.checkout_compose_files(tmp_path, "amd", "full")] == [
        "compose.yaml", "compose.amd.yaml", "compose.workspace.yaml",
    ]


def test_no_build_test_override_uses_the_accepted_test_runner_image(release):
    override = release.test_override_text(
        version="13.4.0", run_id="owned123", dsn="postgresql://test@postgres/cognita",
        models_root="/tmp/models", mode="core", test_runner_ref="cognita/app-test:accepted",
    )
    assert "image: cognita/app-test:accepted" in override
    assert "workspace-runtime" not in override
    assert 'COGNITA_TEST_MODE: "1"' in override


def test_candidate_validation_binds_fragment_to_qualified_cpu_images(release, tmp_path, monkeypatch):
    monkeypatch.setattr(release, "validate_cpu_ocr_inputs", lambda *_args: {})
    monkeypatch.setattr(release, "validate_cpu_ocr_smoke", lambda *_args: {})
    app = "cognita/app:13.4.0-cpu-abc"
    runner = "cognita/app-test:13.4.0-abc"
    postgres = release.postgres_service_reference(REPO)
    app_id, runner_id = "sha256:cpu", "sha256:test"
    (tmp_path / "compose.cpu.core.images.yaml").write_text(
        release.candidate_fragment_text({"cognita": app}), encoding="utf-8")
    (tmp_path / "release.txt").write_text(
        f"version: 13.4.0\ncommit: abc\n"
        f"image_ref_cognita_cpu: {app}\nimage_cognita_cpu: {app_id}\n"
        f"image_ref_postgres: {postgres}\nimage_postgres: {'sha256:' + '1' * 64}\n"
        f"test_runner_ref: {runner}\ntest_runner_id: {runner_id}\n", encoding="utf-8")
    identities = {app: (app_id, "13.4.0", "abc"), runner: (runner_id, "13.4.0", "abc")}
    monkeypatch.setattr(release, "image_identity", lambda ref: identities[ref])
    monkeypatch.setattr(release, "image_id", lambda ref: "sha256:" + "1" * 64)
    refs, observed_runner, _manifest = release.validate_candidate(
        tmp_path, profile="cpu", mode="core", version="13.4.0", commit="abc")
    assert refs == {"cognita": app}
    assert observed_runner == runner
    (tmp_path / "compose.cpu.core.images.yaml").write_text("services: {}\n", encoding="utf-8")
    with pytest.raises(release.ReleaseError, match="fragment does not match"):
        release.validate_candidate(tmp_path, profile="cpu", mode="core", version="13.4.0", commit="abc")


def test_full_cpu_candidate_adds_the_shared_workspace_image(release, tmp_path, monkeypatch):
    monkeypatch.setattr(release, "validate_cpu_ocr_inputs", lambda *_args: {})
    monkeypatch.setattr(release, "validate_cpu_ocr_smoke", lambda *_args: {})
    app = "cognita/app:13.4.0-cpu-abc"
    workspace = "cognita-workspace-runtime:13.4.0-workspace-abc"
    runner = "cognita/app-test:13.4.0-abc"
    postgres = release.postgres_service_reference(REPO)
    identities = {
        app: ("sha256:cpu", "13.4.0", "abc"),
        workspace: ("sha256:workspace", "13.4.0", "abc"),
        runner: ("sha256:test", "13.4.0", "abc"),
    }
    (tmp_path / "compose.cpu.full.images.yaml").write_text(
        release.candidate_fragment_text({"cognita": app, "workspace-runtime": workspace}),
        encoding="utf-8",
    )
    (tmp_path / "release.txt").write_text(
        "version: 13.4.0\ncommit: abc\n"
        f"image_ref_cognita_cpu: {app}\nimage_cognita_cpu: sha256:cpu\n"
        f"image_ref_postgres: {postgres}\nimage_postgres: {'sha256:' + '1' * 64}\n"
        f"image_ref_workspace_runtime: {workspace}\nimage_workspace_runtime: sha256:workspace\n"
        f"test_runner_ref: {runner}\ntest_runner_id: sha256:test\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(release, "image_identity", lambda ref: identities[ref])
    monkeypatch.setattr(release, "image_id", lambda ref: "sha256:" + "1" * 64)
    refs, observed_runner, _manifest = release.validate_candidate(
        tmp_path, profile="cpu", mode="full", version="13.4.0", commit="abc")
    assert refs == {"cognita": app, "workspace-runtime": workspace}
    assert observed_runner == runner


def test_candidate_build_propagates_only_the_compose_postgres_pin_into_single_export(
        release, tmp_path, monkeypatch, log):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "containers").mkdir()
    for name in ("ocr-cpu-requirements.lock", "ocr-cpu-refresh-evidence.json"):
        (repo / "containers" / name).write_bytes(b"synthetic-input")
    monkeypatch.setattr(release, "validate_cpu_ocr_inputs", lambda *_args: {})
    # 14.2.0 replaced stage_cpu_ocr_models with fetch_ocr_weights (faked in the release fixture)
    # and gave cpu_ocr_smoke a required keyword weights_dir. Patch whichever this release.py has.
    monkeypatch.setattr(release, "stage_cpu_ocr_models", lambda *_args: None, raising=False)
    monkeypatch.setattr(release, "cpu_ocr_smoke", lambda _image, _repo, _log, proof_path, **_kwargs:
                        proof_path.write_text("{}", encoding="utf-8"))
    images = tmp_path / "images"
    archive = tmp_path / "cognita-cpu.tar"
    sums = tmp_path / "SHA256SUMS"
    first = "example.invalid/team/pgvector:pg18@sha256:" + "a" * 64
    second = "mirror.invalid/other/pgvector:pg18-next@sha256:" + "b" * 64
    compose = repo / "compose.yaml"
    compose.write_text(f"services:\n  postgres:\n    image: {first}\n  cognita:\n    image: app\n", encoding="utf-8")
    ids = {first: "sha256:" + "1" * 64, second: "sha256:" + "2" * 64}
    calls = []

    def fake_run(command, **_kwargs):
        calls.append(command)
        if command[1] == "save":
            Path(command[command.index("--output") + 1]).write_bytes(b"one-closed-archive")

    monkeypatch.setattr(release, "run", fake_run)
    monkeypatch.setattr(release, "verify_candidate_image", lambda *_args, **_kwargs: "sha256:" + "3" * 64)
    monkeypatch.setattr(release, "image_id", lambda ref: ids[ref])
    release.build_candidate_images(repo, release.TARGETS["test"], "13.4.0", "a" * 40,
                                   ["cpu"], images, archive, sums, log)

    manifest = release.candidate_manifest(images)
    transport1 = "example.invalid/team/pgvector:cognita-transport-" + "a" * 64
    assert manifest["image_ref_postgres"] == first
    assert manifest["image_postgres"] == ids[first]
    assert ["docker", "pull", first] in calls
    assert ["docker", "tag", ids[first], transport1] in calls
    save = next(call for call in calls if call[1] == "save")
    assert save[-2:] == ["cognita/app:13.4.0-cpu-" + "a" * 40, transport1]
    assert len([call for call in calls if call[1] == "save" and "cognita-cpu.tar" in call[3]]) == 1

    calls.clear()
    compose.write_text(compose.read_text(encoding="utf-8").replace(first, second), encoding="utf-8")
    second_images = tmp_path / "images-second"
    second_archive = tmp_path / "second-cpu.tar"
    release.build_candidate_images(repo, release.TARGETS["test"], "13.4.0", "b" * 40,
                                   ["cpu"], second_images, second_archive, tmp_path / "second-sums", log)
    changed = release.candidate_manifest(second_images)
    transport2 = "mirror.invalid/other/pgvector:cognita-transport-" + "b" * 64
    assert changed["image_ref_postgres"] == second
    assert changed["image_postgres"] == ids[second]
    assert ["docker", "tag", ids[second], transport2] in calls
    assert next(call for call in calls if call[1] == "save")[-1] == transport2


@pytest.mark.parametrize("reference", [
    "example.invalid/pgvector:pg18",
    "example.invalid/pgvector:pg18@sha256:abc",
    "example.invalid/pgvector@sha256:" + "a" * 64,
])
def test_compose_postgres_pin_rejects_tag_only_or_malformed_reference(release, tmp_path, reference):
    (tmp_path / "compose.yaml").write_text(
        f"services:\n  postgres:\n    image: {reference}\n", encoding="utf-8")
    with pytest.raises(release.ReleaseError, match="exact repository:tag@sha256"):
        release.postgres_service_reference(tmp_path)


def test_candidate_validation_requires_postgres_metadata_and_exact_compose_repository(
        release, tmp_path, monkeypatch):
    app = "cognita/app:13.4.0-cpu-abc"
    runner = "cognita/app-test:13.4.0-abc"
    postgres = "example.invalid/pgvector:pg18@sha256:" + "a" * 64
    (tmp_path / "compose.yaml").write_text(f"services:\n  postgres:\n    image: {postgres}\n", encoding="utf-8")
    (tmp_path / "compose.cpu.core.images.yaml").write_text(
        release.candidate_fragment_text({"cognita": app}), encoding="utf-8")
    manifest = tmp_path / "release.txt"
    manifest.write_text(
        f"version: 13.4.0\ncommit: abc\nimage_ref_cognita_cpu: {app}\nimage_cognita_cpu: sha256:cpu\n"
        f"test_runner_ref: {runner}\ntest_runner_id: sha256:test\n"
        f"image_ref_postgres: {postgres}\nimage_postgres: {'sha256:' + '1' * 64}\n", encoding="utf-8")
    identities = {app: ("sha256:cpu", "13.4.0", "abc"), runner: ("sha256:test", "13.4.0", "abc")}
    monkeypatch.setattr(release, "image_identity", lambda ref: identities[ref])
    monkeypatch.setattr(release, "image_id", lambda _ref: "sha256:" + "1" * 64)
    with pytest.raises(release.ReleaseError, match="missing image_ref_postgres"):
        manifest.write_text(manifest.read_text(encoding="utf-8").replace(
            f"image_ref_postgres: {postgres}\n", ""), encoding="utf-8")
        release.candidate_manifest(tmp_path)
    manifest.write_text(
        f"version: 13.4.0\ncommit: abc\nimage_ref_cognita_cpu: {app}\nimage_cognita_cpu: sha256:cpu\n"
        f"test_runner_ref: {runner}\ntest_runner_id: sha256:test\n"
        f"image_ref_postgres: wrong.invalid/pgvector:pg18@sha256:{'a' * 64}\n"
        f"image_postgres: {'sha256:' + '1' * 64}\n", encoding="utf-8")
    monkeypatch.setattr(release, "REPO_ROOT", tmp_path)
    with pytest.raises(release.ReleaseError, match="does not match current compose.yaml"):
        release.validate_candidate(tmp_path, profile="cpu", mode="core", version="13.4.0", commit="abc")


def test_no_build_core_qualification_never_calls_build_images(release, tmp_path, log, monkeypatch):
    target = release.TARGETS["test"]
    monkeypatch.setattr(release, "require_clean_checkout", lambda *_args: "abc")
    monkeypatch.setattr(release, "read_version", lambda *_args: "13.4.0")
    monkeypatch.setattr(release, "export_version", lambda *_args: None)
    monkeypatch.setattr(release, "target_lock", lambda *_args: __import__("contextlib").nullcontext())
    monkeypatch.setattr(release, "doctor", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(release, "validate_candidate", lambda *_args, **_kwargs:
                        ({"cognita": "accepted-app"}, "accepted-test-runner", {}))
    monkeypatch.setattr(release, "build_images", lambda *_args, **_kwargs:
                        pytest.fail("no-build qualification attempted to build app images"))
    calls = []
    monkeypatch.setattr(release, "run_test_stack", lambda *args, **kwargs: calls.append((args, kwargs)))
    release.cmd_test(SimpleNamespace(profile="cpu", mode="core", no_build=True, images=tmp_path),
                     target, log)
    assert len(calls) == 1
    assert calls[0][1]["profile"] == "cpu"
    assert calls[0][1]["mode"] == "core"
    assert calls[0][1]["no_build"] is True
    assert calls[0][1]["candidate_images_dir"] == tmp_path


@pytest.mark.parametrize("run_qa, qa_fails", [(False, False), (True, False), (True, True)])
def test_no_build_beta_deploy_honors_live_qa_and_propagates_its_failure(
        release, tmp_path, log, monkeypatch, run_qa, qa_fails):
    target = release.TARGETS["beta"]
    candidate_commit = "a" * 40
    images_dir = tmp_path / "images"
    images = {"cognita": "accepted-app", "workspace-runtime": "accepted-workspace"}
    values = {"commit": candidate_commit, "toolbox_version": "12.6.0"}
    events = []
    qa_paths = []
    monkeypatch.setattr(release, "require_clean_checkout", lambda *_args: "b" * 40)
    monkeypatch.setattr(release, "read_version", lambda *_args: "13.4.0")
    monkeypatch.setattr(release, "export_version", lambda *_args: None)
    monkeypatch.setattr(release, "read_toolbox_version", lambda *_args: values["toolbox_version"])
    monkeypatch.setattr(release, "target_lock", lambda *_args: nullcontext())
    monkeypatch.setattr(release, "doctor", lambda *_args: None)
    monkeypatch.setattr(release, "check_version_free", lambda *_args: None)
    monkeypatch.setattr(release, "candidate_manifest", lambda _path: values)
    monkeypatch.setattr(release.subprocess, "run", lambda *_args, **_kwargs:
                        SimpleNamespace(returncode=0, stdout="same-tree\n"))
    monkeypatch.setattr(release, "validate_candidate", lambda *_args, **_kwargs:
                        (images, "accepted-test-runner", values))
    monkeypatch.setattr(release, "image_id", lambda ref: f"sha256:{ref}")
    monkeypatch.setattr(release, "stage_candidate_toolbox", lambda *_args: None)
    monkeypatch.setattr(release, "build_images", lambda *_args, **_kwargs:
                        pytest.fail("no-build deployment attempted to build app images"))
    monkeypatch.setattr(release, "build_toolbox", lambda *_args, **_kwargs:
                        pytest.fail("no-build deployment attempted to build Toolbox"))

    def qualify(repo, observed_target, version, commit, observed_images, observed_log, **kwargs):
        assert (repo, observed_target, version, commit, observed_images, observed_log) == (
            REPO, target, "13.4.0", candidate_commit, images, log)
        assert kwargs["test_runner_ref"] == "accepted-test-runner"
        assert kwargs["no_build"] is True
        assert kwargs["candidate_images_dir"] == images_dir
        assert (kwargs["profile"], kwargs["mode"]) == ("amd", "full")
        events.append("qualify")

    def stage(*_args, **_kwargs):
        events.append("stage")
        return tmp_path / "staged-release"

    def qa(repo, observed_target, version, scratch, observed_log):
        assert (repo, observed_target, version, observed_log) == (REPO, target, "13.4.0", log)
        assert scratch.is_dir()
        qa_paths.append(scratch)
        (scratch / "test-mode.override.yaml").write_text("synthetic override", encoding="utf-8")
        events.append("qa")
        if qa_fails:
            raise release.ReleaseError("verify-failed", "candidate live QA failed")

    monkeypatch.setattr(release, "run_test_stack", qualify)
    monkeypatch.setattr(release, "stage_release", stage)
    monkeypatch.setattr(release, "apply_release", lambda *_args: events.append("apply"))
    monkeypatch.setattr(release, "verify_release", lambda *_args: events.append("verify"))
    monkeypatch.setattr(release, "qa_release", qa)
    args = SimpleNamespace(no_build=True, images=images_dir, profile="amd", mode="full", test=run_qa)
    if qa_fails:
        with pytest.raises(release.ReleaseError, match="candidate live QA failed") as failure:
            release.cmd_deploy(args, target, log)
        assert failure.value.state == "verify-failed"
    else:
        release.cmd_deploy(args, target, log)
    assert events == ["qualify", "stage", "apply", "verify"] + (["qa"] if run_qa else [])
    assert len(qa_paths) == int(run_qa)
    assert all(not path.exists() for path in qa_paths)
    if qa_fails:
        assert "deploy: reused candidate images" not in log.path.read_text(encoding="utf-8")


def test_core_synthetic_install_has_no_workspace_paths_or_secret(monkeypatch, tmp_path):
    helper = _load_script("worker_a_kei_http", REPO / "scripts" / "kei_http_selftest.py")
    monkeypatch.setattr(helper.os, "getuid", lambda: 1000, raising=False)
    monkeypatch.setattr(helper.os, "getgid", lambda: 1000, raising=False)
    monkeypatch.setattr(helper, "_device_gid", lambda *_args: 44)
    monkeypatch.setattr(helper, "kvm_gid", lambda: 993)
    monkeypatch.setattr(helper, "_self_signed_admin_tls", lambda *_args: None)
    install_root = tmp_path / "core"
    install_root.mkdir()
    generated = helper.write_config(install_root, 18875, 18876, "13.4.0", mode="core")
    env = generated.env_path.read_text(encoding="utf-8")
    assert "COGNITA_WORKSPACE_DATA_ROOT" not in env
    assert "COGNITA_KVM_GID" not in env
    assert not (install_root / "workspaces").exists()
    assert not (install_root / "secrets" / "broker.secret").exists()
    assert not (install_root / "config" / "workspace-connectors.yaml").exists()
    assert "workspace_enabled: false" in (install_root / "config" / "connectors.yaml").read_text()


@pytest.mark.parametrize("deferred", [False, True])
def test_fixture_provisioner_keeps_production_check_separate(monkeypatch, capsys, deferred):
    provision = _load_script("worker_a_provision_selftest", REPO / "scripts" / "provision_selftest.py")
    seen = {}

    def fake_provision(**kwargs):
        seen.update(kwargs)
        return SimpleNamespace(project_created=False, copied=(), verified=())

    monkeypatch.setattr(provision, "provision_self_test", fake_provision)
    arguments = ["provision_selftest.py", "--documents-dir", "docs", "--data-dir", "data",
                 "--registry", "registry.yaml"]
    if deferred:
        arguments.append("--defer-connector-check")
    else:
        arguments.extend(("--connectors", "connectors.yaml"))
    monkeypatch.setattr(sys, "argv", arguments)
    assert provision.main() == 0
    result = json.loads(capsys.readouterr().out)
    assert seen["connectors_path"] == (None if deferred else Path("connectors.yaml"))
    assert result["status"] == ("SELFTEST_FIXTURES_READY_CONNECTOR_DEFERRED"
                                 if deferred else "SELFTEST_FIXTURES_READY")


def test_core_runner_contract_has_mode_aware_exact_catalog_and_negative_calls():
    runner = (REPO / "scripts" / "run-selftest.py").read_text(encoding="utf-8")
    assert 'parser.add_argument("--mode", choices=("core", "full"), required=True)' in runner
    assert "if mode == \"full\":\n        run_workspace_plan" in runner
    assert 'stale.get("reason") == "runtime_unavailable"' in runner
    assert "workspace=not_applicable reason=host_workspace_disabled" in runner


@pytest.mark.parametrize("mode", ["core", "full"])
@pytest.mark.parametrize("update_helper_present", [False, True])
def test_windows_bundle_includes_stdin_credential_helper_in_checksum_set(
        release, tmp_path, monkeypatch, mode, update_helper_present):
    repo = tmp_path / "repo"
    images = tmp_path / "images"
    output = tmp_path / "bundle"
    (repo / "scripts" / "windows").mkdir(parents=True)
    (repo / "scripts").mkdir(exist_ok=True)
    (repo / "docs").mkdir()
    images.mkdir()
    for relative, content in (
        ("compose.yaml", "services:\n  postgres:\n    image: pgvector/pgvector:pg18@sha256:" + "a" * 64 + "\n"),
        ("compose.cpu.yaml", "services: {}\n"),
        ("scripts/set-admin-credentials.py", "# stdin credential helper\n"),
        ("scripts/run-selftest.py", "# runner\n"),
        ("scripts/provision_selftest.py", "# provisioner\n"),
        ("scripts/windows/Install-CognitaWindows.ps1", "# installer\n"),
        ("scripts/windows/Update-Release.py", "# update lifecycle helper\n"),
        ("docs/INSTALL-WINDOWS.md", "Windows operator guide\n"),
    ):
        if relative == "scripts/windows/Update-Release.py" and not update_helper_present:
            continue
        target = repo / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    if mode == "full":
        (repo / "compose.workspace.yaml").write_text("services: {}\n", encoding="utf-8")
    cpu_archive = tmp_path / "cognita-cpu.tar"
    cpu_archive.write_bytes(b"cpu-image")
    workspace_archive = images / "workspace-runtime.tar"
    toolbox_archive = tmp_path / "toolbox.tar"
    workspace_archive.write_bytes(b"workspace-image")
    toolbox_archive.write_bytes(b"toolbox-image")
    app_ref = "cognita/app:13.4.0-cpu-abc"
    postgres_ref = "pgvector/pgvector:pg18@sha256:" + "a" * 64
    workspace_ref = "cognita-workspace-runtime:13.4.0-abc"
    values = {
        "version": "13.4.0", "commit": "abc",
        "image_ref_cognita_cpu": app_ref, "image_cognita_cpu": "sha256:cpu",
        "image_ref_postgres": postgres_ref, "image_postgres": "sha256:" + "1" * 64,
        "cpu_archive_sha256": hashlib.sha256(cpu_archive.read_bytes()).hexdigest(),
        "workspace_archive": workspace_archive.name,
        "workspace_archive_sha256": hashlib.sha256(workspace_archive.read_bytes()).hexdigest(),
        "toolbox_archive": str(toolbox_archive),
        "toolbox_sha256": hashlib.sha256(toolbox_archive.read_bytes()).hexdigest(),
    }
    if mode == "full":
        values["image_ref_workspace_runtime"] = workspace_ref
        values["image_workspace_runtime"] = "sha256:workspace"
        values["test_runner_id"] = "sha256:test"
        values["toolbox_version"] = "synthetic-toolbox"
        receipt = {key: values[key] for key in ("version", "commit", "image_cognita_cpu", "image_workspace_runtime",
                                               "test_runner_id", "toolbox_version", "toolbox_sha256")}
        receipt.update(schema=1, mode="full", result="passed", mandatory_ocr="passed", missing_file_parity="passed",
                       cleanup="verified", canonical_log="synthetic-http.log")
        values["qualification_cpu_full"] = json.dumps(receipt)
    fragment_name = f"compose.cpu.{mode}.images.yaml"
    (images / fragment_name).write_text(f"image: {app_ref}\n", encoding="utf-8")
    monkeypatch.setattr(release, "candidate_manifest", lambda _path: values)
    monkeypatch.setattr(release, "require_clean_checkout", lambda *_args: None)
    monkeypatch.setattr(release, "verify_candidate_image", lambda *_args: None)
    monkeypatch.setattr(release, "validate_candidate", lambda *_args, **_kwargs:
                        ({"cognita": app_ref}, "test-runner", {}))
    monkeypatch.setattr(release.subprocess, "run", lambda *_args, **_kwargs:
                        type("Result", (), {"returncode": 0, "stdout": "same-tree\n"})())

    log = release.Log(tmp_path / "release.log")
    try:
        if not update_helper_present:
            with pytest.raises(release.ReleaseError, match="Update-Release.py"):
                release._bundle_windows(repo, images, cpu_archive, output, log, mode=mode)
            assert not output.exists()
            assert not tuple(tmp_path.glob("bundle.*.staging"))
            return
        release._bundle_windows(repo, images, cpu_archive, output, log, mode=mode)
    finally:
        log.close()

    helper = output / "scripts" / "set-admin-credentials.py"
    sums = (output / "SHA256SUMS").read_text(encoding="utf-8")
    release_metadata = (output / "release.txt").read_text(encoding="utf-8")
    assert helper.read_text(encoding="utf-8") == "# stdin credential helper\n"
    assert "scripts/set-admin-credentials.py" in sums
    assert (output / "scripts/windows/Update-Release.py").read_text() == "# update lifecycle helper\n"
    assert "scripts/windows/Update-Release.py" in sums
    assert f"image_ref_postgres: {postgres_ref}\n" in release_metadata
    assert f"image_postgres: {'sha256:' + '1' * 64}\n" in release_metadata
    release.verify_bundle_checksums(output)
