"""Cognita CLI entry point (DESIGN.md §9).

Commands:
  cognita serve          -> gateway (:8675) + admin UI (:8676, localhost)
  cognita probe-assets   -> disposable in-memory Phase 0 image-handoff probe
  cognita serve --stdio  -> refused: stdio mode was removed in 14.0.0 (flag kept so the
                            refusal can say why); clients connect over HTTP to /mcp
  cognita gen-token      -> retired in 9.0; use an Admin connector instead
  cognita project list   -> print registered projects
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

import uvicorn

from . import __version__

log = logging.getLogger("cognita")

# Bearer tokens ride in the /mcp/<token> URL path (claude.ai custom connectors
# can't send an Authorization header), so they appear in access-log lines. Scrub
# them from EVERY record — console AND file — before anything is written. This is
# the global backstop; gateway.py also redacts in its own 404 handler.
# Case-insensitive, and `/+` so an extra slash cannot slip a token past: both
# spellings reach the uvicorn access log, which is formatted through here.
#   POST /MCP/<token>   — a hand-typed connector URL
#   POST /mcp//<token>  — the ordinary result of a client base URL ending in "/"
# Neither matched the old `(/mcp/)[^/\s"']+`: the first is case-sensitive, and
# the second put an empty segment where the token was expected, so the token
# landed in the console AND in logs/cognita.log, which is rotated and kept x5 —
# a live credential sitting at rest in plaintext.
_TOKEN_IN_PATH = re.compile(r"(?i)(/mcp/+)[^/\s\"']+")

# The token also has a header form. DESIGN-5.0 §1 documents `Authorization:
# Bearer <token>` as an equally supported access path, and nothing logs headers
# today — but "nothing logs headers today" is a fact about the current code, not
# an invariant, and the cost of covering it is one regex.
_TOKEN_IN_BEARER = re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/-]{8,}=*")


def _redact(text: str) -> str:
    """Mask a bearer token in a /mcp/<token> path or an Authorization header.

    The single redaction rule, shared by BOTH formatters so colorized output can
    never skip it (CLAUDE.md: never log a token)."""
    text = _TOKEN_IN_PATH.sub(r"\1<token>", text)
    text = _TOKEN_IN_BEARER.sub(r"\1<token>", text)
    # 11.0 policy mutations return a raw key only once.  Keep the formatter
    # and any future structured handler from leaking generated_key fields.
    text = re.sub(
        r"(?i)([\"']?generated_key[\"']?\s*[:=]\s*[\"']?)[^\s,}\"']+",
        r"\1<redacted>", text,
    )
    return text


# Attributes every LogRecord carries. Anything else on a record arrived through
# ``extra=`` and is what a structured handler would show; the console never did.
_STANDARD_RECORD_FIELDS = frozenset(
    logging.LogRecord("x", logging.INFO, "x", 0, "", (), None).__dict__
) | {"message", "asctime", "taskName"}


def _extra_fields(record: logging.LogRecord) -> str:
    """Render a record's ``extra=`` fields as `` key=value`` for the console.

    13.2.5 (DESIGN-13.2-CONNECTOR-DIAGNOSTICS §4.2): workspace.py logs its
    failures with the reason ONLY in ``extra`` fields, and both console
    formatters dropped them — a failing copy_to_workspace printed 114 identical
    warnings that said nothing. Values are bounded by the emitting code (every
    site truncates to 64 chars); this caps them again and the finished line
    still goes through ``_redact``. ``None`` values are skipped.
    """
    parts = []
    for key in sorted(record.__dict__):
        if key in _STANDARD_RECORD_FIELDS or key.startswith("_"):
            continue
        value = record.__dict__[key]
        if value is None:
            continue
        parts.append(f" {key}={str(value)[:200]}")
    return "".join(parts)


class _RedactingFormatter(logging.Formatter):
    """Standard formatter that masks bearer tokens in the /mcp/<token> path."""

    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        extras = _extra_fields(record)
        if extras:
            # On the message line, ahead of any traceback the base class appended.
            head, newline, tail = text.partition("\n")
            text = f"{head}{extras}{newline}{tail}"
        return _redact(text)


class _RoutineHealthSuccessFilter(logging.Filter):
    """Drop only successful internal health probes from console and file logs.

    Docker checks /healthz every 15 seconds, which also asks the local Admin
    service for readiness. Both INFO records otherwise fill the idle log with
    routine 200s. Failed probes and ordinary access records remain visible.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno != logging.INFO:
            return True
        if record.name == "uvicorn.access":
            args = record.args
            if isinstance(args, tuple) and len(args) == 5:
                client, method, path, _protocol, status = args
                if (
                    isinstance(client, str) and client.startswith("127.0.0.1:")
                    and method == "GET" and path == "/healthz" and status == 200
                ):
                    return False
        elif record.name == "httpx":
            if record.getMessage() == (
                'HTTP Request: GET http://127.0.0.1:8778/_cognita/ready '
                '"HTTP/1.1 200 OK"'
            ):
                return False
        return True


# loguru-style level colors (ANSI): dim timestamp, colored level, cyan logger,
# message tinted for WARNING+. Only ever written to a TTY console (never the log
# file), so grepping cognita.log stays clean.
# Bold + bright variants so it pops like the Ubuntu shell prompt (bold green =
# "\033[1;32m"), not the muted standard-ANSI shades.
_ANSI_RESET = "\033[0m"
_ANSI_DIM = "\033[2m"
_ANSI_NAME = "\033[1;36m"  # bold/bright cyan for the logger name
_LEVEL_COLOR = {
    "DEBUG": "\033[1;36m",  # bright cyan
    "INFO": "\033[1;32m",  # bright green
    "WARNING": "\033[1;33m",  # bright yellow
    "ERROR": "\033[1;31m",  # bright red
    "CRITICAL": "\033[1;97;41m",  # bold white on red
}

# 6.0.1: anything about the GPUs is tinted yellow at EVERY level, so "are the
# cards being used?" is answerable by eye while a rebuild scrolls past. Before
# this, only the relayed ONNX Runtime warnings were colored — they are yellow
# because they are WARNINGs, not because they are GPU — while the lines that
# actually carry the decision (`planned=gpu`, `decision=gpu`, the per-device
# `embed.done` rows) came through at INFO in plain text and read as ordinary
# walk noise.
#
# Matched two ways, because neither alone covers it:
#   - by logger, for `cognita.gpu` (gpu_host/gpu_probe), which relays worker
#     output that need not mention a GPU anywhere in the text; and
#   - by content, for the `embed.plan`/`embed.done` lines, which come from
#     `cognita.embed` — a logger that carries the CPU path too and therefore
#     must NOT be tinted wholesale, or the signal is every walk and means
#     nothing. A pure-CPU walk's fields (`planned=cpu`, `decision=cpu`) contain
#     none of these tokens, which is what keeps the match discriminating.
_ANSI_GPU = "\033[1;33m"  # the same bright yellow WARNING uses
_GPU_LOGGER = "cognita.gpu"
_GPU_TOKENS = re.compile(r"gpu|vram|migraphx|rocm|cpu_fallback", re.IGNORECASE)


def _is_gpu_line(record: logging.LogRecord, msg: str) -> bool:
    """Is this log line about the GPUs?"""
    return record.name.startswith(_GPU_LOGGER) or bool(_GPU_TOKENS.search(msg))


class _ColorFormatter(logging.Formatter):
    """Colorized console formatter (loguru look). Redacts tokens with the SAME
    rule as _RedactingFormatter — the color path must never leak a secret."""

    def format(self, record: logging.LogRecord) -> str:
        color = _LEVEL_COLOR.get(record.levelname, "")
        ts = self.formatTime(record)  # matches the plain formatter's asctime
        msg = record.getMessage() + _extra_fields(record)
        if record.exc_info:
            if not record.exc_text:
                record.exc_text = self.formatException(record.exc_info)
            msg = f"{msg}\n{record.exc_text}"
        if record.stack_info:
            msg = f"{msg}\n{self.formatStack(record.stack_info)}"
        # Tint the message itself so it pops: warnings and errors keep their own
        # level color (an ERROR must stay red — GPU or not), and anything else
        # GPU-related goes yellow at INFO and DEBUG too. The LEVEL word keeps its
        # level color in both cases, so a yellow INFO line is still visibly INFO
        # rather than masquerading as a warning.
        if color and record.levelno >= logging.WARNING:
            body = f"{color}{msg}{_ANSI_RESET}"
        elif _is_gpu_line(record, msg):
            body = f"{_ANSI_GPU}{msg}{_ANSI_RESET}"
        else:
            body = msg
        line = (
            f"{_ANSI_DIM}{ts}{_ANSI_RESET} "
            f"{color}{record.levelname:<7}{_ANSI_RESET} "
            f"{_ANSI_NAME}{record.name}{_ANSI_RESET}: {body}"
        )
        return _redact(line)


def _enable_windows_ansi() -> None:
    """Enable ANSI/VT processing on the Windows console so color codes render
    (no-op off Windows; harmless if it fails — e.g. output isn't a real console)."""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        for handle in (-11, -12):  # STD_OUTPUT_HANDLE, STD_ERROR_HANDLE
            kernel32.SetConsoleMode(kernel32.GetStdHandle(handle), 7)  # +VT processing
    except Exception:
        pass


def _want_color(mode: str, stream) -> bool:
    """Decide whether the console gets ANSI colors. auto = only a real TTY (and
    not NO_COLOR); always = force (e.g. a nohup log you tail); never = off."""
    mode = (mode or "auto").lower()
    if mode == "never" or os.environ.get("NO_COLOR") is not None:
        return False
    if mode == "always":
        return True
    return bool(getattr(stream, "isatty", lambda: False)())


def _setup_logging(config) -> None:
    """Console + rotating file logging (DESIGN.md: user wants on-disk logs)."""
    level = getattr(logging, config.log_level.upper(), logging.INFO)
    plain = _RedactingFormatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(level)
    for h in list(root.handlers):
        root.removeHandler(h)
    mode = str(getattr(config, "log_color", "auto")).lower()
    console = logging.StreamHandler()
    if _want_color(mode, console.stream):
        _enable_windows_ansi()
        console.setFormatter(_ColorFormatter())
    else:
        console.setFormatter(plain)
    console.addFilter(_RoutineHealthSuccessFilter())
    try:
        from .auth_policy import AuthenticationRedactionFilter
        console.addFilter(AuthenticationRedactionFilter())
    except ImportError:
        pass
    root.addHandler(console)
    # uvicorn's lifecycle logger is (badly) NAMED "uvicorn.error" and its INFO
    # chatter ("Started server process", "running on ...") duplicates our own
    # banner — twice, once per server. Quiet it to WARNING: startup gets 8
    # lines shorter and anything that DOES appear under that scary name is
    # then genuinely a warning/error. Requires log_level=None on the uvicorn
    # Config (see _make_server) or uvicorn re-raises the level at serve time.
    logging.getLogger("uvicorn.error").setLevel(logging.WARNING)
    # 6.0.8: `gpu_log_debug: true` traces the GPU worker protocol without
    # dropping every other logger to DEBUG. The GPU failures being chased are
    # intermittent and only appear under real load, so the trace has to be
    # affordable to leave ON — which it is not if it drags httpx and the
    # watcher down with it.
    #
    # ⚠️ Only the named loggers move. Root's level is deliberately left alone:
    # a logger's own effective level decides whether a record is created, and
    # the ancestor's level is not consulted afterwards — only HANDLER levels
    # are. Raising root here would have re-enabled DEBUG everywhere, which is
    # the exact thing this key exists to avoid.
    if getattr(config, "gpu_log_debug", False):
        for name in ("cognita.gpu", "cognita.retrieval"):
            logging.getLogger(name).setLevel(logging.DEBUG)
    try:
        config.log_dir.mkdir(parents=True, exist_ok=True)
        fileh = RotatingFileHandler(
            config.log_dir / "cognita.log", maxBytes=5_000_000, backupCount=5, encoding="utf-8"
        )
        # A file is never a TTY, so "auto" leaves cognita.log plain (grep-safe);
        # "always" colors it too — this IS the log you tail, so it should match.
        fileh.setFormatter(_ColorFormatter() if mode == "always" else plain)
        fileh.addFilter(_RoutineHealthSuccessFilter())
        try:
            from .auth_policy import AuthenticationRedactionFilter
            fileh.addFilter(AuthenticationRedactionFilter())
        except ImportError:
            pass
        root.addHandler(fileh)
        root.info("Logging to %s", config.log_dir / "cognita.log")
    except OSError as exc:
        root.warning("File logging disabled (%s)", exc)


# One Ctrl+C shuts down gracefully but must not hang on MCP's long-lived SSE GET
# stream: cap the wait, then uvicorn closes it and exits. Dropping a stream on
# shutdown is expected and safe (DESIGN.md §4.4).
GRACEFUL_TIMEOUT_S = 3


def _make_server(
    app, host: str, port: int, certfile: str = "", keyfile: str = ""
) -> uvicorn.Server:
    # When cert+key are given, uvicorn serves TLS on this socket directly (native
    # HTTPS — used for the admin UI on the LAN; see config.admin_tls_*).
    ssl_kwargs = {"ssl_certfile": certfile, "ssl_keyfile": keyfile} if certfile and keyfile else {}
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host=host,
            port=port,
            # log_config=None -> don't install uvicorn's own handlers/formatters.
            # Its loggers (incl. uvicorn.access) then propagate to OUR root logger,
            # so every line — startup, access, app — gets the same timestamped,
            # token-redacted format on both console and file (no more untimestamped
            # "INFO:  1.2.3.4 - ..." access lines misaligned under the timestamps).
            # log_level=None -> uvicorn must not touch logger levels at serve time:
            # levels are OURS (root from config; uvicorn.error pinned to WARNING in
            # _setup_logging — its INFO chatter duplicated our banner, twice).
            log_config=None,
            log_level=None,
            access_log=True,
            timeout_graceful_shutdown=GRACEFUL_TIMEOUT_S,
            **ssl_kwargs,
        )
    )
    return server


def _install_shutdown(servers, stopping) -> None:
    """Make ONE Ctrl+C stop every server (and let the workers die).

    uvicorn's serve() installs its OS signal handler inside capture_signals() by
    doing `signal.signal(sig, self.handle_exit)` — it does NOT go through
    install_signal_handlers(), so patching that is useless, and any handler we set
    with signal.signal() gets clobbered when the servers start. The reliable hook
    is to REPLACE each server's `handle_exit` (what capture_signals wires up) with
    one shared callback that stops BOTH servers (the old code only ever stopped
    one, so the other kept the process alive — the "first Ctrl+C does nothing"
    half of the bug).

    First press = graceful (bounded by GRACEFUL_TIMEOUT_S so an open SSE stream
    can't stall it). A second press escalates to force_exit for the impatient.
    """

    def _handle_exit(_sig=None, _frame=None) -> None:
        first = not stopping.is_set()
        if first:
            log.info("Shutdown requested — stopping servers and workers")
        for s in servers:
            if s.should_exit:  # already asked once -> hard-stop now
                s.force_exit = True
            s.should_exit = True
        stopping.set()

    for s in servers:
        s.handle_exit = _handle_exit  # what capture_signals() installs as the handler


def _build_engine_host(config, registry, acceleration_store=None):
    """4.0 core mode: one store + one shared model instance behind the engine."""
    from .embeddings import Embedder, Reranker
    from .engine_local import LocalEngineHost
    from .acceleration_profiles import current_profile
    from .gpu_probe import batch_ceiling_gb, default_probe
    from .index_scheduler import IndexScheduler, gpu_handlers_from_host
    from .parsing import ExtensionPolicy
    from .retrieval import RetrievalCore
    from .store import Store, reset_command_for

    # 13.0 §4.1: a schema-version refusal must name the exact reset command,
    # and only the deployment knows which target this container is. Compose
    # sets COGNITA_RELEASE_TARGET; unset (a developer run) leaves it None so
    # the store's generic `<your target>` wording applies rather than a
    # confidently wrong target name.
    release_target = os.environ.get("COGNITA_RELEASE_TARGET") or None
    log.info(
        "index store reset command bound to release target=%s",
        release_target or "<unset>",
    )
    store = Store(
        config.pg_dsn, embedding_dimensions=config.embedding_dimensions,
        reset_command=reset_command_for(release_target),
    )
    # One scheduler belongs to this host lifetime.  It receives the shared CPU
    # primitive and lazy per-GPU adapters; CPU-only installs retain the same
    # bounded shared lane.
    embedder = Embedder(
        config.embedding_model,
        config.embedding_dimensions,
        config.models_cache_dir,
        threads=config.embedding_threads,
        batch_size=config.embed_batch_size,
    )
    # Admin verification reuses this already-owned CPU primitive for the
    # machine-local vector canary; it never creates a second model instance.
    if acceleration_store is not None:
        acceleration_store.set_cpu_embedder(embedder.embed)
    profile = current_profile()
    gpu_probe = default_probe(profile) if config.gpu_enabled else None
    gpu_handlers = []
    if config.gpu_enabled and config.gpu_venv_python:
        gpu_handlers = gpu_handlers_from_host(
            config,
            gpu_probe,
            embedder.embed,
            # Same configured batch/VRAM gate used by the legacy host path.
            batch_ceiling_gb=batch_ceiling_gb(config.gpu_batch_size, profile),
            holder=f"host:{id(config)}",
        )
    scheduler = IndexScheduler(
        embedder.embed,
        gpu_devices=gpu_handlers,
        settings=config,
        dimensions=config.embedding_dimensions,
    )
    # 15.0.3: Admin's status reports a card indexing has quarantined as not
    # in use; Verify alone cannot see that.
    if acceleration_store is not None:
        acceleration_store.set_scheduler_cards(scheduler.card_states)
    core = RetrievalCore(
        store,
        embedder,
        Reranker(
            config.reranker_model,
            config.models_cache_dir,
            threads=config.embedding_threads,
        ),
        chunk_size=config.chunk_size,
        chunk_overlap=config.chunk_overlap,
        exclude_patterns=config.index_exclude_patterns,
        sync_conflict_patterns=config.sync_conflict_patterns,
        category_mappings=config.category_mappings,
        keyword_routes=config.keyword_routes,
        # Global fallback; per-project overrides are applied at engine startup.
        default_policy=ExtensionPolicy.build(
            config.indexed_extensions, config.registered_extensions
        ),
        gpu_min_chunks=config.gpu_min_chunks,
        # 6.0: the GPU path is handed the config only when it is BOTH enabled
        # and pointed at a worker environment. Passing None means the core never
        # considers a GPU at all, which is the state on every machine that has
        # not deliberately built one — and the probe defaults to Null, so no
        # device is ever discovered by accident.
        gpu_config=config if (config.gpu_enabled and config.gpu_venv_python) else None,
        gpu_probe=gpu_probe,
        scheduler=scheduler,
    )
    return LocalEngineHost(config, registry, core)


def _load_authentication_policy(config, registry):
    """Load the one parent-owned policy store, migrating 10.4 state once.

    Legacy registry hashes are counted for operator diagnostics only.  They are
    deliberately never passed to the policy store and never become production
    credentials.  ``authentication_path`` is the sole durable policy location.
    """
    from .auth_policy import migrate_authentication_policy

    ignored = sum(bool(getattr(project, "token_sha256", "")) for project in registry.projects)
    explicit = getattr(config, "oauth_enabled_provenance", "default") != "default"
    store = migrate_authentication_policy(
        config.authentication_path,
        legacy_oauth_enabled=bool(config.oauth_enabled) if explicit else None,
        ignored_legacy_credentials=ignored,
        project_names=[project.name for project in registry.projects],
    )
    if ignored:
        log.warning("Legacy project credential hashes are inert; ignored_count=%d", ignored)
    orphaned = store.orphaned_project_entries([project.name for project in registry.projects])
    if orphaned:
        log.warning("Authentication policy contains inert orphan entries count=%d", len(orphaned))
    return store


def _authentication_store_kwarg(factory, store) -> dict:
    """Pass the shared store only to slices that expose the 11.0 seam.

    Keeping this compatibility shim lets older isolated test factories start
    while gateway/Admin are migrated independently, without creating a second
    policy store in either runtime.
    """
    import inspect

    try:
        parameters = inspect.signature(factory).parameters
    except (TypeError, ValueError):
        return {}
    return {"authentication_store": store} if "authentication_store" in parameters else {}


async def _serve_async(
    config,
    *,
    config_path: Path | None = None,
    revoke_oauth_tokens_on_start: bool = False,
) -> None:
    from .acceleration import AccelerationStore, acceleration_path, preflight_legacy_env
    from .acceleration_profiles import current_profile
    from .admin_api import create_admin_app
    from .admin_auth import verify_login
    from .auth_policy import CredentialAdminService, CredentialPolicyStore
    from .bridge import BridgeService
    from .config import configured_config_path, ensure_oauth_service_key
    from .connectors import ConnectorStore, WorkspaceConnectorStore
    from .gateway import create_gateway_app
    from .oauth_service_client import OAuthServiceClient
    from .oauth_service_process import OAuthServiceSupervisor
    from .registry import Registry
    from .runtime_broker.network import BraveSearchService, brave_http_search
    from .workspace import (
        BrokerRuntimeClient,
        MountedFilesystemCapacity,
        WorkspaceManager,
        WorkspaceMetadataStore,
    )
    from .workspace_admin import WorkspaceAdminAdapter

    preflight_legacy_env()

    # Acceleration policy is loaded once by the parent and projected into the
    # existing scheduler/OCR config fields.  Legacy cognita.yaml values and
    # host interpreter paths never outrank the Admin-owned record.
    acceleration_store = AccelerationStore(
        acceleration_path(config),
        legacy_config_path=config_path or configured_config_path(),
        runtime_config=config,
    )
    loaded_acceleration = acceleration_store.loaded
    # 15.0: the profile descriptor is the one place that normalizes the
    # environment variable (unknown -> cpu with one warning). "Has a GPU" is the
    # profile's `gpu` flag rather than a literal `== "amd"`, so the nvidia
    # profile projects its Admin-owned record the same way amd always did.
    deployment_profile = current_profile()
    log.info("acceleration profile=%s gpu=%s", deployment_profile.name,
             deployment_profile.gpu)
    config.gpu_enabled = bool(loaded_acceleration.knowledge.gpu_enabled and deployment_profile.gpu)
    config.gpu_device_ids = list(loaded_acceleration.knowledge.gpu_device_ids)
    config.gpu_cards = "all"
    # Worker paths are image-owned release metadata, never migrated host paths
    # or Admin fields. CPU profiles use the packaged CPU OCR runtime; only
    # embedding GPU acceleration is disabled by the profile projection.
    config.gpu_venv_python = "/opt/cognita-runtimes/embed/bin/python"
    config.ocr_python = "/opt/cognita-runtimes/ocr/bin/python"
    # 14.2.0 (DESIGN-LINUX-INSTALLER 6.5, D5): the weights are downloaded into the model-cache mount
    # by the installer / `release.py deploy`, not baked into the image; the qualification MANIFEST
    # (versions plus expected hashes) stays image-owned below. (Superseded: the model directory was
    # /opt/cognita-models/easyocr inside the image.)
    config.ocr_model_dir = "/var/lib/cognita/models/easyocr"
    config.ocr_qualification_manifest = Path(
        "/opt/cognita-models/easyocr-qualification.json"
    )
    config.ocr_launcher = []
    config.ocr_device = loaded_acceleration.ocr.device if deployment_profile.gpu else "cpu"
    config.ocr_gpu_device_ids = list(loaded_acceleration.ocr.gpu_device_ids)
    config.ocr_gpu_device_id = ""

    registry = Registry(config.registry_path)
    authentication_store = _load_authentication_policy(config, registry)
    registry.attach_authentication_store(authentication_store)
    oauth_required_at_start = authentication_store.oauth_runtime_required(
        project.name for project in registry.projects if project.enabled
    )
    if revoke_oauth_tokens_on_start and not oauth_required_at_start:
        raise RuntimeError(
            "--revoke-oauth-tokens-on-start requires effective OAuth policy for "
            "at least one enabled project"
        )
    connector_store = ConnectorStore(config.connectors_path)
    policy_root = Path(config.authentication_path).parent
    credential_store = CredentialPolicyStore(
        policy_root / "credentials-v2.json",
        master_key_dir=policy_root / "master-keys",
        admin_password_hash=config.admin_password_hash or None,
        admin_password_verifier=(
            lambda password: verify_login(config, config.admin_username, password)
        ) if (config.admin_password_hash or config.admin_password_sha256) else None,
    )
    workspace_connector_store = WorkspaceConnectorStore(
        policy_root / "workspace-connectors.yaml"
    )
    workspace_metadata = None
    workspace_manager = None
    workspace_admin = None
    bridge_service = None
    runtime_url = os.environ.get("COGNITA_WORKSPACE_RUNTIME_URL", "").strip()
    broker_secret_file = os.environ.get("COGNITA_INTERNAL_BEARER_FILE", "").strip()
    if runtime_url and broker_secret_file:
        try:
            broker_secret = Path(broker_secret_file).read_text(encoding="ascii").strip()
        except (OSError, UnicodeError) as exc:
            raise RuntimeError("Workspace broker secret file is unavailable") from exc
        if (
            len(broker_secret.encode("ascii", errors="ignore")) < 32
            or not broker_secret.isascii()
            or any(character.isspace() for character in broker_secret)
        ):
            raise RuntimeError("Workspace broker secret is invalid")
        workspace_metadata = WorkspaceMetadataStore(
            Path(config.data_root) / "workspace-metadata.sqlite3"
        )
        brave_search = BraveSearchService(brave_http_search)
        # Only the migration-owned marker is mounted into Cognita.  It lives
        # on the same host filesystem as the broker's Workspace root, so
        # statvfs can measure capacity without exposing guest files to this
        # process.  A missing, inaccessible, or invalid marker makes strict
        # admission fail closed while metadata-only reads remain available.
        workspace_root = os.environ.get("COGNITA_WORKSPACE_DATA_ROOT", "").strip() or None
        capacity_marker = os.environ.get(
            "COGNITA_WORKSPACE_CAPACITY_MARKER",
            "/run/cognita/workspace-capacity.marker",
        ).strip() or None
        capacity_provider = (
            MountedFilesystemCapacity(capacity_marker)
            if capacity_marker is not None else None
        )
        workspace_manager = WorkspaceManager(
            workspace_metadata,
            BrokerRuntimeClient(runtime_url, broker_secret),
            capacity_provider=capacity_provider,
            host_root=workspace_root,
            strict_capacity=True,
            brave_search=brave_search,
            credential_is_active=credential_store.admission_status,
        )
        workspace_admin = WorkspaceAdminAdapter(
            workspace_manager,
            host_root=workspace_root,
            trusted_secret_store=credential_store,
            brave_search=brave_search,
        )
    elif runtime_url or broker_secret_file:
        raise RuntimeError(
            "Workspace runtime requires both COGNITA_WORKSPACE_RUNTIME_URL and "
            "COGNITA_INTERNAL_BEARER_FILE"
        )

    log.info(
        "Workspace host capability established",
        extra={"workspace_mode": "full" if workspace_manager is not None else "core"},
    )

    # Materialize 11.x digest-only credentials as exact-surface legacy rows.
    # This is idempotent and preserves authentication without allowing a
    # global/project digest to bypass the connector slug selected by the URL.
    legacy_policy = authentication_store.snapshot()
    combined_surfaces = connector_store.snapshot(registry.projects).connectors
    for surface in combined_surfaces:
        if legacy_policy.global_.static_key is not None:
            credential_store.migrate_legacy_digest(
                legacy_policy.global_.static_key.digest,
                surface_kind="combined",
                surface_id=surface.id,
                surface_slug=surface.slug,
                scope="global",
            )
        for project_name, project_policy in legacy_policy.projects.items():
            if project_policy.static_key is not None:
                credential_store.migrate_legacy_digest(
                    project_policy.static_key.digest,
                    surface_kind="combined",
                    surface_id=surface.id,
                    surface_slug=surface.slug,
                    scope=f"project:{project_name}",
                )

    def resolve_surface(surface_kind: str, surface_id: str):
        if surface_kind == "combined":
            snapshot = connector_store.snapshot(registry.projects)
            return next((item for item in snapshot.connectors if item.id == surface_id), None)
        if surface_kind == "workspace":
            snapshot = workspace_connector_store.snapshot()
            return next(
                (item for item in snapshot.workspace_connectors if item.id == surface_id),
                None,
            )
        return None

    credential_admin = CredentialAdminService(
        credential_store,
        surface_resolver=resolve_surface,
        public_base_url=config.public_base_url,
        workspace_lifecycle=workspace_manager,
    )

    oauth_client = None
    oauth_supervisor = None
    try:
        oauth_key = ensure_oauth_service_key(config)
    except (OSError, RuntimeError, ValueError) as exc:
        # Keep static-only deployments and the protected Admin surface alive.
        # An attempt to enable OAuth will fail readiness before policy commits.
        log.error("OAuth service key unavailable during startup: %s", type(exc).__name__)
        oauth_key = b""
    oauth_client = OAuthServiceClient(
        f"http://127.0.0.1:{config.oauth_service_port}",
        internal_client_id="cognita-internal",
        internal_client_secret=oauth_key.hex(),
        timeout=config.oauth_service_request_timeout_s,
        public_base_url=config.public_base_url,
        allowed_redirect_hosts=config.oauth_allowed_client_hosts,
    )
    oauth_supervisor = OAuthServiceSupervisor(
        config,
        client=oauth_client,
        # The child must load the same operator-selected config as this parent.
        # In containers the configured path is mounted separately from the
        # package's default path, so falling back to DEFAULT_CONFIG_PATH makes
        # the child fail closed even while the parent has valid credentials.
        config_path=config_path or configured_config_path(),
        revoke_all_on_start=revoke_oauth_tokens_on_start,
    )
    log.debug("OAuth child config path selected path=%s", oauth_supervisor.config_path)
    oauth_client.supervisor = oauth_supervisor

    engine = _build_engine_host(config, registry, acceleration_store)

    if workspace_manager is not None:
        # The bridge is a production service, not merely a catalog adapter.
        # It shares the principal-scoped Workspace manager and uses the
        # existing in-process Knowledge write lock. (Before 14.0.0 the legacy
        # worker mode had no cross-process lock seam for direct filesystem
        # commits, so the bridge stayed fail-closed there instead of writing
        # around the Knowledge service. That mode is gone.)
        async def reconcile_bridge_paths(project_name: str, paths: list[str]):
            project = registry.get(project_name)
            if project is None:
                raise RuntimeError("bridge project is no longer registered")
            return await engine.core.reconcile_paths(
                project_name,
                Path(project.documents_dir),
                paths,
                walk="copy_from_workspace",
            )

        bridge_service = BridgeService(
            workspace_manager,
            staging_root=os.environ.get("COGNITA_TRANSFER_STAGING_ROOT") or None,
            knowledge_core=engine.core,
            watcher=engine.watcher,
            reconcile=reconcile_bridge_paths,
            backup_keep=config.backup_keep_per_file,
        )

    gateway = _make_server(
        create_gateway_app(
            config, registry, engine=engine, oauth_client=oauth_client,
            connector_store=connector_store,
            credential_store=credential_store,
            workspace_connector_store=workspace_connector_store,
            workspace_service=workspace_manager,
            bridge_service=bridge_service,
            **_authentication_store_kwarg(create_gateway_app, authentication_store),
        ),
        config.mcp_host,
        config.mcp_port,
    )
    admin = _make_server(
        create_admin_app(
            config, registry, engine=engine, oauth_client=oauth_client,
            connector_store=connector_store,
            credential_store=credential_admin,
            workspace_connector_store=workspace_connector_store,
            workspace_admin_service=workspace_admin,
            acceleration_store=acceleration_store,
            oauth_supervisor=oauth_supervisor,
            **_authentication_store_kwarg(create_admin_app, authentication_store),
        ),
        config.admin_host,
        config.admin_port,
        config.admin_tls_certfile,
        config.admin_tls_keyfile,
    )
    from .admin_auth import admin_auth_configured, is_loopback

    admin_note = (
        "auth: login required"
        if admin_auth_configured(config)
        else ("localhost only" if is_loopback(config.admin_host) else "OPEN — NO AUTH")
    )
    admin_scheme = "https" if (config.admin_tls_certfile and config.admin_tls_keyfile) else "http"
    log.info("Cognita v%s", __version__)
    log.info(
        "Auth policy : OAuth %s; static keys %s",
        "enabled" if oauth_required_at_start else "disabled",
        "configured" if any(
            authentication_store.effective_static_key(project.name) is not None
            for project in registry.projects if project.enabled
        ) else "not configured",
    )
    log.info("Engine      : retrieval core (PostgreSQL)")
    log.info("MCP gateway : http://%s:%s/mcp", config.mcp_host, config.mcp_port)
    log.info(
        "Admin UI    : %s://%s:%s/  (%s)",
        admin_scheme,
        config.admin_host,
        config.admin_port,
        admin_note,
    )
    log.info("Projects    : %d registered", len(registry.projects))
    log.info("Workspace   : %s", "configured" if workspace_manager is not None else "unavailable")
    # 6.4.1: the card list, with the indices `gpu_cards` takes. It belongs in
    # the banner rather than behind the first GPU job, because the person who
    # needs it is configuring the box, not reading a walk's output.
    from .gpu_probe import log_available_cards

    log_available_cards(config)
    # Connect the store BEFORE serving: a gateway with no store is a 500
    # factory, and failing fast beats serving errors (Postgres down =
    # clean startup error, per DESIGN-4.0 §6 risk 2).
    await engine.startup()
    if oauth_required_at_start:
        await oauth_supervisor.start()

    stopping = asyncio.Event()
    # capture_signals() reads server.handle_exit at serve() entry, so wire our
    # shared handler onto both servers BEFORE starting them.
    _install_shutdown((gateway, admin), stopping)

    async def _stop_oauth_on_shutdown() -> None:
        await stopping.wait()
        if oauth_supervisor is not None:
            await oauth_supervisor.stop()

    oauth_shutdown_task = (
        asyncio.create_task(_stop_oauth_on_shutdown(), name="cognita-oauth-shutdown")
        if oauth_supervisor is not None else None
    )

    retention_task = (
        asyncio.create_task(
            _workspace_retention_loop(credential_store, workspace_manager, stopping),
            name="cognita-workspace-retention",
        ) if workspace_manager is not None else None
    )

    async def _keepalive() -> None:
        # On Windows the Proactor loop won't run a pending Ctrl+C handler until
        # it wakes for I/O or a timer; this tick guarantees prompt shutdown.
        while not stopping.is_set():
            await asyncio.sleep(0.2)

    try:
        await asyncio.gather(gateway.serve(), admin.serve(), _keepalive())
    finally:
        stopping.set()
        if retention_task is not None:
            await retention_task
        if oauth_shutdown_task is not None:
            await oauth_shutdown_task
        await engine.shutdown()
        if workspace_metadata is not None:
            workspace_metadata.close()


async def _workspace_retention_loop(credential_store, manager, stopping: asyncio.Event) -> None:
    """Quiet, bounded startup and periodic reconciliation of durable intents."""
    credential_cursor = ""
    workspace_cursor = ""
    while not stopping.is_set():
        try:
            credentials = await asyncio.to_thread(
                credential_store.reconcile_tombstones, manager,
                limit=32, after_credential_id=credential_cursor,
            )
            credential_cursor = credentials[-1]["credential_id"] if len(credentials) == 32 else ""
            result = await asyncio.to_thread(
                manager.cleanup_retention, apply=True, limit=32,
                after_workspace_id=workspace_cursor,
            )
            workspace_cursor = result["next_cursor"]
            deleted = sum(item["status"] == "deleted" for item in result["items"])
            if deleted:
                log.info("Workspace retention removed %d due Workspace(s)", deleted)
        except Exception:
            # Keep the service up; durable intents retry on the next tick.
            log.exception("Workspace retention pass deferred")
        try:
            await asyncio.wait_for(stopping.wait(), timeout=300)
        except TimeoutError:
            pass


def cmd_workspace_cleanup(args: argparse.Namespace) -> int:
    """Operator preview/apply of a single bounded page; never accepts paths or IDs."""
    from .config import load_config
    from .workspace import BrokerRuntimeClient, WorkspaceManager, WorkspaceMetadataStore

    config = load_config()
    metadata_path = Path(config.data_root) / "workspace-metadata.sqlite3"
    if not metadata_path.is_file():
        raise SystemExit("Workspace metadata is unavailable")
    runtime = None
    if args.apply:
        runtime_url = os.environ.get("COGNITA_WORKSPACE_RUNTIME_URL", "").strip()
        secret_file = os.environ.get("COGNITA_INTERNAL_BEARER_FILE", "").strip()
        if not runtime_url or not secret_file:
            raise SystemExit("Workspace broker is not configured; no cleanup applied")
        try:
            secret = Path(secret_file).read_text(encoding="ascii").strip()
        except (OSError, UnicodeError) as exc:
            raise SystemExit("Workspace broker secret is unavailable") from exc
        if len(secret) < 32 or not secret.isascii() or any(character.isspace() for character in secret):
            raise SystemExit("Workspace broker secret is invalid")
        runtime = BrokerRuntimeClient(runtime_url, secret)
    metadata = WorkspaceMetadataStore(metadata_path, read_only=not args.apply)
    try:
        manager = WorkspaceManager(
            metadata, runtime, host_root=os.environ.get("COGNITA_WORKSPACE_DATA_ROOT") or None,
        )
        result = manager.cleanup_retention(
            apply=args.apply, limit=args.limit, after_workspace_id=args.after_workspace_id,
        )
        print(json.dumps({"mode": "apply" if args.apply else "dry-run", **result}, sort_keys=True))
    finally:
        metadata.close()
    return 0


def _require_admin_auth_if_exposed(config) -> None:
    """Refuse to serve an unauthenticated admin surface on a non-loopback bind.

    The admin API can read the filesystem, mint tokens, and delete indexes. If
    it's bound anywhere other than loopback, a password is mandatory — otherwise
    we fail fast rather than silently expose it (DESIGN.md §6/§8).
    """
    from .admin_auth import admin_auth_configured, is_loopback

    if not is_loopback(config.admin_host) and not admin_auth_configured(config):
        raise SystemExit(
            f"Refusing to start: admin_host={config.admin_host!r} is not loopback but no "
            "admin password is set. Run scripts/set-admin-credentials.py to write an "
            "Argon2id verifier to config/cognita.yaml "
            "(run scripts/set-admin-credentials.*) to expose the admin UI safely, or set "
            "admin_host to 127.0.0.1."
        )
    # Exposed on the LAN with a login password but no TLS -> the password rides
    # in cleartext on submit. Not fatal (a trusted LAN or upstream TLS may be
    # intended), but loud: native TLS (admin_tls_certfile/keyfile) is the fix.
    tls_on = bool(config.admin_tls_certfile and config.admin_tls_keyfile)
    if not is_loopback(config.admin_host) and not tls_on:
        log.warning(
            "Admin UI is exposed on %s over PLAIN HTTP — the login password will "
            "cross the network in cleartext. Set admin_tls_certfile/admin_tls_keyfile "
            "(e.g. a mkcert cert) to serve HTTPS.",
            config.admin_host,
        )


def _validate_tls_config(config) -> None:
    """Fail fast on a half-configured or missing admin TLS cert, so a typo is a
    clean startup error instead of an opaque uvicorn SSL exception at serve time."""
    cert, key = config.admin_tls_certfile, config.admin_tls_keyfile
    if bool(cert) != bool(key):
        raise SystemExit(
            "admin_tls_certfile and admin_tls_keyfile must BOTH be set (or both empty)."
        )
    for label, p in (("admin_tls_certfile", cert), ("admin_tls_keyfile", key)):
        if p and not Path(p).is_file():
            raise SystemExit(f"{label} not found: {p!r}")


def cmd_serve(args: argparse.Namespace) -> int:
    from .config import configured_config_path, load_config

    config_path = configured_config_path()
    config = load_config(config_path)
    # SUPERSEDED (13.0 §7.3): 11.0 retired `--test` because it selected the
    # static-token mode whose gateway fallback is now deleted. The flag is
    # reused here for the 13.0 test window, which admits ONE public key, for
    # ONE project, for 30 minutes. (The `config.test_mode = False` that used to
    # stand here went with the field itself; there is nothing left to pin off.)
    env_test_mode = os.environ.get("COGNITA_TEST_MODE", "")
    config.self_test_mode = env_test_mode == "1" or bool(getattr(args, "test", False))
    if args.stdio:
        # 14.0.0: stdio mode spawned the retired 3.x engine (already refused
        # under core since 4.0, DESIGN-4.0 D4.11) and is gone entirely. The flag
        # stays so a launcher that still passes it gets this sentence instead of
        # argparse's "unrecognized arguments".
        log.info("serve --stdio refused: stdio mode was removed in 14.0.0 (project=%r)",
                 getattr(args, "project", None))
        raise SystemExit(
            "stdio mode was removed in Cognita 14.0.0. Claude Code and other CLI "
            "clients connect over HTTP to /mcp with a key."
        )
    log.info(
        "serve test_mode=%s source=%s (env COGNITA_TEST_MODE=%r, --test=%s)",
        config.self_test_mode,
        "env" if env_test_mode == "1" else ("flag" if getattr(args, "test", False) else "off"),
        env_test_mode, bool(getattr(args, "test", False)),
    )
    revoke_on_start = bool(getattr(args, "revoke_oauth_tokens_on_start", False))
    _validate_tls_config(config)
    _require_admin_auth_if_exposed(config)
    try:
        asyncio.run(
            _serve_async(
                config,
                config_path=config_path,
                revoke_oauth_tokens_on_start=revoke_on_start,
            )
        )
    except KeyboardInterrupt:
        log.info("Shutting down (Ctrl+C)")
    except Exception:
        # Log startup/runtime crashes (e.g. Postgres unreachable) to cognita.log
        # so a detached run's failure is diagnosable without a separate console
        # capture — the log file is the single source of truth.
        log.exception("Cognita exited with an error")
        return 1
    return 0


def cmd_gen_token(_args: argparse.Namespace) -> int:
    from .connectors import PUBLIC_CONTRACT_VERSION

    print(
        "The gen-token command was retired in Cognita 9.0. "
        "Create or migrate a connector with `cognita migrate-connectors --dry-run`, "
        f"then copy its canonical /mcp/connectors/<connector-slug>/mcp/v{PUBLIC_CONTRACT_VERSION} "
        "URL from the Admin UI "
        "and authorize it in Codex or ChatGPT. Static keys are managed through the "
        "Admin authentication policy.",
        file=sys.stderr,
    )
    return 2


def _setup_probe_logging() -> None:
    """Configure stderr-only redacted logging without loading production config."""
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()
    handler = logging.StreamHandler()
    handler.setFormatter(_RedactingFormatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
    root.addHandler(handler)


def cmd_probe_assets(args: argparse.Namespace) -> int:
    """Run the disposable asset-handoff probe as a standalone process."""
    import socket

    import uvicorn

    from .asset_probe import build_probe_app

    if args.host not in {"127.0.0.1", "localhost"}:
        raise SystemExit("probe-assets must bind to loopback (127.0.0.1 or localhost)")

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind((args.host, args.port))
        sock.listen()
        sock.setblocking(False)
        actual_port = sock.getsockname()[1]
        try:
            app, state = build_probe_app(public_origin=args.public_base_url)
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
        origin = args.public_base_url or f"http://{args.host}:{actual_port}"
        print(f"Cognita disposable asset probe: {origin.rstrip('/')}/mcp/KEI", flush=True)
        server = uvicorn.Server(
            uvicorn.Config(
                app,
                host=args.host,
                port=actual_port,
                log_config=None,
                log_level=None,
                access_log=False,
                timeout_graceful_shutdown=2,
            )
        )
        try:
            return asyncio.run(server.serve(sockets=[sock])) or 0
        except KeyboardInterrupt:
            log.info("Disposable asset probe stopped")
            return 0
        finally:
            state.close()
    finally:
        sock.close()


def cmd_project_list(_args: argparse.Namespace) -> int:
    from .config import load_config
    from .connectors import PUBLIC_CONTRACT_VERSION
    from .registry import Registry

    config = load_config()
    registry = Registry(config.registry_path)
    if not registry.projects:
        print("No projects registered.")
        return 0
    for p in registry.projects:
        state = "enabled" if p.enabled else "DISABLED"
        print(f"  {p.name:<20} [{state}]  docs={p.documents_dir}")
    print(
        "\nProject API keys and project-bound connector URLs are retired in 9.0. "
        "Use `cognita migrate-connectors --dry-run` to inspect the unified policy, "
        f"then copy /mcp/connectors/<connector-slug>/mcp/v{PUBLIC_CONTRACT_VERSION} "
        "from the Admin UI."
    )
    return 0


def cmd_migrate_connectors(args: argparse.Namespace) -> int:
    """Migrate legacy per-project writable settings into connectors.yaml.

    Dry-run is intentionally read-only and prints only IDs/counts, never YAML
    contents, paths, credentials, or project data.
    """
    from .config import load_config
    from .connectors import ConnectorStore, PolicyUnavailable, migrate_connectors
    from .registry import Registry

    config = load_config()
    registry_path = Path(args.registry) if args.registry else config.registry_path
    connector_path = Path(args.connectors) if args.connectors else config.connectors_path
    try:
        result = migrate_connectors(
            Registry(registry_path), ConnectorStore(connector_path), dry_run=bool(args.dry_run)
        )
    except (PolicyUnavailable, ValueError, OSError) as exc:
        raise SystemExit(f"Connector migration failed: {exc}") from exc
    print(f"connector migration {'dry-run' if result.dry_run else 'applied'}")
    print(f"revision: {result.revision}")
    if result.connector_id:
        print(f"connector_id: {result.connector_id}")
    for item in result.plan:
        print(f"plan: {item}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="cognita", description="Cognita RAG gateway")
    parser.add_argument("--version", action="version", version=f"cognita {__version__}")
    sub = parser.add_subparsers(dest="command")

    p_serve = sub.add_parser("serve", help="run the gateway + admin UI")
    p_serve.add_argument(
        # 13.0 §7.3: the same test mode COGNITA_TEST_MODE=1 selects. Kept out
        # of --help: this is release-tooling material, not an operator switch.
        # (Superseded: 11.0 through 12.x refused this flag outright, because it
        # used to select the retired static-token mode.)
        "--test",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    p_serve.add_argument("--stdio", action="store_true", help="removed in 14.0.0; refused with an explanation")
    p_serve.add_argument(
        "--revoke-oauth-tokens-on-start",
        action="store_true",
        help="one-shot emergency revocation before the OAuth child becomes ready",
    )
    p_serve.add_argument("--project", help="removed in 14.0.0 along with stdio mode; ignored")
    p_serve.set_defaults(func=cmd_serve)

    p_gen = sub.add_parser("gen-token", help="retired: use a unified OAuth connector")
    p_gen.set_defaults(func=cmd_gen_token)

    p_probe = sub.add_parser("probe-assets", help="run the disposable Phase 0 PNG handoff probe")
    p_probe.add_argument("--host", default="127.0.0.1", help="local bind address")
    p_probe.add_argument(
        "--port", type=int, default=0, help="local port (0 chooses an unused port)"
    )
    p_probe.add_argument(
        "--public-base-url",
        default=None,
        help="external HTTPS origin supplied by a temporary narrowly routed tunnel",
    )
    p_probe.set_defaults(func=cmd_probe_assets)
    p_proj = sub.add_parser("project", help="manage projects")
    proj_sub = p_proj.add_subparsers(dest="project_command", required=True)
    p_list = proj_sub.add_parser("list", help="list registered projects")
    p_list.set_defaults(func=cmd_project_list)

    p_cleanup = sub.add_parser("workspace-cleanup", help="preview or apply one bounded retention page")
    p_cleanup.add_argument("--apply", action="store_true", help="apply due deletions (default: read-only dry-run)")
    p_cleanup.add_argument("--limit", type=int, choices=range(1, 257), default=32)
    p_cleanup.add_argument("--after-workspace-id", default="", help="cursor from a prior page")
    p_cleanup.set_defaults(func=cmd_workspace_cleanup)

    p_migrate = sub.add_parser(
        "migrate-connectors", help="migrate project writable settings into connectors.yaml"
    )
    p_migrate.add_argument("--dry-run", action="store_true", help="inspect the migration without writing")
    p_migrate.add_argument("--registry", help="override the project registry path")
    p_migrate.add_argument("--connectors", help="override the connectors configuration path")
    p_migrate.set_defaults(func=cmd_migrate_connectors)

    args = parser.parse_args(argv)
    if args.command == "probe-assets":
        # This command must be usable before a production configuration
        # exists and must not create files beneath the source installation.
        # (Superseded: `migrate-12` shared this branch until 13.0 retired it.)
        _setup_probe_logging()
    else:
        from .config import load_config

        _setup_logging(load_config())
    if not getattr(args, "func", None):
        # no subcommand: print help (explicit `serve` is required — an implicit
        # default would make an accidental bare `cognita` race the running
        # production instance for its ports)
        parser.print_help()
        return 1
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
