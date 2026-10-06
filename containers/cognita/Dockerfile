# syntax=docker/dockerfile:1.7
# The image is built from the repository so the gateway and its Python package
# are never pulled from an unreviewed registry image. Keep the CPU base immutable.
# The release manifest records the same index
# digest; updating it is a reviewed dependency change.
ARG PYTHON_BASE_IMAGE=python:3.13-slim-bookworm@sha256:ed86c82274b3c69b52fb5820f358f0bd7df0b603332063cb5c6e32bd220c3e6e
# The app stage is named because the `test` stage below builds on top of it.
# Docker builds the LAST stage when none is selected, so anything building the
# service image must select this one; compose.yaml carries `target: app` so a
# hand-run `docker compose build` gets it as well as scripts/release.py.
FROM ${PYTHON_BASE_IMAGE} AS app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    COGNITA_CONFIG_ROOT=/app/config

WORKDIR /app

# Build dependencies are kept in one layer and removed before the runtime
# layer is committed.  No compiler, package cache, or host credential enters
# the resulting image.
RUN apt-get update \
    && apt-get install --no-install-recommends -y ca-certificates libpq5 libgomp1 \
    && rm -rf /var/lib/apt/lists/*

# Image-owned OCR dependencies precede application source so a code change never
# resolves or reinstalls this independent, hash-locked runtime. (The model weights
# used to be COPYed here too; since 14.2.0 they live in the model cache instead.)
COPY containers/ocr-cpu-requirements.lock /opt/cognita-locks/ocr-cpu-requirements.lock
RUN python -m venv /opt/cognita-runtimes/ocr \
    && /opt/cognita-runtimes/ocr/bin/python -m pip install --no-cache-dir --index-url https://pypi.org/simple --require-hashes --no-deps --only-binary=:all: -r /opt/cognita-locks/ocr-cpu-requirements.lock \
    && /opt/cognita-runtimes/ocr/bin/python -m pip check

COPY containers/cognita/runtime-lock.json /opt/cognita-locks/ocr-cpu-runtime.json
COPY docs/easyocr-qualification-dependencies.json /opt/cognita-locks/easyocr-qualification-dependencies.json
COPY scripts/verify_ocr_runtime.py scripts/dependency_locks.py /opt/cognita-runtimes/
# The EasyOCR weights are not in this image. They are
# downloaded into the model cache (mounted at /var/lib/cognita/models/easyocr), and the worker still
# hashes them at run time. The manifest's runtime section is generated from the packages installed
# above; its model_files and aggregate_sha256 come from the source authority without reading any
# model file. This is never the AMD manifest: its ROCm torch pins would fail CPU OCR's runtime check.
RUN mkdir -p /opt/cognita-models \
    && python /opt/cognita-runtimes/verify_ocr_runtime.py --generate-manifest \
       --python /opt/cognita-runtimes/ocr/bin/python \
       --manifest /opt/cognita-models/easyocr-qualification.json \
       --runtime-lock /opt/cognita-locks/ocr-cpu-runtime.json --requirements-lock /opt/cognita-locks/ocr-cpu-requirements.lock \
       --model-source-manifest /opt/cognita-locks/easyocr-qualification-dependencies.json

# Resolve neither runtime at build time. Application source and release ARGs
# stay below these layers: even an ARG's implicit RUN environment can invalidate
# the cache before its first visible interpolation.
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
# 14.0: the license and third-party notices travel with the image.
COPY LICENSE THIRD_PARTY_NOTICES.md ./
COPY src ./src
RUN python -m pip install --no-cache-dir . --no-deps --no-build-isolation \
    && python -m pip check \
    && rm -rf /root/.cache/pip

RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin cognita \
    && mkdir -p /app/config /var/lib/cognita/models /var/lib/cognita/transfers \
    && chown -R cognita:cognita /app /var/lib/cognita

# No default is provided. release_identity.APPLICATION_VERSION is the only
# authority for the version, and compose.yaml/scripts/release.py always pass
# it; a default here would be a second place a stale version could hide.
ARG COGNITA_VERSION
# 13.0: the commit is a label, so a running container can be asked what source
# it was built from without matching image IDs against a release directory.
ARG COGNITA_COMMIT
LABEL org.opencontainers.image.title="Cognita Knowledge gateway" \
      org.opencontainers.image.version="${COGNITA_VERSION}" \
      org.opencontainers.image.revision="${COGNITA_COMMIT}" \
      org.opencontainers.image.source="https://github.com/dbeachy1/Cognita"

USER cognita
EXPOSE 8675 8676
STOPSIGNAL SIGTERM
ENTRYPOINT ["cognita"]
CMD ["serve"]

# --------------------------------------------------------------------------
# Test stage
#
# The suite must run against the INSTALLED package, so it runs in this image
# with the tests copied in rather than bind-mounted from a checkout: nothing
# from the host can shadow /usr/local/lib/python3.13/site-packages/cognita.
# Git is here because a few suites shell out to it.
# --------------------------------------------------------------------------
FROM app AS test

USER root
COPY containers/dev-requirements.lock /opt/cognita-locks/dev-requirements.lock
RUN apt-get update \
    && apt-get install --no-install-recommends -y git \
    && rm -rf /var/lib/apt/lists/* \
    && python -m pip install --no-cache-dir --index-url https://pypi.org/simple --require-hashes --no-deps -r /opt/cognita-locks/dev-requirements.lock \
    && rm -rf /root/.cache/pip

# The suite reads parts of the checkout as fixture data -- the Admin HTML and
# JS, the Compose files, the container manifests and locks, the systemd
# templates, the packaging metadata -- so those come in beside the tests.
# `cognita` itself is still imported from site-packages: /opt/cognita-tests
# holds `src/cognita`, not `cognita`, so nothing here can shadow the installed
# package.
COPY tests /opt/cognita-tests/tests
COPY scripts /opt/cognita-tests/scripts
COPY src /opt/cognita-tests/src
COPY containers /opt/cognita-tests/containers
# Named files only, never the directory: a build context is a working
# checkout, and `config/` in a real installation holds credentials-v2.json,
# master-keys/, the OAuth SQLite database, backups and the admin TLS material.
# `COPY config` baked all of that into this image AND into the build cache the
# moment anyone ran `release.py test` from a live checkout.  The suite reads
# exactly one file from here.  (`.dockerignore` now also excludes those paths,
# so the two halves of this fail closed independently.)
COPY config/cognita-compose.env.example /opt/cognita-tests/config/cognita-compose.env.example
COPY docs /opt/cognita-tests/docs
# 15.0: tests/test_build_setup.py and tests/test_setup_pure.py load
# windows/build_setup.py (and read windows/setup/*) by path. They arrived after
# the 14.2.4 publish, and the first publish that ran them failed at collection
# with FileNotFoundError. Committed source only: no credentials live under windows/.
COPY windows /opt/cognita-tests/windows
COPY compose.yaml compose.amd.yaml compose.nvidia.yaml compose.cpu.yaml compose.workspace.yaml pyproject.toml README.md \
     CHANGELOG.md DEPLOYMENT.md .gitattributes .gitignore .dockerignore cognita /opt/cognita-tests/
RUN chown -R cognita:cognita /opt/cognita-tests

USER cognita
WORKDIR /opt/cognita-tests
ENTRYPOINT []
# The image is read-only to the test user, so pytest's cache plugin is off.
CMD ["python", "-m", "pytest", "tests", "-o", "asyncio_mode=auto", "-rfEs", "-q", "-p", "no:cacheprovider"]
