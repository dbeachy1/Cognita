"""How much embedding work is a job about to do? (DESIGN-6.0 §4)

The GPU decision is made ONCE, up front, over the whole job — never per file
and never partway through. This module is that estimate, and nothing else: it
reads file sizes and a snapshot of what is already indexed, and returns a
number and a verdict. It touches no store, loads no model, and knows nothing
about devices.

**Why an estimate at all.** Spinning up a GPU worker costs real time (~3s
measured directly on kei, 2026-09-07, program cache warm: process spawn to
both cards' ready handshake, no confounds — see `config.py`'s `gpu_min_chunks`
comment), so work too small to amortize it must stay on the CPU. Deciding that
needs a chunk count *before* anything is parsed, because parsing the corpus to
find out how much parsing to do defeats the purpose.

**Why it is nearly exact rather than heuristic.** Cognita's chunker is
character-based, not token-based:

    advance      = chunk_size - chunk_overlap        # 1000 - 200 = 800
    chunks(file) ~= extractable_text_bytes / advance

For text, markdown and source, `st_size` stands in for text length within a few
percent and is already known from the walk. Structure-aware markdown chunking
splits at section boundaries and yields MORE chunks than the formula, never
fewer, so the estimate is a lower bound.

🔴 **The unit is the JOB — the set of files one caller is about to index — not
the file** (§4.4). A per-file loop makes a locally correct decision every time
and never uses the GPU once: `copy_directory` importing 500 documents is 500
decisions over a handful of chunks each, all correctly under the threshold,
while the aggregate is exactly the workload the feature exists for. Nothing in
a per-file rule is wrong; the shape of the loop is what loses.

The three exclusions in `estimate_job` are all easy to forget and each is
silent when wrong. They are individually commented for that reason.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .parsing import TIER_EMBEDDED, TIER_REGISTERED, ExtensionPolicy

# Formats whose file size says nothing about how much text they contain
# (§4.2). A 200 KB PDF may be a scanned page or a 500-page manual.
BINARY_FORMATS = frozenset({".pdf", ".docx", ".xlsx", ".pptx"})

# What the design calls the break-even point: below this, spin-up dominates.
# 🔴 20, matching `config.py`'s `gpu_min_chunks` (2026-09-07) — a direct,
# uncontrolled-nothing measurement on kei put cold start (spawn to both cards'
# ready handshake) at 2.95s with the program cache warm, not the ~33s an
# earlier uncached measurement implied. See that key's comment for the
# break-even arithmetic. This constant disagreeing with `config.py`'s default
# meant any RetrievalCore not built by __main__ (a test, a script, a future
# stdio host) silently used whatever threshold was stale here — keep the two
# in sync.
DEFAULT_MIN_CHUNKS = 20


@dataclass
class WorkEstimate:
    """What a job is about to embed, and where it should run."""

    files: int = 0                 # files the job will actually parse
    considered: int = 0            # files the job was handed
    est_chunks: int = 0
    binary_formats: int = 0
    skipped_unchanged: int = 0
    skipped_deindexed: int = 0
    skipped_registered: int = 0
    threshold: int = DEFAULT_MIN_CHUNKS
    reasons: list[str] = field(default_factory=list)

    @property
    def use_gpu(self) -> bool:
        """§4.3's rule.

        A binary-format file in the set is on its own sufficient. The costs are
        wildly asymmetric: guessing "large" when the file was small wastes one
        spin-up — seconds — while guessing "small" when it was a 500-page manual
        costs minutes of CPU grinding. At roughly 20:1 the uncertain case
        resolves to GPU, and the rule DELETES per-format special-casing rather
        than adding it.
        """
        return self.binary_formats > 0 or self.est_chunks > self.threshold

    @property
    def decision(self) -> str:
        return "gpu" if self.use_gpu else "cpu"

    def as_log_fields(self) -> dict:
        """The §14.1 `embed.plan` fields this estimate is responsible for."""
        return {
            "files": self.files,
            "est_chunks": self.est_chunks,
            "threshold": self.threshold,
            "binary_formats": self.binary_formats,
            "skipped_unchanged": self.skipped_unchanged,
        }


def advance_for(chunk_size: int, chunk_overlap: int) -> int:
    """Characters each chunk moves forward. Derived, never hardcoded (§4.1)."""
    return max(1, chunk_size - chunk_overlap)


def estimate_file_chunks(size_bytes: int, advance: int) -> int:
    """Lower bound on the chunks a file of this size will produce.

    At least one: a file with any content at all yields a chunk, and integer
    division would otherwise call every file under `advance` bytes free.
    """
    if size_bytes <= 0:
        return 0
    return max(1, size_bytes // advance)


def estimate_job(
    files,
    documents_dir: Path,
    *,
    chunk_size: int,
    chunk_overlap: int,
    known: dict | None = None,
    force: bool = False,
    suppressed=None,
    policy: ExtensionPolicy | None = None,
    threshold: int = DEFAULT_MIN_CHUNKS,
) -> WorkEstimate:
    """Estimate the embedding work in one job.

    `known` is the store's existing-rows snapshot (source -> record), used ONLY
    to skip files whose stat is unchanged. `suppressed` is the project's
    de-index list. Both are optional so a caller with neither — a fresh import,
    say — gets a straight size-based estimate.
    """
    advance = advance_for(chunk_size, chunk_overlap)
    suppressed = suppressed or set()
    known = known or {}
    est = WorkEstimate(threshold=threshold)

    for filepath in files:
        est.considered += 1
        try:
            source = Path(filepath).relative_to(documents_dir).as_posix()
        except ValueError:  # not under documents_dir; count it rather than crash
            source = Path(filepath).name

        # 🔴 De-indexed paths (5.7). `deindexed.json` suppresses these from
        # every walk, so counting them describes a different set of files than
        # the one that will be embedded.
        if source in suppressed:
            est.skipped_deindexed += 1
            continue

        # Registered-tier files are stored whole and NEVER embedded (4.4), so
        # they contribute zero chunks however large they are. Not in §4.3's
        # list, but the same class of error: a project of source files would
        # otherwise estimate high and embed nothing.
        suffix = Path(filepath).suffix.lower()
        new_tier = policy.tier_for(suffix) if policy is not None else TIER_EMBEDDED
        if new_tier == TIER_REGISTERED:
            est.skipped_registered += 1
            continue

        try:
            size = Path(filepath).stat().st_size
        except OSError:
            continue

        # 🔴 force=True skips NOTHING, so the estimate must not apply the skip
        # logic at all. An estimator that filters on mtime under a forced
        # rebuild returns near-zero for the single largest job Cognita ever
        # performs and routes it to the CPU — the exact inversion of what this
        # feature is for, and silent when wrong.
        if not force:
            record = known.get(source)
            # 🔴 A TIER CHANGE BEATS THE MTIME SKIP, exactly as the walk's own
            # rule does (retrieval.py's `retier`). This is the same inversion as
            # the `force` case above, reached by a different road and just as
            # silent: move an extension back from `registered_extensions` to
            # `indexed_extensions` and every one of those files is byte-identical
            # on disk, so mtime and size both say "unchanged" while the walk is
            # about to re-embed all of them. Without this term the estimate for
            # the largest job the product performs is ~0 chunks and the whole
            # migration runs on the CPU. `SourceInfo.tier` exists for precisely
            # this comparison and the estimator was the one caller not making it.
            retier = record is not None and record.tier != new_tier
            if record is not None and not retier and _stat_unchanged(record, filepath, size):
                est.skipped_unchanged += 1
                continue

        est.files += 1
        if suffix in BINARY_FORMATS:
            # §4.2: no relationship between file size and text volume, and no
            # yield factor is attempted. Presence is sufficient reason for GPU.
            est.binary_formats += 1
            est.reasons.append(f"binary:{source}")
            continue
        est.est_chunks += estimate_file_chunks(size, advance)

    return est


def _stat_unchanged(record, filepath: Path, size: int) -> bool:
    """The walk's mtime+size short-circuit, without the content hash.

    Deliberately the cheap half only: the hash needs the file parsed, and an
    estimate that parsed the corpus to decide how to parse the corpus would
    have no reason to exist. Over-estimating a touched-but-identical file costs
    one file's worth of chunks in the total, which the threshold absorbs.
    """
    stored_mtime = getattr(record, "file_mtime", None)
    stored_size = getattr(record, "file_size", None)
    if stored_mtime is None or stored_size is None:
        return False
    if stored_size != size:
        return False
    try:
        return abs(Path(filepath).stat().st_mtime - stored_mtime.timestamp()) < 1e-3
    except (OSError, AttributeError):
        return False
