# Cognita 16.1.7 release notes

On AMD GPUs, the GPU sometimes returned the previous batch's vectors instead of computing the current batch. Cognita now catches this on every batch. It no longer relies on the one check made at start-up.

**Latest verified platform installers (16.1.0; these do not include this patch):**
[Windows installer 16.1.0](https://github.com/dbeachy1/Cognita/releases/download/v16.1.0/Cognita-Setup-16.1.0-r1.exe) |
[Linux source installer 16.1.0](https://github.com/dbeachy1/Cognita/releases/download/v16.1.0/cognita-src-16.1.0.tar.gz).

This is a source patch for existing installations. No new Windows executable or installer is supplied by this source patch.

**What was happening.** Each GPU worker embeds one warm-up batch, then a short check sentence that is compared against the CPU's answer. On a two-card AMD (MIGraphX) system, about 2% of worker start-ups failed that check on either card, and every failure gave exactly the same wrong answer. Embedding the warm-up text on the CPU gives exactly that answer, so in those runs the GPU had returned the warm-up batch's output instead of computing the check sentence. The start-up check rejected the card each time, so nothing wrong was stored then. Later batches had no such check, so the same fault partway through indexing would have stored one batch's vectors under the next batch's chunks without any error.

**What changes.** The GPU worker compares every batch with the batch before it. If a whole batch comes back identical to the previous batch while the text differs, the worker stops (exit code 6, "stale output"). That slice is then embedded on another card, or on the CPU if no other card is left. Nothing from the refused batch is stored. A batch whose text repeats exactly is allowed, and so is a single repeated vector, because two texts that differ only in case or spacing can embed identically. A refusal at start-up is still classed as a failed start-up check, as before.

**Start-up check.** The check now also rejects a card that returns NaN or infinite values. Before, such a vector could pass, because a NaN never compares as greater than the limit. When the check fails, the log line now includes the cosine similarity and the length of the returned vector, so it shows what kind of wrong answer came back.

**Existing indexes.** Kei's logs since 2026-10-13 show this fault only at start-up, where the check caught it. A mid-walk occurrence before 16.1.7 would have left no trace in the logs. If search results on an AMD GPU-indexed project look wrong for particular chunks, request `reindex_documents(project=..., full_rebuild=true)` for that project. This release does not start a rebuild automatically.

MCP arguments, results, and connector generations are unchanged (combined v6, Workspace v3).
