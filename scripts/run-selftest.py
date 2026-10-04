"""Execute the Cognita self-test plan against a live gateway, mechanically.

The chat-side self-test has Claude read get_self_test_plan and follow it; this
script is the connector-free equivalent (DESIGN-4.0 D4.11). Full mode runs the
Knowledge plan and every caller-callable Workspace/bridge section over
streamable-http JSON-RPC. Core mode asserts the reduced catalog and bounded
stale Workspace calls without contacting a broker. Both modes verify each
applicable expectation, including the pinned H0-H4 hash chain.

    COGNITA_TEST_API_KEY=<key> python scripts/run-selftest.py --mode full http://127.0.0.1:8875/mcp

Exit code 0 = every step passed.
"""

import argparse
import base64
import hashlib
import json
import os
import time
import uuid
import re
import stat
import unicodedata
from pathlib import Path

import httpx

# 13.0 §7.1/§8: this runner imports the INSTALLED cognita package. The
# `sys.path.insert(<checkout>/src)` that used to be here made a checkout beside
# the script shadow the package under test, so a run inside the app container
# could silently have proven the wrong tree. If these imports fail, the package
# is not installed — that is the answer, not something to work around.
from cognita.auth_policy import SELF_TEST_API_KEY, SELF_TEST_PROJECT_NAME
from cognita.proxy import BRIDGE_TOOL_NAMES, PUBLIC_TOOL_NAMES, WORKSPACE_TOOL_NAMES
from cognita.selftest import (
    _TEST_CONTENT,
    ASSET_RUN_A,
    ASSET_TEST_DATA_URL,
    ASSET_TEST_SHA256,
    ASSET_TEST_SIZE,
    BYTES_FILE,
    EXPECTED_HASHES,
    SELF_TEST_CRLF_BASE64,
    SELF_TEST_CRLF_BYTES,
    TEST_FILE,
    TEST_FILE_MOVED,
)


class McpClient:
    """Minimal streamable-http MCP client: JSON or SSE responses, optional session."""

    def __init__(self, url: str, token: str):
        self.url = url
        self.http = httpx.Client(timeout=120.0, headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        })
        self.session_id: str | None = None
        self._id = 0
        self.project: str | None = None
        self.tool_call_count = 0
        self.tool_response_bytes = 0

    def _post(self, message: dict) -> httpx.Response:
        headers = {"mcp-session-id": self.session_id} if self.session_id else {}
        r = self.http.post(self.url, json=message, headers=headers)
        if sid := r.headers.get("mcp-session-id"):
            self.session_id = sid
        return r

    @staticmethod
    def _payload(r: httpx.Response) -> dict:
        r.raise_for_status()
        body = r.text
        if r.headers.get("content-type", "").startswith("text/event-stream"):
            for line in body.splitlines():  # last data: frame wins
                if line.startswith("data:"):
                    body = line[5:].strip()
        return json.loads(body)

    def initialize(self) -> dict:
        self._id += 1
        r = self._post({"jsonrpc": "2.0", "id": self._id, "method": "initialize",
                        "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                                   "clientInfo": {"name": "cognita-selftest-runner",
                                                  "version": "1.0"}}})
        result = self._payload(r)["result"]
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return result

    def tools_list(self) -> list[dict]:
        self._id += 1
        body = self._payload(self._post({
            "jsonrpc": "2.0", "id": self._id, "method": "tools/list", "params": {},
        }))
        return body.get("result", {}).get("tools", [])

    def discover_project(self) -> dict:
        """Discover live access before routing any project operation."""
        return self.call("list_projects", {}, route=False)

    def call(self, tool: str, arguments: dict | None = None, *, route: bool = True) -> dict:
        """tools/call -> the engine-style JSON payload from the text block.
        JSON-RPC errors (policy blocks) come back as {"_rpc_error": msg}."""
        args = dict(arguments or {})
        if route and tool not in {"list_projects", "batch"} and not tool.startswith("workspace_"):
            if not self.project:
                return {"status": "error", "reason": "project_not_selected",
                        "message": "list_projects must be called before project operations"}
            args.setdefault("project", self.project)
        self._id += 1
        r = self._post({"jsonrpc": "2.0", "id": self._id, "method": "tools/call",
                        "params": {"name": tool, "arguments": args}})
        self.tool_call_count += 1
        self.tool_response_bytes += len(r.content)
        body = self._payload(r)
        if "error" in body:
            return {"_rpc_error": body["error"].get("message", "")}
        content = body.get("result", {}).get("content", [])
        text_block = next(
            (item for item in content if isinstance(item, dict) and item.get("type") == "text"),
            None,
        )
        if not isinstance(text_block, dict) or not isinstance(text_block.get("text"), str):
            return {"status": "error", "reason": "invalid_mcp_result",
                    "message": "tool result did not contain a text payload"}
        payload = json.loads(text_block["text"])
        if isinstance(payload, dict):
            # Preserve the bounded MCP content metadata so image round trips can
            # prove the bytes returned over the client boundary, not merely the
            # server's accompanying text claims.
            payload["_mcp_content"] = content
            payload["_mcp_structured_content"] = body.get("result", {}).get("structuredContent")
        return payload


class Scorecard:
    def __init__(self):
        self.lines: list[str] = []
        self.failed = 0

    def check(self, step: str, ok: bool, evidence: str) -> bool:
        self.lines.append(f"  {'PASS' if ok else 'FAIL'}  {step}: {evidence}")
        if not ok:
            self.failed += 1
        return ok

    def report(self) -> int:
        print("\n".join(self.lines))
        total = len(self.lines)
        print(f"\n{total - self.failed}/{total} steps passed")
        return 1 if self.failed else 0


WORKSPACE_PUBLIC_TOOLS = frozenset(
    {
        "workspace_info", "workspace_list_files", "workspace_stat",
        "workspace_read_file", "workspace_write_file", "workspace_edit_file",
        "workspace_make_directory", "workspace_copy_paths", "workspace_move_paths",
        "workspace_remove_paths", "workspace_search", "workspace_start_job",
        "workspace_get_job", "workspace_cancel_job", "workspace_web_search",
        "copy_to_workspace", "copy_from_workspace", "add_document",
        "read_document", "remove_document", "batch",
    }
)
WORKSPACE_SECTIONS = ("W", "W1", "W2", "W4", "W5", "W6", "W7", "W8", "W9", "W11", "W12")
_BOUNDED_REGEX_PROBE = {"mode": "regex", "pattern": "(a+)+$"}


def _workspace_data(payload: dict) -> dict:
    value = payload.get("data")
    return value if isinstance(value, dict) else payload


def _workspace_job(payload: dict) -> dict:
    value = payload.get("job")
    if isinstance(value, dict):
        return value
    data = _workspace_data(payload)
    value = data.get("job")
    if isinstance(value, dict):
        return value
    # The live Workspace contract returns the compact job record directly
    # under data for workspace_get_job.  Treat that shape as authoritative so
    # completed jobs are not polled until the harness timeout.
    if isinstance(data.get("job_id"), str) and isinstance(data.get("state"), str):
        return data
    return {}


def _replace_workspace_placeholders(value, replacements: dict[str, str]):
    if isinstance(value, str):
        for key, replacement in replacements.items():
            value = value.replace(f"<{key}>", replacement)
        return value
    if isinstance(value, list):
        return [_replace_workspace_placeholders(item, replacements) for item in value]
    if isinstance(value, dict):
        return {
            _replace_workspace_placeholders(key, replacements) if isinstance(key, str) else key:
            _replace_workspace_placeholders(item, replacements)
            for key, item in value.items()
        }
    return value


def _workspace_capture(
    tool: str, arguments: dict, payload: dict, replacements: dict[str, str], *, run: str,
) -> None:
    data = _workspace_data(payload)
    if tool == "workspace_write_file":
        digest = data.get("sha256") or payload.get("sha256")
        path = str(arguments.get("path", ""))
        if isinstance(digest, str) and len(digest) == 64:
            if path.endswith("/alpha.txt"):
                replacements.setdefault("ALPHA_SHA256", digest)
            if path.endswith("/conflict.txt"):
                replacements.setdefault("CONFLICT_SHA256", digest)
    if tool == "workspace_edit_file":
        digest = data.get("sha256") or data.get("new_sha256") or payload.get("new_sha256")
        if isinstance(digest, str) and len(digest) == 64:
            replacements["EDITED_SHA256"] = digest
    if tool == "workspace_start_job":
        job_id = _workspace_job(payload).get("job_id")
        key = str(arguments.get("idempotency_key", ""))
        suffix = key.rsplit("-", 1)[-1].upper()
        if suffix == "TOOLS":
            suffix = "TOOLBOX"
        elif suffix == "DENY":
            # The generated W9 plan names the follow-up local job
            # LOCAL_JOB_ID even though its idempotency key ends in -deny.
            suffix = "LOCAL"
        if isinstance(job_id, str) and suffix:
            replacements[f"{suffix}_JOB_ID"] = job_id
            if suffix == "CANCEL":
                # W12 may replay the explicitly owned cancellation fixture;
                # never leave ACTIVE_JOB_ID pointing at the first W4 job.
                replacements["ACTIVE_JOB_ID"] = job_id
            else:
                replacements.setdefault("ACTIVE_JOB_ID", job_id)
    if tool == "copy_from_workspace":
        rows = data.get("manifest", data.get("files", []))
        if isinstance(rows, list):
            paths = [row.get("path") for row in rows if isinstance(row, dict) and isinstance(row.get("path"), str)]
            if paths:
                if arguments.get("conflict_policy") == "rename":
                    committed = data.get("committed")
                    if isinstance(committed, list) and committed and isinstance(committed[-1], str):
                        replacements["RENAMED_PATH"] = committed[-1]
                    else:
                        replacements["RENAMED_PATH"] = paths[-1]
                digest = next((row.get("sha256") for row in rows if isinstance(row, dict) and isinstance(row.get("sha256"), str)), None)
                if isinstance(digest, str) and "KNOWLEDGE_DEST_SHA256" not in replacements:
                    replacements["KNOWLEDGE_DEST_SHA256"] = digest


def _normalize_workspace_bridge_args(tool: str, arguments: dict, *, run: str) -> dict:
    """Align the generated W11 example with single-file bridge destinations."""
    if tool not in {"copy_from_workspace", "read_document", "remove_document"}:
        return arguments
    bridge_name = f"cognita-workspace-bridge-{run}.md"
    plan_alpha = f".cognita-self-test/{run}/alpha.txt"
    actual_source = f".cognita-self-test/{run}/{bridge_name}"
    output_dir = f"cognita-workspace-bridge-out-{run}"
    actual_output = f"{output_dir}/{bridge_name}"
    encoded = json.dumps(arguments, separators=(",", ":"))
    encoded = encoded.replace(plan_alpha, actual_source)
    encoded = encoded.replace(f"{output_dir}/alpha.txt", actual_output)
    encoded = encoded.replace("<RENAMED_PATH>", actual_output)
    return json.loads(encoded)


def _workspace_call_satisfies_plan(
    section: str, tool: str, payload: dict, *, negative: bool,
    arguments: dict | None = None,
    expected_job_state: str | None = None,
    require_running_output: bool = False,
) -> bool:
    """Check the outcome named by the generated plan, not only tool status.

    A successful get_job call can report a failed job. The W7 concurrency
    checks also need their specific rejection reason, not just an error status.
    """
    if negative:
        # W8 deliberately uses a bounded catastrophic-regex fixture. Depending
        # on host load and the regex deadline, it may finish with no matches or
        # hit the documented search_timeout; only those two outcomes pass.
        if (
            section == "W8"
            and tool == "workspace_search"
            and isinstance(arguments, dict)
            and all(arguments.get(key) == value for key, value in _BOUNDED_REGEX_PROBE.items())
        ):
            return payload.get("status") == "success" or (
                payload.get("status") == "error"
                and payload.get("reason") == "search_timeout"
            )
        if section == "W7" and tool in {"workspace_start_job", "workspace_write_file"}:
            return payload.get("reason") == "job_running"
        return payload.get("status") == "error" or "_rpc_error" in payload
    if payload.get("status") != "success":
        return False
    if tool == "workspace_get_job":
        job = _workspace_job(payload)
        if require_running_output:
            try:
                stdout = base64.b64decode(job.get("stdout", ""), validate=True)
            except (TypeError, ValueError):
                return False
            return (
                job.get("state") == "running"
                and stdout == b"begin\n"
                and isinstance(job.get("stdout_bytes"), int)
                and job["stdout_bytes"] >= len(stdout)
                and job.get("stdout_next_offset") == len(stdout)
                and job.get("has_more") is False
            )
        if expected_job_state is not None:
            return job.get("state") == expected_job_state
    return True


def run_workspace_plan(mcp: McpClient, scorecard: Scorecard, plan_text: str) -> None:
    """Execute every caller-callable Workspace section from the live plan.

    W3 is intentionally absent from this executor: stop/restart is an
    Admin-only release check and cannot be represented as a public connector
    call. The isolated KEI runner performs that check separately.
    """
    # Unique per RUN, not per process. This tag is the whole of every
    # idempotency key in the plan, and the Workspace is deliberately reused
    # across runs (one deterministic self-test principal, §7.3), so two runs
    # that share a tag replay each other's receipts out of
    # `workspace_idempotency` instead of touching the guest.
    # Incident, 2026-09-22: `f"http-{os.getpid()}-{mcp._id}"` produced
    # `http-114-40` for two consecutive live runs -- the runner executes inside
    # a freshly recreated app container, where PIDs are small and repeat -- and
    # the second run scored 100/116 with every keyed write "succeeding" as a
    # replay while every unkeyed read correctly reported the path was absent.
    replacements = {"RUN": f"http-{uuid.uuid4().hex[:12]}"}
    current_section = None
    w7_initial_get_seen = False
    section_ok: dict[str, bool] = {section: True for section in WORKSPACE_SECTIONS}
    section_seen: set[str] = set()
    # The W heading is the public plan/index assertion.  Its only generated
    # call is get_self_test_plan, which was already validated as 0c and is not
    # a Workspace operation to replay through this loop.
    if "## W:" in plan_text:
        section_seen.add("W")
    for line in plan_text.splitlines():
        if line.startswith("## "):
            candidate = line[3:].split(":", 1)[0].strip()
            current_section = candidate if candidate in section_ok else None
            continue
        if current_section is None:
            continue
        if not (line.startswith("CALL ") or line.startswith("NEGATIVE_CALL ")):
            continue
        negative = line.startswith("NEGATIVE_CALL ")
        _, tool, encoded = line.split(" ", 2)
        if tool not in WORKSPACE_PUBLIC_TOOLS:
            continue
        try:
            arguments = json.loads(encoded)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"invalid generated Workspace self-test call: {line}") from exc
        job_token = arguments.get("job_id") if tool == "workspace_get_job" else None
        expected_job_state = {
            "<ARGV_JOB_ID>": "succeeded", "<SHELL_JOB_ID>": "succeeded",
            "<PYTHON_JOB_ID>": "succeeded", "<TOOLBOX_JOB_ID>": "succeeded",
            "<TIMEOUT_JOB_ID>": "timed_out", "<NETWORK_JOB_ID>": "failed",
            "<LOCAL_JOB_ID>": "succeeded",
        }.get(job_token)
        if current_section == "W7" and job_token == "<CANCEL_JOB_ID>" and w7_initial_get_seen:
            expected_job_state = "canceled"
        arguments = _replace_workspace_placeholders(arguments, replacements)
        arguments = _normalize_workspace_bridge_args(tool, arguments, run=replacements["RUN"])
        if tool == "workspace_cancel_job" and "<ACTIVE_JOB_ID>" in json.dumps(arguments):
            # W12 is allowed to repeat the already-owned cancellation when no
            # independent job remains; use the W7 job if the plan left a token.
            arguments = _replace_workspace_placeholders(arguments, {"ACTIVE_JOB_ID": replacements.get("CANCEL_JOB_ID", "")})
        payload = mcp.call(tool, arguments)
        if tool == "workspace_get_job":
            job_id = arguments.get("job_id")
            # W7 deliberately reads the long-running cancellation fixture once
            # while it is active, with bounded output, before issuing the
            # cancellation request.  Do not turn that first observation into a
            # five-minute terminal poll; subsequent W7 reads still poll.
            require_running_output = current_section == "W7" and not w7_initial_get_seen
            poll_terminal = not require_running_output
            if current_section == "W7":
                w7_initial_get_seen = True
            if poll_terminal:
                deadline = time.monotonic() + 300
                while payload.get("status") == "success" and _workspace_job(payload).get("state") not in {
                    "succeeded", "failed", "canceled", "timed_out", "lost",
                } and time.monotonic() < deadline:
                    time.sleep(0.25)
                    payload = mcp.call(tool, arguments)
                if time.monotonic() >= deadline:
                    payload = {"status": "error", "reason": "job_timeout", "job_id": job_id}
        status = payload.get("status")
        if tool == "workspace_web_search" and status == "error" and payload.get("reason") == "network_denied":
            ok = True  # W9 explicitly permits a configured-out Brave service.
        else:
            ok = _workspace_call_satisfies_plan(
                current_section, tool, payload, negative=negative,
                arguments=arguments,
                expected_job_state=expected_job_state,
                require_running_output=(
                    tool == "workspace_get_job" and require_running_output
                ),
            )
        section_seen.add(current_section)
        section_ok[current_section] &= ok
        evidence = f"status={status}"
        if status == "error":
            evidence += f" reason={payload.get('reason')}"
            for field in ("path", "actual_destination_hash"):
                value = payload.get(field)
                if value is None and isinstance(payload.get("data"), dict):
                    value = payload["data"].get(field)
                if value is not None:
                    evidence += f" {field}={value}"
        if tool == "workspace_get_job":
            evidence += f" job_state={_workspace_job(payload).get('state')} expected={expected_job_state}"
        if current_section == "W11" and tool == "copy_from_workspace":
            data = _workspace_data(payload)
            manifest = data.get("manifest")
            if isinstance(manifest, list) and manifest and isinstance(manifest[0], dict):
                digest = manifest[0].get("sha256")
                if isinstance(digest, str):
                    evidence += f" manifest_sha256={digest}"
            committed = data.get("committed")
            if isinstance(committed, list) and committed:
                evidence += f" committed={committed[-1]}"
            expected = arguments.get("expected_destination_hashes")
            if isinstance(expected, dict) and expected:
                expected_path, expected_hash = next(iter(expected.items()))
                evidence += f" expected_path={expected_path} expected_sha256={expected_hash}"
        scorecard.check(f"{current_section} {tool}{' negative' if negative else ''}", ok, evidence)
        _workspace_capture(tool, arguments, payload, replacements, run=replacements["RUN"])
    for section in WORKSPACE_SECTIONS:
        scorecard.check(
            f"{section} section complete",
            section in section_seen and section_ok[section],
            "caller-callable plan section executed" if section in section_seen else "section missing from live plan",
        )


def _ocr_fixture_receipts(root: Path, fixtures) -> dict:
    """Hash only provisioned synthetic paths, preserving all source timestamps."""
    if not hasattr(os, "O_NOATIME") or not hasattr(os, "O_NOFOLLOW"):
        raise RuntimeError("timestamp-preserving fixture descriptors are unavailable")
    receipts = {}
    for fixture in fixtures:
        relative = Path(fixture.path)
        if relative.is_absolute() or ".." in relative.parts:
            raise RuntimeError("fixture manifest path is outside its authorized root")
        target = root / relative
        # Refuse directory links as well as a linked leaf. Never open a path
        # outside the already authenticated Self-Test project's exact root.
        if target.resolve().is_relative_to(root.resolve()) is False:
            raise RuntimeError("fixture path escaped its authorized root")
        if any((root / Path(*relative.parts[:index])).is_symlink() for index in range(1, len(relative.parts) + 1)):
            raise RuntimeError("fixture path is linked")
        descriptor = os.open(target, os.O_RDONLY | os.O_NOATIME | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0))
        try:
            before = os.fstat(descriptor)
            if not stat.S_ISREG(before.st_mode) or before.st_size > 16 * 1024 * 1024:
                raise RuntimeError("fixture is not a bounded regular file")
            digest = hashlib.sha256()
            total = 0
            while chunk := os.read(descriptor, 1024 * 1024):
                total += len(chunk)
                if total > 16 * 1024 * 1024:
                    raise RuntimeError("fixture grew beyond its bounded receipt size")
                digest.update(chunk)
            after = os.fstat(descriptor)
            def fields(info):
                return {"sha256": digest.hexdigest(), "size": info.st_size,
                        "atime_ns": info.st_atime_ns, "mtime_ns": info.st_mtime_ns,
                        "ctime_ns": info.st_ctime_ns, "device": info.st_dev, "inode": info.st_ino}
            if fields(before) != fields(after) or digest.hexdigest() != fixture.sha256:
                raise RuntimeError("fixture source changed or disagrees with its manifest: " + fixture.path)
            receipts[fixture.path] = fields(after)
        finally:
            os.close(descriptor)
    return receipts


def _ocr_fixture_project(project: str):
    from cognita.registry import Registry
    if project != SELF_TEST_PROJECT_NAME:
        raise RuntimeError("OCR acceptance requires the exact Self-Test project")
    config_root = Path(os.environ.get("COGNITA_CONFIG_ROOT", "/app/config"))
    registry = Registry(config_root / "registry.yaml")
    selected = registry.get(SELF_TEST_PROJECT_NAME)
    if selected is None or not selected.enabled or not selected.writable:
        raise RuntimeError("OCR fixture project is unavailable")
    return registry, selected


def _ocr_fixture_root(project: str) -> Path:
    return _ocr_fixture_project(project)[1].documents_dir


def _provision_ocr_fixtures(project: str):
    """Prepare only the installed manifest's allowlist, before source receipts.

    The host generator deliberately has no Cognita dependency. Provision in
    the app, where both package bytes and registered container paths exist.
    Require an existing writable Self-Test entry; never invent a project or
    infer paths relative to this runner (which is copied alone into /tmp).
    """
    from cognita.selftest_fixtures import provision_self_test
    registry, selected = _ocr_fixture_project(project)
    return provision_self_test(
        documents_dir=selected.documents_dir, data_dir=selected.data_dir,
        registry_path=registry.path,
    )


def _ocr_cache_changes(first: dict, repeated: dict) -> list[str]:
    """Compare result and durable pipeline facts, not per-invocation bindings."""
    fields = ("sha256", "width", "height", "text", "regions", "languages", "outcome")
    changed = [key for key in fields if first.get(key) != repeated.get(key)]
    # Physical device bindings describe a worker invocation and are absent on
    # a persisted cache hit. The contract preserves the computation's device,
    # backend, model and pipeline version, not that optional diagnostic field.
    for key in ("name", "version", "model_fingerprint", "pipeline_version", "device", "backend"):
        if first.get("engine", {}).get(key) != repeated.get("engine", {}).get(key):
            changed.append("engine." + key)
    return changed


# What ocr_worker reports for each device it can run on.
_OCR_ENGINE_PAIRS = frozenset({
    ("cpu", "pytorch-cpu"), ("gpu", "pytorch-rocm"), ("gpu", "pytorch-cuda"),
})


def _ocr_search_token(text: str) -> str | None:
    # The fixed fixture begins with filename-like text. Its first regex
    # fragment is not the lexeme PostgreSQL indexes for that filename. The
    # longest extracted token is its standalone label, so this proves lexical
    # publication without searching an artificial filename fragment.
    tokens = re.findall(r"[A-Za-z0-9]{4,}", text)
    return max(tokens, key=len) if tokens else None


def run_ocr_plan(mcp: McpClient, sc: Scorecard, *, provision_fixtures: bool = False) -> bool:
    """Exercise mandatory public OCR and independent host-source receipts.

    Runs in the app container as the service user. The immutable packaged
    manifest is the fixture authority; no image/text content enters logs.
    """
    from cognita.selftest_fixtures import load_manifest
    failed_before = sc.failed
    phase = "provision" if provision_fixtures else "manifest"
    try:
        if provision_fixtures:
            prepared = _provision_ocr_fixtures(mcp.project)
            print(f"OCR fixtures: copied={len(prepared.copied)} verified={len(prepared.verified)}")
        phase = "manifest"
        fixtures = load_manifest()
        phase = "registry"
        root = _ocr_fixture_root(mcp.project)
        phase = "receipts"
        before = _ocr_fixture_receipts(root, fixtures)
    except (OSError, ValueError, RuntimeError) as exc:
        detail = f"fixture_provisioning phase={phase} type={type(exc).__name__}"
        if phase == "receipts" and isinstance(exc, OSError):
            # Log only a recognized synthetic fixture identity, not arbitrary
            # exception text or host paths that may contain personal data.
            fixture = next((row.path for row in fixtures if str(root / row.path) == exc.filename), None)
            if fixture:
                detail += f" fixture={fixture}"
        sc.check("O fixture-provisioning", False, detail)
        return False
    by_name = {Path(row.path).name: row for row in fixtures}
    canonical = by_name["canonical-clear.png"]
    def call(name, **extra):
        return mcp.call("ocr_asset", {"filepath": by_name[name].path, "languages": ["en"], **extra})
    def brief(payload):
        return f"status={payload.get('status')} reason={payload.get('reason')}"
    try:
        first = call("canonical-clear.png")
        engine = first.get("engine", {})
        text = first.get("text", "")
        regions = first.get("regions", [])
        canonical_ok = (first.get("status") == "success" and first.get("outcome") == "text"
                        and first.get("sha256") == canonical.sha256 and isinstance(text, str) and bool(text.strip())
                        and "\r" not in text and unicodedata.normalize("NFC", text) == text
                        and isinstance(regions, list) and 0 < len(regions) <= 10_000
                        and first.get("width", 0) > 0 and first.get("height", 0) > 0
                        and engine.get("name") == "easyocr" and bool(engine.get("version"))
                        and type(engine.get("pipeline_version")) is int and engine["pipeline_version"] > 0
                        # The same live self-test runs on Windows CPU and on
                        # kei, whose OCR runs on the GPU.  2026-09-28: a
                        # CPU-only check failed kei main's 13.7.0 QA on a
                        # correct gpu/pytorch-rocm result.  Accept either
                        # device, but only with its own backend.
                        and (engine.get("device"), engine.get("backend")) in _OCR_ENGINE_PAIRS
                        and bool(re.fullmatch(r"[0-9a-f]{64}", str(engine.get("model_fingerprint", "")))))
        sc.check("O1 canonical extraction", canonical_ok, brief(first))
        repeated = call("canonical-clear.png")
        changed = _ocr_cache_changes(first, repeated)
        sc.check("O2 repeat cache", repeated.get("status") == "success" and repeated.get("cache_hit") is True
                 and not changed, brief(repeated) + f" cache_hit={repeated.get('cache_hit')} changed_fields={','.join(changed) or 'none'}")
        token = _ocr_search_token(text) if isinstance(text, str) else None
        if canonical_ok and token:
            search = mcp.call("search_assets", {"query": token, "path_prefix": canonical.path, "hybrid_alpha": 0, "max_results": 10})
            published = any(row.get("filepath") == canonical.path and row.get("provenance") == "ocr"
                            and row.get("source_sha256") == canonical.sha256 for row in search.get("results", []))
            sc.check("O3 searchable OCR publication", search.get("status") == "success" and published, brief(search) + f" hits={len(search.get('results', []))} matching_source={published}")
        else:
            sc.check("O3 searchable OCR publication", False, "canonical extraction supplied no searchable token")
        blank = call("blank.png")
        blank_repeat = call("blank.png")
        sc.check("O4 blank cached no_text", blank.get("status") == blank_repeat.get("status") == "success"
                 and blank.get("outcome") == blank_repeat.get("outcome") == "no_text"
                 and blank.get("text") == blank_repeat.get("text") == ""
                 and blank.get("regions") == blank_repeat.get("regions") == []
                 and blank.get("sha256") == blank_repeat.get("sha256") == by_name["blank.png"].sha256
                 and blank_repeat.get("cache_hit") is True, brief(blank_repeat))
        for name, reason in (("malformed.png", "invalid_png"), ("animated.png", "animated_png"),
                             ("not-a-png.txt", "wrong_media_type"), ("over-limit.png", "too_large")):
            refused = call(name)
            bounded = not any(item.get("type") == "image" for item in refused.get("_mcp_content", []))
            ok = refused.get("status") == "error" and refused.get("reason") == reason and bounded
            if name == "over-limit.png":
                ok = ok and any(term in str(refused.get("message", "")).lower() for term in ("dimension", "pixel", "byte", "limit"))
            sc.check("O4 refusal " + name, ok, brief(refused))
        unsupported = call("canonical-clear.png", languages=["zz"])
        sc.check("O4 unsupported language", unsupported.get("status") == "error" and unsupported.get("reason") == "unsupported_language", brief(unsupported))
        for invalid in ("../ocr-escape.png", "/ocr-absolute.png"):
            refused = mcp.call("ocr_asset", {"filepath": invalid, "languages": ["en"]})
            sc.check("O4 invalid path", refused.get("status") == "error" and refused.get("reason") == "invalid_path", brief(refused))
        denied = mcp.call("ocr_asset", {"project": "Other-Project", "filepath": canonical.path, "languages": ["en"]})
        sc.check("O4 authorization", denied.get("status") == "error" and denied.get("reason") in {"project_unavailable", "unauthorized"}, brief(denied))
    finally:
        try:
            after = _ocr_fixture_receipts(root, fixtures)
            for fixture in fixtures:
                changed = [field for field in before[fixture.path] if before[fixture.path][field] != after[fixture.path][field]]
                sc.check("O5 source preservation " + Path(fixture.path).name, not changed, "changed=" + (",".join(changed) or "none"))
        except (OSError, ValueError, RuntimeError) as exc:
            sc.check("O5 source preservation", False, f"receipt verification failed type={type(exc).__name__}")
    return sc.failed == failed_before



def run_asset_plan(mcp: McpClient, sc: Scorecard) -> None:
    """Own one synthetic asset; preserve the public plan's historical canaries."""
    run = f"runner-{uuid.uuid4().hex[:12]}"
    asset_path = ASSET_RUN_A.format(RUN=run)
    try:
        existing_asset = mcp.call("get_asset_info", {"filepath": asset_path})
        put_args = {
            "filepath": asset_path,
            "image": {"image_url": ASSET_TEST_DATA_URL},
            "metadata": {"title": "Cognita self-test runner", "description": "runner canary",
                          "alt_text": "self-test pixel", "source": {"type": "generated"},
                          "tags": ["cognita-selftest", "runner"]},
            "metadata_action": "replace", "metadata_storage": "catalog",
            "operation_id": f"{run}-put", "overwrite": True,
            "expected_received_size": ASSET_TEST_SIZE,
            "expected_received_sha256": ASSET_TEST_SHA256,
        }
        if existing_asset.get("status") == "success" and existing_asset.get("final_sha256"):
            put_args["expected_current_sha256"] = existing_asset["final_sha256"]
        asset_a = mcp.call("put_asset", put_args)
        asset_hash = asset_a.get("final_sha256") or asset_a.get("received_sha256")
        sc.check("A1 put_asset", asset_a.get("status") == "success"
                 and asset_hash == ASSET_TEST_SHA256,
                 f"status={asset_a.get('status')} reason={asset_a.get('reason')}")

        info = mcp.call("get_asset_info", {"filepath": asset_path})
        sc.check("A2 get_asset_info", info.get("status") == "success"
                 and info.get("final_sha256") == ASSET_TEST_SHA256,
                 f"status={info.get('status')} reason={info.get('reason')}")

        got = mcp.call("get_asset", {"filepath": asset_path, "expected_sha256": ASSET_TEST_SHA256})
        image_blocks = [
            item for item in got.get("_mcp_content", [])
            if isinstance(item, dict) and item.get("type") == "image"
        ]
        image_size = None
        image_hash = None
        if len(image_blocks) == 1 and image_blocks[0].get("mimeType") == "image/png":
            try:
                image_bytes = base64.b64decode(image_blocks[0].get("data", ""), validate=True)
                image_size = len(image_bytes)
                image_hash = hashlib.sha256(image_bytes).hexdigest()
            except (TypeError, ValueError):
                pass
        sc.check("A3 get_asset", got.get("status") == "success"
                 and len(image_blocks) == 1
                 and image_size == ASSET_TEST_SIZE
                 and image_hash == ASSET_TEST_SHA256,
                 f"status={got.get('status')} image_blocks={len(image_blocks)} "
                 f"size={image_size} sha256={str(image_hash)[:16]}")

        search = mcp.call("search_assets", {"query": "runner canary", "path_prefix": asset_path})
        sc.check("A4 search_assets", search.get("status") == "success",
                 f"status={search.get('status')} reason={search.get('reason')}")

        listing = mcp.call("list_assets", {"prefix": asset_path, "max_results": 100})
        sc.check("A5 list_assets", listing.get("status") == "success",
                 f"status={listing.get('status')} reason={listing.get('reason')}")

        meta = mcp.call("update_asset_metadata", {
            "filepath": asset_path, "metadata": {"description": "runner canary updated"},
            "metadata_action": "merge", "metadata_storage": "catalog",
            "expected_sha256": ASSET_TEST_SHA256, "operation_id": f"{run}-meta",
        })
        sc.check("A6 update_asset_metadata", meta.get("status") == "success",
                 f"status={meta.get('status')} reason={meta.get('reason')}")

        reindexed = mcp.call("reindex_assets", {
            "prefix": asset_path, "operation_id": f"{run}-reindex",
        })
        sc.check("A7 reindex_assets", reindexed.get("status") == "success",
                 f"status={reindexed.get('status')} reason={reindexed.get('reason')}")

    finally:
        removed = mcp.call("remove_asset", {"filepath": asset_path, "expected_sha256": ASSET_TEST_SHA256,
                           "operation_id": f"{run}-cleanup"})
        gone = mcp.call("get_asset_info", {"filepath": asset_path})
        sc.check("A8 owned asset cleanup", removed.get("status") == "success" and gone.get("reason") == "not_found",
                 f"remove={removed.get('status')} missing={gone.get('reason') == 'not_found'}")


def h(name: str) -> str:
    return EXPECTED_HASHES[name][:16]


def missing_file_parity(payload: dict) -> bool:
    """Compare the actual HTTP representations before local client metadata."""
    text_payload = {key: value for key, value in payload.items()
                    if key not in {"_mcp_content", "_mcp_structured_content"}}
    return (text_payload.get("status") == "error"
            and text_payload.get("reason") == "not_found"
            and text_payload == payload.get("_mcp_structured_content"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("core", "full"), required=True)
    parser.add_argument("--provision-ocr-fixtures", action="store_true",
                        help="Prepare the six installed Self-Test fixtures before OCR source receipts")
    parser.add_argument("mcp_url")
    args = parser.parse_args(argv)
    if not os.environ.get("COGNITA_TEST_API_KEY"):
        parser.error("COGNITA_TEST_API_KEY must be set")
    url, token, mode = args.mcp_url, os.environ["COGNITA_TEST_API_KEY"], args.mode
    # 13.0 §7.3: the same runner serves both callers — the throwaway stack,
    # which authenticates with a synthetic ordinary credential, and the live
    # target in test mode, which authenticates with the public built-in key.
    # The two produce DIFFERENT evidence and are scored as separate lines, so a
    # live run can never be read as proof that ordinary authentication works,
    # or the other way round. The key itself is never printed.
    built_in_test_key = token == SELF_TEST_API_KEY
    auth_mode = "built-in test key" if built_in_test_key else "ordinary credential"
    print(f"cognita self-test runner: url={url} auth={auth_mode}")
    mcp = McpClient(url, token)
    sc = Scorecard()

    init = mcp.initialize()
    sc.check("0 initialize", "serverInfo" in init,
             f"server={init.get('serverInfo', {}).get('name')}")

    listed_tools = mcp.tools_list()
    listed_names = tuple(tool.get("name") for tool in listed_tools if isinstance(tool, dict))
    expected_names = (PUBLIC_TOOL_NAMES if mode == "full" else tuple(
        name for name in PUBLIC_TOOL_NAMES
        if name not in set(WORKSPACE_TOOL_NAMES) | set(BRIDGE_TOOL_NAMES)
    ))
    sc.check(f"0a exact-{mode}-tool-contract",
             listed_names == expected_names,
             f"expected={len(expected_names)} received={len(listed_names)}")
    missing_required = sorted(set(expected_names) - set(listed_names))
    if missing_required:
        sc.check("0a required-tools-available", False,
                 f"missing={','.join(missing_required)}")

    projects = mcp.discover_project()
    accessible = projects.get("projects") if isinstance(projects, dict) else None
    preferred_project = os.environ.get("COGNITA_TEST_PROJECT")
    writable_projects = [
        item for item in (accessible or [])
        if isinstance(item, dict) and item.get("access") == "write"
    ] if isinstance(accessible, list) else []
    if preferred_project:
        selected = next(
            (item for item in writable_projects if item.get("name") == preferred_project),
            None,
        )
    else:
        selected = writable_projects[0] if writable_projects else None
    if selected is not None:
        mcp.project = selected.get("name")
    sc.check("0b project-discovery", bool(mcp.project),
             f"selected_writable={mcp.project!r} projects={accessible!r}")

    discovered = [
        item.get("name") for item in (accessible or [])
        if isinstance(item, dict)
    ] if isinstance(accessible, list) else []
    if built_in_test_key:
        # The whole point of the live check: this credential must see the
        # Self-Test project and NOTHING else on a real connector that also
        # serves real projects.
        sc.check(
            "0b1 test-key scope", discovered == [SELF_TEST_PROJECT_NAME],
            f"auth={auth_mode} discovered={discovered!r} "
            f"expected=[{SELF_TEST_PROJECT_NAME!r}]",
        )
    else:
        sc.check(
            "0b1 ordinary-credential scope", bool(discovered),
            f"auth={auth_mode} discovered={discovered!r}",
        )

    plan = mcp.call("get_self_test_plan")
    sc.check("0c plan", plan.get("status") == "success"
             and "SELF-TEST PLAN" in plan.get("plan", ""),
             f"server_version={plan.get('server_version')}")

    # The seven asset tools are release-required. Keep this as individual
    # scorecard lines so a partial worker/catalog cannot be mistaken for an
    # optional skipped section.
    asset_tools = (
        "put_asset", "update_asset_metadata", "search_assets", "list_assets",
        "get_asset_info", "get_asset", "reindex_assets",
    )
    for asset_tool in asset_tools:
        sc.check(f"0d required-{asset_tool}", asset_tool in listed_names,
                 "advertised in tools/list")

    baseline = mcp.call("list_backups", {"filepath": TEST_FILE})
    baseline_ids = {b["backup_id"] for b in baseline.get("backups", [])}

    r = mcp.call("add_document", {"content": _TEST_CONTENT, "filepath": TEST_FILE,
                                  "category": "general"})
    sc.check("1 create", r.get("status") == "success",
             f"chunks_added={r.get('chunks_added')} overwrote={r.get('overwrote_existing')}")

    plural = mcp.call("get_documents", {
        "filepaths": [TEST_FILE], "include_content": False,
    })
    plural_docs = plural.get("documents")
    sc.check("1a get_documents-facts",
             plural.get("status") == "success"
             and plural.get("result_key") == "documents"
             and isinstance(plural_docs, list) and len(plural_docs) == 1
             and plural_docs[0].get("filepath") == TEST_FILE
             and "content" not in (plural_docs[0].get("document") or {}),
             f"status={plural.get('status')} entries={len(plural_docs or [])}")

    encoded_path = f"{BYTES_FILE}.md"
    encoded = mcp.call("add_document", {
        "filepath": encoded_path, "content": SELF_TEST_CRLF_BASE64,
        "content_encoding": "base64", "category": "general",
    })
    encoded_read = mcp.call("read_document", {"filepath": encoded_path})
    encoded_get = mcp.call("get_document", {
        "filepath": encoded_path, "content_encoding": "base64",
    })
    encoded_text = encoded_read.get("text")
    encoded_document = encoded_get.get("document")
    encoded_content = encoded_document.get("content") if isinstance(encoded_document, dict) else None
    encoded_ok = (
        encoded.get("status") == "success"
        and encoded_read.get("status") == "success"
        and encoded_text == SELF_TEST_CRLF_BYTES.decode("utf-8")
        and encoded_read.get("size_bytes") == len(SELF_TEST_CRLF_BYTES)
        and encoded_get.get("status") == "success"
        and encoded_content == SELF_TEST_CRLF_BASE64
    )
    sc.check("1b encoded-byte-independent-readback", encoded_ok,
             f"add={encoded.get('status')} read={encoded_read.get('status')} "
             f"get={encoded_get.get('status')} bytes={encoded_read.get('size_bytes')} "
             f"read_text={encoded_text!r} get_content={encoded_content!r}")

    batched = mcp.call("batch", {
        "calls": [
            {"tool": "get_documents", "arguments": {
                "project": mcp.project, "filepaths": [TEST_FILE], "include_content": False,
            }},
            {"tool": "get_document", "arguments": {
                "project": mcp.project, "filepath": TEST_FILE,
            }},
        ],
        "on_error": "stop",
    }, route=False)
    batch_results = batched.get("results")
    sc.check("1c connector-batch-order",
             batched.get("status") == "success"
             and batched.get("result_key") == "results"
             and [item.get("index") for item in (batch_results or [])] == [0, 1]
             and all(item.get("status") == "success" for item in (batch_results or [])),
             f"status={batched.get('status')} entries={len(batch_results or [])}")

    r = mcp.call("search_knowledge", {"query": "alpha two", "hybrid_alpha": 0,
                                      "snippet_mode": False})
    hit = any(TEST_FILE in res.get("source", "") for res in r.get("results", []))
    sc.check("2 search-after-create", hit, f"result_count={r.get('result_count')}")

    r = mcp.call("read_document", {"filepath": TEST_FILE, "section": "Alpha"})
    h0 = r.get("content_sha256", "")
    sc.check("3 read+H0", "### Alpha-child" in r.get("text", "")
             and h0.startswith(h("H0")) and "mtime" in r,
             f"H0={h0[:16]}")

    r = mcp.call("edit_document", {"filepath": TEST_FILE, "old_str": "alpha one",
                                   "new_str": "alpha ONE", "dry_run": True})
    sc.check("4 dry-run", r.get("applied") is False and r.get("replacements") == 1
             and r.get("current_content_sha256") == h0 and "context_diff" in r,
             f"sha={r.get('current_content_sha256', '')[:16]}")

    r = mcp.call("edit_document", {"filepath": TEST_FILE, "old_str": "alpha one",
                                   "new_str": "alpha ONE", "expected_sha256": h0})
    h1 = r.get("new_content_sha256", "")
    sc.check("5 guarded-edit+H1", r.get("status") == "success" and h1.startswith(h("H1")),
             f"H1={h1[:16]}")

    r = mcp.call("edit_document", {"filepath": TEST_FILE, "old_str": "alpha two",
                                   "new_str": "x", "expected_sha256": h0})
    sc.check("6 stale-reject", r.get("reason") == "stale_file" and "actual_sha256" in r,
             f"reason={r.get('reason')}")

    r = mcp.call("edit_document_batch", {"filepath": TEST_FILE, "expected_sha256": h1,
                                         "edits": [
                                             {"old_str": "alpha two", "new_str": "alpha TWO"},
                                             {"old_str": "beta one", "new_str": "beta ONE"},
                                         ]})
    h2 = r.get("new_content_sha256", "")
    sc.check("7 batch+H2", r.get("edits_applied") == 2 and h2.startswith(h("H2")),
             f"H2={h2[:16]}")

    r = mcp.call("search_knowledge", {"query": "alpha TWO", "hybrid_alpha": 0,
                                      "max_results": 3, "snippet_mode": False})
    fresh = any(TEST_FILE in res.get("source", "") and "alpha TWO" in res.get("content", "")
                for res in r.get("results", []))
    sc.check("7b search-after-edit", fresh, f"result_count={r.get('result_count')}")

    r = mcp.call("edit_document", {"filepath": TEST_FILE, "old_str": "line",
                                   "new_str": "x"})
    sc.check("8 ambiguous-reject", r.get("reason") == "ambiguous",
             f"reason={r.get('reason')}")

    r = mcp.call("insert_in_document", {"filepath": TEST_FILE, "position": "end_of_intro",
                                        "section": "Selftest", "text": "intro two",
                                        "expected_sha256": h2})
    h3 = r.get("new_content_sha256", "")
    sc.check("9 insert-intro+H3", r.get("status") == "success" and h3.startswith(h("H3")),
             f"H3={h3[:16]}")

    r = mcp.call("insert_in_document", {"filepath": TEST_FILE, "position": "end_of_section",
                                        "section": "Alpha", "text": "alpha three",
                                        "expected_sha256": h3})
    h4 = r.get("new_content_sha256", "")
    sc.check("10 insert-section+H4", r.get("status") == "success" and h4.startswith(h("H4")),
             f"H4={h4[:16]}")

    r = mcp.call("list_backups", {"filepath": TEST_FILE})
    run_ids = [b["backup_id"] for b in r.get("backups", [])
               if b["backup_id"] not in baseline_ids]
    sc.check("11 backups", len(run_ids) >= 4, f"new_backups={len(run_ids)}")

    oldest = min(run_ids) if run_ids else ""
    r = mcp.call("diff_backup", {"filepath": TEST_FILE, "backup_id": oldest})
    sc.check("12 diff", r.get("status") == "success" and
             (r.get("identical") is True or bool(r.get("diff"))),
             f"backup_id={oldest}")

    r = mcp.call("restore_backup", {"filepath": TEST_FILE, "backup_id": oldest})
    sc.check("13 round-trip", r.get("new_content_sha256", "").startswith(h("H0")),
             f"sha={r.get('new_content_sha256', '')[:16]} (expect H0 {h('H0')})")

    mv = mcp.call("move_document", {"filepath": TEST_FILE, "new_filepath": TEST_FILE_MOVED})
    old_gone = mcp.call("read_document", {"filepath": TEST_FILE}).get("reason") == "not_found"
    at_new = mcp.call("search_knowledge", {"query": "alpha two", "hybrid_alpha": 0,
                                           "max_results": 3, "snippet_mode": False})
    hit_new = any(TEST_FILE_MOVED in res.get("source", "") for res in at_new.get("results", []))
    same = mcp.call("move_document", {"filepath": TEST_FILE_MOVED, "new_filepath": TEST_FILE_MOVED})
    back = mcp.call("move_document", {"filepath": TEST_FILE_MOVED, "new_filepath": TEST_FILE})
    sc.check("13b move+rename",
             mv.get("status") == "success" and mv.get("chunks_moved", 0) >= 1
             and old_gone and hit_new and same.get("status") == "error"
             and back.get("status") == "success",
             f"moved={mv.get('chunks_moved')} old_gone={old_gone} at_new={hit_new} "
             f"same_path_refused={same.get('status') == 'error'} back={back.get('status')}")

    r = mcp.call("remove_documents", {
        "filepaths": [TEST_FILE, encoded_path], "delete_file": True,
        "on_error": "stop",
    })
    removal_entries = r.get("documents")
    removed_ok = (
        r.get("status") == "success" and r.get("result_key") == "documents"
        and [item.get("filepath") for item in (removal_entries or [])] == [TEST_FILE, encoded_path]
        and all(item.get("status") == "success" for item in (removal_entries or []))
    )
    search_gone = mcp.call("search_knowledge", {"query": "alpha two", "hybrid_alpha": 0})
    no_hit = not any(TEST_FILE in res.get("source", "")
                     for res in search_gone.get("results", []))
    read_gone = mcp.call("read_document", {"filepath": TEST_FILE})
    parity_passed = missing_file_parity(read_gone)
    sc.check("14a missing-file HTTP payload parity", parity_passed,
             f"status={read_gone.get('status')} reason={read_gone.get('reason')} identical={parity_passed}")
    sc.check("14 cleanup", removed_ok and no_hit and read_gone.get("reason") == "not_found",
             f"removed={removed_ok} entries={len(removal_entries or [])} index_gone={no_hit} disk_gone="
             f"{read_gone.get('reason') == 'not_found'}")

    run_asset_plan(mcp, sc)

    ocr_passed = run_ocr_plan(mcp, sc, provision_fixtures=args.provision_ocr_fixtures)

    if mode == "full":
        run_workspace_plan(mcp, sc, plan.get("plan", ""))
    else:
        omitted = set(WORKSPACE_TOOL_NAMES) | set(BRIDGE_TOOL_NAMES)
        sc.check("core Workspace tools omitted",
                 omitted.isdisjoint(listed_names),
                 f"omitted={len(omitted)} listed={len(listed_names)}")
        for name in (WORKSPACE_TOOL_NAMES[0], BRIDGE_TOOL_NAMES[0]):
            arguments = ({"project": mcp.project, "paths": [".cognita-core-negative-probe"]}
                         if name in BRIDGE_TOOL_NAMES else {})
            stale = mcp.call(name, arguments)
            sc.check(f"core stale {name} is bounded",
                     stale.get("status") == "error"
                     and stale.get("reason") == "runtime_unavailable",
                     f"status={stale.get('status')} reason={stale.get('reason')}")
        print("workspace=not_applicable reason=host_workspace_disabled")

    result = sc.report()
    print(
        "connector measurement: "
        f"tool_calls={mcp.tool_call_count} "
        f"serialized_response_bytes={mcp.tool_response_bytes}"
    )
    # The host release runner consumes only this bounded result, never OCR
    # text or credentials. A missing/failed receipt cannot qualify an image.
    print("selftest_receipt=" + json.dumps({"schema": 1, "mode": mode,
          "result": "passed" if result == 0 else "failed",
          "mandatory_ocr": "passed" if ocr_passed else "failed",
          "missing_file_parity": "passed" if parity_passed else "failed"}, sort_keys=True))
    return result


if __name__ == "__main__":
    raise SystemExit(main())
