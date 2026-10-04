"""Which GPUs exist, and is any of them free? (DESIGN-6.0 §6)

🔴 **Nothing here may assume a particular machine's GPU layout, count, vendor or
device indexing.** Cognita runs on laptops with no GPU, on Windows boxes with no
`/sys` at all, and on servers with an arbitrary mix of discrete cards and
integrated graphics. Discovery is dynamic and the no-GPU answer is ORDINARY,
not exceptional — `devices()` returning `[]` is the common case and never an
error.

Two implementations ship, tried in order, each returning `[]` rather than
raising when it does not apply:

- **`SysfsAmdProbe`** — walks `/sys/class/drm/card*/device`. No library, no
  root, no vendor CLI. That last point is not fastidiousness: the reference
  deployment has a complete, working ROCm 7.1 installed from the distribution
  with **no `rocm-smi` and no `amd-smi` shipped at all**, so a probe that
  shelled out to vendor tooling would report a perfectly good machine as having
  no GPUs.
- **`NullProbe`** — the default everywhere else, Windows included.

15.0 adds a third, **`NvmlProbe`**, for the NVIDIA profile (DESIGN-NVIDIA-
ACCELERATION §4). Under WSL there is no DRM card for the GPU (the device is
`/dev/dxg`) and on plain Linux the proprietary driver exposes no VRAM counters
in sysfs, but `libnvidia-ml.so.1` is present in both cases and needs no root and
no vendor CLI — the same role sysfs plays for AMD. `default_probe(profile)`
picks between them by profile.

🔴 **A device's IDENTITY is its PCI address, never its sysfs card number.**
The reference box enumerates `card1`, `card2`, `card3` — there is no `card0` —
while HIP ordinal 0 is `card1`, confirmed by observing which card's VRAM moved
during a single-device run (§12.3, correction 4). An implementation that passed
the sysfs index through as an execution-provider `device_id` would address the
wrong card, or one that does not exist.

The ordinal is therefore resolved by the WORKER, which is the only process with
a GPU runtime to ask; this module never guesses one. The parent's environment is
CPU-only by construction (§11).
"""

from __future__ import annotations

import atexit
import ctypes
import logging
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol

from .acceleration_profiles import (
    AccelerationProfile,
    current_profile,
    effective_max_busy_percent,
    gpu_settings_profile,
)

log = logging.getLogger("cognita.gpu")

GIB = 1024 ** 3
DRM_ROOT = Path("/sys/class/drm")


@dataclass(frozen=True)
class GpuDevice:
    """One discovered device. Identity first, ordinal never."""

    sysfs_name: str            # "card1" — diagnostic only, NEVER an index
    pci_address: str           # "0000:03:00.0" — the join key the worker binds by
    unique_id: str | None      # absent on integrated GPUs
    name: str
    vram_total: int
    vram_free: int
    busy_percent: int | None   # None when the counter is unavailable

    @property
    def vram_free_gb(self) -> float:
        return self.vram_free / GIB

    @property
    def vram_total_gb(self) -> float:
        return self.vram_total / GIB

    def describe(self) -> str:
        return (f"{self.sysfs_name}[{self.pci_address}] "
                f"{self.vram_free_gb:.2f}/{self.vram_total_gb:.2f}GiB free")


class GpuProbe(Protocol):
    def devices(self) -> list[GpuDevice]: ...


class NullProbe:
    """No GPUs. The default, and the correct answer on most machines."""

    def devices(self) -> list[GpuDevice]:
        return []


class SysfsAmdProbe:
    """AMD devices via `/sys/class/drm`. Read-only, unprivileged, no vendor CLI."""

    def __init__(self, root: Path | None = None):
        self.root = root or DRM_ROOT

    def devices(self) -> list[GpuDevice]:
        try:
            if not self.root.is_dir():
                return []
            cards = sorted(self.root.glob("card*"))
        except OSError:
            return []

        found: list[GpuDevice] = []
        for card in cards:
            # card1-DP-1 and friends are CONNECTORS, not cards. They sit in the
            # same directory and would otherwise be counted as devices.
            if "-" in card.name:
                continue
            device_dir = card / "device"
            total = _read_int(device_dir / "mem_info_vram_total")
            if total is None:
                # The marker for "this is a GPU with VRAM". Anything without it
                # is not a device we can use, whatever else it may be.
                continue
            used = _read_int(device_dir / "mem_info_vram_used") or 0
            pci = _pci_address(device_dir)
            if pci is None:
                log.debug("Skipping %s: no resolvable PCI address", card.name)
                continue
            found.append(GpuDevice(
                sysfs_name=card.name,
                pci_address=pci,
                unique_id=_read_text(device_dir / "unique_id"),
                name=_device_name(device_dir, card.name),
                vram_total=total,
                vram_free=max(0, total - used),
                busy_percent=_read_int(device_dir / "gpu_busy_percent"),
            ))
        # 🔴 6.4: SORTED BY PCI ADDRESS, because this order is now a USER-FACING
        # INDEX (`gpu_cards: [0, 1]`) and the glob's order is not fit to be one.
        # `sorted(glob("card*"))` is lexicographic on the NAME, so a box that
        # enumerates ten cards orders them card1, card10, card2 — and the index
        # a user wrote against yesterday's list silently means a different card
        # today. The PCI address is this module's declared identity, it is what
        # the index resolves to, and it is stable across reboots, so ordering by
        # it makes the index mean the same thing every time.
        #
        # ⚠️ This is an ORDERING of the same set, not a filter, and it is NOT
        # the provider's ordinal — see the module docstring. The reference box
        # is unaffected: card1=0000:03:00.0, card2=0000:07:00.0,
        # card3=0000:7e:00.0 sort identically under both rules.
        found.sort(key=lambda d: d.pci_address)
        return found


# --------------------------------------------------------------------------
# NVML (DESIGN-NVIDIA-ACCELERATION §4)
# --------------------------------------------------------------------------


NVML_SONAME = "libnvidia-ml.so.1"
_NVML_SUCCESS = 0


class _NvmlPciInfo(ctypes.Structure):
    """`nvmlPciInfo_t`, the layout `nvmlDeviceGetPciInfo_v3` fills in."""

    _fields_ = [
        ("busIdLegacy", ctypes.c_char * 16),
        ("domain", ctypes.c_uint),
        ("bus", ctypes.c_uint),
        ("device", ctypes.c_uint),
        ("pciDeviceId", ctypes.c_uint),
        ("pciSubSystemId", ctypes.c_uint),
        ("busId", ctypes.c_char * 32),
    ]


class _NvmlMemory(ctypes.Structure):
    """`nvmlMemory_t` (the v1 struct: total, free, used, in bytes)."""

    _fields_ = [
        ("total", ctypes.c_ulonglong),
        ("free", ctypes.c_ulonglong),
        ("used", ctypes.c_ulonglong),
    ]


class _NvmlUtilization(ctypes.Structure):
    """`nvmlUtilization_t`: percent of the last sample period."""

    _fields_ = [("gpu", ctypes.c_uint), ("memory", ctypes.c_uint)]


def _load_nvml():
    """`libnvidia-ml.so.1`, or None. A missing library is an ORDINARY answer.

    WSL ships it in /usr/lib/wsl/lib; on Linux and inside a container the
    NVIDIA container toolkit injects it beside libcuda. Windows has no such
    path in this design (Setup detects the card through WSL), so it is None
    there rather than a guess at nvml.dll.
    """
    if os.name != "posix":
        return None
    try:
        return ctypes.CDLL(NVML_SONAME)
    except OSError:
        return None


_register_atexit: Callable = atexit.register
_warned_once: set[str] = set()


def _warn_once(key: str, message: str, *args) -> None:
    """One WARNING per distinct problem; the repeats go to DEBUG.

    `_vram_free_settled` polls every 0.25s, so a persistent NVML fault reported
    at WARNING on every call would bury the log in one line.
    """
    if key in _warned_once:
        log.debug(message, *args)
        return
    _warned_once.add(key)
    log.warning(message, *args)


class _NvmlSession:
    """One NVML initialization for the life of the process (design §4).

    Measured under WSL: `nvmlInit` + one query + `nvmlShutdown` costs 13.5 ms,
    a query on a kept handle 0.11 ms, and the worker's `_vram_free_settled`
    polls every 0.25s. So init runs ONCE, lazily, on the first `devices()`
    call, the library handle is kept, and `nvmlShutdown` runs from `atexit`.
    NVML opens no device context and allocates no VRAM, so this keeps
    DESIGN-6.0 §11's rule (the service imports no GPU RUNTIME): this is the
    monitoring library, the role sysfs plays for AMD.

    A load or init failure is cached as "unavailable" and never retried: a
    retry per poll would repeat the same failure four times a second.
    """

    def __init__(self, loader: Callable[[], object | None], *, register_exit: bool = True):
        self._loader = loader
        # The process-wide session (below) is shut down by a hook registered at
        # MODULE IMPORT instead, so that it runs after the warm pool's atexit
        # backstop; see `_shutdown_shared_session_at_exit`.
        self._register_exit = register_exit
        self._lock = threading.Lock()
        self._lib: object | None = None
        self._state = "new"  # new | ready | unavailable

    def lib(self):
        with self._lock:
            if self._state == "ready":
                return self._lib
            if self._state == "unavailable":
                return None
            return self._initialize()

    def _initialize(self):
        # Caller holds the lock.
        try:
            lib = self._loader()
        except Exception as exc:
            log.warning("nvml.unavailable stage=load error=%s: %s",
                        type(exc).__name__, exc)
            self._state = "unavailable"
            return None
        if lib is None:
            log.info("nvml.unavailable stage=load reason=library_missing "
                     "library=%s", NVML_SONAME)
            self._state = "unavailable"
            return None
        rc = _call(getattr(lib, "nvmlInit_v2", None))
        if rc != _NVML_SUCCESS:
            log.warning("nvml.unavailable stage=init rc=%s", rc)
            self._state = "unavailable"
            return None
        self._lib = lib
        self._state = "ready"
        if self._register_exit:
            _register_atexit(self.shutdown)
        log.info("nvml.ready library=%s (one handle kept for the process)",
                 NVML_SONAME)
        return lib

    def shutdown(self) -> None:
        with self._lock:
            if self._state != "ready":
                return
            lib, self._lib, self._state = self._lib, None, "unavailable"
        rc = _call(getattr(lib, "nvmlShutdown", None))
        log.debug("nvml.shutdown rc=%s", rc)


_SHARED_SESSION: _NvmlSession | None = None
_SHARED_LOCK = threading.Lock()


def _shared_session() -> _NvmlSession:
    global _SHARED_SESSION
    with _SHARED_LOCK:
        if _SHARED_SESSION is None:
            # Looked up at call time so a test can replace `_load_nvml`.
            _SHARED_SESSION = _NvmlSession(lambda: _load_nvml(), register_exit=False)
        return _SHARED_SESSION


def _shutdown_shared_session_at_exit() -> None:
    """`nvmlShutdown` for the process-wide session, at interpreter exit.

    🔴 ORDER MATTERS (15.0 review). `atexit` runs handlers newest first, and
    `gpu_warm` registers `WARM.shutdown_now` as the backstop that reaps a parked
    pool. Reaping calls `terminate()`, which waits on `_vram_free_settled()`,
    which asks THIS probe. If NVML were shut down first, the probe would list no
    devices, every worker's teardown would wait out its full settle timeout and
    log a false "not released". The session used to register its shutdown
    lazily, on first use, which is AFTER `gpu_warm` registered, so it ran first.
    Registering this hook when the module is imported (and `gpu_warm` importing
    this module before it registers) makes it run after the backstop.
    """
    session = _SHARED_SESSION
    if session is not None:
        session.shutdown()


_register_atexit(_shutdown_shared_session_at_exit)


def _call(fn, *args) -> int | None:
    """Invoke one NVML function. The return code, or None if it could not run.

    Nothing raises out of the probe: a missing symbol, a fake that misbehaves
    and a garbage return type are all "this call failed".
    """
    if fn is None:
        return None
    try:
        rc = fn(*args)
    except Exception as exc:
        log.debug("nvml call failed: %s: %s", type(exc).__name__, exc)
        return None
    return rc if isinstance(rc, int) and not isinstance(rc, bool) else None


def _normalize_pci_bus_id(bus_id: str) -> str | None:
    """`00000000:01:00.0` -> `0000:01:00.0` (the sysfs form, lowercase).

    This is the join key the whole system uses; the worker's cudart read-back
    already produces the 4-digit-domain form, so the two agree without any
    special-casing on either side. Returns None for anything that is not a
    `domain:bus:device.function` address.
    """
    parts = bus_id.strip().lower().split(":")
    if len(parts) == 3:
        domain, bus, tail = parts
    elif len(parts) == 2:  # no domain field at all
        domain, (bus, tail) = "0", parts
    else:
        return None
    if not domain or not bus or "." not in tail:
        return None
    try:
        int(domain, 16), int(bus, 16), int(tail.replace(".", ""), 16)
    except ValueError:
        return None
    return f"{domain[-4:].zfill(4)}:{bus}:{tail}"


class NvmlProbe:
    """NVIDIA devices via NVML (DESIGN-NVIDIA-ACCELERATION §4). Never raises.

    Same `GpuDevice`, no new fields:

    - `pci_address` — `nvmlDeviceGetPciInfo_v3`'s `busId`, normalized to the
      sysfs form. Identity, exactly as for AMD.
    - `unique_id` — the NVML UUID with the `GPU-` prefix STRIPPED, so
      `f"GPU-{unique_id}"` (what `acceleration.py`, `ocr_service` and the worker
      environment already build) is precisely the CUDA-visible-devices form.
    - `sysfs_name` — `gpu<index>`; a diagnostic label only, as the dataclass
      comment says.

    Every failing NVML call yields None for that field or skips that device.
    A device whose memory cannot be read is not a device (mirrors the AMD
    rule), and `busy_percent` is None — not zero — when utilization cannot be
    read. Sorted by PCI address, because that order is the user-facing card
    index (see `SysfsAmdProbe.devices`).
    """

    def __init__(self, loader: Callable[[], object | None] | None = None):
        # A caller-supplied loader gets its own session (tests); the default
        # shares ONE initialization across every probe in the process.
        self._session = _NvmlSession(loader) if loader is not None else _shared_session()

    def devices(self) -> list[GpuDevice]:
        lib = self._session.lib()
        if lib is None:
            return []
        count = ctypes.c_uint(0)
        rc = _call(getattr(lib, "nvmlDeviceGetCount_v2", None), ctypes.byref(count))
        if rc != _NVML_SUCCESS:
            _warn_once("count", "nvml.count failed rc=%s; reporting no devices", rc)
            return []

        found: list[GpuDevice] = []
        for index in range(count.value):
            device = self._read_device(lib, index)
            if device is not None:
                found.append(device)
        found.sort(key=lambda d: d.pci_address)
        log.debug("nvml.devices count=%d usable=%d", count.value, len(found))
        return found

    def _read_device(self, lib, index: int) -> GpuDevice | None:
        handle = ctypes.c_void_p()
        rc = _call(getattr(lib, "nvmlDeviceGetHandleByIndex_v2", None),
                   ctypes.c_uint(index), ctypes.byref(handle))
        if rc != _NVML_SUCCESS:
            _warn_once(f"handle{index}", "nvml.device index=%d handle rc=%s; skipped",
                       index, rc)
            return None

        pci = _NvmlPciInfo()
        rc = _call(getattr(lib, "nvmlDeviceGetPciInfo_v3", None), handle,
                   ctypes.byref(pci))
        address = (_normalize_pci_bus_id(pci.busId.decode("utf-8", "replace"))
                   if rc == _NVML_SUCCESS else None)
        if address is None:
            # Identity is the PCI address; without one the device cannot be
            # gated, pinned or bound, whatever else NVML says about it.
            _warn_once(f"pci{index}", "nvml.device index=%d no usable PCI address "
                       "(rc=%s); skipped", index, rc)
            return None

        memory = _NvmlMemory()
        rc = _call(getattr(lib, "nvmlDeviceGetMemoryInfo", None), handle,
                   ctypes.byref(memory))
        if rc != _NVML_SUCCESS:
            _warn_once(f"mem{index}", "nvml.device index=%d pci=%s memory rc=%s; "
                       "skipped (no readable VRAM is not a device)",
                       index, address, rc)
            return None

        uuid_buffer = ctypes.create_string_buffer(96)
        rc = _call(getattr(lib, "nvmlDeviceGetUUID", None), handle, uuid_buffer, 96)
        unique_id: str | None = None
        if rc == _NVML_SUCCESS:
            raw = uuid_buffer.value.decode("utf-8", "replace").strip()
            if raw[:4].upper() == "GPU-":
                raw = raw[4:]
            unique_id = raw or None

        name_buffer = ctypes.create_string_buffer(96)
        rc = _call(getattr(lib, "nvmlDeviceGetName", None), handle, name_buffer, 96)
        name = (name_buffer.value.decode("utf-8", "replace").strip()
                if rc == _NVML_SUCCESS else "") or f"gpu{index}"

        utilization = _NvmlUtilization()
        rc = _call(getattr(lib, "nvmlDeviceGetUtilizationRates", None), handle,
                   ctypes.byref(utilization))
        busy = int(utilization.gpu) if rc == _NVML_SUCCESS else None

        return GpuDevice(
            sysfs_name=f"gpu{index}",
            pci_address=address,
            unique_id=unique_id,
            name=name,
            vram_total=int(memory.total),
            vram_free=int(memory.free),
            busy_percent=busy,
        )


def sysfs_or_null_probe() -> GpuProbe:
    """Today's probe choice for `cpu` and `amd`: sysfs when there is a DRM tree."""
    if os.name != "posix" or not DRM_ROOT.is_dir():
        return NullProbe()
    return SysfsAmdProbe()


def default_probe(profile: AccelerationProfile | None = None) -> GpuProbe:
    """The probe for this machine and profile (design §4).

    Windows gets Null for every profile (Setup detects the card through WSL, and
    the service runs inside the distro). Otherwise `cpu` and `amd` keep today's
    logic, so a CPU-profile deployment on a box with DRM cards keeps listing
    them exactly as it did; only `nvidia` maps to NVML.
    """
    chosen = profile or current_profile()
    if os.name != "posix":
        return NullProbe()
    probe = chosen.probe_factory()
    return probe


# --------------------------------------------------------------------------
# The gate (§6.2)
# --------------------------------------------------------------------------


# §7's ceiling, from the two MEASURED points in §12.3 rather than the arithmetic
# table above them — which measurement showed to be ~1.8x optimistic:
#     batch  64 -> 3.62 GiB
#     batch 256 -> 8.20 GiB
# A straight line through both: a fixed ~2.09 GiB of weights and scratch, plus
# ~0.0239 GiB per unit of batch. Reproduces each measured point to 0.01 GiB.
#
# 15.0: these two constants moved into `acceleration_profiles.AMD` (as
# `ceiling_fixed_gb` / `ceiling_per_batch_gb`) when a second vendor arrived; the
# NVIDIA row carries its own 2.4 / 0.115 (design §1.3). The values are
# unchanged, and `batch_ceiling_gb(n)` with no profile argument still returns
# exactly what it returned before for every profile that is not `nvidia`.


def batch_ceiling_gb(batch_size: int, profile: AccelerationProfile | None = None) -> float:
    """Peak VRAM one worker needs for a forward pass of `batch_size` (§7).

    🔴 **DERIVED, never hardcoded at the call site.** This was a literal `3.62`
    in two places — the gate's default and `/healthz` — which is the measured
    figure for the DEFAULT batch of 64 and wrong for every other value. Raise
    `gpu_batch_size` to fastembed's own default of 256 (the obvious "make it
    faster" knob, and nothing warns) and the gate would admit a card with 7.7
    GiB free to a worker that then needs 8.2: HIP OOMs at inference, the worker
    dies, its slice is retried on the identically-sized second card and dies
    there too, and the walk finishes on the CPU having paid two spin-ups —
    while `/healthz` said `gpu: ready` throughout. On a 32 GiB card that is a
    wasted spin-up; on an 8 GiB card it decides whether the device qualifies at
    all.

    Rounded UP deliberately. Over-estimating skips a marginal device and costs
    a little throughput; under-estimating admits one that cannot do the work.
    """
    # The constants come from `gpu_settings_profile`, so a `cpu` profile (the
    # bare-metal default) uses the AMD row exactly as before 15.0 and only an
    # explicit `nvidia` profile changes the answer. Existing callers pass no
    # profile and need no change.
    chosen = gpu_settings_profile(profile or current_profile())
    size = max(1, int(batch_size or 1))
    ceiling = round(
        chosen.ceiling_fixed_gb + chosen.ceiling_per_batch_gb * size + 0.004, 2
    )
    return ceiling


@dataclass(frozen=True)
class GateResult:
    device: GpuDevice
    qualifies: bool
    reason: str


CARDS_ALL = "all"
CARDS_NONE = "none"


def normalize_cards(value: object) -> str | list[int]:
    """Parse the `gpu_cards` setting into `"all"` or a sorted list of indices.

    Hand-edited YAML, so every rejection names what was wrong AND what to write
    instead — this runs at startup, where the alternative to a good message is a
    stack trace over a config file the user was told to edit themselves.

    Accepted, and these are the only forms:
      `all` / unset  -> "all"          every discovered card (the default)
      `none` / `[]`  -> []             no GPU at all; index on the CPU
      `1`            -> [1]            a bare integer is a one-card list
      `[0, 2]`       -> [0, 2]         several, deduplicated and sorted

    ⚠️ `true`/`false` are NOT accepted even though YAML makes them tempting, and
    the reason is that `gpu_cards: false` reads as "no cards" to a person and
    parses as the integer 0 — i.e. "card 0" — under Python's `bool` being an
    `int`. Silently enabling the first card for someone who typed the word for
    "off" is the worst available outcome, so the check for `bool` comes FIRST,
    before the `int` branch that would otherwise swallow it.
    """
    if value is None:
        return CARDS_ALL
    if isinstance(value, str):
        token = value.strip().lower()
        if token in (CARDS_ALL, "*", ""):
            return CARDS_ALL
        if token in (CARDS_NONE, "off", "cpu"):
            return []
        # 🔴 NUMBERS ARRIVING AS TEXT ARE THE ENVIRONMENT PATH, NOT AN ODDITY.
        # Every key is overridable as `COGNITA_<KEY>` and `_apply_env_overrides`
        # assigns the raw string; the other fields survive that because pydantic
        # coerces "8675" to an int for a field typed `int`. This one is typed
        # `Any` on purpose (see config.py), so nothing coerces it and
        # `COGNITA_GPU_CARDS=0` would arrive here as the string "0" and be
        # rejected as unrecognized — the documented override, broken for every
        # form except the two keywords.
        inner = token.removeprefix("[").removesuffix("]").strip()
        if not inner and token.startswith("["):
            return []
        parts = [p.strip() for p in inner.split(",") if p.strip()]
        if parts and all(p.lstrip("+-").isdigit() for p in parts):
            return normalize_cards([int(p) for p in parts])
        raise ValueError(
            f"gpu_cards: unrecognized value {value!r} — use 'all', 'none', a "
            "card index like 0, or a list like [0, 1]"
        )
    if isinstance(value, bool):
        raise ValueError(
            f"gpu_cards: {str(value).lower()} is not a card list — write 'all' "
            "to use every card or 'none' to use none. (Written as a bare "
            "true/false this would parse as card index "
            f"{int(value)}, which is the opposite of what it looks like.)"
        )
    if isinstance(value, int):
        value = [value]
    if not isinstance(value, (list, tuple)):
        raise ValueError(
            f"gpu_cards: expected 'all', 'none', an index or a list of indices "
            f"(got {type(value).__name__})"
        )
    indices: set[int] = set()
    for item in value:
        if isinstance(item, bool) or not isinstance(item, int):
            raise ValueError(
                f"gpu_cards: {item!r} is not a card index — the list holds "
                "whole numbers, e.g. [0, 1]"
            )
        if item < 0:
            raise ValueError(
                f"gpu_cards: {item} is not a card index — they start at 0"
            )
        indices.add(item)
    return sorted(indices)


@dataclass(frozen=True)
class CardSelection:
    """Which cards the config allows, resolved against what is actually here."""

    pinned: list[str] | None   # PCI addresses; None means "no restriction"
    source: str                # the setting that decided, for the log line
    warnings: list[str]        # indices that named no card — never silent

    @property
    def selects_nothing(self) -> bool:
        return self.pinned is not None and not self.pinned


def resolve_cards(
    devices: list[GpuDevice],
    gpu_cards: object = CARDS_ALL,
    gpu_device_ids: list[str] | None = None,
) -> CardSelection:
    """Turn the user's card indices into PCI addresses (6.4).

    🔴 **THE INDEX IS A POSITION IN *THIS* LIST, AND NOTHING ELSE.** It is not
    the sysfs card number (the reference box starts at `card1` and has no
    `card0`, so those would be off by one), and it is emphatically not the
    execution provider's ordinal — the module docstring records that HIP ordinal
    0 is `card1` there, established by watching which card's VRAM moved. This
    function exists precisely so that a friendly index can be offered without
    any of that leaking into the rest of the system: it resolves to PCI
    addresses immediately, and every layer below still identifies a device the
    only way this module allows.

    The list it indexes is `GpuProbe.devices()`, ordered by PCI address, which
    is the same order `/healthz` prints with the index beside each row. So "what
    do I type for that card" is answered by reading the number Cognita already
    showed you, not by counting anything yourself.

    🔴 **AN INDEX THAT NAMES NO CARD IS A WARNING, NEVER A SILENT DROP.** Asking
    for `[0, 5]` on a two-card box gets card 0 and a warning about 5. The
    dangerous shape is asking for `[5]` alone: the honest result is "no card
    selected", and the CPU fallback then makes that indistinguishable from a
    machine with no GPU — a config typo that costs a 5x slowdown and reports
    nothing. Hence the warning is carried out of here rather than logged and
    forgotten, so `/healthz` can show it too.
    """
    if gpu_device_ids:
        # The exact form wins. Someone who has written PCI addresses has been
        # more specific than someone who wrote an index, and honoring the
        # vaguer setting over the precise one would be perverse.
        return CardSelection(list(gpu_device_ids), "gpu_device_ids", [])

    cards = normalize_cards(gpu_cards)
    if cards == CARDS_ALL:
        return CardSelection(None, "gpu_cards=all", [])
    if not cards:
        return CardSelection([], "gpu_cards=none", [])

    pinned: list[str] = []
    warnings: list[str] = []
    for index in cards:
        if index < len(devices):
            pinned.append(devices[index].pci_address)
        else:
            warnings.append(
                f"gpu_cards names card {index}, but only "
                f"{len(devices)} card(s) were found (valid: "
                f"{'none' if not devices else '0-' + str(len(devices) - 1)})"
            )
    if not pinned and devices:
        warnings.append(
            "gpu_cards selected no existing card — indexing will use the CPU. "
            "Set gpu_cards: all to use every card."
        )
    return CardSelection(pinned, f"gpu_cards={cards}", warnings)


def log_available_cards(config: object, probe: GpuProbe | None = None,
                        logger: logging.Logger | None = None) -> None:
    """Print the card list, with indices, into the startup banner (6.4.1).

    🔴 **THIS IS WHERE A USER LEARNS WHAT `0` AND `1` MEAN.** Before it existed
    the numbering appeared only in `/healthz` and in an `embed.plan` line
    emitted the first time a GPU job ran — so the answer to "which card is 1?"
    required either an HTTP call or having already indexed something. Anyone
    editing `gpu_cards` needs it BEFORE either of those has happened.

    Never raises. This runs inside the startup banner, where a sysfs oddity
    must not be able to stop the service from serving: worst case the operator
    loses one informational block.
    """
    out = logger or log
    try:
        devices = (probe or default_probe()).devices()
        if not devices:
            out.info("GPU cards   : none found — indexing will use the CPU")
            return

        selection = resolve_cards(
            devices,
            getattr(config, "gpu_cards", CARDS_ALL),
            getattr(config, "gpu_device_ids", None),
        )
        results = gate_devices(
            devices,
            batch_ceiling_gb=batch_ceiling_gb(
                getattr(config, "gpu_batch_size", 4) or 4
            ),
            reserve_vram_gb=getattr(config, "gpu_reserve_vram_gb", 4.0),
            max_busy_percent=effective_max_busy_percent(
                getattr(config, "gpu_max_busy_percent", None)
            ),
            pinned=selection.pinned,
            pinned_by=selection.source,
        )
        usable = sum(1 for r in results if r.qualifies)

        enabled = bool(getattr(config, "gpu_enabled", False))
        have_worker = bool(getattr(config, "gpu_venv_python", None))
        if not enabled:
            state = "acceleration OFF (gpu_enabled: false)"
        elif not have_worker:
            state = "acceleration OFF (gpu_venv_python is unset)"
        else:
            state = f"{usable} usable"
        out.info(
            "GPU cards   : %d found, %s  [%s]",
            len(devices), state, selection.source,
        )
        for index, result in enumerate(results):
            device = result.device
            out.info(
                "   card %d  %-6s %-14s %6.2f GB free   %s",
                index, device.sysfs_name, device.pci_address,
                device.vram_free_gb,
                "usable" if result.qualifies else f"unusable: {result.reason}",
            )
        for warning in selection.warnings:
            # 🔴 The startup banner is the earliest place a bad index can be
            # caught, and every other symptom of one is silence plus a slow
            # index. Warning level so it survives a log level of WARNING.
            out.warning("GPU cards   : %s", warning)
        out.info(
            "   Set `gpu_cards` in config/cognita.yaml to choose — the number "
            "after `card` is what to write (e.g. gpu_cards: [0, 1])."
        )
    except Exception:  # pragma: no cover - a banner must not stop a service
        out.debug("GPU card listing failed", exc_info=True)


def gate_devices(
    devices: list[GpuDevice],
    *,
    batch_ceiling_gb: float,
    reserve_vram_gb: float,
    max_busy_percent: int | None,
    pinned: list[str] | None = None,
    pinned_by: str = "gpu_device_ids",
) -> list[GateResult]:
    """Judge every device on its own merits (§6.2).

    🔴 **Evaluate each device independently and use every one that passes. Do
    not pick a single "best".** The case that matters is someone actively
    working on one GPU while another sits idle: per-device evaluation takes the
    idle one and never touches the busy one. A pick-the-best rule gets that
    right by accident and gets "both are free" wrong on purpose.

    **Free VRAM is the primary signal; utilization is secondary**, because they
    detect different things. `gpu_busy_percent` is an instantaneous sample and
    reads 0% between two renders even while another application holds many GB
    of weights resident and is about to start the next one. Resident memory IS
    the signal that a device is spoken for. Utilization only adds the case of a
    device being hammered while holding little. `max_busy_percent=None` skips
    that secondary check (15.0.1: the NVIDIA profile's default, see
    `acceleration_profiles.effective_max_busy_percent`).

    ⚠️ **The gate is advisory, not atomic.** Seconds pass between reading free
    VRAM and a worker allocating it, and another process may take that memory in
    the window. This lowers the probability of a collision; it cannot eliminate
    one. That is why §10 treats an out-of-memory at load or inference as an
    ordinary handled outcome — passing the gate is a good bet, never a
    guarantee, and code that reads as though it were a guarantee is wrong.

    An integrated GPU needs no special case: it will not have the free VRAM to
    pass, so the generic rule excludes it without naming it.
    """
    needed = batch_ceiling_gb + reserve_vram_gb
    results: list[GateResult] = []
    for device in devices:
        if pinned is not None and device.pci_address not in pinned:
            # Name the setting that actually excluded it. This read
            # "not in gpu_device_ids" unconditionally, which since 6.4 is a lie
            # for anyone using `gpu_cards` — and a health endpoint pointing at
            # the wrong config key is worse than one saying nothing.
            results.append(GateResult(device, False, f"not in {pinned_by}"))
            continue
        if device.vram_free_gb < needed:
            results.append(GateResult(
                device, False,
                f"vram_free={device.vram_free_gb:.2f}GB<{needed:.2f}GB",
            ))
            continue
        if (max_busy_percent is not None and device.busy_percent is not None
                and device.busy_percent > max_busy_percent):
            results.append(GateResult(
                device, False,
                f"busy={device.busy_percent}%>{max_busy_percent}%",
            ))
            continue
        results.append(GateResult(device, True, "ok"))
    return results


def qualifying(results: list[GateResult]) -> list[GpuDevice]:
    return [r.device for r in results if r.qualifies]


def skipped_summary(results: list[GateResult]) -> list[str]:
    """The §14.1 `devices_skipped` field: each rejection with its reason.

    A device that silently fails to qualify is indistinguishable from a device
    that was never there, and the two want completely different responses.

    6.4 carries the CARD INDEX here as well as in `devices`. A rejected card is
    exactly the one a user is about to go and edit `gpu_cards` for — "card3 has
    no VRAM free" is not actionable until you know card3 is index 2 — and it is
    the row that by definition cannot be read off the qualifying list.
    """
    index_of = {r.device.pci_address: i for i, r in enumerate(results)}
    return [
        f"{r.device.sysfs_name}[{index_of[r.device.pci_address]}]:{r.reason}"
        for r in results if not r.qualifies
    ]


# --------------------------------------------------------------------------
# sysfs readers — every one of them returns None rather than raising
# --------------------------------------------------------------------------


def _read_text(path: Path) -> str | None:
    try:
        value = path.read_text().strip()
    except OSError:
        return None
    return value or None


def _read_int(path: Path) -> int | None:
    raw = _read_text(path)
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _pci_address(device_dir: Path) -> str | None:
    """The device's PCI address — what the worker matches on.

    Stable across reboots and across the parent/worker process boundary, unlike
    any ordinal either side might invent.

    Read from `device/uevent`'s `PCI_SLOT_NAME`, which is a documented sysfs
    field, in preference to `realpath` of the `device` symlink. Both give the
    same answer on a real machine, but the symlink target is an implementation
    detail of how sysfs happens to be laid out, and depending on it also makes
    the probe untestable anywhere paths cannot contain a colon. Falls back to
    realpath for any kernel that omits the field.
    """
    uevent = _read_text(device_dir / "uevent")
    if uevent:
        for line in uevent.splitlines():
            if line.startswith("PCI_SLOT_NAME="):
                value = line.split("=", 1)[1].strip()
                if value:
                    return value
    try:
        resolved = os.path.basename(os.path.realpath(device_dir))
    except OSError:
        return None
    # realpath of a plain directory returns its own name, which is not an
    # address. Only accept something that looks like one.
    return resolved if ":" in resolved else None


def _device_name(device_dir: Path, fallback: str) -> str:
    """A human label. Best effort — sysfs exposes no friendly name directly, so
    this falls back to PCI ids and finally to the card name. Never fatal: the
    name is for the log line, and identity lives in the PCI address."""
    for candidate in ("product_name", "label"):
        value = _read_text(device_dir / candidate)
        if value:
            return value
    vendor = _read_text(device_dir / "vendor")
    device = _read_text(device_dir / "device")
    if vendor and device:
        return f"pci:{vendor}:{device}"
    return fallback
