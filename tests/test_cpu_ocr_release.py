"""CPU package proof must fail before any dependent candidate build starts."""
from pathlib import Path
import json
import subprocess
import contextlib
from types import SimpleNamespace

import pytest

from scripts import release


def candidate(tmp_path):
    values = {"version": "test", "commit": "a" * 40, "test_runner_ref": "synthetic:test",
              "test_runner_id": "sha256:" + "b" * 64,
              "image_ref_cognita_cpu": "synthetic:cpu", "image_cognita_cpu": "sha256:" + "c" * 64,
              "image_workspace_runtime": "sha256:" + "d" * 64,
              "image_ref_postgres": "synthetic/pg:pg18@sha256:" + "e" * 64, "image_postgres": "sha256:" + "f" * 64,
              "toolbox_version": "synthetic-toolbox", "toolbox_sha256": "1" * 64}
    (tmp_path / "release.txt").write_text("".join(f"{key}: {value}\n" for key, value in values.items()))
    return values


def qualification(values):
    return {key: values[key] for key in ("version", "commit", "image_cognita_cpu", "image_workspace_runtime",
                                        "test_runner_id", "toolbox_version", "toolbox_sha256")} | {
        "schema": 1, "mode": "full", "result": "passed", "mandatory_ocr": "passed", "missing_file_parity": "passed",
        "cleanup": "verified", "canonical_log": "synthetic-http.log"}


@pytest.mark.parametrize("field", ["commit", "image_cognita_cpu", "image_workspace_runtime", "test_runner_id",
                                    "toolbox_sha256", "mandatory_ocr", "missing_file_parity", "cleanup", "mode"])
def test_cpu_full_qualification_rejects_mismatched_identity_or_results(tmp_path, field):
    values = candidate(tmp_path)
    receipt = qualification(values)
    receipt[field] = "wrong"
    values["qualification_cpu_full"] = json.dumps(receipt)
    with pytest.raises(release.ReleaseError, match="disagrees"):
        release.cpu_full_qualification(values)


def test_qualification_atomic_publication_and_invalidation_preserve_build_identity(tmp_path):
    values = candidate(tmp_path)
    receipt = qualification(values)
    release.update_cpu_full_qualification(tmp_path, receipt)
    recorded = release.candidate_manifest(tmp_path)
    assert release.cpu_full_qualification(recorded) == receipt
    release.update_cpu_full_qualification(tmp_path, None)
    assert release.candidate_manifest(tmp_path) == values
    assert not tuple(tmp_path.glob(".cognita-qualification-*"))


@pytest.mark.parametrize("failure", [None, "OCR", "cleanup"])
def test_canonical_cpu_full_invalidates_before_running_and_publishes_only_after_cleanup(tmp_path, monkeypatch, failure):
    values = candidate(tmp_path)
    release.update_cpu_full_qualification(tmp_path, qualification(values))
    monkeypatch.setattr(release, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(release, "require_clean_checkout", lambda *_args: values["commit"])
    monkeypatch.setattr(release, "read_version", lambda *_args: values["version"])
    monkeypatch.setattr(release, "export_version", lambda *_args: None)
    monkeypatch.setattr(release, "doctor", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(release, "target_lock", lambda *_args: contextlib.nullcontext())
    monkeypatch.setattr(release, "validate_candidate", lambda *_args, **_kwargs: ({"cognita": "synthetic:cpu"}, "synthetic:test", values))
    def execute(*args, **kwargs):
        assert "qualification_cpu_full" not in release.candidate_manifest(tmp_path)
        if failure:
            raise release.ReleaseError("test-failed", failure + " failed")
        return {"schema": 1, "mode": "full", "result": "passed", "mandatory_ocr": "passed",
                "missing_file_parity": "passed", "cleanup": "verified", "canonical_log": "synthetic-http.log"}
    monkeypatch.setattr(release, "run_test_stack", execute)
    args = SimpleNamespace(profile="cpu", mode="full", no_build=True, images=tmp_path)
    if failure:
        with pytest.raises(release.ReleaseError, match=failure):
            release.cmd_test(args, release.TARGETS["test"], release.Log(None))
        assert "qualification_cpu_full" not in release.candidate_manifest(tmp_path)
    else:
        release.cmd_test(args, release.TARGETS["test"], release.Log(None))
        assert release.cpu_full_qualification(release.candidate_manifest(tmp_path))["cleanup"] == "verified"


@pytest.mark.parametrize("receipt", [None, {"mandatory_ocr": "failed"}, {"cleanup": "verified"}])
def test_live_selftest_requires_actual_http_receipt(tmp_path, monkeypatch, receipt):
    monkeypatch.setattr(release, "run", lambda *_args, **_kwargs: (0, "" if receipt is None else "selftest_receipt=" + json.dumps(receipt)))
    with pytest.raises(release.ReleaseError, match="mandatory HTTP/OCR receipt"):
        release.run_live_selftest(repo=tmp_path, target=release.TARGETS["test"], compose_files=[],
                                 key="synthetic", log=release.Log(None), state="test-failed", log_dir=tmp_path)


@pytest.mark.parametrize("alteration", [None, "image", "lock", "worker", "cleanup", "checksum"])
def test_cpu_smoke_proof_binds_images_inputs_inference_and_cleanup(tmp_path, alteration):
    values = candidate(tmp_path)
    (tmp_path / "containers").mkdir()
    for field, relative in (("cpu_ocr_lock_sha256", "ocr-cpu-requirements.lock"),
                            ("cpu_ocr_maintenance_sha256", "ocr-cpu-refresh-evidence.json")):
        target = tmp_path / "containers" / relative
        target.write_bytes(b"synthetic-input")
        values[field] = release.sha256_file(target)
    proof = {"image_id": values["image_cognita_cpu"], "version": values["version"], "commit": values["commit"],
             "requirements_lock_sha256": values["cpu_ocr_lock_sha256"], "maintenance_evidence_sha256": values["cpu_ocr_maintenance_sha256"],
             "exit_code": 0, "worker": {"status": "passed", "worker_cleanup": "reaped", "fixtures": {
                 "canonical-clear.png": {"regions": 4, "device": "cpu", "backend": "pytorch-cpu"},
                 "blank.png": {"regions": 0, "device": "cpu", "backend": "pytorch-cpu"}}},
             "cleanup": {"container_absent": True, "tmpfs_released": True}}
    if alteration == "image":
        proof["image_id"] = "wrong-image"
    elif alteration == "lock":
        proof["requirements_lock_sha256"] = "wrong-lock"
    elif alteration == "worker":
        proof["worker"]["worker_cleanup"] = "unverified"
    elif alteration == "cleanup":
        proof["cleanup"]["container_absent"] = False
    path = tmp_path / "cpu-ocr-smoke.json"
    path.write_text(json.dumps(proof))
    values["cpu_ocr_smoke_sha256"] = "wrong-hash" if alteration == "checksum" else release.sha256_file(path)
    if alteration:
        with pytest.raises(release.ReleaseError, match="proof validation failed"):
            release.validate_cpu_ocr_smoke(tmp_path, values, tmp_path)
    else:
        assert release.validate_cpu_ocr_smoke(tmp_path, values, tmp_path) == proof


def test_cpu_dependency_layers_precede_source_and_release_args():
    dockerfile = Path("containers/cognita/Dockerfile").read_text()
    apt = dockerfile.index("RUN apt-get update")
    ocr = dockerfile.index("RUN python -m venv")
    service = dockerfile.index("RUN python -m pip install --no-cache-dir --index-url")
    source = dockerfile.index("COPY src ./src")
    assert apt < ocr < service < source < dockerfile.index("ARG COGNITA_VERSION")
    assert ocr < dockerfile.index("COPY scripts/verify_ocr_runtime.py")


def test_cpu_smoke_failure_holds_other_profile_builds(tmp_path, monkeypatch):
    events = []
    monkeypatch.setattr(release, "validate_cpu_ocr_inputs", lambda repo: {})
    # 14.2.0: the weights are fetched into the target's model cache, not staged into the build context.
    monkeypatch.setattr(release, "target_models_root", lambda env_file: tmp_path / "models")
    monkeypatch.setattr(release, "fetch_ocr_weights",
                        lambda models_root, log: events.append("models") or models_root / "easyocr")
    monkeypatch.setattr(release, "verify_candidate_image", lambda *args: "sha256:" + "a" * 64)
    monkeypatch.setattr(release, "stage_microsandbox_wheel", lambda *args: events.append("workspace"))
    monkeypatch.setattr(release, "run", lambda command, **kwargs: events.append(command))
    smoke_weights = []
    def smoke(*args, **kwargs):
        events.append("smoke")
        smoke_weights.append(kwargs.get("weights_dir"))
        raise release.ReleaseError("build-failed", "inference failed")
    monkeypatch.setattr(release, "cpu_ocr_smoke", smoke)
    with pytest.raises(release.ReleaseError, match="inference failed"):
        release.build_candidate_images(tmp_path, release.TARGETS["test"], "test", "b" * 40,
            ["amd", "cpu"], tmp_path / "images", tmp_path / "cpu.tar", tmp_path / "SHA256SUMS", release.Log(None))
    assert events[0] == "models" and events[-1] == "smoke"
    assert smoke_weights == [tmp_path / "models" / "easyocr"]
    assert "workspace" not in events
    builds = [event for event in events if isinstance(event, list)]
    assert len(builds) == 1
    assert str(tmp_path / "containers/cognita/Dockerfile") in builds[0]


def test_cpu_smoke_uses_exact_image_and_cleans_container(tmp_path, monkeypatch):
    image = "sha256:" + "a" * 64
    container = "c" * 64
    calls = []
    monkeypatch.setattr(release, "image_identity", lambda ref: (image, "test", "b" * 40))
    monkeypatch.setattr(release, "sha256_file", lambda path: "d" * 64)
    def run(command, **kwargs):
        calls.append(command)
        if command[1] == "create":
            return subprocess.CompletedProcess(command, 0, container + "\n", "")
        return subprocess.CompletedProcess(command, 1 if command[1:3] == ["container", "inspect"] else 0, "", "")
    monkeypatch.setattr(release.subprocess, "run", run)
    class Process:
        returncode = 0
        def __init__(self, command, **kwargs):
            calls.append(command)
        def communicate(self, timeout):
            return json.dumps({"status": "passed", "worker_cleanup": "reaped"}), ""
        def poll(self):
            return 0
    monkeypatch.setattr(release.subprocess, "Popen", Process)
    weights = tmp_path / "models" / "easyocr"
    weights.mkdir(parents=True)
    proof = release.cpu_ocr_smoke(image, tmp_path, release.Log(None), tmp_path / "proof.json", weights_dir=weights)
    creation = calls[0]
    assert "1000:1000" in creation and "--cap-drop=ALL" in creation and "--read-only" in creation
    assert creation[creation.index("--network") + 1] == "none"
    assert image in creation and "--mount" not in creation
    # 14.2.0: the image holds no weights, so the fetched directory is mounted READ-ONLY at exactly the
    # path the service reads them from, and the worker is pointed at that mount (not at /opt).
    assert creation[creation.index("--volume") + 1] == f"{weights.resolve()}:/var/lib/cognita/models/easyocr:ro"
    assert creation[creation.index("--model-dir") + 1] == "/var/lib/cognita/models/easyocr"
    assert creation[creation.index("--manifest") + 1] == "/opt/cognita-models/easyocr-qualification.json"
    assert "/opt/cognita-models/easyocr" not in creation
    assert proof["image_id"] == image and proof["cleanup"]["container_absent"] is True
    assert ["docker", "rm", container] in calls


def test_cpu_smoke_acquisition_timeout_cleans_exact_labeled_container(tmp_path, monkeypatch):
    container = "c" * 64
    owned_name = None
    calls = []
    monkeypatch.setattr(release, "image_identity", lambda ref: ("sha256:" + "a" * 64, "test", "b" * 40))
    def execute(command, **kwargs):
        nonlocal owned_name
        calls.append(command)
        if command[1] == "create":
            owned_name = command[command.index("--name") + 1]
            raise subprocess.TimeoutExpired(command, 60)
        if command[1:3] == ["container", "inspect"] and command[-1] == owned_name:
            return subprocess.CompletedProcess(command, 0, json.dumps([{"Id": container, "Config": {
                "Labels": {"cognita.cpu-ocr-smoke": owned_name}}}]), "")
        return subprocess.CompletedProcess(command, 1 if command[1:3] == ["container", "inspect"] else 0, "", "")
    monkeypatch.setattr(release.subprocess, "run", execute)
    with pytest.raises(release.ReleaseError, match="TimeoutExpired"):
        release.cpu_ocr_smoke("synthetic", tmp_path, release.Log(None), weights_dir=tmp_path / "weights")
    assert ["docker", "rm", container] in calls
    assert not (tmp_path / "cpu-ocr-smoke.json").exists()


def test_the_ocr_weights_are_no_longer_staged_into_the_build_context():
    """14.2.0 (design 5.8, 6.5): nothing copies weights into a build context, and no Dockerfile COPYs them."""
    assert not hasattr(release, "stage_cpu_ocr_models")
    for dockerfile in ("containers/cognita/Dockerfile", "containers/cognita-amd/Dockerfile"):
        text = Path(dockerfile).read_text(encoding="utf-8")
        assert "cognita-amd/models" not in text and ".pth" not in text, dockerfile
    assert not [path for path in Path("containers/cognita-amd").rglob("*.pth")]


def test_cpu_image_generates_its_manifest_without_model_bytes():
    """The CPU build generates the runtime section from its own packages, never reads a model directory, and
    never uses the AMD manifest (its ROCm torch pins would fail CPU OCR's runtime check)."""
    text = Path("containers/cognita/Dockerfile").read_text(encoding="utf-8")
    generate = text[text.index("--generate-manifest"):]
    generate = generate[:generate.index("\n\n")]
    assert "--model-dir" not in generate
    assert "--model-source-manifest /opt/cognita-locks/easyocr-qualification-dependencies.json" in generate
    assert "--manifest /opt/cognita-models/easyocr-qualification.json" in generate
    assert "cognita-amd" not in text
    assert "COPY containers/cognita/runtime-lock.json /opt/cognita-locks/ocr-cpu-runtime.json" in text


def test_committed_cpu_ocr_evidence_matches_the_committed_inputs():
    """The real tree, unmocked (14.0). Every other test that reaches this check
    stubs it out, so an edit to containers/cognita/Dockerfile without a fresh
    `refresh_dependency_locks.py --roles ocr-cpu` passed on Windows and failed
    only when kei tried to build the CPU image."""
    release.validate_cpu_ocr_inputs(Path(release.REPO_ROOT))
