"""Device discovery and the §6.2 gate.

Every test builds a FAKE sysfs tree, because the whole point of §6.1 is that
discovery must not assume a machine. These run identically on the Windows dev
box (which has no `/sys` at all) and on the Linux deployment, and they cover
layouts the reference machine does not have — a card0, a single card, a card
with no busy counter — precisely because the reference machine is one example
and the code must not be written to it.

The layout the reference machine DOES have is pinned as its own test, because
it is the one that breaks the obvious implementation: cards numbered from 1,
no card0, and an integrated GPU that must drop out without being named.
"""

from __future__ import annotations

import logging
import os
import types

import pytest

from cognita import gpu_probe
from cognita.acceleration_profiles import AMD, CPU, NVIDIA
from cognita.gpu_probe import (
    GIB,
    GpuDevice,
    NullProbe,
    NvmlProbe,
    SysfsAmdProbe,
    batch_ceiling_gb,
    default_probe,
    gate_devices,
    qualifying,
    skipped_summary,
)

pytestmark = pytest.mark.skipif(
    os.name != "posix" and False, reason="fake sysfs works everywhere"
)


def make_card(root, name, *, total=None, used=0, busy=None, uid=None,
              pci="0000:03:00.0", extra=None):
    """Build one fake /sys/class/drm/<name> entry.

    The PCI address goes in `uevent` as `PCI_SLOT_NAME`, exactly as the kernel
    writes it — verified against the reference machine. A colon cannot appear
    in a Windows path, so encoding it as a symlink target (which is how real
    sysfs also happens to express it) would make these tests Linux-only for no
    gain in fidelity.
    """
    device = root / name / "device"
    device.mkdir(parents=True)
    if pci is not None:
        (device / "uevent").write_text(f"DRIVER=amdgpu\nPCI_SLOT_NAME={pci}\n")
    for key, value in (extra or {}).items():
        (device / key).write_text(str(value))
    if total is not None:
        (device / "mem_info_vram_total").write_text(str(total))
        (device / "mem_info_vram_used").write_text(str(used))
    if busy is not None:
        (device / "gpu_busy_percent").write_text(str(busy))
    if uid is not None:
        (device / "unique_id").write_text(uid)
    return device


def probe_for(tmp_path):
    return SysfsAmdProbe(root=tmp_path)


# --------------------------------------------------------------------------
# Discovery
# --------------------------------------------------------------------------


def test_no_sysfs_at_all_is_an_ordinary_empty_answer(tmp_path):
    """A laptop with no GPU, and every Windows box. Not an error."""
    assert SysfsAmdProbe(root=tmp_path / "nope").devices() == []


def test_null_probe_is_empty():
    assert NullProbe().devices() == []


def test_windows_gets_the_null_probe(monkeypatch):
    monkeypatch.setattr(os, "name", "nt")
    assert isinstance(default_probe(), NullProbe)


def test_a_card_without_vram_info_is_not_a_device(tmp_path):
    """Plenty under /sys/class/drm is not a usable GPU. `mem_info_vram_total`
    is the marker; anything lacking it is skipped whatever else it exposes."""
    make_card(tmp_path, "card0", total=None, pci="0000:01:00.0")
    assert probe_for(tmp_path).devices() == []


def test_connectors_are_not_counted_as_cards(tmp_path):
    """card1-DP-1 and friends live in the same directory. Counting them would
    invent devices that do not exist."""
    make_card(tmp_path, "card1", total=8 * GIB, pci="0000:03:00.0")
    for connector in ("card1-DP-1", "card1-DP-2", "card1-Writeback-1"):
        (tmp_path / connector).mkdir()
    devices = probe_for(tmp_path).devices()
    assert [d.sysfs_name for d in devices] == ["card1"]


def test_identity_is_the_pci_address_not_the_card_number(tmp_path):
    """🔴 §12.3 correction 4. The sysfs number is diagnostic only; an
    implementation that used it as an execution-provider device_id would
    address the wrong card, or one that does not exist."""
    make_card(tmp_path, "card1", total=8 * GIB, pci="0000:03:00.0", uid="abc123")
    device = probe_for(tmp_path).devices()[0]

    assert device.pci_address == "0000:03:00.0"
    assert device.unique_id == "abc123"
    assert device.sysfs_name == "card1"
    assert not hasattr(device, "index"), "an ordinal must not be invented here"


def test_free_vram_is_total_minus_used(tmp_path):
    make_card(tmp_path, "card1", total=32 * GIB, used=2 * GIB, pci="0000:03:00.0")
    device = probe_for(tmp_path).devices()[0]
    assert device.vram_free == 30 * GIB
    assert device.vram_free_gb == pytest.approx(30.0)


def test_a_missing_busy_counter_reads_as_unknown_not_zero(tmp_path):
    """None and 0 mean opposite things at the gate: unknown must not be read as
    idle, and must not disqualify either."""
    make_card(tmp_path, "card1", total=32 * GIB, busy=None, pci="0000:03:00.0")
    assert probe_for(tmp_path).devices()[0].busy_percent is None


def test_unique_id_is_optional(tmp_path):
    """Integrated GPUs do not have one; its absence must not drop the device."""
    make_card(tmp_path, "card9", total=2 * GIB, pci="0000:7e:00.0")
    device = probe_for(tmp_path).devices()[0]
    assert device.unique_id is None


def test_a_device_with_no_resolvable_address_is_skipped(tmp_path):
    """Identity is the PCI address. A device we cannot name is one the worker
    could not be told to bind, so reporting it would only produce a device that
    fails later for a reason nobody can see from here."""
    make_card(tmp_path, "card1", total=32 * GIB, pci=None)
    assert probe_for(tmp_path).devices() == []


def test_garbage_in_sysfs_does_not_raise(tmp_path):
    make_card(tmp_path, "card1", total=32 * GIB, pci="0000:03:00.0")
    device_dir = tmp_path / "card1" / "device"
    (device_dir / "gpu_busy_percent").write_text("not-a-number")
    assert probe_for(tmp_path).devices()[0].busy_percent is None


# --------------------------------------------------------------------------
# The reference layout — the one that breaks the obvious implementation
# --------------------------------------------------------------------------


def reference_machine(tmp_path):
    """kei: two 31.86 GiB discrete cards and a 2 GiB integrated one, numbered
    from card1. There is no card0."""
    make_card(tmp_path, "card1", total=34208743424, used=60170240, busy=0,
              uid="937278e4639fc6fe", pci="0000:03:00.0")
    make_card(tmp_path, "card2", total=34208743424, used=60174336, busy=0,
              uid="d3fff72309053e7d", pci="0000:07:00.0")
    make_card(tmp_path, "card3", total=2147483648, used=148217856, busy=0,
              pci="0000:7e:00.0")
    return tmp_path


def test_the_reference_machine_enumerates_from_card1(tmp_path):
    devices = probe_for(reference_machine(tmp_path)).devices()
    assert [d.sysfs_name for d in devices] == ["card1", "card2", "card3"]
    assert [d.pci_address for d in devices] == [
        "0000:03:00.0", "0000:07:00.0", "0000:7e:00.0",
    ]


def test_the_integrated_gpu_drops_out_with_no_special_case(tmp_path):
    """§6.1: 'An integrated GPU needs no special case: it will not have enough
    free VRAM to pass the gate.' Nothing in the gate names it."""
    devices = probe_for(reference_machine(tmp_path)).devices()
    results = gate_devices(devices, batch_ceiling_gb=3.62, reserve_vram_gb=4.0,
                           max_busy_percent=20)

    assert [d.sysfs_name for d in qualifying(results)] == ["card1", "card2"]
    # `[2]` is the 6.4 card index — the number to put in `gpu_cards`. It is
    # carried on rejections precisely because a rejected card is the one
    # someone is about to go and configure, and it cannot be read off the
    # qualifying list by definition.
    assert skipped_summary(results) == ["card3[2]:vram_free=1.86GB<7.62GB"]


# --------------------------------------------------------------------------
# The gate (§6.2)
# --------------------------------------------------------------------------


def device(**kw):
    base = dict(sysfs_name="card1", pci_address="0000:03:00.0", unique_id=None,
                name="gpu", vram_total=32 * GIB, vram_free=32 * GIB,
                busy_percent=0)
    base.update(kw)
    return GpuDevice(**base)


def test_each_device_is_judged_on_its_own(tmp_path):
    """🔴 Not 'pick the best'. The case that matters is someone working on one
    card while another sits idle — per-device evaluation takes the idle one and
    never touches the busy one. Pick-the-best gets that right by accident and
    gets 'both are free' wrong on purpose."""
    busy = device(sysfs_name="card1", vram_free=2 * GIB)
    idle = device(sysfs_name="card2", pci_address="0000:07:00.0")
    results = gate_devices([busy, idle], batch_ceiling_gb=4, reserve_vram_gb=4,
                           max_busy_percent=20)

    assert [d.sysfs_name for d in qualifying(results)] == ["card2"]


def test_both_free_devices_qualify(tmp_path):
    a = device(sysfs_name="card1")
    b = device(sysfs_name="card2", pci_address="0000:07:00.0")
    results = gate_devices([a, b], batch_ceiling_gb=4, reserve_vram_gb=4,
                           max_busy_percent=20)
    assert len(qualifying(results)) == 2, "pick-the-best would take only one"


def test_free_vram_must_cover_batch_plus_reserve():
    tight = device(vram_free=int(7.9 * GIB))
    results = gate_devices([tight], batch_ceiling_gb=4, reserve_vram_gb=4,
                           max_busy_percent=20)
    assert not results[0].qualifies
    assert "vram_free" in results[0].reason


def test_high_utilization_disqualifies():
    results = gate_devices([device(busy_percent=90)], batch_ceiling_gb=4,
                           reserve_vram_gb=4, max_busy_percent=20)
    assert not results[0].qualifies
    assert "busy=90%" in results[0].reason


def test_no_busy_limit_ignores_utilization_but_still_checks_vram():
    """15.0.1: `None` is the NVIDIA default. A desktop card at 30% busy is used;
    a card without the free VRAM is still refused."""
    busy = device(busy_percent=90)
    full = device(sysfs_name="card2", pci_address="0000:07:00.0",
                  vram_free=1 * GIB, busy_percent=0)
    results = gate_devices([busy, full], batch_ceiling_gb=4, reserve_vram_gb=4,
                           max_busy_percent=None)
    assert results[0].qualifies
    assert not results[1].qualifies
    assert "vram_free" in results[1].reason


def test_unknown_utilization_does_not_disqualify():
    """None means the counter is unavailable, not that the card is busy. A
    machine without the counter must still be able to use its GPUs."""
    results = gate_devices([device(busy_percent=None)], batch_ceiling_gb=4,
                           reserve_vram_gb=4, max_busy_percent=20)
    assert results[0].qualifies


def test_resident_memory_beats_an_idle_utilization_reading():
    """§6.2: free VRAM is the PRIMARY signal. gpu_busy_percent reads 0% between
    two renders even while another application holds many GB resident and is
    about to start the next one."""
    hoarding = device(vram_free=1 * GIB, busy_percent=0)
    results = gate_devices([hoarding], batch_ceiling_gb=4, reserve_vram_gb=4,
                           max_busy_percent=20)
    assert not results[0].qualifies


def test_pinning_restricts_to_named_devices():
    """gpu_device_ids lets an operator reserve the rest of the box. It
    overrides discovery; it is never required for it."""
    a = device(sysfs_name="card1", pci_address="0000:03:00.0")
    b = device(sysfs_name="card2", pci_address="0000:07:00.0")
    results = gate_devices([a, b], batch_ceiling_gb=4, reserve_vram_gb=4,
                           max_busy_percent=20, pinned=["0000:07:00.0"])

    assert [d.sysfs_name for d in qualifying(results)] == ["card2"]
    assert "not in gpu_device_ids" in skipped_summary(results)[0]


def test_an_empty_machine_gates_to_nothing():
    assert qualifying(gate_devices([], batch_ceiling_gb=4, reserve_vram_gb=4,
                                   max_busy_percent=20)) == []


def test_every_rejection_carries_a_reason():
    """A device that silently fails to qualify is indistinguishable from one
    that was never there, and the two want different responses."""
    devices = [device(sysfs_name="card1", vram_free=1 * GIB),
               device(sysfs_name="card2", pci_address="0000:07:00.0",
                      busy_percent=99)]
    reasons = skipped_summary(gate_devices(devices, batch_ceiling_gb=4,
                                           reserve_vram_gb=4, max_busy_percent=20))
    assert len(reasons) == 2
    assert all(":" in r and r.split(":", 1)[1] for r in reasons)


# --------------------------------------------------------------------------
# 15.0: NvmlProbe (DESIGN-NVIDIA-ACCELERATION §4)
#
# A fake NVML library injected through the probe's loader seam. It answers the
# same C calls the real one does, writing through `ctypes.byref` arguments the
# way NVML does (`ref._obj` is the ctypes object the reference points at), so
# the probe's real struct handling is what runs. No card, no library, no clock.
# --------------------------------------------------------------------------


class FakeNvml:
    """`libnvidia-ml.so.1` with scripted devices.

    Each device is a dict; a `*_rc` key makes that one call fail with that code,
    and a value of `"garbage"` (or None) is returned instead of an int.
    """

    def __init__(self, devices, init_rc=0, count_rc=0):
        self.devices = devices
        self.init_rc = init_rc
        self.count_rc = count_rc
        self.init_calls = 0
        self.shutdown_calls = 0

    def _dev(self, handle):
        return self.devices[handle.value - 1]

    def nvmlInit_v2(self):
        self.init_calls += 1
        return self.init_rc

    def nvmlShutdown(self):
        self.shutdown_calls += 1
        return 0

    def nvmlDeviceGetCount_v2(self, ref):
        ref._obj.value = len(self.devices)
        return self.count_rc

    def nvmlDeviceGetHandleByIndex_v2(self, index, ref):
        ref._obj.value = index.value + 1  # non-zero, so c_void_p keeps it
        return self.devices[index.value].get("handle_rc", 0)

    def nvmlDeviceGetPciInfo_v3(self, handle, ref):
        dev = self._dev(handle)
        ref._obj.busId = dev["bus_id"].encode()
        return dev.get("pci_rc", 0)

    def nvmlDeviceGetUUID(self, handle, buf, size):
        dev = self._dev(handle)
        buf.value = dev["uuid"].encode()
        return dev.get("uuid_rc", 0)

    def nvmlDeviceGetName(self, handle, buf, size):
        dev = self._dev(handle)
        buf.value = dev["name"].encode()
        return dev.get("name_rc", 0)

    def nvmlDeviceGetMemoryInfo(self, handle, ref):
        dev = self._dev(handle)
        ref._obj.total = dev["total"]
        ref._obj.free = dev["free"]
        ref._obj.used = dev["total"] - dev["free"]
        return dev.get("mem_rc", 0)

    def nvmlDeviceGetUtilizationRates(self, handle, ref):
        dev = self._dev(handle)
        ref._obj.gpu = dev.get("util", 0)
        return dev.get("util_rc", 0)


def nv_card(bus_id="00000000:01:00.0", uuid="GPU-4cd28834-e5a4-6b4e-85aa-3e54bcbf0630",
            name="NVIDIA GeForce RTX 4090", total=24 * GIB, free=22 * GIB,
            util=3, **failures):
    return {"bus_id": bus_id, "uuid": uuid, "name": name, "total": total,
            "free": free, "util": util, **failures}


@pytest.fixture
def nvml_atexit(monkeypatch):
    """Capture what the probe registers with `atexit` instead of registering a
    fake library's shutdown into the real interpreter exit."""
    registered = []
    monkeypatch.setattr(gpu_probe, "_register_atexit", registered.append)
    return registered


def nvml_probe(lib):
    return NvmlProbe(loader=lambda: lib)


def test_a_missing_nvml_library_is_an_ordinary_empty_answer(nvml_atexit, caplog):
    """No NVIDIA driver at all is the common case, exactly like no sysfs."""
    with caplog.at_level(logging.INFO, logger="cognita.gpu"):
        assert NvmlProbe(loader=lambda: None).devices() == []
    assert "library_missing" in caplog.text
    assert nvml_atexit == [], "nothing was initialized, so nothing to shut down"


def test_a_loader_that_raises_is_an_empty_answer_and_is_logged(nvml_atexit, caplog):
    def broken():
        raise RuntimeError("dlopen exploded")

    with caplog.at_level(logging.WARNING, logger="cognita.gpu"):
        assert NvmlProbe(loader=broken).devices() == []
    assert "dlopen exploded" in caplog.text


def test_an_init_failure_is_empty_logged_with_the_code_and_not_retried(
        nvml_atexit, caplog):
    """`_vram_free_settled` polls four times a second; retrying a failed init on
    every poll would repeat one warning four times a second."""
    lib = FakeNvml([nv_card()], init_rc=999)
    probe = nvml_probe(lib)
    with caplog.at_level(logging.WARNING, logger="cognita.gpu"):
        assert probe.devices() == []
        assert probe.devices() == []
    assert lib.init_calls == 1
    warnings = [r.getMessage() for r in caplog.records
                if r.levelno == logging.WARNING]
    assert len(warnings) == 1 and "rc=999" in warnings[0]
    assert nvml_atexit == []


def test_a_device_is_read_with_its_identity_normalized(nvml_atexit):
    """The PCI address is the join key the whole system uses and the worker's
    cudart read-back produces the 4-digit-domain form; the UUID loses `GPU-` so
    `f"GPU-{unique_id}"` is exactly the CUDA-visible-devices value."""
    [dev] = nvml_probe(FakeNvml([nv_card(bus_id="00000000:01:00.0")])).devices()
    assert dev.pci_address == "0000:01:00.0"
    assert dev.unique_id == "4cd28834-e5a4-6b4e-85aa-3e54bcbf0630"
    assert f"GPU-{dev.unique_id}" == "GPU-4cd28834-e5a4-6b4e-85aa-3e54bcbf0630"
    assert dev.name == "NVIDIA GeForce RTX 4090"
    assert dev.sysfs_name == "gpu0", "a diagnostic label, never an index"
    assert dev.vram_total == 24 * GIB
    assert dev.vram_free == 22 * GIB
    assert dev.busy_percent == 3


@pytest.mark.parametrize("raw, expected", [
    ("00000000:01:00.0", "0000:01:00.0"),
    ("00000000:0A:00.0", "0000:0a:00.0"),
    ("0000:7E:00.0", "0000:7e:00.0"),
    ("00000001:81:00.0", "0001:81:00.0"),
    ("01:00.0", "0000:01:00.0"),
    ("  00000000:01:00.0  ", "0000:01:00.0"),
])
def test_pci_bus_ids_are_normalized_to_the_sysfs_form(raw, expected):
    assert gpu_probe._normalize_pci_bus_id(raw) == expected


@pytest.mark.parametrize("raw", ["", "garbage", "zz:01:00.0", "0000:01:00", "a:b:c:d"])
def test_a_bus_id_that_is_not_an_address_is_none(raw):
    assert gpu_probe._normalize_pci_bus_id(raw) is None


def test_two_devices_come_back_sorted_by_pci_address(nvml_atexit):
    """The order is a user-facing index (`gpu_cards: [0, 1]`); NVML's own
    enumeration order is not fit to be one."""
    lib = FakeNvml([
        nv_card(bus_id="00000000:81:00.0", uuid="GPU-b"),
        nv_card(bus_id="00000000:01:00.0", uuid="GPU-a"),
    ])
    devices = nvml_probe(lib).devices()
    assert [d.pci_address for d in devices] == ["0000:01:00.0", "0000:81:00.0"]
    assert [d.unique_id for d in devices] == ["a", "b"]
    assert [d.sysfs_name for d in devices] == ["gpu1", "gpu0"], (
        "the label keeps NVML's own index; only the ORDER follows the address"
    )


def test_a_device_with_no_readable_memory_is_not_a_device(nvml_atexit, caplog):
    """Mirrors the AMD rule: without a VRAM figure the gate cannot judge it."""
    lib = FakeNvml([nv_card(mem_rc=2),
                    nv_card(bus_id="00000000:02:00.0", uuid="GPU-ok")])
    with caplog.at_level(logging.WARNING, logger="cognita.gpu"):
        devices = nvml_probe(lib).devices()
    assert [d.unique_id for d in devices] == ["ok"]
    assert "memory rc=2" in caplog.text


def test_a_device_with_no_pci_address_is_skipped(nvml_atexit):
    lib = FakeNvml([nv_card(pci_rc=3), nv_card(bus_id="not an address")])
    assert nvml_probe(lib).devices() == []


def test_a_failed_handle_lookup_skips_only_that_device(nvml_atexit):
    lib = FakeNvml([nv_card(handle_rc=17),
                    nv_card(bus_id="00000000:02:00.0", uuid="GPU-ok")])
    assert [d.unique_id for d in nvml_probe(lib).devices()] == ["ok"]


def test_a_failed_utilization_read_is_unknown_not_zero(nvml_atexit):
    """Zero would read as 'idle'; unknown must not disqualify or qualify."""
    [dev] = nvml_probe(FakeNvml([nv_card(util=0, util_rc=9)])).devices()
    assert dev.busy_percent is None


def test_a_failed_uuid_read_keeps_the_device_without_an_id(nvml_atexit):
    [dev] = nvml_probe(FakeNvml([nv_card(uuid_rc=9)])).devices()
    assert dev.unique_id is None
    assert dev.vram_free == 22 * GIB


def test_a_failed_name_read_falls_back_to_the_label(nvml_atexit):
    [dev] = nvml_probe(FakeNvml([nv_card(name_rc=9)])).devices()
    assert dev.name == "gpu0"


def test_a_failed_count_is_empty_not_an_error(nvml_atexit, caplog):
    with caplog.at_level(logging.WARNING, logger="cognita.gpu"):
        assert nvml_probe(FakeNvml([nv_card()], count_rc=5)).devices() == []
    assert "count failed rc=5" in caplog.text


@pytest.mark.parametrize("garbage", ["garbage", None, 3.5, True])
def test_garbage_return_codes_never_raise(nvml_atexit, garbage):
    """A misbehaving library (or a fake) returning something that is not an int
    is 'this call failed', never an exception out of `devices()`."""
    for key in ("handle_rc", "pci_rc", "mem_rc", "util_rc", "uuid_rc", "name_rc"):
        nvml_probe(FakeNvml([nv_card(**{key: garbage})])).devices()  # must not raise
    assert nvml_probe(FakeNvml([nv_card()], init_rc=garbage)).devices() == []
    assert nvml_probe(FakeNvml([nv_card()], count_rc=garbage)).devices() == []


def test_a_library_missing_a_symbol_is_empty_not_an_error(nvml_atexit):
    class Bare:
        def nvmlInit_v2(self):
            return 0

    assert nvml_probe(Bare()).devices() == []


def test_the_handle_is_kept_and_nvml_is_initialized_once(nvml_atexit):
    """Measured under WSL: init+query+shutdown 13.5 ms against 0.11 ms for a
    query on a kept handle, and the worker polls every 0.25 s."""
    lib = FakeNvml([nv_card()])
    probe = nvml_probe(lib)
    probe.devices()
    probe.devices()
    assert lib.init_calls == 1
    assert len(nvml_atexit) == 1, "one shutdown hook for the process"


def test_shutdown_runs_once_from_atexit_and_a_late_call_is_empty(nvml_atexit):
    lib = FakeNvml([nv_card()])
    probe = nvml_probe(lib)
    assert len(probe.devices()) == 1
    nvml_atexit[0]()
    nvml_atexit[0]()
    assert lib.shutdown_calls == 1
    assert probe.devices() == [], "after shutdown the probe does not re-init"
    assert lib.init_calls == 1


def test_an_unloadable_default_library_is_empty(monkeypatch, nvml_atexit):
    """The shared session's loader is `_load_nvml`, which returns None off
    posix and when `libnvidia-ml.so.1` cannot be opened."""
    monkeypatch.setattr(gpu_probe, "_SHARED_SESSION", None)
    monkeypatch.setattr(gpu_probe, "_load_nvml", lambda: None)
    assert NvmlProbe().devices() == []


def test_the_default_loader_is_none_off_posix(monkeypatch):
    monkeypatch.setattr(gpu_probe, "os", types.SimpleNamespace(name="nt"))
    assert gpu_probe._load_nvml() is None


# --------------------------------------------------------------------------
# 15.0: default_probe follows the acceleration profile (§4)
# --------------------------------------------------------------------------


def _as_os(monkeypatch, name, drm_root):
    """Make the probe module see `name` as the OS and `drm_root` as /sys/class/drm
    without changing the real `os.name` (pathlib on Windows depends on it)."""
    monkeypatch.setattr(gpu_probe, "os", types.SimpleNamespace(
        name=name, path=os.path))
    monkeypatch.setattr(gpu_probe, "DRM_ROOT", drm_root)


@pytest.mark.parametrize("profile", [CPU, AMD], ids=lambda p: p.name)
def test_cpu_and_amd_keep_todays_probe_logic(monkeypatch, tmp_path, profile):
    """A CPU-profile deployment on a box with DRM cards keeps listing them, so
    cpu is NOT mapped to a plain NullProbe."""
    _as_os(monkeypatch, "posix", tmp_path)
    assert isinstance(default_probe(profile), SysfsAmdProbe)
    _as_os(monkeypatch, "posix", tmp_path / "no-drm-here")
    assert isinstance(default_probe(profile), NullProbe)


def test_nvidia_gets_the_nvml_probe(monkeypatch, tmp_path):
    _as_os(monkeypatch, "posix", tmp_path)
    assert isinstance(default_probe(NVIDIA), NvmlProbe)
    # WSL has no DRM card at all; NVML must not depend on sysfs existing.
    _as_os(monkeypatch, "posix", tmp_path / "no-drm-here")
    assert isinstance(default_probe(NVIDIA), NvmlProbe)


@pytest.mark.parametrize("profile", [CPU, AMD, NVIDIA], ids=lambda p: p.name)
def test_windows_gets_the_null_probe_for_every_profile(monkeypatch, tmp_path, profile):
    _as_os(monkeypatch, "nt", tmp_path)
    assert isinstance(default_probe(profile), NullProbe)


def test_default_probe_with_no_argument_reads_the_environment(monkeypatch, tmp_path):
    _as_os(monkeypatch, "posix", tmp_path)
    monkeypatch.delenv("COGNITA_ACCELERATION_PROFILE", raising=False)
    assert isinstance(default_probe(), SysfsAmdProbe)
    monkeypatch.setenv("COGNITA_ACCELERATION_PROFILE", "nvidia")
    assert isinstance(default_probe(), NvmlProbe)


# --------------------------------------------------------------------------
# 15.0: the gate's ceiling follows the profile's constants (§1.3, §3)
# --------------------------------------------------------------------------


@pytest.mark.parametrize("batch, expected", [(4, 2.86), (16, 4.24), (32, 6.08), (64, 9.76)])
def test_the_nvidia_ceiling_sits_on_or_above_every_measured_point(batch, expected):
    """§1.3 measured 2.36 / 3.42 / 5.48 / 9.41 GB at batch 4 / 16 / 32 / 64 with
    every text at 512 tokens; the line 2.4 + 0.115 x batch must not dip below."""
    ceiling = batch_ceiling_gb(batch, NVIDIA)
    assert ceiling == expected
    measured = {4: 2.36, 16: 3.42, 32: 5.48, 64: 9.41}[batch]
    assert ceiling >= measured


def test_at_batch_four_the_nvidia_gate_needs_about_seven_gb_free():
    assert batch_ceiling_gb(4, NVIDIA) + 4.0 == pytest.approx(6.86, abs=0.01)


def test_the_amd_ceiling_is_unchanged():
    assert batch_ceiling_gb(64, AMD) == 3.62
    assert batch_ceiling_gb(256, AMD) == 8.2


def test_a_cpu_profile_uses_the_amd_ceiling():
    """The bare-metal default keeps yesterday's numbers."""
    assert batch_ceiling_gb(64, CPU) == 3.62
    assert batch_ceiling_gb(4, CPU) == batch_ceiling_gb(4, AMD)


def test_existing_callers_pass_no_profile_and_follow_the_environment(monkeypatch):
    monkeypatch.delenv("COGNITA_ACCELERATION_PROFILE", raising=False)
    assert batch_ceiling_gb(4) == batch_ceiling_gb(4, AMD)
    monkeypatch.setenv("COGNITA_ACCELERATION_PROFILE", "nvidia")
    assert batch_ceiling_gb(4) == 2.86


def test_nvml_shuts_down_after_the_warm_pool_backstop_at_interpreter_exit():
    """15.0 review: `atexit` runs newest first. The NVML session registered its
    shutdown lazily, after `gpu_warm` registered the backstop that reaps a parked
    pool, so NVML went first and every reaped worker waited out its settle
    timeout against a probe that listed nothing. The hook is now registered when
    `gpu_probe` is imported, and `gpu_warm` imports it before registering.

    A fresh interpreter, because registration order is a property of a clean
    import. The timeout is a hang guard on a child that always exits."""
    import subprocess
    import sys

    code = (
        "import atexit\n"
        "order = []\n"
        "real = atexit.register\n"
        "def spy(fn, *a, **k):\n"
        "    order.append(getattr(fn, '__qualname__', repr(fn)))\n"
        "    return real(fn, *a, **k)\n"
        "atexit.register = spy\n"
        "import cognita.gpu_warm\n"
        "nvml = order.index('_shutdown_shared_session_at_exit')\n"
        "warm = order.index('WarmPool.shutdown_now')\n"
        "print('OK' if nvml < warm else 'WRONG ' + repr(order))\n"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True,
                            text=True, timeout=120)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "OK", result.stdout
