# Cognita 16.1.0 release notes

Publishes the audiobook production workflow and fixes missing-document connector error parity.

**Windows installer:** [Cognita Setup 16.1.0](https://github.com/dbeachy1/Cognita/releases/download/v16.1.0/Cognita-Setup-16.1.0-r1.exe)

**Linux source installer:** [Cognita 16.1.0 source installer](https://github.com/dbeachy1/Cognita/releases/download/v16.1.0/cognita-src-16.1.0.tar.gz)

Audiobook support includes registered source import, generation receipts, chapter and whole-book MP3 builds, immutable recipe evidence, retakes, rollback, readers, and scoped backup/restore. Project-storage tools provide exact hash-bound file reads, pagination, and folder indexing policies.

The connected missing-document result exposed `error_code=INVALID_ARGUMENT` only in structured content, while the raw server omitted the code from both representations. Legacy errors now receive the stable code before validation and serialization in the shared result builder and generic proxy/gateway responses. Explicit error codes, diagnostic fields, and `isError=true` are preserved. Generated book and project-storage error contracts remain strict.

The combined connector is v6 with 73 tools; Workspace remains v3, the PostgreSQL schema remains 1, the Workspace metadata schema remains 1, and Toolbox remains 12.6.0. Upgrades from 15.7 require recreating the combined connector to load the v6 catalog. Clients already on v6 do not need recreation for the parity fix.

The full installed suites passed on Linux and the Windows reference engine: 4,394 passed, 142 skipped, and 23 subtests on each. Windows live connector verification passed 137/137 steps plus both explicit missing-document parity cases. The full native Windows suite ran with 4,275 passed and two pre-existing French PowerShell fixture failures; those fixtures were repaired and all three affected checks passed. Existing packaged audiobook workflow evidence was reused for unchanged production code. No macOS qualification is claimed.

Windows Setup, its launcher, and its uninstaller are signed and timestamped by Douglas Beachy through Microsoft Azure Artifact Signing. The release includes one SHA256SUMS and separate build/source-license companions: the WSL build payload, published image manifest, expanded FFmpeg dependency notices, exact-version source access instructions, and matching libkrunfw corresponding source. These are separate from the installers.

[GitHub release and all assets](https://github.com/dbeachy1/Cognita/releases/tag/v16.1.0). The source/package freeze is `e8a85d19816f34c758df6205a09e28b8303982e5`; later release-documentation updates do not change the verified package bytes.
