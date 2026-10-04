#!/bin/sh
set -eu

if [ ! -r /dev/kvm ] || [ ! -w /dev/kvm ]; then
    echo 'workspace-runtime: /dev/kvm must be readable and writable' >&2
    exit 78
fi

data_dir="${MSB_DATA_DIR:-/root/.microsandbox}"
if [ ! -d "$data_dir" ] || [ ! -w "$data_dir" ]; then
    echo "workspace-runtime: Microsandbox data directory must be writable: $data_dir" >&2
    exit 78
fi

if [ -n "${COGNITA_INTERNAL_BEARER_FILE:-}" ] && [ ! -s "$COGNITA_INTERNAL_BEARER_FILE" ]; then
    echo 'workspace-runtime: internal bearer secret is missing' >&2
    exit 78
fi

exec "$@"
