from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from scripts.clear_watcher_queue import AdminRequestError, clear_queue


class _Handler(BaseHTTPRequestHandler):
    requests = []
    reject_login = False

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
        type(self).requests.append((self.path, body, dict(self.headers)))
        if self.path == "/api/login":
            if type(self).reject_login:
                self.send_response(401)
                self.end_headers()
                self.wfile.write(b'{"detail":"private response must not be printed"}')
                return
            self.send_response(200)
            self.send_header("Set-Cookie", "cognita_csrf=fresh-token; Path=/; SameSite=Strict")
            payload = {"ok": True}
        else:
            self.send_response(200)
            payload = {"project": "KEI", "cleared_paths": 2, "active_cancelled": False}
        encoded = json.dumps(payload).encode()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, _format, *_args):
        pass


@pytest.fixture
def admin_server():
    _Handler.requests = []
    _Handler.reject_login = False
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=False)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()
        assert not thread.is_alive(), "test Admin HTTP server did not stop"


def test_cli_client_logs_in_sends_csrf_and_posts_encoded_project(admin_server):
    result = clear_queue(admin_server, "Project A", "admin", "test-password")
    assert result == {"project": "KEI", "cleared_paths": 2, "active_cancelled": False}
    login, clear = _Handler.requests
    assert login[0] == "/api/login"
    assert login[1] == {"username": "admin", "password": "test-password"}
    assert clear[0] == "/api/projects/Project%20A/watcher/clear-queue"
    assert next(value for key, value in clear[2].items() if key.lower() == "x-csrf-token") == "fresh-token"


def test_cli_client_reports_auth_status_without_echoing_response_body(admin_server, capsys):
    _Handler.reject_login = True
    with pytest.raises(AdminRequestError, match="HTTP 401"):
        clear_queue(admin_server, "KEI", "admin", "sensitive-password")
    captured = capsys.readouterr()
    assert "sensitive-password" not in captured.out + captured.err
    assert "private response" not in captured.out + captured.err


def test_cli_client_rejects_url_with_embedded_credentials():
    with pytest.raises(AdminRequestError, match="without embedded credentials"):
        clear_queue("http://admin:secret@127.0.0.1:8676", "KEI", "admin", "password")
