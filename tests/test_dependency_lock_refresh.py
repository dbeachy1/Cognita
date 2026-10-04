"""Focused safety contracts for the deliberate dependency refresh command."""

from __future__ import annotations

from pathlib import Path
import hashlib
import json
import zipfile

import pytest

from scripts import refresh_dependency_locks as refresh
from scripts.dependency_locks import cpu_ocr_declaration, validate_cpu_ocr_lock


def test_cpu_role_does_not_require_unselected_inputs(tmp_path):
    root = Path(__file__).parents[1]
    for name in refresh.ROLE_SOURCE_DECLARATIONS["ocr-cpu"]:
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((root / name).read_bytes())
    assert tuple(refresh.role_source_paths(tmp_path, ("ocr-cpu",))) == ("ocr-cpu",)
    assert tuple(refresh._resolver_specs(tmp_path, ("ocr-cpu",))) == ("ocr-cpu",)
    text = refresh._role_input_text(tmp_path, "ocr-cpu", refresh.role_source_paths(tmp_path, ("ocr-cpu",))["ocr-cpu"])
    versions, vendors = cpu_ocr_declaration(tmp_path / "containers/cognita/runtime-lock.json")
    assert len(versions) == 27 and set(vendors) == {"torch", "torchvision"}
    assert "rocm" not in text and "triton" not in text


@pytest.mark.parametrize("roles", [(), ("ocr-cpu", "ocr-cpu"), ("unknown",)])
def test_roles_are_rejected_before_acquisition(tmp_path, roles):
    with pytest.raises(refresh.RefreshError, match="nonempty, unique"):
        refresh._validate_request(refresh.RefreshRequest(tmp_path, tmp_path / "evidence", "https://pypi.org/simple", roles=roles))


def test_cpu_lock_follows_source_authority_and_rejects_unhashed_input(tmp_path):
    root = Path(__file__).parents[1]
    declaration = tmp_path / "runtime.json"
    declaration.write_bytes((root / "containers/cognita/runtime-lock.json").read_bytes())
    value = json.loads(declaration.read_text())
    records = []
    for requirement in value["requirements"]:
        digest = requirement.split("#sha256=")[-1] if "#sha256=" in requirement else "a" * 64
        records.append(requirement + " --hash=sha256:" + digest)
    lock = tmp_path / "cpu.lock"
    lock.write_text("\n".join(records) + "\n")
    assert validate_cpu_ocr_lock(declaration, lock)["easyocr"]
    value["requirements"][0] = "easyocr==1.7.3"
    declaration.write_text(json.dumps(value))
    with pytest.raises(refresh.DependencyLockError, match="version mismatch"):
        validate_cpu_ocr_lock(declaration, lock)
    value["requirements"][-1] = value["requirements"][-1].split("#", 1)[0]
    declaration.write_text(json.dumps(value))
    with pytest.raises(refresh.DependencyLockError, match="invalid vendor"):
        cpu_ocr_declaration(declaration)


@pytest.mark.parametrize("change_input", [False, True])
def test_selected_refresh_publishes_only_cpu_and_detects_input_changes(tmp_path, monkeypatch, change_input):
    import subprocess
    root = tmp_path / "checkout"
    source_root = Path(__file__).parents[1]
    for name in (*refresh.ROLE_SOURCE_DECLARATIONS["ocr-cpu"], "containers/resolver-tooling.lock"):
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((source_root / name).read_bytes())
    sentinel = root / "containers/service-requirements.lock"
    sentinel.write_bytes(b"unselected bytes")
    monkeypatch.setattr(refresh.platform, "system", lambda: "Linux")
    monkeypatch.setattr(refresh.shutil, "which", lambda name: "docker")
    identity = refresh.SourceIdentity("a" * 40, "b" * 40, "test", "https://example.test/repo")
    monkeypatch.setattr(refresh, "source_identity", lambda root: identity)
    monkeypatch.setattr(refresh, "_download_and_validate_ocr_wheels", lambda *args, **kwargs: {})
    calls = []
    scratches = []
    def runner(command, **kwargs):
        calls.append(command)
        if "/work/ocr-cpu.lock" in command:
            mount = next(value for value in command if value.startswith("type=bind,src=") and value.endswith(",dst=/work"))
            scratch = Path(mount.removeprefix("type=bind,src=").removesuffix(",dst=/work"))
            scratches.append(scratch)
            lines = []
            for line in (scratch / "ocr-cpu.in").read_text().splitlines():
                digest = line.split("#sha256=")[-1] if "#sha256=" in line else "a" * 64
                lines.append(line + " --hash=sha256:" + digest)
            (scratch / "ocr-cpu.lock").write_text("\n".join(lines) + "\n")
            if change_input:
                with (root / "containers/cognita/runtime-lock.json").open("a") as output:
                    output.write("\n")
        return subprocess.CompletedProcess(command, 0, "pip-compile test", "")
    request = refresh.RefreshRequest(root, tmp_path / "evidence", "https://pypi.org/simple", roles=("ocr-cpu",))
    if change_input:
        with pytest.raises(refresh.RefreshError, match="inputs changed"):
            refresh.refresh(request, runner=runner)
        assert not (root / "containers/ocr-cpu-requirements.lock").exists()
        assert not request.evidence_root.exists()
    else:
        result = refresh.refresh(request, runner=runner)
        assert tuple(result.locks) == ("ocr-cpu",)
        evidence = json.loads(result.evidence.read_text())
        assert evidence["selected_roles"] == ["ocr-cpu"]
        assert set(evidence["roles"]) == {"ocr-cpu"}
        assert str((root / "containers/cognita/runtime-lock.json").resolve()) in evidence["consumed_source_sha256"]
    assert sentinel.read_bytes() == b"unselected bytes"
    assert all(not path.exists() for path in scratches)
    assert all("embedding" not in " ".join(map(str, command)) for command in calls)


def test_role_sources_are_code_owned_and_cover_exactly_all_eight_roles() -> None:
    root = Path(__file__).parents[1]
    parsed = refresh.role_source_paths(root)
    assert tuple(parsed) == refresh.LOCK_ROLES
    assert all(parsed[role] for role in refresh.LOCK_ROLES)


def test_role_sources_reject_caller_selected_inputs() -> None:
    with pytest.raises(refresh.RefreshError, match="source-owned"):
        refresh.parse_role_sources(["service=a"])


def test_resolver_mapping_matches_shipped_interpreters_and_indexes() -> None:
    root = Path(__file__).parents[1]
    specs = refresh._resolver_specs(root)
    assert "python:3.13" in specs["service"].image
    assert specs["service"].python == "/usr/local/bin/python"
    assert "rocm/dev-ubuntu-24.04" in specs["embedding"].image
    assert specs["embedding"].python == "/usr/bin/python3"
    assert specs["ocr"].indexes == ("pypi",)


def test_embedding_artifact_is_source_owned_and_exact() -> None:
    root = Path(__file__).parents[1]
    url, digest = refresh._embedding_artifact(root)
    assert "cp312" in url
    assert digest == refresh._MIGRAPHX_SHA256


def test_ocr_inputs_use_exact_vendor_artifacts_without_a_broad_extra_index() -> None:
    root = Path(__file__).parents[1]
    paths = refresh.role_source_paths(root)["ocr"]
    text = refresh._role_input_text(root, "ocr", paths)
    assert f"torch @ {refresh._TORCH_ROCM_URL}#sha256={refresh._TORCH_ROCM_SHA256}" in text
    assert f"torchvision @ {refresh._TORCHVISION_ROCM_URL}#sha256={refresh._TORCHVISION_ROCM_SHA256}" in text
    assert f"triton-rocm @ {refresh._TRITON_ROCM_URL}#sha256={refresh._TRITON_ROCM_SHA256}" in text
    assert "--extra-index-url" not in text


def test_ocr_vendor_contract_rejects_wrong_triton_source_or_abi(tmp_path: Path) -> None:
    root = Path(__file__).parents[1]
    runtime = json.loads((root / "containers/cognita-amd/runtime-lock.json").read_text(encoding="utf-8"))
    runtime["ocr"]["vendor_wheels"]["triton-rocm"]["url"] = refresh._TRITON_ROCM_URL.replace("https://", "http://")
    path = tmp_path / "runtime-lock.json"
    path.write_text(json.dumps(runtime), encoding="utf-8")
    with pytest.raises(refresh.RefreshError, match="approved triton-rocm"):
        refresh._ocr_vendor_wheels(path)


def test_ocr_wheel_validation_checks_bytes_metadata_and_tag(tmp_path: Path) -> None:
    record = dict(refresh._OCR_VENDOR_WHEEL_CONTRACT["triton-rocm"])
    metadata = b"Metadata-Version: 2.1\nName: triton-rocm\nVersion: 3.6.0\n"
    wheel_text = b"Wheel-Version: 1.0\nTag: cp313-cp313-linux_x86_64\n"
    path = tmp_path / record["filename"]
    with zipfile.ZipFile(path, "w") as wheel:
        wheel.writestr("triton_rocm-3.6.0.dist-info/METADATA", metadata)
        wheel.writestr("triton_rocm-3.6.0.dist-info/WHEEL", wheel_text)
    record["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    record["metadata_sha256"] = hashlib.sha256(metadata).hexdigest()
    validated = refresh._validate_ocr_wheel(path, record)
    assert validated["name"] == "triton-rocm"
    record["python_abi"] = "cp312"
    with pytest.raises(refresh.RefreshError, match="ABI/platform"):
        refresh._validate_ocr_wheel(path, record)


def test_pip_tools_hash_continuations_are_canonicalized_without_dropping_hashes() -> None:
    text = "# generated\ndemo==1.0 \\\n+  --hash=sha256:" + "a" * 64 + " \\\n+  --hash=sha256:" + "b" * 64 + "\n"
    text = "\n".join(
        [
            "# generated",
            "demo==1.0 \\",
            "  --hash=sha256:" + "a" * 64 + " \\",
            "  --hash=sha256:" + "b" * 64,
            "",
        ]
    )
    canonical = refresh._canonicalize_compiled_lock(text, role="service")
    parsed = refresh.parse_hash_locked_requirements(canonical, role="service")
    assert parsed[0].hashes == ("a" * 64, "b" * 64)


def test_hash_lock_parser_accepts_pinned_distribution_extras() -> None:
    text = "uvicorn[standard]==0.30.0 --hash=sha256:" + "a" * 64 + "\n"
    parsed = refresh.parse_hash_locked_requirements(text, role="service")
    assert parsed[0].name == "uvicorn"
    assert parsed[0].version == "0.30.0"


def test_embedding_lock_omits_only_fastembed_cpu_onnxruntime() -> None:
    text = "fastembed==0.8.0 --hash=sha256:" + "a" * 64 + "\n" + "onnxruntime==1.30.0 --hash=sha256:" + "b" * 64 + "\n"
    omitted = refresh._omit_fastembed_cpu_onnxruntime(text, role="embedding")
    assert "onnxruntime==" not in omitted
    assert "fastembed==" in omitted
    assert refresh._omit_fastembed_cpu_onnxruntime(text, role="service") == text


def test_hash_lock_parser_preserves_verified_direct_artifact_reference() -> None:
    url = "https://repo.radeon.com/example.whl#sha256=" + "b" * 64
    text = "onnxruntime-migraphx @ " + url + " --hash=sha256:" + "b" * 64 + "\n"
    parsed = refresh.parse_hash_locked_requirements(text, role="embedding")
    assert parsed[0].name == "onnxruntime-migraphx"
    assert parsed[0].version == "@ " + url


def test_hash_lock_parser_rejects_direct_artifact_with_wrong_fragment_hash() -> None:
    url = "https://repo.radeon.com/example.whl#sha256=" + "c" * 63
    text = "onnxruntime-migraphx @ " + url + " --hash=sha256:" + "b" * 64 + "\n"
    with pytest.raises(refresh.DependencyLockError, match="direct artifact URL"):
        refresh.parse_hash_locked_requirements(text, role="embedding")


@pytest.mark.parametrize(
    "url",
    [
        "http://repo.radeon.com/example.whl#sha256=" + "b" * 64,
        "https://repo.radeon.com/example.whl?token=secret#sha256=" + "b" * 64,
    ],
)
def test_hash_lock_parser_rejects_noncanonical_direct_artifact_urls(url: str) -> None:
    text = "onnxruntime-migraphx @ " + url + " --hash=sha256:" + "b" * 64 + "\n"
    with pytest.raises(refresh.DependencyLockError, match="HTTPS"):
        refresh.parse_hash_locked_requirements(text, role="embedding")


def test_resolver_tooling_lock_is_the_fixed_eight_wheel_selection() -> None:
    root = Path(__file__).parents[1]
    lines = tuple(
        line.strip()
        for line in (root / "containers/resolver-tooling.lock").read_text(encoding="utf-8").splitlines()
        if line.strip()
    )
    assert lines == refresh._RESOLVER_TOOLING_LOCK


def test_resolver_dockerfile_bootstraps_isolated_pinned_toolchain(tmp_path: Path) -> None:
    spec = refresh.ResolverSpec("python:3.13-slim@sha256:" + "a" * 64, "/usr/local/bin/python", ("pypi",))
    path = tmp_path / "Dockerfile"
    refresh._write_resolver_dockerfile(path, role="service", spec=spec, index_url="https://pypi.org/simple")
    dockerfile = path.read_text(encoding="utf-8")
    assert "pip install --no-cache-dir --index-url" not in dockerfile
    assert "pip download --isolated --no-deps --only-binary=:all: --require-hashes" in dockerfile
    assert "python -m venv /opt/cognita-resolver" in dockerfile
    assert "/opt/cognita-resolver/bin/python -m pip check" in dockerfile


def test_embedding_artifact_rejects_wrong_hash_and_abi(tmp_path: Path) -> None:
    import json

    source_root = Path(__file__).parents[1]
    amd = tmp_path / "containers" / "cognita-amd"
    amd.mkdir(parents=True)
    runtime = source_root / "containers/cognita-amd/runtime-lock.json"
    dockerfile = source_root / "containers/cognita-amd/Dockerfile"

    wrong_hash = json.loads(runtime.read_text(encoding="utf-8"))
    wrong_hash["embedding"]["provider_wheel"]["sha256"] = "d" * 64
    (amd / "runtime-lock.json").write_text(json.dumps(wrong_hash), encoding="utf-8")
    (amd / "Dockerfile").write_text(dockerfile.read_text(encoding="utf-8"), encoding="utf-8")
    with pytest.raises(refresh.RefreshError, match="approved CPython 3.12 wheel"):
        refresh._embedding_artifact(tmp_path)

    wrong_abi = json.loads(runtime.read_text(encoding="utf-8"))
    wheel = wrong_abi["embedding"]["provider_wheel"]
    wheel["filename"] = wheel["filename"].replace("cp312", "cp311")
    wrong_url = refresh._MIGRAPHX_URL.replace("cp312", "cp311")
    (amd / "runtime-lock.json").write_text(json.dumps(wrong_abi), encoding="utf-8")
    (amd / "Dockerfile").write_text(dockerfile.read_text(encoding="utf-8").replace(refresh._MIGRAPHX_URL, wrong_url), encoding="utf-8")
    with pytest.raises(refresh.RefreshError, match="approved CPython 3.12 wheel"):
        refresh._embedding_artifact(tmp_path)


def test_release_script_does_not_import_or_call_refresh_command() -> None:
    """Refreshing the dependency locks stays an explicit developer command.

    (Superseded: this used to import `scripts.kei_release`, which 13.0 Ã‚Â§8
    deletes.  `scripts/release.py` is the release path now, and the rule is
    the same one: a release must never re-resolve dependencies behind Doug's
    back, so it may not reach this module at all.)
    """
    source = (Path(__file__).parents[1] / "scripts/release.py").read_text(encoding="utf-8")
    assert "refresh_dependency_locks" not in source
    # Release validation may consume locks; only resolution is maintenance-only.
    assert "pip-compile" not in source


def test_refresh_does_not_require_host_python_or_pip_tools(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    root = Path(__file__).parents[1]
    monkeypatch.setattr(refresh.platform, "system", lambda: "Linux")
    monkeypatch.setattr(refresh.shutil, "which", lambda name: "docker")
    monkeypatch.setattr(refresh, "role_source_paths", lambda checkout, roles=refresh.LOCK_ROLES: {role: (checkout / "pyproject.toml",) for role in refresh.LOCK_ROLES})
    request = refresh.RefreshRequest(root, tmp_path / "evidence", "https://pypi.org/simple")
    refresh._validate_request(request)


@pytest.mark.parametrize("index_url", ["http://pypi.org/simple", "https://mirror.example/simple"])
def test_refresh_rejects_non_code_owned_general_index(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, index_url: str) -> None:
    monkeypatch.setattr(refresh.platform, "system", lambda: "Linux")
    request = refresh.RefreshRequest(Path(__file__).parents[1], tmp_path / "evidence", index_url)
    with pytest.raises(refresh.RefreshError, match="code-owned general index"):
        refresh._validate_request(request)


def test_refresh_cache_paths_are_task_owned_without_ambient_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HOME", raising=False)
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    environment = refresh._clean_environment(tmp_path)
    assert environment["HOME"] == str(tmp_path / "home")
    assert environment["XDG_CACHE_HOME"] == str(tmp_path / "cache")
    assert environment["PIP_CACHE_DIR"] == str(tmp_path / "pip-cache")
    assert Path(environment["HOME"]).is_absolute()


def test_fake_refresh_validates_all_outputs_before_atomic_publish(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    root = tmp_path / "checkout"
    containers = root / "containers"
    containers.mkdir(parents=True)
    (containers / "resolver-tooling.lock").write_text("\n".join(refresh._RESOLVER_TOOLING_LOCK) + "\n", encoding="utf-8")
    for role in refresh.LOCK_ROLES:
        source = root / f"{role}.in"
        source.write_text("demo==1.0\n", encoding="utf-8")
    monkeypatch.setattr(refresh, "_validate_request", lambda request: None)
    monkeypatch.setattr(refresh, "role_source_paths", lambda checkout, roles=refresh.LOCK_ROLES: {role: (checkout / f"{role}.in",) for role in refresh.LOCK_ROLES})
    monkeypatch.setattr(refresh, "_role_input_text", lambda root, role, paths: "demo==1.0\n")
    monkeypatch.setattr(refresh, "_resolver_specs", lambda checkout, roles=refresh.LOCK_ROLES: {role: refresh.ResolverSpec("python:3.13@sha256:" + "a" * 64, "/usr/local/bin/python", ("pypi",)) for role in refresh.LOCK_ROLES})
    monkeypatch.setattr(refresh, "_ocr_vendor_wheels", lambda path: {"triton-rocm": {"sha256": "a" * 64}})
    monkeypatch.setattr(refresh, "_download_and_validate_ocr_wheels", lambda *args, **kwargs: {"triton-rocm": {"sha256": "a" * 64}})
    identity = refresh.SourceIdentity("c" * 40, "d" * 40, "test", "https://example.test/repo")
    monkeypatch.setattr(refresh, "source_identity", lambda root: identity)
    monkeypatch.setattr(refresh, "load_dependency_locks", lambda root, roles=refresh.LOCK_ROLES: None)
    # The fake checkout carries no NVIDIA runtime lock; its cross-check is covered by test_dependency_locks.py.
    monkeypatch.setattr(refresh, "validate_nvidia_ocr_lock", lambda declaration, lock: {})

    calls: list[tuple[str, ...]] = []

    def fake_runner(command, **kwargs):
        del kwargs
        command = tuple(map(str, command))
        calls.append(command)
        if "--version" in command or command[1:3] in (("build", "--pull=false"), ("image", "rm")):
            return type("Completed", (), {"returncode": 0, "stdout": "pip-compile 7.0", "stderr": ""})()
        if "/work/ocr-vendor" in command:
            return type("Completed", (), {"returncode": 0, "stdout": "", "stderr": ""})()
        role = next(role for role in refresh.LOCK_ROLES if f"/work/{role}.lock" in command)
        output = tmp_path / "scratch" / f"{role}.lock"
        output.parent.mkdir(parents=True, exist_ok=True)
        if role == "embedding":
            output.write_text(
                "fastembed==0.8.0 --hash=sha256:" + "a" * 64 + "\n"
                "onnxruntime-migraphx==1.23.1 --hash=sha256:" + "a" * 64 + "\n",
                encoding="utf-8",
            )
        elif role == "embedding-nvidia":
            # pip-compile emits FastEmbed's CPU onnxruntime beside onnxruntime-gpu; the refresh must drop it.
            output.write_text(
                "fastembed==0.8.0 --hash=sha256:" + "a" * 64 + "\n"
                "onnxruntime==1.24.0 --hash=sha256:" + "b" * 64 + "\n"
                "onnxruntime-gpu==1.30.0 --hash=sha256:" + "a" * 64 + "\n",
                encoding="utf-8",
            )
        elif role == "ocr-nvidia":
            # pip-compile emits torch's requirement on triton; the refresh must drop it.
            output.write_text(
                "easyocr==1.7.2 --hash=sha256:" + "a" * 64 + "\n"
                "torch==2.14.0 --hash=sha256:" + "a" * 64 + "\n"
                "torchvision==0.29.0 --hash=sha256:" + "a" * 64 + "\n"
                "triton==3.8.0 --hash=sha256:" + "c" * 64 + "\n",
                encoding="utf-8",
            )
        else:
            output.write_text("demo==1.0 --hash=sha256:" + "a" * 64 + "\n", encoding="utf-8")
        return type("Completed", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(refresh.tempfile, "mkdtemp", lambda **kwargs: str(scratch))
    request = refresh.RefreshRequest(root, tmp_path / "evidence", "https://pypi.org/simple")
    result = refresh.refresh(request, runner=fake_runner)
    # Three docker calls per role (resolver image build, its version probe, the pip-compile run) plus the final image removal.
    assert len(calls) == 3 * len(refresh.LOCK_ROLES) + 1
    run_calls = [command for command in calls if len(command) > 1 and command[1] == "run"]
    assert run_calls
    assert all(
        "HOME=/work/home" in command
        and "XDG_CACHE_HOME=/work/cache" in command
        and "PIP_CACHE_DIR=/work/pip-cache" in command
        for command in run_calls
    )
    ocr_calls = [command for command in run_calls if "/work/ocr.lock" in command]
    assert ocr_calls and "--extra-index-url" not in ocr_calls[-1] and "--allow-unsafe" in ocr_calls[-1]
    assert all(path.is_file() for path in result.locks.values())
    assert (request.evidence_root / "dependency-refresh-evidence.json").is_file()
    # 15.0.0: the NVIDIA roles ran on PyPI alone, the CPU onnxruntime and triton never reach their locks,
    # and their maintenance evidence is published beside them in the checkout.
    nvidia_calls = [command for command in run_calls if "/work/ocr-nvidia.lock" in command or "/work/embedding-nvidia.lock" in command]
    assert len(nvidia_calls) == 2 and all("--extra-index-url" not in command for command in nvidia_calls)
    assert "--allow-unsafe" in next(command for command in nvidia_calls if "/work/ocr-nvidia.lock" in command)
    embedding_lock = result.locks["embedding-nvidia"].read_text(encoding="utf-8")
    ocr_lock = result.locks["ocr-nvidia"].read_text(encoding="utf-8")
    assert "onnxruntime-gpu==1.30.0" in embedding_lock and "\nonnxruntime==" not in "\n" + embedding_lock
    assert "torch==2.14.0" in ocr_lock and "triton" not in ocr_lock
    published = json.loads((containers / "nvidia-refresh-evidence.json").read_text(encoding="utf-8"))
    # A default run (all eight roles, no ocr-cpu) writes the NVIDIA file alone, holding only its own roles.
    assert published["selected_roles"] == ["embedding-nvidia", "ocr-nvidia"]
    assert set(published["roles"]) == {"embedding-nvidia", "ocr-nvidia"} and set(published["interpreters"]) == set(published["roles"])
    assert not (containers / "ocr-cpu-refresh-evidence.json").exists()


def test_nvidia_roles_resolve_on_the_cpu_base_from_pypi_alone() -> None:
    root = Path(__file__).parents[1]
    specs = refresh._resolver_specs(root, ("embedding-nvidia", "ocr-nvidia", "ocr-cpu"))
    for role in ("embedding-nvidia", "ocr-nvidia"):
        assert specs[role] == specs["ocr-cpu"]
        assert specs[role].python == "/usr/local/bin/python" and specs[role].expected_python == "3.13"
        assert specs[role].indexes == ("pypi",) and "python:3.13-slim-bookworm@sha256:" in specs[role].image
    # Sources are the NVIDIA Dockerfile and runtime lock, never the AMD ones.
    for role in ("embedding-nvidia", "ocr-nvidia"):
        assert all("cognita-amd" not in path for path in refresh.ROLE_SOURCE_DECLARATIONS[role])
        assert "containers/cognita-nvidia/runtime-lock.json" in refresh.ROLE_SOURCE_DECLARATIONS[role]


def test_nvidia_role_inputs_come_from_the_runtime_lock_without_vendor_urls_or_triton() -> None:
    root = Path(__file__).parents[1]
    paths = refresh.role_source_paths(root, ("embedding-nvidia", "ocr-nvidia"))
    embedding = refresh._role_input_text(root, "embedding-nvidia", paths["embedding-nvidia"])
    ocr = refresh._role_input_text(root, "ocr-nvidia", paths["ocr-nvidia"])
    assert "onnxruntime-gpu[cuda,cudnn]==1.30.0" in embedding.splitlines()
    assert "fastembed==0.8.0" in embedding.splitlines()
    assert "onnxruntime-migraphx" not in embedding and "@" not in embedding
    for pin in ("easyocr==1.7.2", "torch==2.14.0", "torchvision==0.29.0"):
        assert pin in ocr.splitlines()
    assert "triton" not in ocr and "@" not in ocr and "rocm" not in ocr and "--extra-index-url" not in ocr


def test_embedding_and_triton_omissions_apply_only_to_their_own_nvidia_roles() -> None:
    embedding = "fastembed==0.8.0 --hash=sha256:" + "a" * 64 + "\n" + "onnxruntime==1.30.0 --hash=sha256:" + "b" * 64 + "\n"
    assert "onnxruntime==" not in refresh._omit_fastembed_cpu_onnxruntime(embedding, role="embedding-nvidia")
    ocr = "torch==2.14.0 --hash=sha256:" + "a" * 64 + "\n" + "triton==3.8.0 --hash=sha256:" + "b" * 64 + "\n" + "triton-x==1 --hash=sha256:" + "c" * 64 + "\n"
    stripped = refresh._omit_torch_triton(ocr, role="ocr-nvidia")
    assert "\ntriton==" not in "\n" + stripped and "torch==2.14.0" in stripped and "triton-x==1" in stripped
    assert refresh._omit_torch_triton(ocr, role="ocr") == ocr and refresh._omit_torch_triton(ocr, role="ocr-cpu") == ocr


def test_nvidia_refresh_refuses_an_unusable_runtime_lock(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import shutil
    root = Path(__file__).parents[1]
    for role in ("embedding-nvidia", "ocr-nvidia"):
        for name in refresh.ROLE_SOURCE_DECLARATIONS[role]:
            target = tmp_path / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(root / name, target)
    (tmp_path / "containers/cognita").mkdir(parents=True, exist_ok=True)
    shutil.copyfile(root / "containers/resolver-tooling.lock", tmp_path / "containers/resolver-tooling.lock")
    shutil.copyfile(root / "containers/cognita/Dockerfile", tmp_path / "containers/cognita/Dockerfile")
    monkeypatch.setattr(refresh.platform, "system", lambda: "Linux")
    monkeypatch.setattr(refresh.shutil, "which", lambda name: "docker")
    request = refresh.RefreshRequest(tmp_path, tmp_path.parent / "evidence-nvidia", "https://pypi.org/simple", roles=("embedding-nvidia", "ocr-nvidia"))
    refresh._validate_request(request)
    lock = tmp_path / "containers/cognita-nvidia/runtime-lock.json"
    value = json.loads(lock.read_text(encoding="utf-8"))
    value["ocr"]["triton"] = "3.8.0"
    lock.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(refresh.RefreshError, match="not usable as a refresh source"):
        refresh._validate_request(request)


# --- 15.0.0: each committed evidence file records only its own roles -----------------------------------

_CPU_FILE = "ocr-cpu-refresh-evidence.json"
_NVIDIA_FILE = "nvidia-refresh-evidence.json"


def test_evidence_files_map_roles_explicitly() -> None:
    assert refresh.EVIDENCE_FILES == {_CPU_FILE: ("ocr-cpu",), _NVIDIA_FILE: ("embedding-nvidia", "ocr-nvidia")}
    assert refresh.evidence_files_for(("ocr-cpu",)) == (_CPU_FILE,)
    assert refresh.evidence_files_for(("ocr-nvidia",)) == (_NVIDIA_FILE,)
    assert refresh.evidence_files_for(("embedding-nvidia", "ocr-nvidia")) == (_NVIDIA_FILE,)
    assert refresh.evidence_files_for(("service", "build")) == ()
    # The default run (no --roles) has no ocr-cpu, so it feeds the NVIDIA file alone and keeps working.
    assert refresh.evidence_files_for(refresh.LOCK_ROLES) == (_NVIDIA_FILE,)


@pytest.mark.parametrize("roles", [("ocr-cpu", "ocr-nvidia"), ("embedding-nvidia", "ocr-cpu", "ocr-nvidia"), ("service", "ocr-cpu", "embedding-nvidia")])
def test_a_run_that_would_write_both_evidence_files_is_refused_before_any_work(tmp_path, monkeypatch, roles) -> None:
    calls = []
    def runner(command, **kwargs):
        calls.append(command)
        raise AssertionError("no docker command may run for a refused request")
    message = "run them separately: --roles ocr-cpu, then --roles embedding-nvidia,ocr-nvidia"
    with pytest.raises(refresh.RefreshError, match=message):
        refresh._validate_request(refresh.RefreshRequest(tmp_path, tmp_path / "evidence", "https://pypi.org/simple", roles=roles))
    with pytest.raises(refresh.RefreshError, match=message):
        refresh.refresh(refresh.RefreshRequest(tmp_path, tmp_path / "evidence", "https://pypi.org/simple", roles=roles), runner=runner)
    assert calls == [] and not (tmp_path / "evidence").exists()


def _one_evidence_run(tmp_path, monkeypatch, roles):
    """Drive a real refresh() with a fake docker whose pip-compile 'resolves' to the committed lock."""
    import shutil
    import subprocess
    source_root = Path(__file__).parents[1]
    root = tmp_path / "checkout"
    names = {"containers/resolver-tooling.lock", "containers/cognita/Dockerfile"}
    for role in roles:
        names.update(refresh.ROLE_SOURCE_DECLARATIONS[role])
        names.add(f"containers/{refresh.LOCK_FILES[role]}")
    for name in names:
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source_root / name, root / name)
    # Pre-existing evidence of the OTHER file must survive untouched.
    other = root / "containers" / (_NVIDIA_FILE if roles == ("ocr-cpu",) else _CPU_FILE)
    other.write_bytes(b"other evidence bytes")
    monkeypatch.setattr(refresh.platform, "system", lambda: "Linux")
    monkeypatch.setattr(refresh.shutil, "which", lambda name: "docker")
    identity = refresh.SourceIdentity("a" * 40, "b" * 40, "test", "https://example.test/repo")
    monkeypatch.setattr(refresh, "source_identity", lambda checkout: identity)
    monkeypatch.setattr(refresh, "_download_and_validate_ocr_wheels", lambda *args, **kwargs: {})

    def runner(command, **kwargs):
        for role in roles:
            if f"/work/{role}.lock" in command:
                mount = next(value for value in command if value.startswith("type=bind,src=") and value.endswith(",dst=/work"))
                scratch = Path(mount.removeprefix("type=bind,src=").removesuffix(",dst=/work"))
                shutil.copyfile(source_root / "containers" / refresh.LOCK_FILES[role], scratch / f"{role}.lock")
        return subprocess.CompletedProcess(command, 0, "pip-compile test", "")

    request = refresh.RefreshRequest(root, tmp_path / "evidence", "https://pypi.org/simple", roles=roles)
    refresh.refresh(request, runner=runner)
    return root, other


def _evidence(root: Path, name: str) -> dict:
    return json.loads((root / "containers" / name).read_text(encoding="utf-8"))


def _consumed_relative(root: Path, evidence: dict) -> set:
    """Inputs as checkout-relative paths. The two maintenance scripts are read from where the tool runs (the
    repository under test here, the clone's own scripts/ in a real run), so they are named by file."""
    result = set()
    for path in evidence["consumed_source_sha256"]:
        try:
            result.add(Path(path).relative_to(root.resolve()).as_posix())
        except ValueError:
            result.add("scripts/" + Path(path).name)
    return result


def test_a_cpu_only_run_writes_only_the_cpu_file_with_only_cpu_inputs(tmp_path, monkeypatch) -> None:
    root, other = _one_evidence_run(tmp_path, monkeypatch, ("ocr-cpu",))
    assert other.name == _NVIDIA_FILE and other.read_bytes() == b"other evidence bytes"
    evidence = _evidence(root, _CPU_FILE)
    assert evidence["selected_roles"] == ["ocr-cpu"] and set(evidence["roles"]) == {"ocr-cpu"} and set(evidence["interpreters"]) == {"ocr-cpu"}
    assert set(evidence["tool"]["versions"]) == {"ocr-cpu"}
    # The same input set the file carried before this feature: the CPU Dockerfile and lock, the qualification
    # source, the resolver tooling and the two maintenance scripts -- and nothing of the NVIDIA image.
    assert _consumed_relative(root, evidence) == {
        "containers/cognita/Dockerfile", "containers/cognita/runtime-lock.json", "containers/resolver-tooling.lock",
        "docs/easyocr-qualification-dependencies.json", "scripts/dependency_locks.py", "scripts/refresh_dependency_locks.py",
    }
    assert "nvidia" not in json.dumps(evidence["roles"]).lower()


def test_an_nvidia_only_run_writes_only_the_nvidia_file_with_only_nvidia_inputs(tmp_path, monkeypatch) -> None:
    root, other = _one_evidence_run(tmp_path, monkeypatch, ("embedding-nvidia", "ocr-nvidia"))
    assert other.name == _CPU_FILE and other.read_bytes() == b"other evidence bytes"
    evidence = _evidence(root, _NVIDIA_FILE)
    assert evidence["selected_roles"] == ["embedding-nvidia", "ocr-nvidia"]
    assert set(evidence["roles"]) == {"embedding-nvidia", "ocr-nvidia"} and set(evidence["interpreters"]) == set(evidence["roles"])
    consumed = _consumed_relative(root, evidence)
    assert {"containers/cognita-nvidia/Dockerfile", "containers/cognita-nvidia/runtime-lock.json"} <= consumed
    # Inputs only the CPU image has are not pinned here; the shared resolver files are.
    assert "containers/cognita/runtime-lock.json" not in consumed and "containers/cognita/Dockerfile" not in consumed
    assert {"containers/resolver-tooling.lock", "scripts/dependency_locks.py", "scripts/refresh_dependency_locks.py"} <= consumed
