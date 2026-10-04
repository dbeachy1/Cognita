# syntax=docker/dockerfile:1.7
ARG TOOLBOX_BASE_IMAGE=debian:bookworm-slim@sha256:88200866dfff7ea7f5cbcb6ec7c8a701889efe6fe859fe64d6990e4b07ea4171
FROM ${TOOLBOX_BASE_IMAGE}

ARG COGNITA_VERSION=12.6.0
LABEL org.opencontainers.image.title="Cognita Workspace toolbox" \
      org.opencontainers.image.version="${COGNITA_VERSION}" \
      org.opencontainers.image.description="Credential-free CPU toolbox for isolated Workspaces"

ENV DEBIAN_FRONTEND=noninteractive \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    npm_config_cache=/tmp/npm-cache \
    PYTHONDONTWRITEBYTECODE=1

COPY containers/workspace-toolbox/packages.lock /usr/share/cognita/toolbox-packages.lock
COPY containers/workspace-toolbox/cognita-workspace-job /usr/local/bin/cognita-workspace-job

# Keep this list aligned with packages.lock.  The operator build job records
# exact Debian package versions and the resulting image digest before the
# toolbox is pre-pulled; a running Workspace never installs host packages.
# Guest package commands default to no retained pip downloads and an ephemeral
# /tmp npm cache.  Explicit client overrides remain client-owned behavior.
RUN apt-get update \
    && apt-get install --no-install-recommends -y \
       bash ca-certificates coreutils findutils grep sed gawk \
       tar gzip bzip2 xz-utils zip unzip file tree \
       git curl jq \
       python3 python3-venv python3-pip python3-setuptools \
       nodejs npm \
       build-essential pkg-config \
       sqlite3 \
       poppler-utils pandoc tesseract-ocr imagemagick ffmpeg \
    && rm -rf /var/lib/apt/lists/* /var/cache/apt/*

RUN useradd --create-home --uid 10001 --shell /bin/bash workspace \
    && mkdir -p /workspace \
    && chown workspace:workspace /workspace \
    && chmod 0755 /usr/local/bin/cognita-workspace-job
WORKDIR /workspace
USER workspace
CMD ["bash"]
