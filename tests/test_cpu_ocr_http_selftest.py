"""The packaged HTTP runner must execute OCR, rather than silently skip it."""
import copy
import importlib.util
import json
import os
import io
import argparse
from types import SimpleNamespace
from pathlib import Path

import httpx
import pytest

from cognita.selftest_fixtures import load_manifest


spec = importlib.util.spec_from_file_location("cpu_ocr_http_runner", Path(__file__).parents[1] / "scripts/run-selftest.py")
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


def test_mcp_client_reports_http_rejection_before_json_parsing():
    response = httpx.Response(401, text="Authorization required",
                              request=httpx.Request("POST", "http://test.invalid/mcp"))
    with pytest.raises(httpx.HTTPStatusError, match="401"):
        runner.McpClient._payload(response)


class Client:
    project = "Self-Test"

    def __init__(self, *, unavailable=False, broken_cache=False, engine=("cpu", "pytorch-cpu")):
        self.calls = []
        self.engine = engine
        self.counts = {}
        self.unavailable = unavailable
        self.broken_cache = broken_cache
        self.fixtures = {Path(row.path).name: row for row in load_manifest()}

    def call(self, tool, arguments):
        self.calls.append((tool, arguments))
        path = arguments.get("filepath", "")
        if tool == "search_assets":
            row = self.fixtures["canonical-clear.png"]
            return {"status": "success", "results": [{"filepath": row.path, "provenance": "ocr", "source_sha256": row.sha256}]}
        if arguments.get("project") == "Other-Project":
            return {"status": "error", "reason": "project_unavailable"}
        if path.startswith(("../", "/")):
            return {"status": "error", "reason": "invalid_path"}
        if arguments.get("languages") == ["zz"]:
            return {"status": "error", "reason": "unsupported_language"}
        name = Path(path).name
        refusals = {"malformed.png": "invalid_png", "animated.png": "animated_png",
                    "not-a-png.txt": "wrong_media_type", "over-limit.png": "too_large"}
        if name in refusals:
            return {"status": "error", "reason": refusals[name], "message": "dimension limit"}
        if self.unavailable:
            return {"status": "error", "reason": "ocr_unavailable"}
        self.counts[name] = self.counts.get(name, 0) + 1
        return {"status": "success", "outcome": "no_text" if name == "blank.png" else "text",
                "text": "" if name == "blank.png" else "Searchable token",
                "regions": [] if name == "blank.png" else [{"text": "Searchable token"}],
                "sha256": self.fixtures[name].sha256, "width": 1600, "height": 424,
                "engine": {"name": "easyocr", "version": "1.7.2", "model_fingerprint": "a" * 64,
                           "device": self.engine[0], "backend": self.engine[1], "pipeline_version": 1},
                "languages": ["en"],
                "cache_hit": self.counts[name] > 1 and not self.broken_cache}


def receipts(monkeypatch, *, changed=None):
    rows = {row.path: {"sha256": row.sha256, "atime_ns": 1, "mtime_ns": 2, "ctime_ns": 3}
            for row in load_manifest()}
    reads = []
    def read(root, fixtures):
        reads.append((root, tuple(row.path for row in fixtures)))
        result = copy.deepcopy(rows)
        if changed and len(reads) == 2:
            result[next(path for path in result if path.endswith(changed))]["atime_ns"] += 1
        return result
    monkeypatch.setattr(runner, "_ocr_fixture_root", lambda project: Path("authorized-self-test"))
    monkeypatch.setattr(runner, "_ocr_fixture_receipts", read)
    return reads


def test_all_ocr_calls_and_six_source_receipts_are_mandatory(monkeypatch):
    reads = receipts(monkeypatch)
    client = Client()
    score = runner.Scorecard()
    assert runner.run_ocr_plan(client, score)
    assert score.failed == 0
    assert len(reads) == 2 and len(reads[0][1]) == 6
    assert reads[0] == reads[1]
    assert any(args.get("project") == "Other-Project" for tool, args in client.calls)
    assert sum("O5 source preservation" in line for line in score.lines) == 6


@pytest.mark.parametrize("engine,passes", [
    (("gpu", "pytorch-rocm"), True),    # kei main: OCR on the AMD GPU
    (("gpu", "pytorch-cuda"), True),
    (("cpu", "pytorch-rocm"), False),   # a device must come with its own backend
    (("gpu", "pytorch-cpu"), False),
])
def test_canonical_extraction_accepts_each_device_with_its_own_backend(monkeypatch, engine, passes):
    """2026-09-28: the CPU-only O1 check failed kei main's 13.7.0 QA on a
    correct GPU result, and O3 failed with it because it needs O1's token."""
    receipts(monkeypatch)
    score = runner.Scorecard()
    assert runner.run_ocr_plan(Client(engine=engine), score) is passes
    o1 = next(line for line in score.lines if "O1 canonical extraction" in line)
    assert ("PASS" in o1) is passes


@pytest.mark.parametrize("failure", ["unavailable", "broken_cache", "source_changed"])
def test_ocr_failure_cannot_pass_qualification(monkeypatch, failure):
    receipts(monkeypatch, changed="not-a-png.txt" if failure == "source_changed" else None)
    client = Client(unavailable=failure == "unavailable", broken_cache=failure == "broken_cache")
    score = runner.Scorecard()
    assert not runner.run_ocr_plan(client, score)
    assert score.failed
    assert any("FAIL" in line for line in score.lines)


@pytest.mark.parametrize("enriched", [False, True])
def test_missing_file_parity_compares_raw_http_representations(enriched):
    payload = {"status": "error", "reason": "not_found", "message": "synthetic file was not found"}
    structured = dict(payload)
    if enriched:
        structured["error_code"] = "INVALID_ARGUMENT"
    result = {"content": [{"type": "text", "text": json.dumps(payload)}], "structuredContent": structured}
    client = runner.McpClient("http://test.invalid/mcp", "synthetic")
    client.http.close()
    client.http = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"result": result})))
    client.project = "Self-Test"
    try:
        response = client.call("read_document", {"filepath": "synthetic-missing.md"})
        assert runner.missing_file_parity(response) is (not enriched)
    finally:
        client.http.close()


@pytest.mark.parametrize("failure", [False, True])
def test_asset_runner_preserves_canaries_and_cleans_its_run_path(failure):
    calls = []
    removed = False
    class Assets:
        def call(self, tool, args):
            nonlocal removed
            calls.append((tool, args))
            if tool == "remove_asset":
                removed = True
                return {"status": "success"}
            if tool == "get_asset_info":
                return {"status": "error", "reason": "not_found"} if removed or len(calls) == 1 else {
                    "status": "success", "final_sha256": runner.ASSET_TEST_SHA256}
            if tool == "get_asset":
                if failure:
                    raise OSError("synthetic transport interruption")
                return {"status": "success", "_mcp_content": [{"type": "image", "mimeType": "image/png",
                    "data": runner.ASSET_TEST_DATA_URL.partition(",")[2]}]}
            return {"status": "success", "final_sha256": runner.ASSET_TEST_SHA256}
    score = runner.Scorecard()
    if failure:
        with pytest.raises(OSError, match="interruption"):
            runner.run_asset_plan(Assets(), score)
    else:
        runner.run_asset_plan(Assets(), score)
        assert score.failed == 0
    paths = {args["filepath"] for tool, args in calls if "filepath" in args}
    assert len(paths) == 1
    path = paths.pop()
    assert path.startswith("cognita-selftest-assets/selftest-runner-")
    assert path.endswith("/a.png")
    assert all(args.get("filepath") not in {"cognita-selftest-assets/a.png", "cognita-selftest-assets/b.png"} for tool, args in calls)
    assert removed and any("PASS  A8 owned asset cleanup" in line for line in score.lines)


@pytest.mark.parametrize("outcome", ["passed", "failed", "missing"])
@pytest.mark.parametrize("compose_project", ["synthetic-ocr-test", "cognita-test-owned"])
def test_host_runner_requires_packaged_ocr_receipt_not_only_zero_exit(tmp_path, monkeypatch, capsys, outcome, compose_project):
    from scripts import kei_http_selftest as host
    env = tmp_path / "synthetic.env"
    compose = tmp_path / "synthetic.yaml"
    env.write_text("synthetic")
    compose.write_text("services: {}")
    args = SimpleNamespace(compose_project=compose_project, env_file=env, compose_files=[compose],
                           mcp_port=8675, connector="self-test", host_mode="core", log_dir=tmp_path / "logs")
    def execute(*args, output, **kwargs):
        assert kwargs["capture_failure_logs"] is compose_project.startswith("cognita-test-")
        receipt = {"schema": 1, "mode": "core", "result": "passed", "mandatory_ocr": outcome,
                   "missing_file_parity": "passed"}
        output.write_text("1/1 steps passed\n" + ("" if outcome == "missing" else "selftest_receipt=" + json.dumps(receipt) + "\n"))
        return 0
    monkeypatch.setattr(host, "run_selftest", execute)
    with io.StringIO("synthetic-secret-never-log") as stdin:
        monkeypatch.setattr(host.sys, "stdin", stdin)
        assert host.run_live(args, argparse.ArgumentParser()) == (0 if outcome == "passed" else 1)
    captured = capsys.readouterr()
    assert "synthetic-secret-never-log" not in captured.out + captured.err
    if outcome == "passed":
        line = next(line for line in captured.out.splitlines() if line.startswith("selftest_receipt="))
        assert Path(json.loads(line.partition("=")[2])["canonical_log"]).is_file()
    else:
        assert "missing or failed mandatory HTTP/OCR receipt" in captured.err


@pytest.mark.skipif(not hasattr(os, "O_NOATIME"), reason="Linux packaged acceptance proves timestamp-preserving descriptors")
def test_fixture_receipts_preserve_all_timestamps(tmp_path):
    from importlib.resources import files
    fixtures = load_manifest()
    for row in fixtures:
        target = tmp_path / row.path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(files("cognita.selftest_fixtures").joinpath("data", row.path).read_bytes())
        os.utime(target, ns=(1_000_000_000, 2_000_000_000))
    before = {row.path: (tmp_path / row.path).stat() for row in fixtures}
    first = runner._ocr_fixture_receipts(tmp_path, fixtures)
    assert first == runner._ocr_fixture_receipts(tmp_path, fixtures)
    for row in fixtures:
        after = (tmp_path / row.path).stat()
        assert (after.st_atime_ns, after.st_mtime_ns, after.st_ctime_ns) == (
            before[row.path].st_atime_ns, before[row.path].st_mtime_ns, before[row.path].st_ctime_ns)


def test_explicit_provisioning_finishes_before_source_receipts(monkeypatch):
    events = []
    reads = receipts(monkeypatch)
    original_read = runner._ocr_fixture_receipts
    monkeypatch.setattr(runner, "_provision_ocr_fixtures", lambda project:
                        events.append("provision") or SimpleNamespace(copied=(), verified=tuple(range(6))))
    def measured(root, fixtures):
        assert events == ["provision"]
        return original_read(root, fixtures)
    monkeypatch.setattr(runner, "_ocr_fixture_receipts", measured)
    score = runner.Scorecard()
    assert runner.run_ocr_plan(Client(), score, provision_fixtures=True)
    assert len(reads) == 2


def test_provisioning_failure_prevents_ocr_calls(monkeypatch):
    def fail(project):
        raise RuntimeError("synthetic policy refusal")
    monkeypatch.setattr(runner, "_provision_ocr_fixtures", fail)
    client = Client()
    score = runner.Scorecard()
    assert not runner.run_ocr_plan(client, score, provision_fixtures=True)
    assert not client.calls
    assert any("fixture_provisioning phase=provision type=RuntimeError" in line for line in score.lines)


@pytest.mark.parametrize("mode", ["core", "full"])
@pytest.mark.parametrize("exit_code", [0, 1])
def test_host_explicitly_provisions_installed_fixtures_and_removes_copied_runner(
        tmp_path, monkeypatch, mode, exit_code):
    from scripts import kei_http_selftest as host
    commands, inputs = [], []
    def command(args, *unused):
        commands.append(args)
        return 0
    class Process:
        returncode = exit_code
        def __init__(self, args, **kwargs):
            commands.append(args)
        def communicate(self, value, timeout):
            inputs.append(value)
            return None, None
    monkeypatch.setattr(host, "run_command", command)
    monkeypatch.setattr(host.subprocess, "Popen", Process)
    compose = ["docker", "compose", "-p", "synthetic-fixture-proof"]
    assert host.run_selftest(tmp_path, compose, "synthetic-not-for-log", tmp_path, mode=mode) == exit_code
    # The multiline-argv check starts Workspace jobs: full mode only (a core
    # install has no Workspace runtime; seen failing on the installer VM).
    multiline = exit_code == 0 and mode == "full"
    assert len(commands) == (4 if multiline else 3)
    assert commands[0][-1] == "cognita:/tmp/cognita-run-selftest.py"
    assert f"--provision-ocr-fixtures --mode {mode}" in commands[1][-1]
    if multiline:
        compile(host._MULTILINE_ARGV_HTTP_PROBE, "<multiline-argv-http-probe>", "exec")
        assert commands[2][-4:] == ["python", "-c", host._MULTILINE_ARGV_HTTP_PROBE,
                                    f"http://127.0.0.1:8675/mcp/connectors/self-test/mcp/v{PUBLIC_CONTRACT_VERSION}"]
        assert inputs == [b"synthetic-not-for-log\n", b"synthetic-not-for-log\n"]
    else:
        assert inputs == [b"synthetic-not-for-log\n"]
    assert commands[-1][-3:] == ["rm", "-f", "/tmp/cognita-run-selftest.py"]
    assert "synthetic-not-for-log" not in (tmp_path / "selftest.log").read_text()


@pytest.mark.parametrize("wire, fault, expected_error", [
    ("json", None, None),
    ("sse", None, None),
    ("json", "missing_receipt", "did not return its backup receipt"),
    ("json", "wrong_replay_receipt", "did not replay the original backup receipt"),
    ("json", "missing_replay_flag", "did not replay the original backup receipt"),
    ("json", "duplicate_backup", "replay created a duplicate backup"),
    ("json", "missing_conflict", "changed remove_document arguments did not conflict"),
])
def test_canonical_probe_executes_raw_http_envelopes(monkeypatch, capsys, wire, fault, expected_error):
    """Execute the shipped probe against native MCP envelopes, without a data wrapper."""
    import base64
    from scripts import kei_http_selftest as host

    calls, clients, jobs, files, backups, operations = [], [], {}, {}, [], {}
    effects = {"deletes": 0, "backup_lists": 0, "cleanup": False}
    real_client = httpx.Client
    expected_stdout = "line1\nline2\n"

    def envelope(message, payload):
        body = {"jsonrpc": "2.0", "id": message["id"], "result": {
            "content": [{"type": "text", "text": json.dumps(payload)}],
            "structuredContent": payload,
        }}
        if wire == "sse":
            return httpx.Response(200, headers={"Content-Type": "text/event-stream"},
                                  text="event: message\ndata: " + json.dumps(body) + "\n\n")
        return httpx.Response(200, json=body)

    def respond(request):
        assert request.url == "http://probe.invalid/mcp"
        assert request.headers["authorization"] == "Bearer synthetic-probe-secret"
        message = json.loads(request.content)
        if message["method"] == "initialize":
            return httpx.Response(200, headers={"mcp-session-id": "probe-session"},
                                  json={"jsonrpc": "2.0", "id": 0, "result": {}})
        assert request.headers["mcp-session-id"] == "probe-session"
        if message["method"] == "notifications/initialized":
            return httpx.Response(202)
        assert message["method"] == "tools/call"
        calls.append(message)
        name, args = message["params"]["name"], message["params"]["arguments"]
        if name == "workspace_start_job":
            if any("\x00" in argument for argument in args["argv"]):
                return envelope(message, {"status": "error", "reason": "runtime_unavailable"})
            assert args["argv"] == ["python3", "-c", "print('line1')\nprint('line2')"]
            assert args["cwd"] == "/workspace" and args["output_encoding"] == "text"
            key = args["idempotency_key"]
            if key not in jobs:
                jobs[key] = "job-" + str(len(jobs) + 1)
            return envelope(message, {"status": "success", "job": {
                "job_id": jobs[key], "state": "succeeded", "stdout": expected_stdout,
            }})
        if name in {"workspace_get_job", "workspace_cancel_job"}:
            # Unknown handles must not make this fake silently pass a broken probe.
            assert args["job_id"] in jobs.values()
            return envelope(message, {"status": "success", "job": {
                "job_id": args["job_id"], "state": "succeeded",
                "stdout": base64.b64encode(expected_stdout.encode()).decode(),
            }})
        assert args["project"] == "Self-Test"
        path = args["filepath"]
        assert path.startswith("cognita-selftest-operation-replay-") and path.endswith(".md")
        if name == "add_document":
            assert not files
            files[path] = args["content"]
            payload = {"status": "success", "filepath": path}
        elif name == "list_backups":
            effects["backup_lists"] += 1
            visible = list(backups)
            if fault == "duplicate_backup" and effects["backup_lists"] == 3:
                visible.append("unexpected-second-backup")
            payload = {"status": "success", "backups": [
                {"filepath": path, "backup_id": backup_id} for backup_id in visible
            ]}
        elif name == "remove_document":
            operation = args["operation_id"]
            if operation.endswith("-cleanup"):
                effects["cleanup"] = True
                if path in files:
                    del files[path]
                    effects["deletes"] += 1
                    backups.append("cleanup-backup")
                    payload = {"status": "success", "file_deleted": True,
                               "previous_backup_id": "cleanup-backup"}
                else:
                    payload = {"status": "error", "reason": "not_found"}
            elif operation in operations:
                if args != operations[operation]:
                    payload = {"status": "error", "reason":
                               "not_found" if fault == "missing_conflict" else "operation_conflict"}
                else:
                    payload = {"status": "success", "file_deleted": True, "replayed": True,
                               "previous_backup_id": "backup-1"}
                    if fault == "wrong_replay_receipt":
                        payload["previous_backup_id"] = "different-backup"
                    if fault == "missing_replay_flag":
                        del payload["replayed"]
            else:
                assert args["delete_file"] is True and path in files
                del files[path]
                effects["deletes"] += 1
                backups.append("backup-1")
                operations[operation] = dict(args)
                payload = {"status": "success", "file_deleted": True, "previous_backup_id": "backup-1"}
                if fault == "missing_receipt":
                    del payload["previous_backup_id"]
        elif name == "read_document":
            assert path not in files
            payload = {"status": "error", "reason": "not_found"}
        else:
            raise AssertionError("unexpected probe tool: " + name)
        return envelope(message, payload)

    def make_client(**kwargs):
        client = real_client(transport=httpx.MockTransport(respond), **kwargs)
        clients.append(client)
        return client

    monkeypatch.setattr(httpx, "Client", make_client)
    monkeypatch.setattr(host.sys, "argv", ["probe", "http://probe.invalid/mcp"])
    with io.StringIO("synthetic-probe-secret\n") as stdin:
        monkeypatch.setattr(host.sys, "stdin", stdin)
        try:
            if expected_error:
                with pytest.raises(AssertionError, match=expected_error):
                    exec(compile(host._MULTILINE_ARGV_HTTP_PROBE, "<http-probe>", "exec"), {})
            else:
                exec(compile(host._MULTILINE_ARGV_HTTP_PROBE, "<http-probe>", "exec"), {})
        finally:
            for client in clients:
                client.close()
    assert clients and all(client.is_closed for client in clients)
    assert effects["cleanup"] and not files
    assert effects["deletes"] == 1 and backups == ["backup-1"]
    assert len(jobs) == 2
    ids = [message["id"] for message in calls]
    assert len(ids) == len(set(ids))
    removes = [message for message in calls if message["params"]["name"] == "remove_document"
               and not message["params"]["arguments"]["operation_id"].endswith("-cleanup")]
    if fault != "missing_receipt":
        assert removes[0]["params"]["arguments"] == removes[1]["params"]["arguments"]
        assert removes[0]["params"]["_meta"]["progressToken"] != removes[1]["params"]["_meta"]["progressToken"]
    captured = capsys.readouterr()
    assert "synthetic-probe-secret" not in captured.out + captured.err
    assert ("PASS canonical HTTP" in captured.out) is (expected_error is None)


@pytest.mark.parametrize("field", ["sha256", "width", "height", "text", "regions", "languages", "outcome"])
def test_cache_check_rejects_changed_durable_result(field):
    first = Client().call("ocr_asset", {"filepath": "canonical-clear.png"})
    repeated = copy.deepcopy(first)
    repeated[field] = "changed"
    assert field in runner._ocr_cache_changes(first, repeated)


@pytest.mark.parametrize("field", ["name", "version", "model_fingerprint", "pipeline_version", "device", "backend"])
def test_cache_check_rejects_changed_pipeline_identity(field):
    first = Client().call("ocr_asset", {"filepath": "canonical-clear.png"})
    repeated = copy.deepcopy(first)
    repeated["engine"][field] = "changed"
    assert "engine." + field in runner._ocr_cache_changes(first, repeated)


def test_cache_check_allows_absent_invocation_binding_and_search_uses_distinctive_token():
    first = Client().call("ocr_asset", {"filepath": "canonical-clear.png"})
    first["engine"]["device_binding"] = ""
    repeated = copy.deepcopy(first)
    del repeated["engine"]["device_binding"]
    assert runner._ocr_cache_changes(first, repeated) == []
    assert runner._ocr_search_token("Report-v2.txt MixedCase Label: Alpha-42") == "MixedCase"
    assert runner._ocr_search_token("") is None


@pytest.mark.parametrize("capture, exit_code, log_failure", [(True, 1, False), (True, 1, True),
                                                            (True, 0, False), (False, 1, False)])
def test_owned_failure_logs_are_captured_before_cleanup_without_masking_result(
        tmp_path, monkeypatch, capture, exit_code, log_failure):
    from scripts import kei_http_selftest as host
    commands = []
    def command(args, *unused):
        commands.append(args)
        if "logs" in args and log_failure:
            raise host.RunnerError("synthetic diagnostic failure")
        return 0
    class Process:
        returncode = exit_code
        def __init__(self, args, **kwargs):
            # Header must be durable before the child inherits the descriptor.
            assert "--provision-ocr-fixtures" in (tmp_path / "selftest.log").read_text()
        def communicate(self, value, timeout):
            return None, None
    monkeypatch.setattr(host, "run_command", command)
    monkeypatch.setattr(host.subprocess, "Popen", Process)
    compose = ["docker", "compose", "-p", "cognita-test-owned"]
    assert host.run_selftest(tmp_path, compose, "synthetic-secret", tmp_path,
                             capture_failure_logs=capture) == exit_code
    assert commands[-1][-3:] == ["rm", "-f", "/tmp/cognita-run-selftest.py"]
    assert any("logs" in command for command in commands) is bool(capture and exit_code)
    if capture and exit_code:
        assert commands[-2][-5:] == ["logs", "--no-color", "--tail", "120", "cognita"]
    output = (tmp_path / "selftest.log").read_text()
    assert "synthetic-secret" not in output
    if log_failure:
        assert "app failure log capture failed type=RunnerError" in output
