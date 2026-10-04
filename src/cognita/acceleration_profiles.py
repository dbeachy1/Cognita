"""The acceleration profile descriptor (DESIGN-NVIDIA-ACCELERATION §3).

Cognita ships three image profiles: `cpu`, `amd` and `nvidia`. Before 15.0 the
AMD path was written as literals scattered through the tree: the provider name,
`profile == "amd"` checks, `ROCR_VISIBLE_DEVICES`, the MIGraphX program cache,
the fixed sequence length and the VRAM ceiling constants. This module pulls
every one of them into ONE table so a second GPU vendor is a row, not a hunt.

🔴 **The `amd` row reproduces the pre-refactor literals EXACTLY.** That is the
proof that the refactor changed nothing for AMD, and `tests/test_acceleration_
profiles.py` pins each value.

🔴 **The `cpu` profile resolves GPU settings as the `amd` row does** (design §3,
lead's clarification 2026-09-29). With `COGNITA_ACCELERATION_PROFILE` unset (a
bare-metal `cognita serve`, and every test that does not set it) the profile is
`cpu`, and before this design every GPU default was the MIGraphX one regardless
of profile. So wherever a GPU setting falls back to "the profile's" — the
provider name, `gpu_fixed_seq_len: -1`, program-cache gating, the ceiling
constants, the worker's device environment variable — a profile whose `gpu` is
false uses the `amd` row's value. Only an explicit `nvidia` profile changes
anything. `gpu_settings_profile()` is the ONE place that rule lives; never
re-implement it at a call site.

This module imports nothing from the rest of the package at load time (the
probe factories import `gpu_probe` lazily), so `gpu_probe` can import it
without a cycle.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Callable

log = logging.getLogger("cognita.gpu")

# The one environment variable that selects the profile. The image sets it
# (`COGNITA_ACCELERATION_PROFILE=amd` / `nvidia`); everywhere else it is unset
# and the profile is `cpu`.
PROFILE_ENV = "COGNITA_ACCELERATION_PROFILE"

# Where the image keeps the worker runtimes (the same for both GPU images).
RUNTIME_ROOT = "/opt/cognita-runtimes"


def _null_probe():
    from . import gpu_probe

    return gpu_probe.NullProbe()


def _sysfs_or_null_probe():
    """Today's logic for `cpu` and `amd`: sysfs when there is a DRM tree."""
    from . import gpu_probe

    return gpu_probe.sysfs_or_null_probe()


def _nvml_probe():
    from . import gpu_probe

    return gpu_probe.NvmlProbe()


@dataclass(frozen=True)
class AccelerationProfile:
    """Everything that differs between the image profiles, in one place.

    A field that means nothing for a profile is `None` (or False), never a
    made-up value: the `cpu` row has no provider, no device variable and no
    ceiling constants of its own, and `gpu_settings_profile()` is what supplies
    the AMD values where a caller still needs a number.
    """

    name: str
    gpu: bool
    embed_provider: str | None       # the ONNX Runtime provider name
    provider_key: str | None         # the `gpu_provider` config short name
    device_env: str | None           # the variable that scopes a worker to one card
    fixed_seq_len_default: int | None  # 0 means "do not pin"
    program_cache: bool              # is `gpu_program_cache_dir` meaningful
    ocr_backend: str | None
    devices_exposed: bool            # does this deployment expose GPU devices
    ceiling_fixed_gb: float | None   # the VRAM gate: fixed part ...
    ceiling_per_batch_gb: float | None  # ... plus this much per unit of batch
    # The card-utilization gate's default. `None` means the profile does not
    # gate on utilization at all, only on free VRAM.
    max_busy_percent_default: int | None
    probe_factory: Callable[[], object]
    runtime_root: str | None
    vendor_label: str | None         # UI text

    def device_env_value(self, unique_id: str) -> str | None:
        """What to put in `device_env` to scope a worker to ONE card.

        `GPU-<unique_id>` for both vendors: the probes strip the `GPU-` prefix
        from the vendor's own UUID so this form is exactly what ROCR and CUDA
        accept. `None` for a profile that has no device variable.
        """
        if self.device_env is None:
            return None
        return f"GPU-{unique_id}"


CPU = AccelerationProfile(
    name="cpu",
    gpu=False,
    embed_provider=None,
    provider_key=None,
    device_env=None,
    fixed_seq_len_default=None,
    program_cache=False,
    ocr_backend=None,
    devices_exposed=False,
    ceiling_fixed_gb=None,
    ceiling_per_batch_gb=None,
    max_busy_percent_default=None,
    # §4: a CPU-profile deployment on a box with DRM cards keeps listing them
    # (with `profile_cpu`) exactly as it did before, so cpu maps to today's
    # sysfs-or-Null logic and NOT to a plain NullProbe.
    probe_factory=_sysfs_or_null_probe,
    runtime_root=None,
    vendor_label=None,
)

AMD = AccelerationProfile(
    name="amd",
    gpu=True,
    embed_provider="MIGraphXExecutionProvider",
    provider_key="migraphx",
    device_env="ROCR_VISIBLE_DEVICES",
    fixed_seq_len_default=512,
    program_cache=True,
    ocr_backend="pytorch-rocm",
    devices_exposed=True,
    # The two MEASURED points of DESIGN-6.0 §12.3 (batch 64 -> 3.62 GiB,
    # batch 256 -> 8.20 GiB) as a straight line. Unchanged from gpu_probe's
    # pre-15.0 module constants.
    ceiling_fixed_gb=2.093,
    ceiling_per_batch_gb=0.02385,
    # Unchanged from pre-15.0: a card busier than 20% is left to whoever is
    # using it (ComfyUI, llama.cpp on kei's two compute cards).
    max_busy_percent_default=20,
    probe_factory=_sysfs_or_null_probe,
    runtime_root=RUNTIME_ROOT,
    vendor_label="AMD",
)

NVIDIA = AccelerationProfile(
    name="nvidia",
    gpu=True,
    embed_provider="CUDAExecutionProvider",
    provider_key="cuda",
    device_env="CUDA_VISIBLE_DEVICES",
    # Dynamic shapes. Padding every batch to 512 tokens halved CUDA throughput
    # (158 -> 83 chunks/s, DESIGN-NVIDIA §1.3); the pin is a MIGraphX
    # shape-compile workaround and has no business here.
    fixed_seq_len_default=0,
    # The MIGraphX compiled-program cache. CUDA has no shape compile.
    program_cache=False,
    ocr_backend="pytorch-cuda",
    devices_exposed=True,
    # DESIGN-NVIDIA §1.3, re-measured on the WORST case the ceiling is defined
    # for (every text in the batch at the model's full 512 tokens): 2.4 GB fixed
    # plus 0.115 GB per unit of batch sits on or above every measured point.
    # (The first fit's 2.2 / 0.09 used ~200-token chunks and was stale.)
    ceiling_fixed_gb=2.4,
    ceiling_per_batch_gb=0.115,
    # 15.0.1: NO utilization gate on NVIDIA; free VRAM alone decides. Measured
    # on Maia (2026-09-30): the 4090 that drives the desktop sits near 30% busy
    # doing nothing but drawing it, so the AMD rule of 20% left the card unused
    # on every job, while even sharing that card it embedded at 112 chunks/s
    # against the CPU's 3.6. Doug: a shared NVIDIA card is still always faster
    # than the CPU. An explicit `gpu_max_busy_percent` still applies.
    max_busy_percent_default=None,
    probe_factory=_nvml_probe,
    runtime_root=RUNTIME_ROOT,
    vendor_label="NVIDIA",
)

PROFILES: dict[str, AccelerationProfile] = {
    "cpu": CPU,
    "amd": AMD,
    "nvidia": NVIDIA,
}

GPU_PROFILE_NAMES = frozenset({"amd", "nvidia"})

# Unknown profile names already warned about. The lookup runs on hot paths
# (every probe construction, every ceiling), so one line per DISTINCT bad value
# is the contract: a warning per call would bury the log in the very thing it is
# reporting.
_warned_unknown: set[str] = set()


def profile_named(name: str | None) -> AccelerationProfile:
    """The profile called `name`, normalized as `__main__` always did.

    Unknown -> `cpu`, with ONE warning line naming the bad value, never an
    exception: a mistyped environment variable must not stop the service from
    starting, and the CPU profile is always correct, only slower.
    """
    token = (name if name is not None else "cpu").strip().lower() or "cpu"
    found = PROFILES.get(token)
    if found is not None:
        return found
    if token not in _warned_unknown:
        _warned_unknown.add(token)
        log.warning(
            "acceleration.profile unknown value=%r (expected one of %s); "
            "using cpu", token, sorted(PROFILES),
        )
    return CPU


def current_profile() -> AccelerationProfile:
    """The profile this process runs under, from `COGNITA_ACCELERATION_PROFILE`.

    Read on every call rather than cached: it is one dict lookup, and a cached
    value would make a test (or an operator's restart-free experiment) unable to
    change it. Unset, empty and unknown all mean `cpu`.
    """
    raw = os.environ.get(PROFILE_ENV, "cpu")
    profile = profile_named(raw)
    return profile


def gpu_settings_profile(profile: AccelerationProfile | None = None) -> AccelerationProfile:
    """The profile whose GPU defaults apply: itself if it has a GPU, else `amd`.

    See the module docstring: a `cpu` profile resolves every GPU default as the
    AMD row does, because that is what every GPU default was before the
    profiles existed. This is the single implementation of that rule.
    """
    chosen = profile if profile is not None else current_profile()
    return chosen if chosen.gpu else AMD


def effective_max_busy_percent(
    configured: int | None, profile: AccelerationProfile | None = None
) -> int | None:
    """The utilization limit the card gate applies, or `None` for no limit.

    An explicit `gpu_max_busy_percent` wins on every profile. Unset, it is the
    GPU-settings profile's default: 20 for AMD (and `cpu`, which resolves as
    AMD), none for NVIDIA.
    """
    if configured is not None:
        return configured
    return gpu_settings_profile(profile).max_busy_percent_default
