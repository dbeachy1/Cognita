# syntax=docker/dockerfile:1.7
# Microsandbox is the only upstream runtime.  The immutable index digest is
# repeated in compose.yaml and image-manifest.json; preflight resolves the
# linux/amd64 manifest before systemd is enabled.
ARG MICROSANDBOX_IMAGE=ghcr.io/superradcompany/microsandbox:0.7.0@sha256:fed39d2863121ac1fec25bdeaaeab9498f48ee381294faa8fe5805b0e9ac0fdc
FROM ${MICROSANDBOX_IMAGE}

# 13.0 §4: no default. release_identity.APPLICATION_VERSION is the only
# authority for the version, and compose.yaml/scripts/release.py always pass
# it; a default here would be a second place a stale version could hide.
ARG COGNITA_VERSION
# 13.0: the commit is a label, so a running container can be asked what source
# it was built from without matching image IDs against a release directory.
ARG COGNITA_COMMIT
ARG MICROSANDBOX_WHEEL=/opt/build-artifacts/microsandbox-0.7.0-cp310-abi3-manylinux_2_28_x86_64.whl
LABEL org.opencontainers.image.title="Cognita Workspace runtime foundation" \
      org.opencontainers.image.version="${COGNITA_VERSION}" \
      org.opencontainers.image.revision="${COGNITA_COMMIT}" \
      org.opencontainers.image.description="Pinned Microsandbox with Cognita's private broker foundation"

ENV COGNITA_VERSION=${COGNITA_VERSION} \
    MSB_DATA_DIR=/root/.microsandbox \
    COGNITA_MICROSANDBOX_RUNTIME_ROOT=/opt/microsandbox/0.7.0 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/opt/cognita

COPY containers/workspace-runtime/microsandbox.lock /opt/microsandbox.lock
COPY LICENSE /opt/cognita/LICENSE
COPY THIRD_PARTY_NOTICES.md /opt/cognita/THIRD_PARTY_NOTICES.md
COPY containers/workspace-runtime/requirements.in /opt/cognita/requirements.in
COPY containers/broker-requirements.lock /opt/cognita-locks/
COPY build-artifacts/microsandbox-0.7.0-cp310-abi3-manylinux_2_28_x86_64.whl /opt/build-artifacts/microsandbox-0.7.0-cp310-abi3-manylinux_2_28_x86_64.whl
RUN apt-get update \
    && apt-get install --no-install-recommends -y python3 python3-pip \
    && python3 -m pip install --break-system-packages --no-cache-dir --index-url https://pypi.org/simple --require-hashes --no-deps -r /opt/cognita-locks/broker-requirements.lock \
    && echo '848eab6e23b3bc168934b3093baafe26b086348d2914ae710900884e7bb74470  /opt/build-artifacts/microsandbox-0.7.0-cp310-abi3-manylinux_2_28_x86_64.whl' | sha256sum -c - \
    && python3 -m pip install --break-system-packages --no-index --no-cache-dir \
       /opt/build-artifacts/microsandbox-0.7.0-cp310-abi3-manylinux_2_28_x86_64.whl \
    && rm -rf /var/lib/apt/lists/* /root/.cache/pip
COPY src/cognita/runtime_broker /opt/cognita/cognita/runtime_broker
# The broker reports its version from the one authority (13.0 §4), and this
# image carries only the broker package -- so that leaf module of literals has
# to come with it. It imports nothing, which is what makes this safe: no part
# of the gateway follows it into the runtime image.
COPY src/cognita/release_identity.py /opt/cognita/cognita/release_identity.py
COPY scripts/verify_microsandbox_install.py /opt/cognita/scripts/verify_microsandbox_install.py
COPY containers/workspace-runtime/entrypoint.sh /usr/local/bin/cognita-runtime-entrypoint
# The runtime deliberately runs as the host service UID.  Keep the upstream
# data location, but permit that UID to traverse /root to reach the owner-only
# bind mount at /root/.microsandbox.
RUN chmod 0755 /usr/local/bin/cognita-runtime-entrypoint \
    && chmod 0711 /root \
    && mkdir -p /root/.microsandbox /var/lib/cognita/toolbox-cache \
    && python3 /opt/cognita/scripts/verify_microsandbox_install.py --materialize \
    && test -x /opt/microsandbox/0.7.0/msb \
    && test -d /opt/microsandbox/0.7.0

# The runtime serves only Cognita's authenticated private protocol. The SDK
# readiness probe gates health before any Workspace operation is admitted.
ENTRYPOINT ["/usr/local/bin/cognita-runtime-entrypoint"]
CMD ["python3", "-m", "cognita.runtime_broker"]
