# Cognita 16.1.8 release notes

On AMD GPUs, Cognita could store wrong vectors for indexed chunks. This release fixes the cause. Projects indexed on an AMD GPU before this release should be rebuilt.

**Latest verified platform installers (16.1.0; these do not include this patch):**
[Windows installer 16.1.0](https://github.com/dbeachy1/Cognita/releases/download/v16.1.0/Cognita-Setup-16.1.0-r1.exe) |
[Linux source installer 16.1.0](https://github.com/dbeachy1/Cognita/releases/download/v16.1.0/cognita-src-16.1.0.tar.gz).

This is a source patch for existing installations. No new Windows executable or installer is supplied by this source patch.

**What was wrong.** The AMD GPU runtime Cognita uses (ONNX Runtime 1.23.1 with MIGraphX) runs each batch on the GPU in the background, then copies the result back. That copy did not reliably wait for the GPU to finish. When the card was slower than the copy, Cognita got whatever was left in the result buffer: usually the previous batch's vectors, sometimes a half-written mix of two batches. Once a card fell behind, it could stay behind for a whole slice, so every batch came back with the batch before it.

**How it showed up.** Cognita's start-up check failed on about 2% of GPU starts, always with the same wrong answer: the warm-up text's vector. 16.1.7 added a check on every batch that refused a batch identical to the previous one. That check ends the worker when a lagging run begins with a clean copy of the previous batch. It never catches a mix, or a lagging run that begins with one, so it reduced the damage without stopping it.

**The fix.** AMD embedding workers now start with `AMD_SERIALIZE_KERNEL=3` and `AMD_SERIALIZE_COPY=3`. These AMD runtime settings make each GPU step finish before the next one starts, so the copy always sees the finished result. On Kei, with two workers per card to make the race likely: without the settings, 13 stale batches and 29 wrong vectors in 4,608; with them, all 14,208 vectors matched the CPU's, and no race was seen in about 3,700 batches that were each run twice to detect one. A single card embeds about 3% slower (47.1 against 48.6 chunks per second). Cognita sets these values when it starts each AMD worker, overriding any inherited value. The 16.1.7 check stays in place as a backstop. The AMD image's runtime check (`scripts/amd_runtime_preflight.py`) sets the same values; there a race could only fail a healthy card, never store a vector.

**What to do.** A project indexed on an AMD GPU before 16.1.8 can hold wrong vectors. Some are copies of other chunks' vectors, and some match no text at all, so the bad ones cannot be picked out reliably. Rebuild each AMD-indexed project with `reindex_documents(project=..., full_rebuild=true)`. On two cards this runs at roughly 90 chunks per second. CPU indexing was not affected. NVIDIA uses a different runtime (CUDA) and was not tested for this race.

MCP arguments, results, and connector generations are unchanged (combined v6, Workspace v3).
