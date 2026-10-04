"""Static release checks for the reproducible AMD image inputs."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path


ROOT = Path(__file__).parents[1]


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _sha256_text(path: Path) -> str:
    """Hash the canonical LF build-context bytes for a text artifact."""
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def test_amd_base_and_models_are_digest_qualified() -> None:
    dockerfile = (ROOT / "containers" / "cognita-amd" / "Dockerfile").read_text(encoding="utf-8")
    image_manifest = json.loads((ROOT / "containers" / "image-manifest.json").read_text(encoding="utf-8"))
    qualification = json.loads(
        (ROOT / "docs" / "easyocr-qualification-dependencies.json").read_text(encoding="utf-8")
    )

    base = "rocm/dev-ubuntu-24.04:7.1-complete@sha256:5d39ae42487f8f66a4eaa50fdcb82bf57f6a667faf0bb71b5efcd123d3dc3107"
    python_runtime = "python:3.13-slim-bookworm@sha256:ed86c82274b3c69b52fb5820f358f0bd7df0b603332063cb5c6e32bd220c3e6e"
    assert f"ARG PYTHON_BASE_IMAGE={base}" in dockerfile
    assert f"ARG PYTHON_RUNTIME_IMAGE={python_runtime}" in dockerfile
    assert "FROM ${PYTHON_RUNTIME_IMAGE} AS python-runtime" in dockerfile
    assert "COPY --from=python-runtime /usr/local /usr/local" in dockerfile
    # (Superseded: this used to compare the manifest's `release` field and the
    # `cognita_amd` `tag` against __version__.  13.0 §8 removed both fields:
    # release_identity.py is the only version authority, and the manifest now
    # records build inputs only.)
    # 13.0 §4: the image takes its version from the build arg, which
    # compose.yaml and scripts/release.py pass from the one authority.  A
    # default here would be a second place a stale version could hide, so the
    # ARG is declared bare and the label interpolates it.
    assert re.search(r"^ARG COGNITA_VERSION$", dockerfile, re.MULTILINE)
    assert 'org.opencontainers.image.version="${COGNITA_VERSION}"' in dockerfile
    assert image_manifest["built"]["cognita_amd"]["base_reference"] == base
    assert image_manifest["built"]["cognita_amd"]["python_runtime_reference"] == python_runtime

    # 14.2.0 (DESIGN-LINUX-INSTALLER 6.5): the weights are not in the tree or the image; the
    # qualification manifest is the one authority for their hashes and its aggregate must be the hash
    # of the per-file hashes in name order (what the worker computes over the downloaded directory).
    # (Superseded: this test used to hash containers/cognita-amd/models/*.pth.)
    assert "containers/cognita-amd/models" not in dockerfile and "*.pth" not in dockerfile
    assert "COPY docs/easyocr-qualification-dependencies.json /opt/cognita-models/easyocr-qualification.json" in dockerfile
    assert "COGNITA_OCR_MODEL_DIR=/var/lib/cognita/models/easyocr" in dockerfile
    model_files = qualification["model_files"]
    hashes = [model_files[name] for name in ("craft_mlt_25k.pth", "english_g2.pth")]
    assert hashlib.sha256("".join(hashes).encode()).hexdigest() == model_files["aggregate_sha256"]


def test_amd_service_install_uses_pinned_python_runtime() -> None:
    dockerfile = (ROOT / "containers" / "cognita-amd" / "Dockerfile").read_text(encoding="utf-8")
    lock = json.loads((ROOT / "containers" / "cognita-amd" / "runtime-lock.json").read_text(encoding="utf-8"))
    image_manifest = json.loads((ROOT / "containers" / "image-manifest.json").read_text(encoding="utf-8"))
    assert "/usr/local/bin/python -m pip install --no-cache-dir ." in dockerfile
    assert "/usr/bin/python3 -m venv /opt/cognita-runtimes/embed" in dockerfile
    assert "/usr/local/bin/python -m venv /opt/cognita-runtimes/ocr" in dockerfile
    assert "python3-venv" in dockerfile
    assert lock["embedding"]["python_abi"] == "cp312"
    assert lock["embedding"]["onnxruntime-migraphx"] == "1.23.1"
    wheel = lock["embedding"]["provider_wheel"]
    assert wheel == image_manifest["built"]["cognita_amd"]["embedding_provider_wheel"]
    assert image_manifest["built"]["cognita_amd"]["embedding_python"] == "rocm-base:/usr/bin/python3 (cp312)"
    assert f'{wheel["origin"]}{wheel["filename"]}#sha256={wheel["sha256"]}' in dockerfile
    migraphx_deb = lock["embedding"]["migraphx_deb"]
    assert migraphx_deb == image_manifest["built"]["cognita_amd"]["embedding_migraphx_deb"]
    assert f'"migraphx={migraphx_deb["version"]}"' in dockerfile
    assert f"SHA256: {migraphx_deb['sha256']}" in dockerfile
    assert "'not found' not in p.stdout" in dockerfile


def test_amd_expensive_layers_precede_source_and_version_layers() -> None:
    dockerfile = (ROOT / "containers" / "cognita-amd" / "Dockerfile").read_text(encoding="utf-8")
    source_copy = dockerfile.index("COPY --chown=cognita:cognita pyproject.toml README.md ./")
    service_install = dockerfile.index(
        "RUN /usr/local/bin/python -m pip install --no-cache-dir --index-url https://pypi.org/simple "
        "--require-hashes --no-deps -r /opt/cognita-locks/build-requirements.lock"
    )
    embedding_install = dockerfile.index("RUN /usr/bin/python3 -m venv /opt/cognita-runtimes/embed")
    ocr_install = dockerfile.index("RUN /usr/local/bin/python -m venv /opt/cognita-runtimes/ocr")
    model_copy = dockerfile.index("COPY docs/easyocr-qualification-dependencies.json /opt/cognita-models/easyocr-qualification.json")
    ownership_setup = dockerfile.index("RUN useradd --create-home --uid 10001")
    version_label = dockerfile.index('LABEL org.opencontainers.image.title="Cognita Knowledge gateway (AMD)"')

    assert ownership_setup < embedding_install
    assert embedding_install < source_copy
    assert ocr_install < source_copy
    assert model_copy < source_copy
    assert source_copy < service_install < version_label
    assert "chown -R cognita:cognita /app /opt/cognita-runtimes" not in dockerfile


def test_amd_build_cache_maintenance_is_bounded_and_operator_gated() -> None:
    script = (ROOT / "scripts" / "maintain-amd-build-cache.sh").read_text(encoding="utf-8")
    assert 'COGNITA_BUILD_CACHE_MAX_USED_SPACE:-100GB' in script
    assert 'COGNITA_CONFIRM_BUILD_CACHE_PRUNE:-}' in script
    assert '!= "YES"' in script
    assert 'buildx prune' in script
    assert '--max-used-space "$max_used_space"' in script
    assert 'buildx du --builder "$builder"' in script


def test_beta_icon_is_present_without_replacing_canonical_blue_icon() -> None:
    web = ROOT / "src" / "cognita" / "web"
    assert (web / "cognita-icon-plugin-beta.png").is_file()
    assert _sha256(web / "cognita-icon-512.png") == (
        "ad708b90b8b533ac3cd0593983a07d84b46d573da41d3351546ab14cbeeaf0ff"
    )


# 13.0 §8: `test_amd_preflight_uses_the_isolated_embedding_runtime` is gone
# with `scripts/preflight-amd.sh`, which ran the canary below through the
# image's isolated embed and OCR interpreters before systemd started the
# stack.  The canary source itself is still shipped in the AMD image and is
# still covered, two tests down; the live provider proof on the target is now
# `deploy`'s `/healthz` check for `embed.gpu: ready` (§6.2 step 6), which runs
# against the image that is actually about to serve.


def test_amd_overlay_keeps_gpu_devices_off_workspace_runtime() -> None:
    # Moved here from the deleted tests/test_release_image_integrity.py, whose
    # other subjects (the evidence scripts, the per-profile unit files and the
    # manifest's pending status) 13.0 §8 removed.
    base = (ROOT / "compose.yaml").read_text(encoding="utf-8")
    overlay = (ROOT / "compose.amd.yaml").read_text(encoding="utf-8")
    assert "COGNITA_ACCELERATION_PROFILE: amd" in overlay
    assert "/dev/kfd:/dev/kfd" in overlay and "/dev/dri:/dev/dri" in overlay
    assert "workspace-runtime" not in overlay
    assert "COGNITA_GPU_ENABLED" not in base + overlay
    assert "COGNITA_OCR_DEVICE" not in base + overlay


def test_amd_runtime_lock_covers_source_and_model_hashes() -> None:
    lock = json.loads((ROOT / "containers" / "cognita-amd" / "runtime-lock.json").read_text(encoding="utf-8"))
    artifacts = lock["artifacts"]
    assert artifacts["qualification_manifest"]["sha256"] == _sha256_text(ROOT / "docs" / "easyocr-qualification-dependencies.json")
    # 14.2.0: the model bindings are hashes only, checked against the qualification manifest, and the
    # AMD worker finds the downloaded files in the model cache.
    authority = json.loads((ROOT / "docs" / "easyocr-qualification-dependencies.json").read_text(encoding="utf-8"))["model_files"]
    assert set(artifacts["ocr_models"]) == {"craft_mlt_25k.pth", "english_g2.pth"}
    for name, item in artifacts["ocr_models"].items():
        assert item == {"sha256": authority[name]}
    assert lock["runtime_paths"]["ocr_models"] == "/var/lib/cognita/models/easyocr"
    assert artifacts["preflight_source"]["sha256"] == _sha256_text(ROOT / "scripts" / "amd_runtime_preflight.py")
    assert artifacts["dockerfile_source"]["sha256"] == _sha256_text(ROOT / "containers" / "cognita-amd" / "Dockerfile")


def test_amd_text_hash_contract_is_crlf_stable() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as root:
        path = Path(root) / "artifact.txt"
        path.write_bytes(b"alpha\r\nbeta\r\n")
        crlf = _sha256_text(path)
        path.write_bytes(b"alpha\nbeta\n")
        assert _sha256_text(path) == crlf


def test_amd_preflight_performs_live_session_canaries_and_cleanup() -> None:
    source = (ROOT / "scripts" / "amd_runtime_preflight.py").read_text(encoding="utf-8")
    assert "MIGraphXExecutionProvider" in source
    assert "TextEmbedding" in source and "CPUExecutionProvider" in source
    assert "OCRWorkerRunner" in source and "asyncio.run" in source
    assert "python=sys.executable" in source
    assert "gc.collect()" in source
    assert "verification_timeout" in source
    assert '"migraphx_model_cache_dir": program_cache_dir' in source
    assert 'PROGRAM_CACHE = MODEL_CACHE / "migraphx-cache"' in source


def test_amd_preflight_requires_the_ocr_weights_only_for_the_ocr_component() -> None:
    """14.2.0: the weights are in the model cache, so an embedding canary must not fail for want of them."""
    source = (ROOT / "scripts" / "amd_runtime_preflight.py").read_text(encoding="utf-8")
    embed = source[source.index("def _embed("):source.index("def _ocr(")]
    ocr = source[source.index("def _ocr("):source.index("def main(")]
    assert "_load_lock(models=False)" in embed
    assert "_load_lock()" in ocr


def test_amd_hash_inputs_have_platform_stable_git_attributes() -> None:
    attributes = (ROOT / ".gitattributes").read_text(encoding="utf-8")
    assert "scripts/amd_runtime_preflight.py text eol=lf" in attributes
    assert "containers/cognita-amd/runtime-lock.json text eol=lf" in attributes
    assert "containers/cognita-amd/Dockerfile text eol=lf" in attributes
    assert "docs/easyocr-qualification-dependencies.json text eol=lf" in attributes
    assert "containers/cognita-amd/models/*.pth" not in attributes
