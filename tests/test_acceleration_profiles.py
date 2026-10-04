"""The acceleration profile descriptor (DESIGN-NVIDIA-ACCELERATION §3, §14).

Hardware-free and clock-free. What these pin:

- the table is complete for every profile, and the AMD row reproduces the
  literals the tree used before profiles existed (the proof that the refactor
  changed nothing for AMD);
- the NVIDIA row carries the measured numbers of §1.3;
- an unknown name is `cpu` with ONE warning that names the bad value, never an
  exception;
- `gpu_settings_profile` is the single place the "a cpu profile resolves GPU
  settings as amd" rule lives.
"""

from __future__ import annotations

import dataclasses
import logging

import pytest

from cognita import acceleration_profiles as ap
from cognita import gpu_probe
from cognita.acceleration_profiles import (
    AMD,
    CPU,
    GPU_PROFILE_NAMES,
    NVIDIA,
    PROFILE_ENV,
    PROFILES,
    current_profile,
    effective_max_busy_percent,
    gpu_settings_profile,
    profile_named,
)


@pytest.fixture(autouse=True)
def _fresh_warning_state(monkeypatch):
    """The unknown-name warning is once per distinct value per process, so each
    test starts with nothing already warned about and no ambient variable."""
    ap._warned_unknown.clear()
    monkeypatch.delenv(PROFILE_ENV, raising=False)
    yield
    ap._warned_unknown.clear()


# --------------------------------------------------------------------------
# The table
# --------------------------------------------------------------------------


def test_the_table_has_exactly_the_three_profiles_and_each_knows_its_name():
    assert set(PROFILES) == {"cpu", "amd", "nvidia"}
    for key, profile in PROFILES.items():
        assert profile.name == key


@pytest.mark.parametrize("profile", [AMD, NVIDIA], ids=lambda p: p.name)
def test_every_field_is_set_for_a_gpu_profile(profile):
    """No `None` may survive in a row that has a GPU: a None here would surface
    as a `TypeError` inside the worker launch or the gate, on the CPU fallback's
    path, where nobody would notice it. The one exception is
    `max_busy_percent_default`, where `None` is a value: "no utilization gate"
    (NVIDIA, 15.0.1), and `gate_devices` is tested with it."""
    assert profile.gpu is True
    for field in dataclasses.fields(profile):
        if field.name == "max_busy_percent_default":
            continue
        assert getattr(profile, field.name) is not None, (
            f"{profile.name}.{field.name} is unset"
        )
    assert callable(profile.probe_factory)
    assert profile.devices_exposed is True


def test_the_cpu_row_has_no_gpu_values_of_its_own():
    """cpu means "no GPU here": nothing invented to fill the columns. The AMD
    values a cpu deployment still needs come from `gpu_settings_profile`."""
    assert CPU.gpu is False
    assert CPU.devices_exposed is False
    for field in ("embed_provider", "provider_key", "device_env",
                  "fixed_seq_len_default", "ocr_backend", "ceiling_fixed_gb",
                  "ceiling_per_batch_gb", "max_busy_percent_default",
                  "runtime_root", "vendor_label"):
        assert getattr(CPU, field) is None, field
    assert CPU.program_cache is False
    assert CPU.device_env_value("abc") is None


def test_the_amd_row_reproduces_the_pre_refactor_literals():
    """Every value below was a literal in the tree before 15.0: the provider in
    `acceleration.EMBED_PROVIDER` and `gpu_host._provider_name`, the variable in
    `GpuWorker.start`, 512 in `config.gpu_fixed_seq_len`, and the two ceiling
    constants in `gpu_probe`. If one of these moves, AMD behavior moved."""
    assert AMD.embed_provider == "MIGraphXExecutionProvider"
    assert AMD.provider_key == "migraphx"
    assert AMD.device_env == "ROCR_VISIBLE_DEVICES"
    assert AMD.device_env_value("0123abcd") == "GPU-0123abcd"
    assert AMD.fixed_seq_len_default == 512
    assert AMD.program_cache is True
    assert AMD.ocr_backend == "pytorch-rocm"
    assert AMD.ceiling_fixed_gb == 2.093
    assert AMD.ceiling_per_batch_gb == 0.02385
    assert AMD.runtime_root == "/opt/cognita-runtimes"
    assert AMD.vendor_label == "AMD"
    assert AMD.max_busy_percent_default == 20


def test_the_nvidia_row_carries_the_measured_numbers():
    """§1.3: dynamic shapes (pinning to 512 halved CUDA throughput), no program
    cache, and the ceiling re-measured at the model's full 512 tokens — 2.4 GB
    fixed plus 0.115 GB per unit of batch. The first fit's 2.2 / 0.09 was stale."""
    assert NVIDIA.embed_provider == "CUDAExecutionProvider"
    assert NVIDIA.provider_key == "cuda"
    assert NVIDIA.device_env == "CUDA_VISIBLE_DEVICES"
    # NVML strips `GPU-` from the UUID, so the profile puts it back.
    assert NVIDIA.device_env_value("4cd28834-e5a4-6b4e-85aa-3e54bcbf0630") == (
        "GPU-4cd28834-e5a4-6b4e-85aa-3e54bcbf0630"
    )
    assert NVIDIA.fixed_seq_len_default == 0
    assert NVIDIA.program_cache is False
    assert NVIDIA.ocr_backend == "pytorch-cuda"
    assert NVIDIA.ceiling_fixed_gb == 2.4
    assert NVIDIA.ceiling_per_batch_gb == 0.115
    assert NVIDIA.runtime_root == "/opt/cognita-runtimes"
    assert NVIDIA.vendor_label == "NVIDIA"
    # 15.0.1: free VRAM alone decides; Maia's desktop 4090 idles near 30% busy.
    assert NVIDIA.max_busy_percent_default is None


def test_a_profile_is_immutable():
    with pytest.raises(dataclasses.FrozenInstanceError):
        AMD.fixed_seq_len_default = 0  # type: ignore[misc]


def test_the_gpu_profile_names():
    assert GPU_PROFILE_NAMES == {"amd", "nvidia"}
    assert all(PROFILES[name].gpu for name in GPU_PROFILE_NAMES)
    assert not PROFILES["cpu"].gpu


# --------------------------------------------------------------------------
# Name lookup and the environment variable
# --------------------------------------------------------------------------


@pytest.mark.parametrize("raw, expected", [
    ("cpu", "cpu"), ("amd", "amd"), ("nvidia", "nvidia"),
    ("AMD", "amd"), ("  NVIDIA  ", "nvidia"), ("", "cpu"), ("   ", "cpu"),
    (None, "cpu"),
])
def test_names_are_normalized_the_way_main_always_did(raw, expected, caplog):
    with caplog.at_level(logging.WARNING, logger="cognita.gpu"):
        assert profile_named(raw).name == expected
    assert caplog.records == [], "a valid or empty name must not warn"


def test_an_unknown_name_is_cpu_with_one_warning_naming_the_value(caplog):
    with caplog.at_level(logging.WARNING, logger="cognita.gpu"):
        first = profile_named("intel")
        second = profile_named("intel")
    assert first is CPU and second is CPU
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1, [r.getMessage() for r in warnings]
    assert "intel" in warnings[0].getMessage()
    assert "cpu" in warnings[0].getMessage()


def test_a_second_bad_value_gets_its_own_warning(caplog):
    with caplog.at_level(logging.WARNING, logger="cognita.gpu"):
        profile_named("intel")
        profile_named("apple")
    messages = [r.getMessage() for r in caplog.records]
    assert any("intel" in m for m in messages)
    assert any("apple" in m for m in messages)


def test_the_environment_variable_selects_the_profile(monkeypatch):
    assert current_profile() is CPU, "unset means cpu"
    monkeypatch.setenv(PROFILE_ENV, "amd")
    assert current_profile() is AMD
    monkeypatch.setenv(PROFILE_ENV, " NVIDIA ")
    assert current_profile() is NVIDIA
    monkeypatch.setenv(PROFILE_ENV, "")
    assert current_profile() is CPU


def test_a_bad_environment_value_is_cpu_and_never_an_exception(monkeypatch, caplog):
    monkeypatch.setenv(PROFILE_ENV, "rocm-classic")
    with caplog.at_level(logging.WARNING, logger="cognita.gpu"):
        assert current_profile() is CPU
        assert current_profile() is CPU
    warnings = [r.getMessage() for r in caplog.records
                if r.levelno == logging.WARNING]
    assert len(warnings) == 1 and "rocm-classic" in warnings[0]


# --------------------------------------------------------------------------
# gpu_settings_profile: the one home of "cpu resolves GPU settings as amd"
# --------------------------------------------------------------------------


def test_a_cpu_profile_resolves_gpu_settings_as_amd():
    """Before profiles existed EVERY GPU default was the MIGraphX one whatever
    the deployment; a bare-metal `cognita serve` (profile cpu) must keep that."""
    assert gpu_settings_profile(CPU) is AMD


def test_a_gpu_profile_resolves_as_itself():
    assert gpu_settings_profile(AMD) is AMD
    assert gpu_settings_profile(NVIDIA) is NVIDIA


def test_no_argument_means_the_current_profile(monkeypatch):
    assert gpu_settings_profile() is AMD
    monkeypatch.setenv(PROFILE_ENV, "nvidia")
    assert gpu_settings_profile() is NVIDIA


# --------------------------------------------------------------------------
# The probe factories (§4): cpu and amd keep today's logic, nvidia is NVML
# --------------------------------------------------------------------------


def test_cpu_and_amd_use_todays_probe_logic_and_nvidia_uses_nvml(monkeypatch):
    sentinel = object()
    monkeypatch.setattr(gpu_probe, "sysfs_or_null_probe", lambda: sentinel)
    assert CPU.probe_factory() is sentinel
    assert AMD.probe_factory() is sentinel
    assert isinstance(NVIDIA.probe_factory(), gpu_probe.NvmlProbe)


# --------------------------------------------------------------------------
# The utilization gate's default (15.0.1)
# --------------------------------------------------------------------------

def test_unset_busy_limit_is_twenty_on_amd_and_cpu_and_none_on_nvidia():
    assert effective_max_busy_percent(None, AMD) == 20
    # cpu resolves GPU settings as AMD, as every other GPU default does.
    assert effective_max_busy_percent(None, CPU) == 20
    assert effective_max_busy_percent(None, NVIDIA) is None


@pytest.mark.parametrize("profile", [CPU, AMD, NVIDIA], ids=lambda p: p.name)
def test_an_explicit_busy_limit_wins_on_every_profile(profile):
    assert effective_max_busy_percent(35, profile) == 35
    assert effective_max_busy_percent(0, profile) == 0


def test_the_busy_default_follows_the_environment(monkeypatch):
    monkeypatch.setenv(PROFILE_ENV, "nvidia")
    assert effective_max_busy_percent(None) is None
    monkeypatch.setenv(PROFILE_ENV, "amd")
    assert effective_max_busy_percent(None) == 20
