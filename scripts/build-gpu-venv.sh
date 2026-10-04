#!/usr/bin/env bash
# Build Cognita's GPU worker environment (DESIGN-6.0 §11, recipe from §12.3).
#
# 🔴 This NEVER touches the service's virtual environment. GPU builds of
# onnxruntime install the SAME `onnxruntime` module as the CPU build, so a
# broken or mismatched wheel installed there would not degrade Cognita to CPU —
# it would remove the embedder entirely and indexing would stop. That is the
# worst failure available in this feature and it is a packaging accident rather
# than a bug anyone would write. Hence two environments, always.
#
# Every soname below is satisfied by a package that DECLARES it, at the version
# it declares. Nothing is renamed or aliased. See §12.3 for what happened when
# that rule was broken.
set -euo pipefail

UV="$HOME/.local/bin/uv"
ENV=${1:-$HOME/Cognita/gpu-venv}
PREFIX="$ENV/.syslibs"

echo "=== target: $ENV (the service venv is $HOME/Cognita/.venv and is NOT touched) ==="
rm -rf "$ENV"
"$UV" venv "$ENV" --python 3.13 2>&1 | tail -2

echo
echo "=== fastembed (pinned to the service's version), then swap the runtime ==="
"$UV" pip install --python "$ENV/bin/python" 'fastembed==0.8.0' -q
# fastembed pulls the CPU onnxruntime as a dependency, and the GPU distributions
# install the same module under a different distribution name — pip will happily
# leave both in place with whichever landed last winning by file overwrite.
"$UV" pip uninstall --python "$ENV/bin/python" onnxruntime 2>&1 | tail -1

echo
echo "=== MIGraphX runtime libs (AMD's own wheel index) ==="
"$UV" pip install --python "$ENV/bin/python" \
    --index-url https://stable.repo.amd.com/rocm/migraphx/whl-next/ \
    'migraphx-libs==2.17.0+rocm10.0.0' -q

echo "=== onnxruntime-migraphx (PyPI; the service runs ORT 1.27.0) ==="
"$UV" pip install --python "$ENV/bin/python" 'onnxruntime-migraphx==1.27.1' -q

echo
echo "=== hipBLASLt ==="
# libmigraphx_gpu needs libhipblaslt.so.1. Ubuntu packages it at the IDENTICAL
# ROCm version as everything else installed (7.1.1+dfsg-3ubuntu2, universe) but
# does not install it by default. If it is present system-wide we use it; if not
# we unpack the same package privately, which needs no root and changes nothing
# outside this directory.
if ldconfig -p | grep -q 'libhipblaslt\.so\.1'; then
    echo "  system-wide: $(ldconfig -p | grep -m1 'libhipblaslt\.so\.1')"
    HIPBLASLT=""
else
    echo "  not installed system-wide; unpacking libhipblaslt1 privately"
    echo "  (the clean form is: sudo apt install libhipblaslt1)"
    mkdir -p "$PREFIX"
    (cd "$PREFIX" && apt-get download libhipblaslt1 >/dev/null 2>&1 && dpkg -x ./*.deb . && rm -f ./*.deb)
    HIPBLASLT=$(dirname "$(find "$PREFIX" -name 'libhipblaslt.so.1' | head -1)")
    echo "  unpacked to $HIPBLASLT"
fi

MGXLIB="$ENV/lib/python3.13/site-packages/migraphx_libs"
LIBPATH="$MGXLIB"
[ -n "$HIPBLASLT" ] && LIBPATH="$MGXLIB:$HIPBLASLT"
echo "$LIBPATH" > "$ENV/.ld_library_path"

echo
echo "=== unresolved sonames (must be none) ==="

# 🔴 A MISSING LIBRARY USED TO PRINT "all resolve" AND EXIT 0.
# The check was `ldd <lib> | grep -i 'not found' || echo "  ...: all resolve"`.
# If the file does not exist, ldd writes to stderr and exits non-zero, grep
# matches nothing and also exits non-zero, so the `||` branch fires and the
# script reports success for a library that is not there. The hardcoded
# `2017000` soname is pinned to migraphx-libs 2.17.0, so the next version bump
# is precisely the case that triggers it — and the failure is silent all the way
# to runtime, where ORT quietly falls back to CPU. That is the bug ee57df2
# already shipped once.
check_sonames() {
  local label="$1" lib="$2"
  if [ ! -e "$lib" ]; then
    echo "  $label: MISSING — $lib does not exist" >&2
    return 1
  fi
  local out
  if ! out=$(LD_LIBRARY_PATH="$LIBPATH" ldd "$lib" 2>&1); then
    echo "  $label: ldd FAILED on $lib" >&2
    echo "$out" | sed 's/^/    /' >&2
    return 1
  fi
  if echo "$out" | grep -qi 'not found'; then
    echo "  $label: UNRESOLVED —" >&2
    echo "$out" | grep -i 'not found' | sed 's/^/    /' >&2
    return 1
  fi
  echo "  $label: all resolve"
}

failed=0
check_sonames "ort provider" \
  "$ENV/lib/python3.13/site-packages/onnxruntime/capi/libonnxruntime_providers_migraphx.so" || failed=1
# Match whatever migraphx_libs actually shipped rather than pinning a version.
gpu_lib=$(ls "$MGXLIB"/libmigraphx_gpu.so.* 2>/dev/null | head -1)
check_sonames "migraphx gpu" "${gpu_lib:-$MGXLIB/libmigraphx_gpu.so}" || failed=1
if [ "$failed" -ne 0 ]; then
  echo >&2
  echo "BUILD FAILED: the worker environment would load without its GPU provider" >&2
  echo "and fall back to the CPU silently. Fix the libraries above." >&2
  exit 1
fi

echo
echo "=== providers visible to the worker ==="
LD_LIBRARY_PATH="$LIBPATH" "$ENV/bin/python" -c "
import onnxruntime as ort
print('  ort', ort.__version__, ort.get_available_providers())
"
echo
echo "READY: $ENV/bin/python   (LD_LIBRARY_PATH in $ENV/.ld_library_path)"
