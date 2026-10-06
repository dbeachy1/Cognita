# syntax=docker/dockerfile:1.7
# AMD Cognita image.  Unlike the CPU image, this image owns both isolated
# worker interpreters; the service interpreter remains CPU onnxruntime-only.
ARG PYTHON_BASE_IMAGE=rocm/dev-ubuntu-24.04:7.1-complete@sha256:5d39ae42487f8f66a4eaa50fdcb82bf57f6a667faf0bb71b5efcd123d3dc3107
ARG PYTHON_RUNTIME_IMAGE=python:3.13-slim-bookworm@sha256:ed86c82274b3c69b52fb5820f358f0bd7df0b603332063cb5c6e32bd220c3e6e
ARG EMBEDDING_PROVIDER_WHEEL_URL=https://repo.radeon.com/rocm/manylinux/rocm-rel-7.1/onnxruntime_migraphx-1.23.1-cp312-cp312-manylinux_2_27_x86_64.manylinux_2_28_x86_64.whl
ARG EMBEDDING_PROVIDER_WHEEL_SHA256=ccbbcf44b06e57d6b1f103843649a6f6c2d01ed7655133cf25cb441ccde60e35
# Source-owned artifact identity: https://repo.radeon.com/rocm/manylinux/rocm-rel-7.1/onnxruntime_migraphx-1.23.1-cp312-cp312-manylinux_2_27_x86_64.manylinux_2_28_x86_64.whl#sha256=ccbbcf44b06e57d6b1f103843649a6f6c2d01ed7655133cf25cb441ccde60e35
FROM ${PYTHON_RUNTIME_IMAGE} AS python-runtime

# Named `app` so that compose.yaml's `target: app` -- which the CPU image needs,
# because its last stage is the test stage -- is also valid here, where this is
# the only stage that could be meant.
FROM ${PYTHON_BASE_IMAGE} AS app

# The ROCm image is intentionally retained as the final base so its driver and
# accelerator libraries remain image-owned.  Copying the official Python
# runtime's /usr/local tree supplies the service interpreter without importing
# Debian's system packages or replacing any ROCm/Ubuntu libraries.
COPY --from=python-runtime /usr/local /usr/local

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    COGNITA_CONFIG_ROOT=/app/config \
    COGNITA_ACCELERATION_PROFILE=amd \
    COGNITA_EMBED_RUNTIME=/opt/cognita-runtimes/embed/bin/python \
    COGNITA_OCR_RUNTIME=/opt/cognita-runtimes/ocr/bin/python \
    COGNITA_OCR_MODEL_DIR=/var/lib/cognita/models/easyocr

WORKDIR /app

RUN apt-get update \
    && apt-cache show migraphx | grep -Fx 'SHA256: 40e5aeeb6009c2e08dd7c3b10642835c3ff1aa9e321222a971ee43ba98c1d307' \
    && DEBIAN_FRONTEND=noninteractive apt-get install --no-install-recommends -y \
       ca-certificates libpq5 python3-venv \
       "migraphx=2.14.0.70100-20~24.04" \
    && rm -rf /var/lib/apt/lists/*

RUN /usr/local/bin/python -c \
       "import sys; assert sys.version_info >= (3, 13), sys.version"

# Create the mutable runtime roots before installing the large AMD workers.
# The image-owned runtimes and model files under /opt remain root-owned and
# readable; only Cognita's config, model cache, and transfer roots need writes.
RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin cognita \
    && mkdir -p /app/config /var/lib/cognita/models /var/lib/cognita/transfers \
    && chown -R cognita:cognita /app /var/lib/cognita

# Pinned, image-owned embedding runtime. It is never mounted from the host or
# installed into the service environment. ROCm/MIGraphX libraries come from
# this digest-pinned base plus the exact hash-checked role lock below.
# AMD's ROCm 7.1 MIGraphX wheel is available for Python 3.12, not 3.13.  Use
# the pinned ROCm base's Ubuntu interpreter for this isolated worker.  FastEmbed
# pulls CPU onnxruntime; remove it before installing the hash-pinned ROCm wheel
# that owns the same import namespace.  The service remains on Python 3.13.
RUN /usr/bin/python3 -m venv /opt/cognita-runtimes/embed
COPY containers/embedding-requirements.lock /opt/cognita-locks/embedding-requirements.lock
RUN /opt/cognita-runtimes/embed/bin/pip install --no-cache-dir --index-url https://pypi.org/simple --require-hashes --no-deps -r /opt/cognita-locks/embedding-requirements.lock \
    && /opt/cognita-runtimes/embed/bin/python -c \
       "import subprocess; p=subprocess.run(['/opt/cognita-runtimes/embed/bin/python', '-m', 'pip', 'check'], capture_output=True, text=True); lines=[line for line in p.stdout.splitlines() if not (line.lower().startswith('fastembed ') and 'requires onnxruntime, which is not installed.' in line.lower())]; assert p.returncode == 0 or not lines, p.stdout" \
    && /opt/cognita-runtimes/embed/bin/python -c \
       "import onnxruntime as o; assert 'MIGraphXExecutionProvider' in o.get_available_providers()"

# Provider registration is not enough: the actual shared object must resolve
# against the image-owned ROCm 7.1 libraries before this image can be released.
RUN /usr/local/bin/python -c \
       "import subprocess; p=subprocess.run(['ldd', '/opt/cognita-runtimes/embed/lib/python3.12/site-packages/onnxruntime/capi/libonnxruntime_providers_migraphx.so'], capture_output=True, text=True, check=True); assert 'not found' not in p.stdout, p.stdout"

# Pinned OCR runtime. 14.2.0 (DESIGN-LINUX-INSTALLER 6.5, D5): the model weights are no
# longer in this image. They are downloaded into the model cache (mounted at
# /var/lib/cognita/models/easyocr) and the OCR worker hashes them against the qualification
# manifest at run time; this image never downloads them itself. (Superseded: the release
# build context used to supply the weight bytes, COPYed below after qualification.)
RUN /usr/local/bin/python -m venv /opt/cognita-runtimes/ocr
COPY containers/ocr-requirements.lock /opt/cognita-locks/ocr-requirements.lock
RUN /opt/cognita-runtimes/ocr/bin/pip install --no-cache-dir --index-url https://pypi.org/simple \
       --only-binary=torch,torchvision --require-hashes --no-deps \
       -r /opt/cognita-locks/ocr-requirements.lock

# /opt/cognita-models now holds only this manifest (the source authority, whose runtime
# pins are the ROCm torch build this image installs).
COPY docs/easyocr-qualification-dependencies.json /opt/cognita-models/easyocr-qualification.json
COPY containers/cognita-amd/runtime-lock.json /opt/cognita-runtimes/runtime-lock.json
COPY scripts/verify_ocr_runtime.py /opt/cognita-runtimes/verify_ocr_runtime.py
COPY scripts/amd_runtime_preflight.py /opt/cognita-runtimes/amd_runtime_preflight.py

# Keep source and release metadata after the expensive, immutable AMD runtime
# layers.  Ordinary source edits and version bumps can then reuse the ROCm,
# embedding, OCR, and model layers above.
RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install --no-install-recommends -y ffmpeg libmp3lame0 \
    && dpkg-query -W -f='${Package}=${Version}\n' ffmpeg libmp3lame0 \
    && ffmpeg -version | head -n 1 \
    && ffprobe -version | head -n 1 \
    && ffmpeg -hide_banner -encoders | grep -q libmp3lame \
    && rm -rf /var/lib/apt/lists/*
COPY --chown=cognita:cognita pyproject.toml README.md ./
# 14.0: the license and third-party notices travel with the image.
COPY --chown=cognita:cognita LICENSE THIRD_PARTY_NOTICES.md ./
COPY --chown=cognita:cognita src ./src
# The service environment intentionally receives CPU onnxruntime only.
COPY containers/build-requirements.lock containers/service-requirements.lock /opt/cognita-locks/
RUN /usr/local/bin/python -m pip install --no-cache-dir --index-url https://pypi.org/simple --require-hashes --no-deps -r /opt/cognita-locks/build-requirements.lock \
    && /usr/local/bin/python -m pip install --no-cache-dir --index-url https://pypi.org/simple --require-hashes --no-deps -r /opt/cognita-locks/service-requirements.lock \
    && /usr/local/bin/python -m pip install --no-cache-dir . --no-deps --no-build-isolation \
    && /usr/local/bin/python -m pip check \
    && rm -rf /root/.cache/pip

# 13.0 §4: no default. release_identity.APPLICATION_VERSION is the only
# authority for the version, and compose.yaml/scripts/release.py always pass
# it; a default here would be a second place a stale version could hide.
ARG COGNITA_VERSION
# 13.0: the commit is a label, so a running container can be asked what source
# it was built from without matching image IDs against a release directory.
ARG COGNITA_COMMIT
LABEL org.opencontainers.image.title="Cognita Knowledge gateway (AMD)" \
      org.opencontainers.image.version="${COGNITA_VERSION}" \
      org.opencontainers.image.revision="${COGNITA_COMMIT}" \
      org.opencontainers.image.source="https://github.com/dbeachy1/Cognita"

USER cognita
EXPOSE 8675 8676
STOPSIGNAL SIGTERM
ENTRYPOINT ["cognita"]
CMD ["serve"]
