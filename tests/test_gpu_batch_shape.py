"""6.3 — the compiled batch shape, and why it is 4 (DESIGN-6.2 §3.2).

**The bug this closes.** `gpu_batch_size` was 64, justified by a comment reading
"64 measured as fast as anything larger". That was true and beside the point:
nobody had measured anything SMALLER. `gpu_worker.embed_batch` pads every slice
out to the full batch and discards the surplus, so the batch size is also the
FLOOR on what a one-chunk write costs.

Swept on kei card2, one compiled shape per worker so nothing switches programs,
arms interleaved, two runs sharing batch 4 as a control:

    batch   bulk (256-chunk slice)   a 1-chunk write
       2        46.2 chunks/s            0.044s
       4        49.5 chunks/s  BEST      0.081s
       8        45.9 chunks/s            0.176s
      16        48.6 chunks/s            0.329s
      64        35.7 chunks/s  WORST     1.832s

Bulk is flat from 2 to 16 and collapses at 64. Small-write cost is exactly
proportional to the batch — ~0.021-0.029s per PADDED SEQUENCE — so
64 x 0.0286 = 1.83s, which is precisely what a single-chunk write cost in
production. It was padding from end to end.

4 is therefore best at both ends: +39% bulk against 64, and a small write 23x
faster. These specs pin the two constants that encode that, and the reasoning,
so the next person to touch them has to disagree with a measurement rather than
with a preference.
"""

from __future__ import annotations

import pytest

from cognita.config import CognitaConfig
from cognita.gpu_probe import batch_ceiling_gb


def test_the_compiled_shape_is_the_measured_one():
    """🔴 4, not 64. Raising this raises the cost of EVERY small write in
    lockstep, because a short slice is padded out to it — that is the whole
    mechanism, and it is invisible in any benchmark that only sends full
    batches."""
    assert CognitaConfig().gpu_batch_size == 4


def test_a_warm_card_takes_every_chunk():
    """🔴 6.2.1 introduced `gpu_warm_min_chunks` to keep tiny writes off the GPU,
    because a 1-chunk write cost 1.83s against 0.025s on the CPU. With the shape
    at 4 that write costs 0.081s, so the floor is answering a question that no
    longer exists — and it was derived from wall clock alone, ignoring that the
    CPU path pegs 32 cores while the GPU path costs ~0 CPU."""
    assert CognitaConfig().gpu_warm_min_chunks == 0


def test_the_floor_remains_available_for_a_box_with_the_opposite_problem():
    """0 is a default, not a removal. A machine whose cards are contended and
    whose CPU is idle wants the opposite trade, and it stays one config key."""
    assert CognitaConfig(gpu_warm_min_chunks=25).gpu_warm_min_chunks == 25
    with pytest.raises(ValueError, match="gpu_warm_min_chunks must not be negative"):
        CognitaConfig(gpu_warm_min_chunks=-1)


def test_the_vram_gate_follows_the_batch_size_down():
    """The gate's ceiling is DERIVED from the batch size rather than pinned, so
    shrinking the shape shrinks what a device must have free. It must keep
    tracking: a ceiling still sized for batch 64 would refuse cards that the
    smaller shape fits comfortably."""
    assert batch_ceiling_gb(4) < batch_ceiling_gb(64)


def test_a_zero_batch_size_is_still_refused():
    """`range(start, stop, 0)` inside the slicer, per window, mid-walk, after
    the pool has been paid for."""
    with pytest.raises(ValueError, match="gpu_batch_size must be greater than 0"):
        CognitaConfig(gpu_batch_size=0)


def test_the_slice_size_is_not_the_batch_size():
    """Two different numbers that a 6.3 reader will be tempted to collapse now
    that one of them is small. `gpu_slice_chunks` is how much work is handed to
    a worker in one request; `gpu_batch_size` is the compiled tensor shape the
    worker then runs it in. Making the slice as small as the batch would pay the
    per-request cost 128 times per slice."""
    config = CognitaConfig()
    assert config.gpu_slice_chunks == 512
    assert config.gpu_batch_size == 4
    assert config.gpu_slice_chunks > config.gpu_batch_size


# --------------------------------------------------------------------------
# 15.0 (DESIGN-NVIDIA-ACCELERATION §3): two GPU keys default to "the profile's"
# --------------------------------------------------------------------------


def test_the_provider_and_sequence_length_default_to_the_profiles():
    """"" and -1 are the shipped defaults: resolved to a concrete value in ONE
    place (`GpuWorker.start()`), per acceleration profile. The MIGraphX literals
    they replaced now live in the AMD row of the profile table."""
    config = CognitaConfig()
    assert config.gpu_provider == ""
    assert config.gpu_fixed_seq_len == -1


def test_an_empty_provider_is_valid_and_explicit_ones_still_are():
    for value in ("", "migraphx", "rocm", "cuda", "CUDA"):
        assert CognitaConfig(gpu_provider=value).gpu_provider == value


def test_an_unknown_provider_is_still_refused_at_load():
    """The validator is what keeps a typo from ever reaching the worker launch:
    there is no 'silent MIGraphX' path behind it."""
    with pytest.raises(ValueError, match="gpu_provider must be one of"):
        CognitaConfig(gpu_provider="tensorrt")


def test_every_meaningful_sequence_length_is_valid():
    """-1 = the profile's, 0 = do not pin (it used to be refused as
    non-positive), anything positive = a token length."""
    for value in (-1, 0, 128, 512):
        assert CognitaConfig(gpu_fixed_seq_len=value).gpu_fixed_seq_len == value


def test_a_sequence_length_below_minus_one_is_refused():
    with pytest.raises(ValueError, match="gpu_fixed_seq_len must be -1"):
        CognitaConfig(gpu_fixed_seq_len=-2)
