"""Catalog-bound Workspace self-test plans.

The plan is generated from one section registry shared by the Workspace-only
v3 tool and the combined v5 adapter.  It is instructions only: no test action
is executed and no caller-selected Workspace or project is accepted here.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

WORKSPACE_SELFTEST_TOOL_NAME = "workspace_generate_self_test"
WORKSPACE_SELFTEST_PLAN_VERSION = "workspace-4"
WORKSPACE_SECTIONS = ("W", "W1", "W2", "W4", "W5", "W6", "W7", "W8", "W9", "W11", "W13", "W14", "W12")
WORKSPACE_ONLY_SECTIONS = tuple(item for item in WORKSPACE_SECTIONS if item != "W11")
KNOWLEDGE_BRIDGE_PREFIXES = ("copy_to_workspace", "copy_from_workspace")

WORKSPACE_TOOL_NAMES = (
    "workspace_info", "workspace_list_files", "workspace_stat", "workspace_read_file",
    "workspace_write_file", "workspace_edit_file", "workspace_make_directory",
    "workspace_copy_paths", "workspace_move_paths", "workspace_remove_paths",
    "workspace_search", "workspace_start_job", "workspace_get_job", "workspace_cancel_job",
    "workspace_web_search",
)


@dataclass(frozen=True, slots=True)
class WorkspaceSelfTestSection:
    section: str
    title: str
    coverage: tuple[str, ...]
    prerequisites: tuple[str, ...] = ()
    cleanup: tuple[str, ...] = ()
    bridge: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {"id": self.section, "title": self.title, "coverage": list(self.coverage),
                "prerequisites": list(self.prerequisites), "cleanup": list(self.cleanup),
                "available": True, "bridge": self.bridge}


SECTION_CATALOG = (
    WorkspaceSelfTestSection("W", "Workspace prerequisites, safety, and cleanup index", ("workspace_info",), cleanup=("W12",)),
    WorkspaceSelfTestSection("W1", "Identity, lazy creation, and bounded metadata", ("workspace_info",), prerequisites=("W",), cleanup=("W12",)),
    WorkspaceSelfTestSection("W2", "Typed filesystem lifecycle and hashes", tuple(WORKSPACE_TOOL_NAMES[1:11]), prerequisites=("W1",), cleanup=("W12",)),
    WorkspaceSelfTestSection("W4", "Direct argv, unpolled completion, and explicit Bash guest jobs", ("workspace_start_job", "workspace_get_job", "workspace_make_directory"), prerequisites=("W1",), cleanup=("W7", "W12")),
    WorkspaceSelfTestSection("W5", "Python, venv, pip, and offline local package", ("workspace_start_job",), prerequisites=("W4",), cleanup=("W12",)),
    WorkspaceSelfTestSection("W6", "Node/npm and pinned toolbox commands", ("workspace_start_job",), prerequisites=("W4",), cleanup=("W12",)),
    WorkspaceSelfTestSection("W7", "Polling, offsets, cancellation, timeout, and truncation", ("workspace_start_job", "workspace_get_job", "workspace_cancel_job"), prerequisites=("W4",), cleanup=("W12",)),
    WorkspaceSelfTestSection("W8", "Quota, paths, hashes, conflict, and bounds", tuple(WORKSPACE_TOOL_NAMES[1:14]), prerequisites=("W1",), cleanup=("W12",)),
    WorkspaceSelfTestSection("W9", "Network-off and explicitly configured policy", ("workspace_start_job", "workspace_web_search"), prerequisites=("W1",), cleanup=("W12",)),
    WorkspaceSelfTestSection("W11", "Knowledge–Workspace bridge hashes and conflicts", ("copy_to_workspace", "copy_from_workspace"), prerequisites=("W1",), cleanup=("W12",), bridge=True),
    WorkspaceSelfTestSection("W13", "Run-and-wait and text job output", ("workspace_start_job", "workspace_get_job"), prerequisites=("W4",), cleanup=("W12",)),
    WorkspaceSelfTestSection("W14", "Tail, line ranges, and usage visibility", ("workspace_write_file", "workspace_read_file", "workspace_get_job", "workspace_info"), prerequisites=("W2", "W4"), cleanup=("W12",)),
    WorkspaceSelfTestSection("W12", "Exact owned cleanup and absence proof", ("workspace_cancel_job", "workspace_remove_paths", "workspace_info"), prerequisites=("W",), cleanup=()),
)


def workspace_selftest_tool_definition() -> dict[str, Any]:
    return {"name": WORKSPACE_SELFTEST_TOOL_NAME,
            "description": "Generate the catalog-bound Workspace QA plan; this tool performs no test actions.",
            "inputSchema": {"type": "object", "properties": {"section": {"type": "string", "maxLength": 64, "description": "Omit or use full, index, W, W1-W2, W4-W9, W12, W13, or W14."}}, "required": [], "additionalProperties": False}}


def workspace_selftest_sections(*, bridge: bool = False) -> list[dict[str, Any]]:
    return [item.as_dict() for item in SECTION_CATALOG if bridge or not item.bridge]


def _with_project(values: dict[str, Any], project: str | None) -> dict[str, Any]:
    return ({"project": project, **values} if project is not None else dict(values))


def _section_calls(
    section: str, *, plan_tool_name: str, project: str | None, bridge: bool,
    batch_available: bool = False,
) -> list[tuple[str, dict[str, Any], str]]:
    """Return calls executable by the authenticated connector client."""
    root = "/workspace/.cognita-self-test/<RUN>"
    alpha, beta = f"{root}/alpha.txt", f"{root}/beta.txt"
    bridge_root = ".cognita-self-test/<RUN>"
    # W11 round-trips the Knowledge file it copies in itself. It used to copy W2's alpha.txt back out,
    # although W11 declares only W1 as a prerequisite: an assistant that ran W11 on its own, or cleaned
    # up W2's files first, found no source (ChatGPT, 2026-10-01).
    bridge_md_name = "cognita-workspace-bridge-<RUN>.md"
    bridge_md = f"{bridge_root}/{bridge_md_name}"
    bridge_out = f"cognita-workspace-bridge-out-<RUN>/{bridge_md_name}"
    calls: dict[str, list[tuple[str, dict[str, Any], str]]] = {
        "W": [(plan_tool_name, _with_project({"section": "index"}, project), "Record the advertised section index and safety prerequisites.")],
        "W1": [
            ("workspace_info", {}, "Before the first W2 mutation, record workspace:null if no Workspace exists; otherwise record the existing Workspace ID and state."),
            ("workspace_info", {}, "Repeat after the first W2 mutation; record Workspace ID/state, quota, measured usage, runtime generation, and effective network mode. If a Workspace existed before W2, require its ID to remain unchanged."),
        ],
        "W2": [
            ("workspace_make_directory", {"path": root, "parents": True, "idempotency_key": "<RUN>-mkdir"}, "The owned directory is created."),
            ("workspace_write_file", {"path": alpha, "text": "cognita workspace <RUN>\nneedle-one\n", "create_policy": "parents", "idempotency_key": "<RUN>-write"}, "Capture returned SHA-256 as <ALPHA_SHA256>."),
            ("workspace_write_file", {"path": alpha, "text": "cognita workspace <RUN>\nneedle-one\n", "create_policy": "parents", "idempotency_key": "<RUN>-write"}, "Exact replay returns the original receipt, marks idempotent_replay=true and replayed=true, and does not write twice."),
            ("workspace_stat", {"path": alpha, "include_hash": True}, "Hash equals <ALPHA_SHA256>."),
            ("workspace_read_file", {"path": alpha, "offset": 0, "max_bytes": 1048576, "encoding": "text"}, "Bytes exactly match the fixture."),
            ("workspace_edit_file", {"path": alpha, "expected_sha256": "<ALPHA_SHA256>", "edits": [{"match": "needle-one", "replacement": "needle-two"}], "idempotency_key": "<RUN>-edit"}, "Capture returned hash as <EDITED_SHA256>."),
            ("workspace_search", {"roots": [root], "pattern": "*.txt", "mode": "glob"}, "The alpha path is returned."),
            ("workspace_search", {"roots": [root], "pattern": "needle-two", "mode": "text"}, "Only matching file content is returned."),
            ("workspace_search", {"roots": [root], "pattern": "alpha[.]txt$", "mode": "regex"}, "The bounded regex path match is returned."),
            ("workspace_copy_paths", {"sources": [alpha], "destination": beta, "conflict_policy": "fail", "idempotency_key": "<RUN>-copy"}, "destination_sha256 equals <EDITED_SHA256>."),
            ("workspace_move_paths", {"sources": [beta], "destination": f"{root}/moved.txt", "conflict_policy": "fail", "idempotency_key": "<RUN>-move"}, "Source is absent, destination is present, and destination_sha256 equals <EDITED_SHA256>."),
            ("workspace_list_files", {"path": root, "recursive": True, "max_entries": 2000}, "Bounded results contain only owned fixtures."),
            ("workspace_remove_paths", {"paths": [f"{root}/moved.txt"], "recursive": False, "expected_hashes": {f"{root}/moved.txt": "<EDITED_SHA256>"}, "idempotency_key": "<RUN>-remove-moved"}, "The guarded owned file is removed; alpha remains."),
        ],
        "W4": [
            ("workspace_start_job", {"argv": ["true"], "cwd": root, "timeout": 30, "env": {}, "idempotency_key": "<RUN>-unpolled"}, "Capture <UNPOLLED_JOB_ID>; intentionally do not call workspace_get_job for this job."),
            ("workspace_make_directory", {"path": f"{root}/after-unpolled", "parents": True, "idempotency_key": "<RUN>-after-unpolled"}, "Within 10 seconds this mutation succeeds without polling <UNPOLLED_JOB_ID>. A transient job_running response is allowed only while the job is genuinely active and must include its job_id for diagnosis; retry the same call after a brief delay."),
            ("workspace_start_job", {"argv": ["python3", "-c", "print('argv-ok')"], "cwd": root, "timeout": 60, "env": {"COGNITA_RUN": "<RUN>"}, "idempotency_key": "<RUN>-argv"}, "Capture <ARGV_JOB_ID> and poll it to succeeded."),
            ("workspace_get_job", {"job_id": "<ARGV_JOB_ID>", "stdout_offset": 0, "stderr_offset": 0, "max_bytes": 1048576}, "Base64-decode stdout before comparing it with argv-ok; offsets are monotonic."),
            ("workspace_start_job", {"shell_script": "printf '%s\\n' shell-ok", "cwd": root, "timeout": 60, "env": {}, "idempotency_key": "<RUN>-shell"}, "Capture <SHELL_JOB_ID>; explicit Bash mode succeeds."),
            ("workspace_get_job", {"job_id": "<SHELL_JOB_ID>", "stdout_offset": 0, "stderr_offset": 0, "max_bytes": 1048576}, "Poll through a terminal succeeded state, base64-decode stdout, and require shell-ok."),
        ],
        "W5": [
            ("workspace_start_job", {"shell_script": "set -eu\npython3 --version\npython3 -m venv --system-site-packages .venv\n.venv/bin/python -m pip --version\nmkdir -p tiny_pkg/tiny_pkg\nprintf '[build-system]\\nrequires=[]\\nbuild-backend=\"setuptools.build_meta:__legacy__\"\\n' > tiny_pkg/pyproject.toml\nprintf 'def value(): return \"python-ok\"\\n' > tiny_pkg/tiny_pkg/__init__.py\nprintf 'from setuptools import setup\\nsetup(name=\"tiny-pkg\",version=\"0.0.1\",packages=[\"tiny_pkg\"])\\n' > tiny_pkg/setup.py\n.venv/bin/python -m pip install --no-index --no-build-isolation ./tiny_pkg\n.venv/bin/python -c \"import tiny_pkg; print(tiny_pkg.value())\"\nrm -rf .venv tiny_pkg", "cwd": root, "timeout": 300, "env": {"PIP_NO_INDEX": "1"}, "idempotency_key": "<RUN>-python"}, "Capture <PYTHON_JOB_ID> for the offline local-package run."),
            ("workspace_get_job", {"job_id": "<PYTHON_JOB_ID>", "stdout_offset": 0, "stderr_offset": 0, "max_bytes": 1048576}, "Poll to succeeded; base64-decode stdout/stderr before checking the exact Python version, venv, pip, offline install/import, and cleanup without fetching."),
        ],
        "W6": [
            ("workspace_start_job", {"shell_script": "set -eu\nnode --version\nnpm --version\nnode -e 'console.log(JSON.stringify({node:\"ok\"}))' > node.json\njq -e '.node == \"ok\"' node.json\nprintf '# Synthetic <RUN>\\n' > input.md\npandoc input.md -t plain -o output.txt\ngrep -q 'Synthetic' output.txt\ngit --version\nrm -f node.json input.md output.txt", "cwd": root, "timeout": 120, "env": {}, "idempotency_key": "<RUN>-tools"}, "Capture <TOOLBOX_JOB_ID> for the deterministic toolbox run."),
            ("workspace_get_job", {"job_id": "<TOOLBOX_JOB_ID>", "stdout_offset": 0, "stderr_offset": 0, "max_bytes": 1048576}, "Poll to succeeded; base64-decode stdout/stderr before checking Node, npm, git, jq, and pandoc output."),
        ],
        "W7": [
            ("workspace_start_job", {"argv": ["python3", "-c", "import time; print('begin', flush=True); time.sleep(300)"], "cwd": root, "timeout": 600, "env": {}, "idempotency_key": "<RUN>-cancel"}, "Capture <CANCEL_JOB_ID> while running; the child flushes begin before sleeping."),
            ("workspace_get_job", {"job_id": "<CANCEL_JOB_ID>", "stdout_offset": 0, "stderr_offset": 0, "max_bytes": 16}, "While state is running, base64-decode stdout and require begin\\n, stdout_bytes at least 6, stdout_next_offset 6, and has_more=false."),
            ("!workspace_start_job", {"argv": ["true"], "cwd": root, "timeout": 30, "env": {}, "idempotency_key": "<RUN>-parallel"}, "A second job is rejected with job_running while <CANCEL_JOB_ID> is active."),
            ("!workspace_write_file", {"path": f"{root}/during-job.txt", "text": "blocked", "create_policy": "parents", "idempotency_key": "<RUN>-during-job"}, "A file mutation is rejected with job_running while the job is active."),
            ("workspace_cancel_job", {"job_id": "<CANCEL_JOB_ID>", "idempotency_key": "<RUN>-cancel-request"}, "Terminal state is canceled and replay is stable."),
            ("workspace_get_job", {"job_id": "<CANCEL_JOB_ID>", "stdout_offset": 0, "stderr_offset": 0, "max_bytes": 1048576}, "State remains canceled and no process remains."),
            ("workspace_start_job", {"argv": ["python3", "-c", "import time; time.sleep(10)"], "cwd": root, "timeout": 1, "env": {}, "idempotency_key": "<RUN>-timeout"}, "Capture <TIMEOUT_JOB_ID>; it reaches timed_out within the explicit bound."),
            ("workspace_get_job", {"job_id": "<TIMEOUT_JOB_ID>", "stdout_offset": 0, "stderr_offset": 0, "max_bytes": 1048576}, "Terminal timed_out state is stable on repeated polls."),
            ("workspace_start_job", {"argv": ["python3", "-c", "import sys; print('o'*64); print('e'*64,file=sys.stderr)"], "cwd": root, "timeout": 60, "env": {}, "idempotency_key": "<RUN>-page"}, "Capture <PAGE_JOB_ID> and wait for success."),
            ("workspace_get_job", {"job_id": "<PAGE_JOB_ID>", "stdout_offset": 0, "stderr_offset": 0, "max_bytes": 8}, "Follow returned next offsets to prove stdout/stderr pagination and truncation bounds."),
        ],
        "W8": [
            ("workspace_write_file", {"path": f"{root}/conflict.txt", "text": "first", "create_policy": "parents", "idempotency_key": "<RUN>-conflict-create"}, "Capture <CONFLICT_SHA256>."),
            ("!workspace_write_file", {"path": f"{root}/conflict.txt", "text": "second", "create_policy": "existing", "expected_sha256": "0000000000000000000000000000000000000000000000000000000000000000", "idempotency_key": "<RUN>-conflict-check"}, "Stale hash fails closed without changing bytes."),
            ("!workspace_copy_paths", {"sources": [alpha], "destination": f"{root}/conflict.txt", "conflict_policy": "fail", "idempotency_key": "<RUN>-copy-conflict"}, "Destination conflict does not replace bytes."),
            ("workspace_make_directory", {"path": f"{root}/{'a' * 200}!", "parents": True, "idempotency_key": "<RUN>-regex-fixture"}, "Create one bounded RUN-owned path that forces the following non-match to exercise backtracking."),
            ("!workspace_search", {"roots": [root], "pattern": "(a+)+$", "mode": "regex"}, "The bounded regex probe may complete successfully with no matches or return reason=search_timeout; any other error fails the self-test."),
            ("!workspace_read_file", {"path": "../outside", "offset": 0, "max_bytes": 1, "encoding": "text"}, "Traversal is rejected with invalid_arguments."),
            ("!workspace_write_file", {"path": f"{root}/invalid.txt", "text": "x", "base64": "eA==", "create_policy": "parents"}, "Mutually supplied content forms are rejected with invalid_arguments."),
            ("!workspace_start_job", {"argv": ["true"], "shell_script": "true", "cwd": root, "timeout": 60, "env": {}}, "Mutually supplied execution forms are rejected with invalid_arguments."),
            ("!workspace_copy_paths", {"sources": [alpha], "destination": beta, "conflict_policy": "invalid", "idempotency_key": "<RUN>-bad-conflict"}, "Invalid conflict policy is rejected by the client schema or server without mutation."),
            ("!workspace_start_job", {"argv": ["true"], "cwd": root, "timeout": 3601, "env": {}}, "Timeout above the public bound is rejected by the client schema or server."),
            ("workspace_info", {}, "The dedicated Self-Test Workspace reports its configured quota and measured usage without attempting to change the caller-inaccessible quota."),
        ],
        "W9": [
            ("workspace_start_job", {"argv": ["python3", "-c", "import socket; socket.create_connection(('example.com',443),2)"], "cwd": root, "timeout": 10, "env": {}, "idempotency_key": "<RUN>-network"}, "Capture <NETWORK_JOB_ID> for the denied direct-egress attempt."),
            ("workspace_get_job", {"job_id": "<NETWORK_JOB_ID>", "stdout_offset": 0, "stderr_offset": 0, "max_bytes": 1048576}, "Poll to failed; direct guest egress remains denied when network policy is off."),
            ("workspace_web_search", {"query": "Cognita synthetic workspace self test", "result_count": 3}, "If configured, only Brave metadata is returned and no credential enters the guest. If unavailable, report SKIPPED with reason network_denied."),
            ("workspace_start_job", {"argv": ["python3", "-c", "print('local-ok')"], "cwd": root, "timeout": 30, "env": {}, "idempotency_key": "<RUN>-local-after-deny"}, "Capture <LOCAL_JOB_ID> after denied egress."),
            ("workspace_get_job", {"job_id": "<LOCAL_JOB_ID>", "stdout_offset": 0, "stderr_offset": 0, "max_bytes": 1048576}, "Poll to succeeded, base64-decode stdout, and require local-ok."),
        ],
        "W11": [
            ("workspace_make_directory", {"path": root, "parents": True, "idempotency_key": "<RUN>-w11-mkdir"}, "The owned RUN root exists (created here, or already there from W2); W11 needs no file from any other section."),
            ("add_document", _with_project({"filepath": "cognita-workspace-bridge-<RUN>.md", "content": "# Bridge <RUN>\n\nsynthetic bridge bytes\n", "operation_id": "<RUN>-knowledge-create"}, project), "Create only this synthetic file and capture its content SHA-256."),
            ("copy_to_workspace", _with_project({"paths": ["cognita-workspace-bridge-<RUN>.md"], "destination": bridge_root, "conflict_policy": "fail", "idempotency_key": "<RUN>-bridge-in"}, project), "Source and destination SHA-256 values match; bridge paths are Workspace-root relative."),
            ("copy_from_workspace", _with_project({"paths": [bridge_md], "destination": "cognita-workspace-bridge-out-<RUN>", "conflict_policy": "fail", "idempotency_key": "<RUN>-bridge-out"}, project), "Both SHA-256 values match and conflict policy is honored."),
            ("copy_from_workspace", _with_project({"paths": [bridge_md], "destination": "cognita-workspace-bridge-out-<RUN>", "conflict_policy": "skip", "idempotency_key": "<RUN>-bridge-skip"}, project), "Existing destination is skipped without mutation."),
            ("copy_from_workspace", _with_project({"paths": [bridge_md], "destination": "cognita-workspace-bridge-out-<RUN>", "conflict_policy": "rename", "idempotency_key": "<RUN>-bridge-rename"}, project), "Rename creates a distinct synthetic path with equal hash."),
            ("copy_from_workspace", _with_project({"paths": [bridge_md], "destination": "cognita-workspace-bridge-out-<RUN>", "conflict_policy": "replace", "expected_destination_hashes": {bridge_out: "<KNOWLEDGE_DEST_SHA256>"}, "idempotency_key": "<RUN>-bridge-replace"}, project), "Guarded replace succeeds only for the captured destination hash."),
            ("!copy_from_workspace", _with_project({"paths": [bridge_md], "destination": "cognita-workspace-bridge-out-<RUN>", "conflict_policy": "replace", "expected_destination_hashes": {bridge_out: "0000000000000000000000000000000000000000000000000000000000000000"}, "idempotency_key": "<RUN>-bridge-stale"}, project), "Stale destination hash fails closed. If the caller already has a read-only or denied-policy connector, repeat the same stale-hash request there and require a stable denial."),
            ("read_document", _with_project({"filepath": bridge_out}, project), "Knowledge bytes/hash equal the Workspace source and the original Knowledge file; separately exercise guarded replace and stale-hash cases."),
        ],
        "W13": [
            ("workspace_start_job", {"argv": ["sleep", "3"], "cwd": root, "timeout": 30, "env": {}, "wait_ms": 10000, "idempotency_key": "<RUN>-w13-wait-exit"}, "One call returns wake_reason=exited with waited_ms between 2500 and 6000."),
            ("workspace_start_job", {"argv": ["sleep", "3"], "cwd": root, "timeout": 30, "env": {}, "wait_ms": 1000, "idempotency_key": "<RUN>-w13-wait-timeout"}, "wake_reason=timeout, state remains running, and waited_ms is between 1000 and 1500. Capture <WAIT_TIMEOUT_JOB_ID>."),
            ("workspace_get_job", {"job_id": "<WAIT_TIMEOUT_JOB_ID>", "wait_ms": 10000}, "The same job reaches wake_reason=exited once its remaining sleep elapses."),
            ("workspace_start_job", {"argv": ["python3", "-c", "print(1)"], "cwd": root, "timeout": 30, "env": {}, "wait_ms": 10000, "output_encoding": "text", "idempotency_key": "<RUN>-w13-text"}, "stdout is exactly \"1\\n\" and stdout_encoding is text; no base64 decode is needed."),
            ("workspace_start_job", {"argv": ["python3", "-c", "import sys; sys.stdout.buffer.write(b'\\xff')"], "cwd": root, "timeout": 30, "env": {}, "wait_ms": 10000, "output_encoding": "auto", "idempotency_key": "<RUN>-w13-auto-binary"}, "stdout_encoding is base64 because the single byte is not valid UTF-8; auto never guesses with replacement characters."),
        ],
        "W14": [
            ("workspace_start_job", {"shell_script": "seq 1 10000 > big.txt", "cwd": root, "timeout": 60, "env": {}, "wait_ms": 10000, "idempotency_key": "<RUN>-w14-fixture"}, "wake_reason=exited and the job succeeds, writing a 10,000-line fixture at big.txt under the RUN root."),
            ("workspace_read_file", {"path": f"{root}/big.txt", "tail_lines": 5}, "Exactly 5 lines are returned (\"9996\"..\"10000\", one per line) and has_more is false."),
            ("workspace_read_file", {"path": f"{root}/big.txt", "start_line": 10, "end_line": 12}, "Lines 10-12 (\"10\", \"11\", \"12\") are returned and total_lines is 10000."),
            ("workspace_info", {}, "usage_by_directory is present, quota_remaining_bytes is a non-negative integer, and usage_status is not unknown now that the fixture write has measured usage."),
        ],
        "W12": [
            ("workspace_cancel_job", {"job_id": "<ACTIVE_JOB_ID>", "idempotency_key": "<RUN>-cleanup-cancel"}, "Issue only for a RUN-owned active job; require terminal state."),
            ("workspace_remove_paths", {"paths": [alpha], "recursive": False, "expected_hashes": {alpha: "<EDITED_SHA256>"}, "idempotency_key": "<RUN>-cleanup-alpha"}, "Remove the owned file only when its captured hash still matches."),
            ("workspace_remove_paths", {"paths": [root], "recursive": True, "idempotency_key": "<RUN>-cleanup-remove"}, "Remove exactly the owned RUN root."),
            ("remove_document", _with_project({"filepath": "cognita-workspace-bridge-<RUN>.md", "delete_file": True, "operation_id": "<RUN>-knowledge-cleanup"}, project), "On combined W11 runs only, remove the exact synthetic Knowledge fixture and its normal guarded backup paths."),
            ("remove_document", _with_project({"filepath": bridge_out, "delete_file": True, "operation_id": "<RUN>-knowledge-out-cleanup"}, project), "On combined W11 runs only, remove the exact round-trip destination."),
            ("remove_document", _with_project({"filepath": "<RENAMED_PATH>", "delete_file": True, "operation_id": "<RUN>-knowledge-renamed-cleanup"}, project), "On combined W11 runs only, remove the exact renamed path returned earlier."),
            ("workspace_info", {}, "No RUN-owned active job remains."),
        ],
    }
    selected = calls[section]
    if section == "W2" and batch_available:
        selected = [
            *selected,
            (
                "batch",
                {
                    "calls": [
                        {"tool": "workspace_info", "arguments": {}},
                        {"tool": "workspace_write_file", "arguments": {
                            "path": f"{root}/batch.txt", "text": "cognita batch <RUN>\n",
                            "create_policy": "parents", "idempotency_key": "<RUN>-batch-write",
                        }},
                    ],
                    "on_error": "stop",
                },
                "Batch succeeds: workspace_info may report workspace:null, and workspace_write_file commits its receipt without output_contract_violation.",
            ),
        ]
    if section == "W12" and not bridge:
        selected = [call for call in selected if call[0] != "remove_document"]
    return selected


def workspace_selftest_plan(*, server_version: str, section: str | None = None, bridge: bool = False, catalog: Iterable[str] = WORKSPACE_TOOL_NAMES + (WORKSPACE_SELFTEST_TOOL_NAME,), plan_tool_name: str = WORKSPACE_SELFTEST_TOOL_NAME, project: str | None = None, workspace_only: bool = True) -> dict[str, Any]:
    allowed_sections = tuple(item["id"] for item in workspace_selftest_sections(bridge=bridge))
    if section is None:
        section = "full"
    names = set(catalog)
    common = {"status": "success", "server_version": server_version, "plan_version": WORKSPACE_SELFTEST_PLAN_VERSION, "section": section,
              "catalog_assertions": {"workspace_tools_present": True,
                                      "knowledge_tools_absent": workspace_only and not any(name.startswith("knowledge_") for name in names),
                                      "bridge_tools_absent": not any(name in names for name in KNOWLEDGE_BRIDGE_PREFIXES)}}
    required = set(WORKSPACE_TOOL_NAMES) | {plan_tool_name}
    if bridge:
        required.update((*KNOWLEDGE_BRIDGE_PREFIXES, "add_document", "read_document", "remove_document"))
    missing = sorted(required - names)
    if missing:
        return {**common, "status": "blocked", "reason": "catalog_missing", "missing_tools": missing}
    if section == "full":
        return {**common, "plan": _render_sections(allowed_sections, plan_tool_name=plan_tool_name, project=project, bridge=bridge, batch_available="batch" in names)}
    if section == "index":
        return {**common, "sections": workspace_selftest_sections(bridge=bridge)}
    if section not in allowed_sections:
        return {**common, "status": "error", "reason": "unknown_section", "valid_sections": ["full", "index", *allowed_sections]}
    return {**common, "plan": _render_sections((section,), plan_tool_name=plan_tool_name, project=project, bridge=bridge, batch_available="batch" in names)}


def _render_sections(section_ids: Iterable[str], *, plan_tool_name: str = WORKSPACE_SELFTEST_TOOL_NAME, project: str | None = None, bridge: bool = False, batch_available: bool = False) -> str:
    lines = ["Workspace self-test safety: replace <RUN> with a fresh unpredictable token and replace captured <...> values before dependent calls; use only synthetic bounded data beneath `/workspace/.cognita-self-test/<RUN>/`; never use personal files; clean only owned paths. Every CALL line is an exact JSON argument object for the named tool."]
    for section_id in section_ids:
        row = next(item for item in SECTION_CATALOG if item.section == section_id)
        lines.append(f"\n## {row.section}: {row.title}\nCoverage: {', '.join(row.coverage)}. Prerequisites: {', '.join(row.prerequisites) or 'none'}. Cleanup: {', '.join(row.cleanup) or 'none'}.")
        for tool, arguments, assertion in _section_calls(row.section, plan_tool_name=plan_tool_name, project=project, bridge=bridge, batch_available=batch_available):
            payload = json.dumps(arguments, sort_keys=True, separators=(",", ":"))
            label = "NEGATIVE_CALL" if tool.startswith("!") else "CALL"
            directive = f"{label} {tool.removeprefix('!')}"
            lines.append(f"{directive} {payload}\nASSERT {assertion}")
    return "\n".join(lines)
