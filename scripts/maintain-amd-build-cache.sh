#!/usr/bin/env bash
set -Eeuo pipefail

# Report the selected BuildKit builder by default.  Pruning is deliberately
# operator gated because this command removes unused build cache entries.
docker_bin="${DOCKER_BIN:-docker}"
builder="${BUILDX_BUILDER:-default}"
max_used_space="${COGNITA_BUILD_CACHE_MAX_USED_SPACE:-100GB}"

usage() {
  cat >&2 <<'EOF'
Usage:
  scripts/maintain-amd-build-cache.sh [--confirm]

Without --confirm, print the selected builder's cache usage only.
With --confirm, prune unused cache down to COGNITA_BUILD_CACHE_MAX_USED_SPACE
(100GB by default).  The confirmation guard must be set in the same shell:

  COGNITA_CONFIRM_BUILD_CACHE_PRUNE=YES scripts/maintain-amd-build-cache.sh --confirm

Optional environment:
  DOCKER_BIN                         Docker executable (default: docker)
  BUILDX_BUILDER                     BuildKit builder (default: default)
  COGNITA_BUILD_CACHE_MAX_USED_SPACE BuildKit size cap (default: 100GB)
EOF
}

if [[ "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi
if [[ "$#" -gt 1 || ( "$#" -eq 1 && "${1:-}" != "--confirm" ) ]]; then
  usage
  exit 64
fi
command -v "$docker_bin" >/dev/null || {
  echo "AMD build cache: Docker executable not found: $docker_bin" >&2
  exit 127
}
[[ "$max_used_space" =~ ^[0-9]+(MB|GB|TB)$ ]] || {
  echo "AMD build cache: invalid COGNITA_BUILD_CACHE_MAX_USED_SPACE: $max_used_space" >&2
  exit 64
}

echo "AMD build cache: builder=$builder cap=$max_used_space"
"$docker_bin" buildx du --builder "$builder"

if [[ "${1:-}" != "--confirm" ]]; then
  exit 0
fi
if [[ "${COGNITA_CONFIRM_BUILD_CACHE_PRUNE:-}" != "YES" ]]; then
  echo "AMD build cache: refusing to prune without COGNITA_CONFIRM_BUILD_CACHE_PRUNE=YES" >&2
  exit 77
fi

"$docker_bin" buildx prune \
  --builder "$builder" \
  --max-used-space "$max_used_space" \
  --force

echo "AMD build cache: usage after prune"
"$docker_bin" buildx du --builder "$builder"
