# syntax=docker/dockerfile:1.7
# NVIDIA Cognita image (DESIGN-NVIDIA-ACCELERATION 5).  It is the CPU image plus two
# pip-installed, hash-locked, PyPI-only CUDA 13 runtimes: one interpreter (the base's
# Python 3.13) owns the service and both isolated worker venvs.  Never `FROM nvidia/cuda`
# (that would bring in NVIDIA's Deep Learning Container License) and never a `-devel`
# image: CUDA developer tools are not redistributable.  The driver (libcuda, libnvidia-ml)
# is not in this image; the NVIDIA Container Toolkit injects it from the host at run time.
#
# The base is the CPU image's, byte for byte.  The release manifest records the same
# digest; updating it is a reviewed dependency change made in all images together.
ARG PYTHON_BASE_IMAGE=python:3.13-slim-bookworm@sha256:ed86c82274b3c69b52fb5820f358f0bd7df0b603332063cb5c6e32bd220c3e6e
# Named `app` so that compose.yaml's `target: app` (which the CPU image needs, because
# its last stage is the test stage) is also valid here, where this is the only stage.
FROM ${PYTHON_BASE_IMAGE} AS app

# NVIDIA_DRIVER_CAPABILITIES makes the container toolkit inject libcuda.so.1 (compute) AND
# libnvidia-ml.so.1 (utility, what the card probe reads) whatever the host's default is.
# NVIDIA_VISIBLE_DEVICES is deliberately not set here: compose.nvidia.yaml's device
# reservation decides which cards the container sees.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    COGNITA_CONFIG_ROOT=/app/config \
    COGNITA_ACCELERATION_PROFILE=nvidia \
    COGNITA_EMBED_RUNTIME=/opt/cognita-runtimes/embed/bin/python \
    COGNITA_OCR_RUNTIME=/opt/cognita-runtimes/ocr/bin/python \
    COGNITA_OCR_MODEL_DIR=/var/lib/cognita/models/easyocr \
    NVIDIA_DRIVER_CAPABILITIES=compute,utility

WORKDIR /app

# The CPU image's package line, verbatim: no compiler, package cache, or host credential
# enters the image, and the python base already has `venv`.
RUN apt-get update \
    && apt-get install --no-install-recommends -y ca-certificates libpq5 libgomp1 \
    && rm -rf /var/lib/apt/lists/*

# Create the mutable runtime roots before installing the large runtimes.  The
# image-owned runtimes and manifests under /opt stay root-owned and readable; only
# Cognita's config, model cache, and transfer roots need writes.
RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin cognita \
    && mkdir -p /app/config /var/lib/cognita/models /var/lib/cognita/transfers \
    && chown -R cognita:cognita /app /var/lib/cognita

# Embedding runtime: FastEmbed on onnxruntime-gpu 1.30.0 (CUDA 13) plus the NVIDIA runtime
# wheels it needs (cudart, cuBLAS, cuFFT, cuRAND, NVRTC, nvJitLink, cuDNN 9), every one
# hash-pinned from PyPI.  FastEmbed declares the CPU `onnxruntime`, whose import
# namespace onnxruntime-gpu owns here, so it is omitted from the lock and `pip check`
# exempts exactly that one line (the same exception the AMD embed build makes).
COPY containers/embedding-nvidia-requirements.lock /opt/cognita-locks/embedding-nvidia-requirements.lock
RUN python -m venv /opt/cognita-runtimes/embed \
    && /opt/cognita-runtimes/embed/bin/python -m pip install --no-cache-dir --index-url https://pypi.org/simple --require-hashes --no-deps --only-binary=:all: -r /opt/cognita-locks/embedding-nvidia-requirements.lock \
    && /opt/cognita-runtimes/embed/bin/python -c \
       "import subprocess; p=subprocess.run(['/opt/cognita-runtimes/embed/bin/python', '-m', 'pip', 'check'], capture_output=True, text=True); lines=[line for line in p.stdout.splitlines() if not (line.lower().startswith('fastembed ') and 'requires onnxruntime, which is not installed.' in line.lower())]; print('pip check:', 'clean' if p.returncode == 0 else 'exempted=' + str(len(p.stdout.splitlines()) - len(lines)) + ' remaining=' + str(len(lines))); assert p.returncode == 0 or not lines, p.stdout" \
    && /opt/cognita-runtimes/embed/bin/python -c \
       "import onnxruntime as o; providers=o.get_available_providers(); print('providers:', providers); assert 'CUDAExecutionProvider' in providers"

# The library-path marker (DESIGN-NVIDIA-ACCELERATION 5.3): the colon-joined
# site-packages/nvidia/*/lib directories of the embed venv.  gpu_host.start() prepends it to
# the worker's LD_LIBRARY_PATH.  Without it CUDAExecutionProvider still reports "available"
# but the live session silently runs on the CPU, so the build refuses an empty marker.
RUN /opt/cognita-runtimes/embed/bin/python -c \
       "import glob, os, sysconfig; site=sysconfig.get_paths()['purelib']; dirs=sorted(glob.glob(os.path.join(site, 'nvidia', '*', 'lib'))); assert dirs, 'no nvidia lib directories in the embed venv'; open('/opt/cognita-runtimes/embed/.ld_library_path', 'w').write(':'.join(dirs)); print('ld_library_path marker: dirs=' + str(len(dirs)))"

# Provider registration is not enough: the actual shared object must resolve against the
# image-owned CUDA libraries, found the way the worker finds them (the marker's path).  Only
# the two driver-side sonames may be unresolved at build time; the toolkit injects them at
# run time.  There is no card on the build host, so this is the static half of the proof
# and the live canary is Admin verification on real hardware.
RUN /opt/cognita-runtimes/embed/bin/python -c \
       "import os, subprocess; env=dict(os.environ, LD_LIBRARY_PATH=open('/opt/cognita-runtimes/embed/.ld_library_path').read().strip()); so='/opt/cognita-runtimes/embed/lib/python3.13/site-packages/onnxruntime/capi/libonnxruntime_providers_cuda.so'; p=subprocess.run(['ldd', so], capture_output=True, text=True, check=True, env=env); missing=[line.split()[0] for line in p.stdout.splitlines() if 'not found' in line]; unexpected=[name for name in missing if name not in ('libcuda.so.1', 'libnvidia-ml.so.1')]; print('ldd unresolved (driver side, expected):', missing); assert not unexpected, unexpected"

# OCR runtime: EasyOCR on the PyPI CUDA 13 build of torch 2.14.0 / torchvision 0.29.0, every
# wheel hash-pinned.  triton is deliberately NOT in the lock: its wheel bundles ptxas,
# cuobjdump and nvdisasm (CUDA developer binaries the EULA does not list as redistributable)
# and EasyOCR inference never compiles a kernel.  `pip check` therefore exempts exactly one
# line, "torch ... requires triton, which is not installed".  torch finds its nvidia/*/lib
# siblings through its own RPATH, so this venv needs no marker.
COPY containers/ocr-nvidia-requirements.lock /opt/cognita-locks/ocr-nvidia-requirements.lock
RUN python -m venv /opt/cognita-runtimes/ocr \
    && /opt/cognita-runtimes/ocr/bin/python -m pip install --no-cache-dir --index-url https://pypi.org/simple --require-hashes --no-deps --only-binary=:all: -r /opt/cognita-locks/ocr-nvidia-requirements.lock \
    && /opt/cognita-runtimes/ocr/bin/python -c \
       "import subprocess; p=subprocess.run(['/opt/cognita-runtimes/ocr/bin/python', '-m', 'pip', 'check'], capture_output=True, text=True); lines=[line for line in p.stdout.splitlines() if not (line.lower().startswith('torch ') and 'requires triton, which is not installed.' in line.lower())]; print('pip check:', 'clean' if p.returncode == 0 else 'exempted=' + str(len(p.stdout.splitlines()) - len(lines)) + ' remaining=' + str(len(lines))); assert p.returncode == 0 or not lines, p.stdout"

# The OCR qualification manifest is generated here from the runtime just installed, the way
# the CPU image does it (never the AMD manifest: its ROCm torch pins would fail this
# runtime's check).  Its model_files and aggregate come from the source authority without
# reading any model file: the weights are downloaded into the model cache
# (/var/lib/cognita/models/easyocr) and the worker hashes them at run time.  The manifest
# records this image's own torch string ("2.14.0+cu130"), which the worker compares as is.
COPY containers/cognita-nvidia/runtime-lock.json /opt/cognita-runtimes/runtime-lock.json
COPY docs/easyocr-qualification-dependencies.json /opt/cognita-locks/easyocr-qualification-dependencies.json
COPY scripts/verify_ocr_runtime.py scripts/dependency_locks.py scripts/nvidia_runtime_preflight.py /opt/cognita-runtimes/
RUN mkdir -p /opt/cognita-models \
    && python /opt/cognita-runtimes/verify_ocr_runtime.py --generate-manifest \
       --python /opt/cognita-runtimes/ocr/bin/python \
       --manifest /opt/cognita-models/easyocr-qualification.json \
       --runtime-lock /opt/cognita-runtimes/runtime-lock.json \
       --requirements-lock /opt/cognita-locks/ocr-nvidia-requirements.lock \
       --model-source-manifest /opt/cognita-locks/easyocr-qualification-dependencies.json

# The service interpreter gets the same hash-locked build and service locks as the CPU image
# (CPU onnxruntime only: it never touches a card).  Resolve nothing at build time.
# Application source and release ARGs stay below the expensive runtime layers above: even
# an ARG's implicit RUN environment can invalidate the cache before its first interpolation.
RUN apt-get update \
    && apt-get install --no-install-recommends -y ffmpeg libmp3lame0 \
    && dpkg-query -W -f='${Package}=${Version}\n' ffmpeg libmp3lame0 \
    && ffmpeg -version | head -n 1 \
    && ffprobe -version | head -n 1 \
    && ffmpeg -hide_banner -encoders | grep -q libmp3lame \
    && rm -rf /var/lib/apt/lists/*
COPY containers/build-requirements.lock containers/service-requirements.lock /opt/cognita-locks/
RUN python -m pip install --no-cache-dir --index-url https://pypi.org/simple --require-hashes --no-deps -r /opt/cognita-locks/build-requirements.lock \
    && python -m pip install --no-cache-dir --index-url https://pypi.org/simple --require-hashes --no-deps -r /opt/cognita-locks/service-requirements.lock \
    && python -m pip check \
    && rm -rf /root/.cache/pip

COPY pyproject.toml README.md ./
# 14.0: the license and third-party notices travel with the image.  The NVIDIA wheels carry
# their own LICENSE files and .dist-info directories, and nothing here deletes them.
COPY LICENSE THIRD_PARTY_NOTICES.md ./
COPY src ./src
RUN python -m pip install --no-cache-dir . --no-deps --no-build-isolation \
    && python -m pip check \
    && rm -rf /root/.cache/pip

# 13.0 §4: no default. release_identity.APPLICATION_VERSION is the only
# authority for the version, and compose.yaml/scripts/release.py always pass
# it; a default here would be a second place a stale version could hide.
ARG COGNITA_VERSION
# 13.0: the commit is a label, so a running container can be asked what source
# it was built from without matching image IDs against a release directory.
ARG COGNITA_COMMIT
LABEL org.opencontainers.image.title="Cognita Knowledge gateway (NVIDIA)" \
      org.opencontainers.image.version="${COGNITA_VERSION}" \
      org.opencontainers.image.revision="${COGNITA_COMMIT}" \
      org.opencontainers.image.source="https://github.com/dbeachy1/Cognita"

USER cognita
EXPOSE 8675 8676
STOPSIGNAL SIGTERM
ENTRYPOINT ["cognita"]
CMD ["serve"]
