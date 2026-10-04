#!/bin/sh
# Explicit operator reset. The bind roots are fixed by the dedicated install.
set -eu
project=cognita-windows
root=/srv/cognita
set -- --file "$root/compose.yaml" --file "$root/compose.cpu.yaml"
if [ -f "$root/compose.workspace.yaml" ]; then
    set -- "$@" --file "$root/compose.workspace.yaml"
fi
set -- "$@" --file "$root/compose.cpu.images.yaml"
# shellcheck disable=SC2086
docker compose --project-name "$project" --env-file "$root/compose.env" "$@" down
find "$root/postgres" -mindepth 1 -maxdepth 1 -exec rm -rf -- {} +
if [ -d "$root/workspaces" ]; then
    find "$root/workspaces" -mindepth 1 -maxdepth 1 \
        ! -name toolbox-cache ! -name .cognita-12-workspaces.json \
        -exec rm -rf -- {} +
fi
printf '%s\n' 'Cognita derived index and Workspace scratch reset; source and configuration roots retained.'
