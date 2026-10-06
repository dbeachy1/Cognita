"""Cognita configuration.

Loads config/cognita.yaml (if present) with COGNITA_* environment variable
overrides. All paths are resolved to absolute Paths. See DESIGN.md §5.3.
"""

from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, PrivateAttr, model_validator

from .parsing import (
    DEFAULT_INDEXED_EXTENSIONS,
    DEFAULT_REGISTERED_EXTENSIONS,
    SYNC_CONFLICT_PATTERNS,
)

# Repo root = two levels up from this file's package dir (src/cognita/ -> repo)
REPO_ROOT = Path(__file__).resolve().parents[2]

DEFAULT_CONFIG_PATH = REPO_ROOT / "config" / "cognita.yaml"

# Shared OAuth protocol scope; token state remains package-owned by the child.
OAUTH_SCOPE = "cognita:access"


# 15.0: "" means "the acceleration profile's provider" (amd -> migraphx, nvidia
# -> cuda; the cpu profile resolves it as amd, see acceleration_profiles). It is
# the shipped default; an explicit name is honored as before. The validator
# below is what keeps an unknown value from ever reaching `_provider_name`.
_GPU_PROVIDERS = frozenset({"", "migraphx", "rocm", "cuda"})

# A card UUID as `ocr_gpu_device_ids` takes it: the same pattern as
# `acceleration._UUID` (kept here rather than imported: config is a leaf
# module and acceleration pulls in the probe and the verifier). AMD `GPU-<hex>`; NVIDIA `GPU-<hex>-<hex>-...`.
_GPU_UUID = re.compile(r"^GPU-[0-9a-f]+(-[0-9a-f]+)*$", re.I)


class CognitaConfig(BaseModel):
    """Top-level Cognita settings (gateway + admin)."""

    # The Admin may replace ``public_base_url`` with the effective persisted
    # identity at runtime.  Keep the deployment seed separately so a missing
    # override never turns the last effective value into a fake deployment
    # default.  This is process-local metadata and must not be serialized.
    _deployment_public_base_url: str | None = PrivateAttr(default=None)

    mcp_host: str = "127.0.0.1"  # cloudflared runs on this box; no LAN exposure needed
    mcp_port: int = 8675  # public MCP endpoint (behind the tunnel) — 8675309 mnemonic
    # Admin bind. Default loopback. Binding to a non-loopback address (LAN/public)
    # is allowed ONLY with an admin password set — enforced at startup
    # (__main__._require_admin_auth_if_exposed). See DESIGN.md §6/§8.
    admin_host: str = "127.0.0.1"
    admin_port: int = 8676
    # Intranet HTTPS for the admin UI (e.g. a mkcert cert reached at https://cognita-host:8676
    # on the home LAN). When BOTH are set, the admin server serves TLS directly via
    # uvicorn — no reverse proxy, matching the house rule that the gate lives in the
    # app, not a second moving part. Empty (default) = plain HTTP. PEM paths.
    # The MCP gateway is intentionally NOT covered: it sits behind cloudflared, which
    # already terminates TLS at the Cloudflare edge. Set via COGNITA_ADMIN_TLS_CERTFILE
    # / COGNITA_ADMIN_TLS_KEYFILE or config/cognita.yaml. A non-loopback admin_host
    # still requires a password (startup guard); TLS is what keeps that password from
    # riding the LAN in cleartext.
    admin_tls_certfile: str = ""
    admin_tls_keyfile: str = ""
    # In-app form login for the admin surface. Ships with NO password (5.1.0):
    # it used to default to admin / "welcome", which silently disabled the
    # exposed-bind startup guard, since that guard only refuses a non-loopback
    # bind when no password is SET. Set one with
    # `scripts/set-admin-credentials.{sh,bat}` (prompts for
    # username + password, writes an Argon2id verifier to config/cognita.yaml).
    # Credentials live ONLY in the config file — there is no environment override
    # for them, so a stale env var (e.g. in a systemd unit) can never silently
    # revert the stored password. Only the hash is ever stored; the plaintext is
    # never persisted. Leave both verifier fields empty to disable auth (loopback
    # only — the startup guard refuses an exposed bind with no password).
    admin_username: str = "admin"
    # EMPTY by default, matching config/cognita.example.yaml. This used to default to
    # sha256("welcome"), which silently disabled the startup guard below it: that guard
    # refuses a non-loopback admin bind when no password is set, and "is a password set?"
    # is `bool(admin_password_sha256)` — never false while a default was shipped. So an
    # exposed admin surface with publicly-known credentials started cleanly, which is the
    # exact condition the guard exists to refuse. Empty = open on loopback (dev UX,
    # unchanged), refused off loopback. Set it with scripts/set-admin-credentials.*.
    # 7.0 stores password verifiers with Argon2id. The legacy SHA-256 field is
    # read only so an upgraded installation can still reach the LAN admin page
    # and run the credential migration; OAuth refuses to start with it.
    admin_password_hash: str = ""
    admin_password_sha256: str = ""
    # How long a signed admin login cookie stays valid (days). The cookie's
    # signing key is derived from the active verifier, so changing the password
    # invalidates every session regardless of this. Logging out clears it early.
    admin_session_max_age_days: int = 30
    # Host header values the admin surface answers to (5.4). EMPTY = derive them
    # from the TLS certificate's SANs, this machine's hostname/FQDN/addresses and
    # loopback — which is how the default cannot lock the operator out of a
    # surface they reach by a name the cert already had to cover. Set explicitly
    # to override; ["*"] disables the check.
    admin_allowed_hosts: list[str] = []
    data_root: Path = REPO_ROOT / "data"
    # 4.0 vector engine (DESIGN-4.0-vector-engine.md §8): PostgreSQL + pgvector DSN.
    # The empty host means Unix socket -> peer authentication (no password needed);
    # a hostname (even "localhost") would mean TCP + scram, which wants a password.
    # Override via COGNITA_PG_DSN — e.g. from the Windows box over an SSH tunnel:
    #   postgresql://dbuser@127.0.0.1:15433/cognita
    # 14.0.0: the `engine` field ("core" | "workers") is gone with the 3.x worker
    # engine. load_config refuses `engine: workers` loudly rather than letting the
    # unknown key be ignored, which would silently mean core.
    pg_dsn: str = "postgresql:///cognita"
    # 4.0 retrieval core (M2). Chunking matches the 3.x engine defaults; the
    # mappings/routes default empty (every doc lands in "general") and exist
    # for config parity with the 3.x keyword-routing features.
    chunk_size: int = 1000
    chunk_overlap: int = 200
    index_exclude_patterns: list[str] = ["backups"]  # backups/ is never indexed
    # 5.0 §10: fnmatch globs over the FILENAME identifying cloud-sync conflict
    # copies, which are never indexed (parsing.SYNC_CONFLICT_PATTERNS is the
    # default). Set to [] to switch the filter off entirely — the escape hatch
    # for a corpus whose genuine filenames collide with the globs. Every skip is
    # logged and counted into get_index_stats().sync_conflicts either way.
    sync_conflict_patterns: list[str] = SYNC_CONFLICT_PATTERNS
    category_mappings: dict[str, str] = {}  # path substring -> category
    keyword_routes: dict[str, list[str]] = {}  # category -> query keywords
    # 4.4 document tiers. These are the DEFAULTS; a project in registry.yaml may
    # override either list, because connectors can have different
    # contents and a scripts-heavy one must not make a prose one start hoovering
    # up source files (DESIGN-4.4-registered-tier.md §2).
    #   indexed_extensions    -> embedded tier: chunked, embedded, hybrid search
    #   registered_extensions -> registered tier: stored whole, keyword search
    #                            only, never embedded
    # An extension in BOTH resolves to embedded and warns at startup.
    indexed_extensions: list[str] = sorted(DEFAULT_INDEXED_EXTENSIONS)
    registered_extensions: list[str] = sorted(DEFAULT_REGISTERED_EXTENSIONS)
    # 4.0-M4 (D4.9): Cognita-side file watcher. On-disk edits become searchable
    # within the debounce window; bursts (editor saves, OneDrive sync batches)
    # coalesce into one smart sync. (Before 14.0.0 this applied to engine "core"
    # mode only; the 3.x workers carried their own engine-side watcher.)
    watch_enabled: bool = True
    watch_debounce_s: float = 10.0
    watch_poll_interval_s: float = 5.0
    watch_max_pending_paths: int = 100_000
    watch_retry_initial_s: float = 5.0
    watch_retry_max_s: float = 300.0
    # 4.0.1: the deep probe, degenerated to what DESIGN-4.0 §3 promised — a
    # periodic SELECT 1 against Postgres. It should never fire; when it does,
    # the log says so immediately instead of search 500s saying it slowly.
    # 0 disables.
    pg_probe_interval_s: int = 60
    models_cache_dir: Path = Field(
        default_factory=lambda: Path.home() / ".cache" / "cognita" / "models"
    )
    # Audiobook production never resolves media executables from PATH.  The
    # service receives these explicit absolute registrations and refuses MP3
    # output when either is absent or no longer a regular local executable.
    ffmpeg_executable: Path | None = None
    ffprobe_executable: Path | None = None
    # 14.0.0: worker_port_min/max and worker_probe_interval_s (the 3.x worker
    # port pool and its ChromaDB-wedge deep probe) were removed with the workers.
    # Global kill-switch: force every remote connector project read-only.
    # Off by default; connector/project grants otherwise govern write access.
    remote_readonly: bool = False
    # Backup retention: keep the newest N backups per file; older ones are
    # pruned (and logged) right after each new backup is made. 0 = unlimited.
    backup_keep_per_file: int = 20
    registry_path: Path = REPO_ROOT / "config" / "registry.yaml"
    # Versioned parent-owned connector definitions and effective access policy.
    connectors_path: Path = REPO_ROOT / "config" / "connectors.yaml"
    # 11.0 parent-owned OAuth/static-key policy.  This path may be selected by
    # environment, but policy fields and raw keys never come from environment.
    authentication_path: Path = REPO_ROOT / "config" / "authentication.yaml"
    # Admin-owned acceleration policy.  Kept separate from cognita.yaml so
    # revisioned GPU/OCR changes never rewrite credentials or other settings.
    acceleration_path: Path | None = None
    # Canonical public origin used for OAuth issuer and resource identifiers.
    # OAuth requires HTTPS, except loopback URLs in tests/local development.
    public_base_url: str = ""
    oauth_enabled: bool = False
    # Migration provenance is runtime metadata, excluded from YAML dumps.  A
    # missing legacy setting means an 11.0 installation defaults OAuth on;
    # explicit false remains an intentional operator choice.
    oauth_enabled_provenance: str = Field(default="default", exclude=True)
    # Cognita 8.0's package-owned OAuth child. The child is always loopback;
    # these bounded controls are parent lifecycle settings, not token policy.
    oauth_service_port: int = 8778
    oauth_service_start_timeout_s: float = 20.0
    oauth_service_probe_interval_s: float = 5.0
    oauth_service_request_timeout_s: float = 10.0
    # 10.0 §20: bounded request-relative grace while the live OAuth child is
    # listening/establishing authenticated readiness. This is not a startup
    # sleep and does not alter token or authorization policy.
    oauth_connect_grace_s: float = 6.0
    oauth_service_shutdown_timeout_s: float = 2.0
    oauth_service_store_path: Path | None = None  # default: <data_root>/oauth-service.sqlite3
    oauth_cimd_allowed_hosts: list[str] = ["chatgpt.com", "claude.ai"]
    oauth_store_path: Path | None = None  # default: <data_root>/oauth.sqlite3
    oauth_access_token_ttl_seconds: int = 3600
    oauth_allowed_client_hosts: list[str] = [
        "claude.ai",
        "chatgpt.com",
        "openai.com",
        "127.0.0.1",
        "localhost",
    ]
    # Isolated-test compatibility switch for retired /mcp/<secret> requests.
    # Release mode ignores it; production uses the connector's canonical
    # /mcp/connectors/<connector-slug>/mcp/v<generation> resource.
    legacy_path_auth_enabled: bool = True
    # REMOVED in 13.0: `test_mode`. It was a runtime-only process mode that
    # armed one gateway branch — the `registry.find_by_token` fallback, which
    # honored raw project tokens — and that branch is deleted (§7.3). The flag
    # went with it rather than staying as a permanently-false leftover. 13.0
    # test mode is the separate field below; do NOT reintroduce `test_mode` or
    # map test mode onto it, because that would re-arm the old fallback.
    #
    # Runtime-only. 13.0 §7.3 test mode: set ONLY by `cmd_serve` from
    # COGNITA_TEST_MODE=1 (or `--test`), never read from YAML, never written
    # back, never overridable by COGNITA_SELF_TEST_MODE — see
    # _RUNTIME_ONLY_FIELDS below, which excludes it from both directions.
    self_test_mode: bool = False
    log_level: str = "INFO"
    # Retained as a compatibility-shaped runtime field for old callers only.
    # 11.0 never enables or persists Debug Tokens Mode.
    debug_tokens_mode: bool = False
    # Colorized (loguru-style) logging. "auto": color the console only when it's a
    # real TTY (cognita.log stays plain, grep-safe). "always": color BOTH the
    # console AND cognita.log — the log you tail — accepting ANSI codes in the
    # file (grep still matches; use `less -R`). "never": off. NO_COLOR env forces off.
    log_color: str = "auto"
    log_dir: Path = REPO_ROOT / "logs"  # rotating cognita.log lives here
    # 13.2.10 (DESIGN-13.2 §12): wire capture for the connector MCP route.
    # When true, EVERY request and response body on that route is written
    # whole to <log_dir>/mcp-wire/<utc>-<connector>-<id>.json (bearer token
    # redacted, nothing else touched) so a call a client drops can be read
    # next to one it shows. Off by default: the files hold whatever the tools
    # returned, document content included. Added on 2026-09-23 to make failed
    # client exchanges inspectable without guessing from sizes and hashes.
    mcp_wire_capture: bool = False
    # Newest files kept under <log_dir>/mcp-wire/; older ones are removed as
    # new captures arrive, so a switch left on cannot fill the disk.
    mcp_wire_capture_keep: int = 500

    # Engine model defaults for new projects (quality-first — DESIGN.md §5.2).
    # bge-reranker-v2-m3 is NOT in fastembed's supported list, so Cognita pins the
    # ONNX export itself (embeddings.PINNED_RERANKERS). It replaced Jina v2 in
    # 14.0.0 because Jina's weights are CC-BY-NC. Any fastembed-supported
    # reranker name still works here.
    embedding_model: str = "BAAI/bge-large-en-v1.5"
    embedding_dimensions: int = 1024
    reranker_model: str = "BAAI/bge-reranker-v2-m3"

    # Defaults delegate thread and batch sizing to ONNX Runtime and fastembed.
    # These settings are overrides for constrained systems. In measurements,
    # unbounded settings improved rebuild throughput by 2.3x while peak RSS
    # stayed within 60 MB of the four-thread configuration.
    #
    # 0 = let ORT size its own intra-op pool from the core count.
    # A positive value caps it, and is clamped to os.cpu_count().
    # Override with COGNITA_EMBEDDING_THREADS / COGNITA_EMBED_BATCH_SIZE.
    embedding_threads: int = 0
    # fastembed's own default. Texts held in flight per embed() call.
    embed_batch_size: int = 256

    # ---- 10.0 shared indexing scheduler -------------------------------
    # These bounds cover admitted queued/in-flight chunks, not source-file
    # size.  A single oversized chunk may enter when the queue is otherwise
    # empty so valid input cannot deadlock admission.
    index_scheduler_max_chunks: int = 8_192
    index_scheduler_max_text_bytes: int = 16 * 1024 * 1024
    # UTF-8 byte proxy for the one shared CPU indexing lane.  Query/rerank
    # embedding remains independent of this lane.
    index_cpu_max_chunk_bytes: int = 1_024
    gpu_retry_cooldown_s: float = 30.0
    gpu_probe_interval_s: float = 5.0

    # ---- 6.0 GPU indexing (DESIGN-6.0 §13) ------------------------------
    #
    # All of these default to leaving behavior EXACTLY as it is today. A
    # machine that has not deliberately built a GPU worker environment gets the
    # CPU path and never spawns anything, which is the ordinary case.
    #
    # Measured on the reference deployment (DESIGN-6.0 §12.3): 212.3 chunks/s
    # across two cards against 8.23 on the CPU — 25.8x — with the canary at
    # 1.36e-07 and VRAM fully returned on worker exit.
    gpu_enabled: bool = False
    # Interpreter for the separate GPU virtual environment. Empty disables GPU
    # workers by default. Keep GPU runtimes out of the service environment:
    # CPU and GPU builds share the `onnxruntime` module name, so a bad GPU wheel
    # there can remove the embedder and stop indexing instead of enabling CPU
    # fallback.
    gpu_venv_python: str = ""
    # Estimated chunks below which a job stays on the CPU.
    #
    # Estimated chunks below which a job stays on CPU. Pool startup measured
    # 2.95s with the program cache warm; measured CPU and warm-GPU rates put
    # break-even between about 11 and 28 chunks, so the default is 20. The file
    # size estimate is only a lower bound and can overcount unchanged files;
    # workers start lazily when a window contains chunks.
    gpu_min_chunks: int = 20
    # How long a proven pool stays available after a job so the next job in a
    # burst avoids startup. Zero tears the pool down after every job.
    #
    # A cold start is about 3s for worker startup and a canary. Lingering keeps
    # idle workers and their model in VRAM; the 30s default covers short bursts
    # of consecutive edits without holding the hardware through long idle gaps.
    #
    # This bounds idle time only; active claims reset the timer so a busy
    # service does not tear down a pool between documents.
    gpu_idle_linger_s: float = 30.0
    # A warm pool accepts small writes. The earlier 16-chunk floor came from
    # ~1.8 s of padding at batch 64; batch 4 reduced a one-chunk write to
    # ~0.081 s while avoiding CPU contention. Reassess if the batch changes.
    gpu_warm_min_chunks: int = 0
    gpu_reserve_vram_gb: float = 4.0
    # Unset (None) means the acceleration profile's default: 20 on AMD, and no
    # utilization gate at all on NVIDIA, where free VRAM alone decides
    # (`acceleration_profiles.effective_max_busy_percent`). A value set here
    # applies on every profile.
    gpu_max_busy_percent: int | None = None
    # Forward-pass width selects a MIGraphX compiled shape. In the measured
    # 2/4/8/16/64 sweep, batch 4 delivered 49.5 chunks/s and a 0.081 s
    # one-chunk write; batch 64 delivered 35.7 chunks/s and 1.832 s because
    # short requests are padded. A new shape costs ~25-38 s to compile and
    # ~1.4 GB of cache. Re-measure both throughput and short-write latency
    # before changing this setting.
    gpu_batch_size: int = 4
    # `gpu_slice_chunks` is how much work is handed to a worker in ONE request,
    # which the worker then runs as several forward passes of gpu_batch_size.
    # Measured: sending 64 chunks per request gave 33.5 chunks/s while the same
    # device sustained far more when handed a large slice — a fixed per-call
    # cost inside fastembed (tokenizer and pool setup) dominates a small
    # request. It is bounded at the other end by the need to keep every device
    # fed: a slice is the unit of work-stealing, so one enormous slice would
    # hand the whole window to one card and idle the rest.
    gpu_slice_chunks: int = 512
    # Card indexes refer to the PCI-ordered list in /healthz, not sysfs or
    # provider ordinals. Selected cards must still pass VRAM and utilization
    # gates. Out-of-range indexes warn because GPU faults otherwise fall back
    # to CPU. Keep Any: Pydantic's lax union coerces false to integer 0
    # before normalize_cards can reject it.
    gpu_cards: Any = "all"
    # The expert form of the same decision, by PCI ADDRESS (e.g.
    # ["0000:03:00.0"]) — never by index, because the sysfs card number is not
    # the provider's device ordinal (§12.3 correction 4). Empty means "not
    # used"; set, it OVERRIDES `gpu_cards`, on the principle that the more
    # specific statement wins over the vaguer one. Prefer `gpu_cards` unless
    # addresses are actually needed (a box whose card set changes, say).
    gpu_device_ids: list[str] = []
    # 🔴 `rocm` is DEAD on ROCm 7.1+: the ROCm execution provider was REMOVED in
    # ONNX Runtime 1.23, and ROCm 7.0 is the last release AMD supported it on.
    # MIGraphX is not the alternative, it is the only option (§5).
    # 15.0: "" (the default) means the acceleration profile's provider. It was
    # "migraphx" before the nvidia profile existed; with no profile set the
    # resolved value is still migraphx, so nothing changes for AMD.
    gpu_provider: str = ""
    gpu_worker_shutdown_s: float = 10.0
    # Generous on purpose: a COLD shape compile is ~30s and must not be mistaken
    # for a hang. A hang is a distinct failure from a crash — a wedged worker
    # holds its slice and its VRAM and looks identical to a slow one — so it
    # needs its own bound, but the cost of a false positive is one requeued
    # slice and the cost of too tight a bound is a walk that keeps killing
    # healthy workers.
    gpu_worker_slice_timeout_s: float = 120.0
    # 🔴 The bound on the STARTUP handshake, which had none. A worker only
    # answers after model resolution (possibly a first-run download), the HIP
    # init and the MIGraphX shape compile, so this is generous on purpose —
    # but bounded, because unbounded meant a wedged worker hung the walk inside
    # the project write lock, where concurrent writes are refused rather than
    # queued: a reindex stuck at active:true forever and every connector write
    # to that project failing, with no log line after embed.plan.
    gpu_worker_startup_timeout_s: float = 300.0
    # Where the worker loads MODEL artifacts from. Defaults to the service's own
    # cache so the first GPU run downloads nothing and the canary compares two
    # runtimes against one set of weights rather than silently comparing two
    # different models (§10.1).
    gpu_model_cache_dir: str = ""
    # MIGraphX compiled-program (`.mxr`) cache. Without it, each per-walk worker
    # recompiles the graph, measured at about 60s per worker. The cache avoids
    # paying that cost on every GPU walk.
    #
    # The cache stores the kernel configuration found during tuning, so a loaded
    # program should perform like a freshly compiled one. Earlier throughput
    # comparisons (36.5 vs. 148.3 chunks/s) ran cache and no-cache cases in
    # fixed order and confounded cache effects with GPU warm-up; interleave or
    # randomize cases when measuring again.
    #
    # Roughly 1.4 GB per cached shape. Must not sit on a tmpfs: at that size a
    # RAM-backed /tmp trades the VRAM this design carefully returns for system
    # memory it never gives back.
    gpu_program_cache_dir: str = ""
    # Pad every batch to one fixed shape. Shape variety is what makes MIGraphX
    # recompile; compilation is keyed on shape and never on content, so a fixed
    # shape means one compile for the life of the deployment.
    #
    # 15.0: -1 (the default) means "the acceleration profile's value": 512 for
    # amd (and for cpu, which resolves GPU settings as amd), 0 = do not pin for
    # nvidia, where padding every batch to 512 tokens halved CUDA throughput
    # (DESIGN-NVIDIA-ACCELERATION §1.3). Any explicit value, including 0, is
    # honored as written. Resolution happens in ONE place, `GpuWorker.start()`.
    gpu_fixed_seq_len: int = -1
    # Max acceptable GPU-vs-CPU vector delta (§9.1). 1e-4 sits well above the
    # ~1e-6 FP32 noise floor and well below the ~1e-3 an FP16 graph would show.
    gpu_canary_tolerance: float = 1e-4
    # 6.0.8 §14.8: turn the GPU path's protocol trace on WITHOUT dropping the
    # whole service to DEBUG. The interesting failures here are intermittent
    # and load-dependent, so they are caught by leaving tracing on across
    # ordinary traffic — which is only tolerable if it does not also enable
    # every httpx, uvicorn and watcher DEBUG line in the same log file.
    #
    # ⚠️ Death reports are NOT gated on this. A worker dying is an ERROR and is
    # always logged in full; this key only adds the per-slice, per-spawn and
    # per-teardown trace that says what was happening around it.
    gpu_log_debug: bool = False

    asset_max_png_bytes: int = 16 * 1_048_576
    asset_max_metadata_bytes: int = 65_536
    asset_max_prompt_bytes: int = 16_384
    asset_max_dimension: int = 4_096
    asset_max_pixels: int = 16_777_216
    asset_max_chunks: int = 2_048
    asset_max_chunk_bytes: int = 16 * 1_048_576
    asset_max_list_results: int = 200
    asset_max_search_results: int = 20

    # 10.0 local PNG OCR.  The request surface intentionally exposes only
    # languages; model/device settings are administrator-owned deployment
    # configuration and are never accepted from a client.
    ocr_model_dir: str = ""
    # OCR runs in a separately provisioned interpreter.  An empty value is
    # intentionally unavailable; OCR must never silently reuse Cognita's
    # service interpreter or its dependency set.
    ocr_python: str = ""
    ocr_launcher: list[str] = []
    ocr_qualification_manifest: Path = (
        REPO_ROOT / "docs" / "easyocr-qualification-dependencies.json"
    )
    ocr_enabled_languages: list[str] = ["en"]
    ocr_device: str = "cpu"
    # Ordered stable ROCm UUID bindings. Empty means derive eligible cards
    # from gpu_cards/gpu_device_ids in probe (PCI) order. The singular setting
    # below remains a compatibility pin for one-card deployments.
    ocr_gpu_device_ids: list[str] = []
    ocr_gpu_device_id: str = ""
    ocr_gpu_required_vram_mb: int = 4_096
    ocr_require_noatime: bool = True
    ocr_max_png_bytes: int = 16 * 1024 * 1024
    ocr_max_pixels: int = 16_777_216
    ocr_max_dimension: int = 8_192
    ocr_max_result_bytes: int = 1 * 1024 * 1024
    ocr_max_regions: int = 10_000
    ocr_timeout_s: float = 60.0
    ocr_queue_timeout_s: float = 30.0
    ocr_concurrency: int = 1
    ocr_queue_size: int = 4
    ocr_cpu_threads: int = 2
    ocr_worker_memory_mb: int = 2_048
    ocr_cpu_reserve_ram_mb: int = 1_024
    ocr_low_confidence: float = 0.60

    @model_validator(mode="after")
    def _validate_gpu_settings(self) -> CognitaConfig:
        """Refuse nonsense GPU settings at startup rather than deep in a walk.

        🔴 The one that matters most is `gpu_canary_tolerance`. §9.1's canary is
        the check that caught a provider which loaded, reported itself active,
        ran at 8.6x and returned vectors with a cosine of 0.536 against the CPU —
        i.e. the check standing between a working index and silent corruption. A
        tolerance of 1.0 makes `delta > tolerance` unreachable, so the canary
        still runs, still logs, still "passes", and guards nothing. Nothing else
        in the system would ever say so. An inert safety check is worse than an
        absent one, because it reads as cover.

        The rest are ordinary crash-prevention: `gpu_batch_size` or
        `gpu_slice_chunks` at 0 reaches `range(start, stop, 0)` inside the
        slicer and raises per window, mid-walk, after the pool has been paid for.
        """
        # Filesystem discovery is bounded and fail-closed.  Polling may be
        # explicitly disabled with zero; all other timing values must be
        # finite and positive, and retry_max must not truncate retry growth.
        for name, value in {
            "watch_debounce_s": self.watch_debounce_s,
            "watch_retry_initial_s": self.watch_retry_initial_s,
            "watch_retry_max_s": self.watch_retry_max_s,
        }.items():
            if isinstance(value, bool) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be finite and greater than 0 (got {value!r})")
        if (
            isinstance(self.watch_poll_interval_s, bool)
            or not math.isfinite(self.watch_poll_interval_s)
            or self.watch_poll_interval_s < 0
        ):
            raise ValueError("watch_poll_interval_s must be finite and >= 0")
        if (
            isinstance(self.watch_max_pending_paths, bool)
            or self.watch_max_pending_paths < 1
            or self.watch_max_pending_paths > 10_000_000
        ):
            raise ValueError("watch_max_pending_paths must be between 1 and 10000000")
        if self.watch_retry_max_s < self.watch_retry_initial_s:
            raise ValueError("watch_retry_max_s must be >= watch_retry_initial_s")

        positive = {
            "gpu_min_chunks": self.gpu_min_chunks,
            "gpu_batch_size": self.gpu_batch_size,
            "gpu_slice_chunks": self.gpu_slice_chunks,
            "gpu_worker_shutdown_s": self.gpu_worker_shutdown_s,
            "gpu_worker_slice_timeout_s": self.gpu_worker_slice_timeout_s,
            "gpu_worker_startup_timeout_s": self.gpu_worker_startup_timeout_s,
            "index_scheduler_max_chunks": self.index_scheduler_max_chunks,
            "index_scheduler_max_text_bytes": self.index_scheduler_max_text_bytes,
            "index_cpu_max_chunk_bytes": self.index_cpu_max_chunk_bytes,
        }
        for name, value in positive.items():
            if value <= 0:
                raise ValueError(f"{name} must be greater than 0 (got {value!r})")
        # 15.0: -1 = "the profile's" (the default) and 0 = "do not pin" are both
        # legal now (0 was refused as a non-positive value before, and 512 was
        # the only way to say anything). Anything below -1 is a typo.
        if isinstance(self.gpu_fixed_seq_len, bool) or self.gpu_fixed_seq_len < -1:
            raise ValueError(
                "gpu_fixed_seq_len must be -1 (the acceleration profile's value), "
                f"0 (do not pin) or a token length (got {self.gpu_fixed_seq_len!r})"
            )
        if self.gpu_reserve_vram_gb < 0:
            raise ValueError(
                f"gpu_reserve_vram_gb must not be negative (got {self.gpu_reserve_vram_gb!r})"
            )
        # Zero is legal and meaningful here (6.1 behavior: never linger), which
        # is why it is not in `positive` above. Negative is not: it would arm a
        # timer in the past and read as "already expired" at every claim, i.e.
        # exactly the same as zero but by accident rather than on purpose.
        if self.gpu_idle_linger_s < 0:
            raise ValueError(
                "gpu_idle_linger_s must not be negative — use 0 to tear a pool "
                f"down at the end of every job (got {self.gpu_idle_linger_s!r})"
            )
        # Same reasoning: 0 is meaningful (no floor), negative is not.
        if self.gpu_warm_min_chunks < 0:
            raise ValueError(
                "gpu_warm_min_chunks must not be negative — use 0 to send every "
                f"chunk to a warm pool (got {self.gpu_warm_min_chunks!r})"
            )
        if self.gpu_retry_cooldown_s < 0 or self.gpu_probe_interval_s <= 0:
            raise ValueError("GPU scheduler cooldown must be >= 0 and probe interval > 0")
        # 6.4: parse `gpu_cards` HERE, at startup, and let the parse error be
        # the startup error. It is the one GPU key edited by hand, so it is the
        # one most likely to be wrong — and every consumer of it sits behind the
        # CPU fallback, where a bad value degrades instead of failing. Validate
        # it where a mistake is still loud.
        from .gpu_probe import normalize_cards

        normalize_cards(self.gpu_cards)
        if self.gpu_max_busy_percent is not None and not 0 <= self.gpu_max_busy_percent <= 100:
            raise ValueError(
                "gpu_max_busy_percent is a percentage and must be 0-100 "
                f"(got {self.gpu_max_busy_percent!r})"
            )
        # Upper bound as well as lower: 1e-3 is the order an FP16 graph shows and
        # is a defensible loosening; anything at or above 1e-2 admits a device
        # returning a different answer entirely, which is what the check is for.
        if not 0 < self.gpu_canary_tolerance < 1e-2:
            raise ValueError(
                "gpu_canary_tolerance must be >0 and <1e-2 — a looser value does "
                "not relax the §9.1 canary, it disables it silently "
                f"(got {self.gpu_canary_tolerance!r})"
            )
        # A typo here used to fall through to MIGraphX silently, so a box
        # configured for ROCm would run on a provider nobody chose.
        if self.gpu_provider.lower() not in _GPU_PROVIDERS:
            raise ValueError(
                "gpu_provider must be one of "
                f"{sorted(_GPU_PROVIDERS - {''})} or empty for the acceleration "
                f"profile's provider (got {self.gpu_provider!r})"
            )
        asset_limits = {
            "asset_max_png_bytes": (16 * 1_048_576, 1),
            "asset_max_metadata_bytes": (65_536, 1),
            "asset_max_prompt_bytes": (16_384, 1),
            "asset_max_dimension": (4_096, 1),
            "asset_max_pixels": (16_777_216, 1),
            "asset_max_chunks": (2_048, 1),
            "asset_max_chunk_bytes": (16 * 1_048_576, 1),
            "asset_max_list_results": (200, 1),
            "asset_max_search_results": (20, 1),
        }
        for name, (ceiling, floor) in asset_limits.items():
            value = getattr(self, name)
            if isinstance(value, bool) or not floor <= value <= ceiling:
                raise ValueError(f"{name} must be between {floor} and {ceiling} (got {value!r})")
        ocr_positive = {
            "ocr_max_png_bytes": (16 * 1024 * 1024, 1),
            "ocr_max_pixels": (16_777_216, 1),
            "ocr_max_dimension": (8_192, 1),
            "ocr_max_result_bytes": (1 * 1024 * 1024, 1),
            "ocr_max_regions": (10_000, 1),
            "ocr_concurrency": (8, 1),
            "ocr_queue_size": (64, 0),
            "ocr_cpu_threads": (64, 1),
            "ocr_worker_memory_mb": (16_384, 128),
            "ocr_cpu_reserve_ram_mb": (65_536, 1),
            "ocr_gpu_required_vram_mb": (65_536, 1),
        }
        for name, (ceiling, floor) in ocr_positive.items():
            value = getattr(self, name)
            if isinstance(value, bool) or not floor <= value <= ceiling:
                raise ValueError(f"{name} must be between {floor} and {ceiling} (got {value!r})")
        for name in ("ocr_timeout_s", "ocr_queue_timeout_s"):
            value = getattr(self, name)
            if isinstance(value, bool) or not 0 < value <= 120:
                raise ValueError(f"{name} must be between 0 and 120 seconds (got {value!r})")
        if not 0 < self.ocr_low_confidence < 1:
            raise ValueError("ocr_low_confidence must be between 0 and 1")
        if self.ocr_device not in {"cpu", "gpu"} and not self.ocr_device.startswith("cuda"):
            raise ValueError("ocr_device must be cpu, gpu, or a cuda device")
        if self.ocr_gpu_device_ids and self.ocr_gpu_device_id:
            raise ValueError("set only one of ocr_gpu_device_ids or ocr_gpu_device_id")
        if len(set(self.ocr_gpu_device_ids)) != len(self.ocr_gpu_device_ids):
            raise ValueError("ocr_gpu_device_ids must contain unique UUIDs")
        for device_id in self.ocr_gpu_device_ids:
            # 15.0: the same form `acceleration._UUID` accepts. AMD's is
            # `GPU-<hex>`; NVIDIA's (from NVML) is dashed,
            # `GPU-4cd28834-e5a4-6b4e-85aa-3e54bcbf0630`. The old check parsed
            # everything after the first dash as ONE hex number, so it refused
            # every NVIDIA card set through this legacy key.
            if not _GPU_UUID.match(device_id):
                raise ValueError(
                    "ocr_gpu_device_ids must contain GPU-<hex UUID> values "
                    "(GPU-<hex> for AMD, or NVIDIA's dashed GPU-xxxxxxxx-xxxx-... form)"
                )
        if self.ocr_python and not Path(self.ocr_python).is_absolute():
            raise ValueError("ocr_python must be an absolute path")
        if any(not isinstance(item, str) or not item for item in self.ocr_launcher):
            raise ValueError("ocr_launcher must contain non-empty executable arguments")
        if not 1 <= len(self.ocr_enabled_languages) <= 3 or len(
            set(self.ocr_enabled_languages)
        ) != len(self.ocr_enabled_languages):
            raise ValueError("ocr_enabled_languages must contain one to three unique codes")
        if any(
            not isinstance(value, str) or not value.isascii() or not value.islower()
            for value in self.ocr_enabled_languages
        ):
            raise ValueError("ocr_enabled_languages must contain lowercase ASCII codes")
        if not 1 <= self.oauth_service_port <= 65535:
            raise ValueError("oauth_service_port must be between 1 and 65535")
        if self.oauth_service_port in {self.mcp_port, self.admin_port}:
            raise ValueError("oauth_service_port must not equal mcp_port or admin_port")
        for name in (
            "oauth_service_start_timeout_s",
            "oauth_service_probe_interval_s",
            "oauth_service_request_timeout_s",
            "oauth_service_shutdown_timeout_s",
        ):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be greater than 0")
        if (
            isinstance(self.oauth_connect_grace_s, bool)
            or not 5.0 <= self.oauth_connect_grace_s <= 30.0
        ):
            raise ValueError("oauth_connect_grace_s must be between 5 and 30 seconds")
        if self.oauth_service_shutdown_timeout_s > 2.0:
            raise ValueError("oauth_service_shutdown_timeout_s must not exceed 2 seconds")
        for host in self.oauth_cimd_allowed_hosts:
            labels = host.split(".")
            if (
                host != host.strip()
                or not host
                or host.startswith(".")
                or host.endswith(".")
                or "*" in host
                or ":" in host
                or "/" in host
                or any(
                    not label
                    or not label.replace("-", "").isalnum()
                    or label.startswith("-")
                    or label.endswith("-")
                    for label in labels
                )
            ):
                raise ValueError(
                    "oauth_cimd_allowed_hosts entries must be exact hostnames "
                    f"without wildcard or subdomain syntax (got {host!r})"
                )
        return self


def oauth_service_store_path(config: CognitaConfig) -> Path:
    """Return the DOT-exclusive database path used by the OAuth child."""
    return Path(
        config.oauth_service_store_path or (Path(config.data_root) / "oauth-service.sqlite3")
    )


def oauth_service_key_path(config: CognitaConfig) -> Path:
    """Return the installation-local parent/child authentication key path."""
    return Path(config.data_root) / ".oauth-service-key"


def ensure_oauth_service_key(config: CognitaConfig) -> bytes:
    """Create/read the fixed-length OAuth child key without text normalization."""
    import os
    import secrets

    path = oauth_service_key_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        raw = None
    if raw is None:
        key = secrets.token_bytes(32)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
        try:
            fd = os.open(path, flags, 0o600)
        except FileExistsError:
            raw = path.read_bytes()
        else:
            try:
                os.write(fd, key)
            finally:
                os.close(fd)
            return key
    if raw is None or len(raw) != 32:
        raise RuntimeError(f"OAuth service key must be exactly 32 bytes: {path}")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return raw


def oauth_service_readiness(config: CognitaConfig) -> list[str]:
    """Validate parent prerequisites for the package-owned OAuth child."""
    from urllib.parse import urlparse

    from .admin_auth import has_argon2_credentials

    problems: list[str] = []
    base = config.public_base_url.rstrip("/")
    parsed = urlparse(base)
    if not base:
        problems.append("public_base_url is not configured")
    elif parsed.scheme != "https" and parsed.hostname not in {"127.0.0.1", "localhost", "test"}:
        problems.append("public_base_url must use HTTPS")
    if not has_argon2_credentials(config):
        if config.admin_password_sha256:
            problems.append(
                "admin credentials still use legacy SHA-256; rerun set-admin-credentials"
            )
        else:
            problems.append("an Argon2id admin password is not configured")
    if config.oauth_access_token_ttl_seconds < 60:
        problems.append("oauth_access_token_ttl_seconds must be at least 60")
    if not config.oauth_allowed_client_hosts:
        problems.append("oauth_allowed_client_hosts is empty")
    if not config.oauth_cimd_allowed_hosts:
        problems.append("oauth_cimd_allowed_hosts is empty")
    try:
        ensure_oauth_service_key(config)
    except (OSError, RuntimeError, ValueError) as exc:
        problems.append(f"OAuth service key is unavailable ({type(exc).__name__})")
    return problems


# Credentials are read ONLY from config/cognita.yaml (written by
# scripts/set-admin-credentials.*). They are deliberately excluded from the
# COGNITA_<KEY> override mechanism so a stale environment variable can never
# silently override the stored admin password/username at load.
_CREDENTIAL_FIELDS = frozenset({"admin_username", "admin_password_hash", "admin_password_sha256"})
_RUNTIME_ONLY_FIELDS = frozenset({
    "debug_tokens_mode", "self_test_mode", "oauth_enabled_provenance",
})


def _apply_env_overrides(data: dict) -> dict:
    """Override any top-level key via COGNITA_<UPPERCASE_KEY> (except credentials)."""
    import os

    for field in CognitaConfig.model_fields:
        if field in _CREDENTIAL_FIELDS or field in _RUNTIME_ONLY_FIELDS:
            continue
        env_val = os.environ.get(f"COGNITA_{field.upper()}")
        if env_val is not None:
            data[field] = env_val
    return data


def _refuse_removed_engine(data: dict, cfg_path: Path) -> None:
    """14.0.0: `engine` is no longer a setting; `workers` is refused, `core` is accepted.

    CognitaConfig ignores unknown keys, so deleting the field alone would make an
    old `engine: workers` config (or COGNITA_ENGINE=workers, which
    _apply_env_overrides used to honor) start quietly as core — the same
    "success describes a state that does not hold" failure CLAUDE.md warns about.
    Both sources are checked and the message names the one that carried the value.
    Absent, blank or exactly "core" is accepted and discarded.
    """
    import logging
    import os

    log = logging.getLogger("cognita.config")
    for where, setting, remedy, value in (
        (str(cfg_path), "engine", "Delete the engine line (or set engine: core)",
         data.pop("engine", None)),
        ("environment", "COGNITA_ENGINE", "Unset COGNITA_ENGINE (or set it to core)",
         os.environ.get("COGNITA_ENGINE")),
    ):
        if value in (None, "", "core"):
            log.debug("engine setting accepted where=%s value=%r", where, value)
            continue
        log.error("engine setting refused where=%s setting=%s value=%r", where, setting, value)
        raise SystemExit(
            f"{where}: {setting}: {value} is no longer supported. The 3.x worker engine "
            f"and knowledge-rag were removed in Cognita 14.0.0. {remedy} and start again."
        )


def configured_config_path() -> Path:
    """Return the config file path selected by the current process environment."""
    import os

    config_root = os.environ.get("COGNITA_CONFIG_ROOT")
    configured_path = os.environ.get("COGNITA_CONFIG_PATH")
    if configured_path:
        return Path(configured_path)
    if config_root:
        return Path(config_root) / "cognita.yaml"
    return DEFAULT_CONFIG_PATH


def load_config(path: Path | None = None) -> CognitaConfig:
    """Load YAML config (optional) + env overrides. Missing file => defaults."""
    import os

    cfg_path = Path(path) if path is not None else configured_config_path()
    data: dict = {}
    oauth_provenance = "default"
    if cfg_path.is_file():
        with open(cfg_path, encoding="utf-8") as f:
            loaded = yaml.safe_load(f) or {}
        if not isinstance(loaded, dict):
            raise ValueError(f"Config file {cfg_path} must contain a YAML mapping")
        data = loaded
        if "oauth_enabled" in data:
            oauth_provenance = "config"
    if os.environ.get("COGNITA_OAUTH_ENABLED") is not None:
        oauth_provenance = "environment"
    # Debug Tokens Mode is intentionally ephemeral. Even if an old/manual
    # config contains the key, never carry plaintext-token logging across a
    # restart.
    for field in _RUNTIME_ONLY_FIELDS:
        data.pop(field, None)
    _refuse_removed_engine(data, cfg_path)
    data = _apply_env_overrides(data)
    pg_dsn_file = os.environ.get("COGNITA_PG_DSN_FILE")
    if pg_dsn_file:
        if os.environ.get("COGNITA_PG_DSN") is not None:
            raise ValueError("set only one of COGNITA_PG_DSN or COGNITA_PG_DSN_FILE")
        try:
            pg_dsn = Path(pg_dsn_file).read_text(encoding="ascii").strip()
        except (OSError, UnicodeError) as exc:
            raise ValueError("COGNITA_PG_DSN_FILE is unavailable") from exc
        if not pg_dsn or len(pg_dsn) > 4096 or any(character.isspace() for character in pg_dsn):
            raise ValueError("COGNITA_PG_DSN_FILE contains an invalid DSN")
        data["pg_dsn"] = pg_dsn
    config = CognitaConfig(**data)
    config.acceleration_path = cfg_path.parent / "acceleration.yaml"
    # The deployment value seeds the installation; an Admin-saved Cognita
    # state override takes precedence without mutating deployment config.
    from .public_url import effective_public_base_url

    config.public_base_url = effective_public_base_url(config)
    config.oauth_enabled_provenance = oauth_provenance
    return config
