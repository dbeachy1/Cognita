#!/bin/sh
# Builds the Cognita WSL image and the matching source tarball (DESIGN-WINDOWS-INSTALLER §6).
#
#   build-wsl-image.sh --commit <sha> --published-release <path> --out <dir> [--repo <path>]
#
# Runs on any Linux host with Docker and git (kei in production, called by `release.py
# publish` after the container images are pushed and published-release.txt is written; the
# Linux design's §19.3). It changes nothing on the host outside --out and Docker's own image
# store: the temporary image and container are removed on every exit path by the trap below.
#
# Outputs in --out, each with a `.sha256` file:
#   cognita-wsl-<version>.tar.gz   the distro image Setup imports with `wsl --import`
#   cognita-src-<version>.tar.gz   the same source tree Setup extracts into
#                                  /opt/cognita/trees/<version>-<commit12>/ on an update
#                                  (§7.3); the tarball's contents sit at its root, no
#                                  top-level directory, so `tar -xzf X -C <that dir>` works.
# One final line per file on stdout: file=<name> sha256=<hex> bytes=<n>. Everything else
# (progress, decisions, values) is logged with a local-time stamp to stderr.
#
# <version> is the `version:` key of published-release.txt. Its `commit:` key must equal
# --commit: the tree stamp (/opt/cognita/src/.cognita-tree) says which commit the tree is, and
# the Linux CLI refuses a tree whose stamp differs from published-release.txt (its §19.9).
set -eu
umask 022

# The Ubuntu 24.04 WSL root filesystem this image starts from, pinned by URL and SHA-256.
# Spike S4 (2026-09-29) built the prototype from exactly this file. To move to a newer
# base, change BOTH values together and rebuild; a hash mismatch stops the build.
BASE_URL="https://cloud-images.ubuntu.com/wsl/releases/24.04/current/ubuntu-noble-wsl-amd64-wsl.rootfs.tar.gz"
BASE_SHA256="8251e27ffff381a4af5f41dcb94d867de3e0d9774a9241908ab34555d99315ea"

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)

log() {
    printf '%s build-wsl-image: %s\n' "$(date '+%Y-%m-%d %H:%M:%S%z')" "$*" >&2
}

die() {
    log "FAILED: $*"
    exit 1
}

usage() {
    echo "usage: $0 --commit <sha> --published-release <path> --out <dir> [--repo <path>]" >&2
    exit 2
}

COMMIT=""
RELEASE_FILE=""
OUT=""
REPO=""
while [ $# -gt 0 ]; do
    case "$1" in
        --commit) [ $# -ge 2 ] || usage; COMMIT="$2"; shift 2 ;;
        --published-release) [ $# -ge 2 ] || usage; RELEASE_FILE="$2"; shift 2 ;;
        --out) [ $# -ge 2 ] || usage; OUT="$2"; shift 2 ;;
        --repo) [ $# -ge 2 ] || usage; REPO="$2"; shift 2 ;;
        *) log "unknown argument: $1"; usage ;;
    esac
done
[ -n "$COMMIT" ] && [ -n "$RELEASE_FILE" ] && [ -n "$OUT" ] || usage
if [ -z "$REPO" ]; then
    REPO=$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel) || die "cannot find this script's repository; pass --repo"
fi

# --- Inputs -------------------------------------------------------------------------------
for tool in git docker curl sha256sum tar gzip; do
    command -v "$tool" >/dev/null 2>&1 || die "$tool is required and was not found"
done
for f in wsl.conf cognita-keepalive 51cognita-docker; do
    [ -f "$SCRIPT_DIR/$f" ] || die "missing $SCRIPT_DIR/$f"
done
[ -f "$RELEASE_FILE" ] || die "published-release.txt not found: $RELEASE_FILE"
[ -d "$OUT" ] || mkdir -p "$OUT" || die "cannot create --out $OUT"
OUT=$(cd "$OUT" && pwd)
RELEASE_FILE=$(cd "$(dirname "$RELEASE_FILE")" && pwd)/$(basename "$RELEASE_FILE")

FULL_COMMIT=$(git -C "$REPO" rev-parse --verify --quiet "$COMMIT^{commit}") || die "commit $COMMIT is not in repository $REPO"
REL_VERSION=$(sed -n 's/^version:[[:space:]]*//p' "$RELEASE_FILE" | head -n 1 | tr -d '\r' | sed 's/[[:space:]]*$//')
REL_COMMIT=$(sed -n 's/^commit:[[:space:]]*//p' "$RELEASE_FILE" | head -n 1 | tr -d '\r' | sed 's/[[:space:]]*$//')
[ -n "$REL_VERSION" ] || die "$RELEASE_FILE has no version: key"
case "$REL_VERSION" in
    *[!0-9A-Za-z.+-]*|"") die "version '$REL_VERSION' in $RELEASE_FILE has characters not allowed in a file name" ;;
esac
[ "$REL_COMMIT" = "$FULL_COMMIT" ] || die "published-release.txt commit '$REL_COMMIT' is not --commit '$FULL_COMMIT'; the image must carry the tree of the commit the release names"
# The Linux CLI is the tree's `cognita` command; without it the image's /usr/local/bin/cognita
# would dangle and Setup could not do anything. Fail here, not on a user's machine.
git -C "$REPO" cat-file -e "$FULL_COMMIT:cognita" 2>/dev/null || die "commit $FULL_COMMIT has no 'cognita' command at its root; the image cannot be built from it"
COMMIT12=$(printf '%s' "$FULL_COMMIT" | cut -c1-12)
TREE_NAME="$REL_VERSION-$COMMIT12"
log "inputs: repo=$REPO commit=$FULL_COMMIT version=$REL_VERSION tree=$TREE_NAME release_file=$RELEASE_FILE out=$OUT"

WSL_NAME="cognita-wsl-$REL_VERSION.tar.gz"
SRC_NAME="cognita-src-$REL_VERSION.tar.gz"

# --- Cleanup on every exit path -----------------------------------------------------------
WORK=$(mktemp -d "$OUT/.wsl-build.XXXXXX") || die "cannot create a work directory under $OUT"
BASE_IMG="cognita-wsl-base-$$"
BUILD_CTR="cognita-wsl-build-$$"
cleanup() {
    status=$?
    docker rm -f "$BUILD_CTR" >/dev/null 2>&1 || log "cleanup: container $BUILD_CTR not present (nothing to remove)"
    docker image rm -f "$BASE_IMG" >/dev/null 2>&1 || log "cleanup: image $BASE_IMG not present (nothing to remove)"
    rm -rf "$WORK"
    log "cleanup: removed container $BUILD_CTR, image $BASE_IMG and $WORK (exit $status)"
    exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

T0=$(date +%s)

# --- Step 1: the base root filesystem -----------------------------------------------------
log "step 1: downloading the pinned base $BASE_URL"
curl -fsSL -o "$WORK/base.tar.gz" "$BASE_URL" || die "download of the base root filesystem failed: $BASE_URL"
ACTUAL_SHA=$(sha256sum "$WORK/base.tar.gz" | cut -d' ' -f1)
if [ "$ACTUAL_SHA" != "$BASE_SHA256" ]; then
    die "base root filesystem SHA-256 mismatch: expected $BASE_SHA256, got $ACTUAL_SHA (the pinned file changed or the download is damaged)"
fi
log "step 1: base verified sha256=$ACTUAL_SHA bytes=$(wc -c < "$WORK/base.tar.gz")"
zcat "$WORK/base.tar.gz" | docker import - "$BASE_IMG" >/dev/null || die "docker import of the base failed"
rm -f "$WORK/base.tar.gz"
log "step 1: imported as image $BASE_IMG"

# --- The files that go into the container -------------------------------------------------
# inputs/ is copied to /tmp/inputs in the container and deleted by the in-container script.
INPUTS="$WORK/inputs"
mkdir -p "$INPUTS/tree"
log "step 2: git archive of $FULL_COMMIT into the tree"
git -C "$REPO" archive --format=tar "$FULL_COMMIT" | tar -x -C "$INPUTS/tree" || die "git archive of $FULL_COMMIT failed"
mkdir -p "$INPUTS/tree/containers"
cp "$RELEASE_FILE" "$INPUTS/tree/containers/published-release.txt"
printf 'commit: %s\n' "$FULL_COMMIT" > "$INPUTS/tree/.cognita-tree"
log "step 2: tree has $(find "$INPUTS/tree" -type f | wc -l) files; stamp: $(cat "$INPUTS/tree/.cognita-tree")"
cp "$SCRIPT_DIR/wsl.conf" "$SCRIPT_DIR/cognita-keepalive" "$SCRIPT_DIR/51cognita-docker" "$INPUTS/"

# The source tarball is written now, from the same tree the image gets.
log "step 2: writing $SRC_NAME"
tar --sort=name --owner=0 --group=0 --numeric-owner -C "$INPUTS/tree" -cf - . | gzip -n -6 > "$WORK/$SRC_NAME" || die "writing $SRC_NAME failed"

# --- Step 2: the container that does the installs -----------------------------------------
# Runs inside the imported base. Any failing command stops it (-e); nothing here is allowed
# to fail quietly. Docker Engine comes from Docker's own repository (the Linux design's §4
# step 2 packages).
# 15.1.0 (DESIGN-WINDOWS-INSTALLER §22.5, §22.12 items 4 and 7): the NVIDIA Container Toolkit is in the image too,
# pinned to the same version as $script:NvidiaToolkitVersion in windows/CognitaWin.ps1 (tests/test_wsl_image_recipe.py
# keeps the two equal). Four packages at the pin, held so a hand-run apt upgrade cannot move them apart;
# `nvidia-ctk runtime configure --runtime=docker` registers the runtime in /etc/docker/daemon.json. Never run
# nvidia-ctk's CDI spec generation (it writes machine-specific device facts). Inert without a card. Rules for this
# string: it is ONE single-quoted shell string, so the new lines use double quotes only; the key and the list go
# to temp files (no pipe from curl: `sh` has no pipefail) and `gpg --batch --yes --dearmor` replaces an existing
# keyring; the list rewrite uses `|` as the sed delimiter.
IN_CONTAINER='
set -eu
export DEBIAN_FRONTEND=noninteractive
echo "in-container: base has UID 1000: $(getent passwd 1000 || echo none)"
if getent passwd 1000 >/dev/null; then echo "in-container: UID 1000 is already taken"; exit 1; fi
apt-get update -qq
apt-get install -y -qq ca-certificates curl gnupg python3 >/dev/null
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
echo "deb [arch=amd64 signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu noble stable" > /etc/apt/sources.list.d/docker.list
apt-get update -qq
apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin >/dev/null
NV_VER=1.20.1-1
echo "in-container: installing the NVIDIA Container Toolkit $NV_VER from NVIDIA repository"
curl -fsSL -o /tmp/cognita-nv.gpg https://nvidia.github.io/libnvidia-container/gpgkey
gpg --batch --yes --dearmor -o /etc/apt/keyrings/nvidia-container-toolkit-keyring.gpg /tmp/cognita-nv.gpg
curl -fsSL -o /tmp/cognita-nv.list https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list
sed "s|deb https://|deb [signed-by=/etc/apt/keyrings/nvidia-container-toolkit-keyring.gpg] https://|" /tmp/cognita-nv.list > /etc/apt/sources.list.d/nvidia-container-toolkit.list
rm -f /tmp/cognita-nv.gpg /tmp/cognita-nv.list
apt-get update -qq
apt-get install -y -qq nvidia-container-toolkit=$NV_VER nvidia-container-toolkit-base=$NV_VER libnvidia-container1=$NV_VER libnvidia-container-tools=$NV_VER >/dev/null
apt-mark hold nvidia-container-toolkit nvidia-container-toolkit-base libnvidia-container1 libnvidia-container-tools
nvidia-ctk runtime configure --runtime=docker
echo "in-container: NVIDIA Container Toolkit configured for Docker, held at $NV_VER"
useradd -m -u 1000 -s /bin/bash cognita
usermod -aG docker cognita
mkdir -p /var/lib/systemd/linger
touch /var/lib/systemd/linger/cognita
systemctl enable docker
install -m 0644 /tmp/inputs/wsl.conf /etc/wsl.conf
install -d -m 0755 /usr/local/libexec
install -m 0755 /tmp/inputs/cognita-keepalive /usr/local/libexec/cognita-keepalive
install -m 0644 /tmp/inputs/51cognita-docker /etc/apt/apt.conf.d/51cognita-docker
install -d -m 0755 /opt/cognita/trees
mv /tmp/inputs/tree "/opt/cognita/trees/$TREE_NAME"
ln -s "/opt/cognita/trees/$TREE_NAME" /opt/cognita/src
ln -s /opt/cognita/src/cognita /usr/local/bin/cognita
rm -rf /tmp/inputs
apt-get clean
rm -rf /var/lib/apt/lists/* /var/log/apt/* /var/cache/apt/*
# Docker bind-mounts /etc/resolv.conf into every container, so it cannot be removed while
# mounted. The container is created with SYS_ADMIN (and no AppArmor confinement) only so it
# can unmount it here and remove it: WSL generates its own at first start (design section 6,
# proven in spike S4). If the unmount is refused the empty Docker placeholder stays, which
# S4 also proved harmless (WSL replaces it), and that is logged. /.dockerenv is an ordinary
# file and is removed so systemd does not take the distro for a Docker container.
if umount /etc/resolv.conf 2>/dev/null; then
    rm -f /etc/resolv.conf
    echo "in-container: unmounted and removed /etc/resolv.conf"
else
    echo "in-container: could not unmount /etc/resolv.conf; the empty Docker placeholder stays and WSL replaces it at first start"
fi
rm -f /.dockerenv
echo "in-container: check: user=$(getent passwd cognita)"
id -nG cognita | tr " " "\n" | grep -qx docker
test -e /var/lib/systemd/linger/cognita
systemctl is-enabled docker
grep -qx "commit: $FULL_COMMIT" /opt/cognita/src/.cognita-tree
test -f /opt/cognita/src/containers/published-release.txt
test -x /opt/cognita/src/cognita
test -L /usr/local/bin/cognita
nvidia-ctk --version | grep -qF "${NV_VER%-*}"
grep -q "\"nvidia\"" /etc/docker/daemon.json
for pkg in nvidia-container-toolkit nvidia-container-toolkit-base libnvidia-container1 libnvidia-container-tools; do
    apt-mark showhold | grep -qx "$pkg"
done
echo "in-container: done, $(dpkg-query -W docker-ce | tr "	" " "), $(docker --version), $(dpkg-query -W nvidia-container-toolkit | tr "	" " ")"
'

log "step 2: creating the build container from $BASE_IMG"
docker create --name "$BUILD_CTR" --cap-add SYS_ADMIN --security-opt apparmor=unconfined     -e "TREE_NAME=$TREE_NAME" -e "FULL_COMMIT=$FULL_COMMIT" \
    "$BASE_IMG" /bin/bash -c "$IN_CONTAINER" >/dev/null || die "docker create failed"
docker cp "$INPUTS" "$BUILD_CTR:/tmp/inputs" || die "copying the inputs into the build container failed"
log "step 2: running the package installs and setup in the container"
docker start -a "$BUILD_CTR" >&2 || die "the in-container setup failed (its output is above)"

# --- Step 5: export, verify, gzip ---------------------------------------------------------
# Exported to an uncompressed temporary tar under $WORK (about the size of the filesystem)
# so its exit status is checked and the archive is proven readable before it is compressed.
# (Editing the tar with `tar --delete` was tried and rejected: it leaves an archive that
# Windows bsdtar reports as damaged during `wsl --import`.)
log "step 5: exporting the container to $WORK/rootfs.tar"
docker export -o "$WORK/rootfs.tar" "$BUILD_CTR" || die "docker export failed"
log "step 5: exported rootfs.tar bytes=$(wc -c < "$WORK/rootfs.tar" | tr -d ' ')"
tar -tf "$WORK/rootfs.tar" > "$WORK/rootfs.list" || die "the exported archive is not readable"
log "step 5: export lists $(wc -l < "$WORK/rootfs.list" | tr -d ' ') entries"
for member in .dockerenv etc/resolv.conf; do
    if grep -qx "$member" "$WORK/rootfs.list"; then
        log "step 5: the export still contains $member (an empty Docker placeholder; WSL replaces it at first start)"
    else
        log "step 5: the export has no $member"
    fi
done
log "step 5: compressing to $WSL_NAME"
gzip -n -6 < "$WORK/rootfs.tar" > "$WORK/$WSL_NAME" || die "compressing the export failed"
rm -f "$WORK/rootfs.tar" "$WORK/rootfs.list"

# --- Outputs ------------------------------------------------------------------------------
for name in "$WSL_NAME" "$SRC_NAME"; do
    sum=$(sha256sum "$WORK/$name" | cut -d' ' -f1)
    bytes=$(wc -c < "$WORK/$name" | tr -d ' ')
    printf '%s  %s\n' "$sum" "$name" > "$WORK/$name.sha256"
    mv -f "$WORK/$name" "$OUT/$name"
    mv -f "$WORK/$name.sha256" "$OUT/$name.sha256"
    log "output: $OUT/$name sha256=$sum bytes=$bytes"
    printf 'file=%s sha256=%s bytes=%s\n' "$name" "$sum" "$bytes"
done
log "done: version=$REL_VERSION commit=$FULL_COMMIT elapsed_s=$(( $(date +%s) - T0 ))"
