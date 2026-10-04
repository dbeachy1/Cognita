"""Native admin-UI TLS wiring (4.1.0).

The admin server can serve HTTPS directly (uvicorn ssl) from a mkcert cert on the
LAN — no reverse proxy. These tests cover the plumbing without needing a real
socket: that the cert/key reach uvicorn's Config, and that startup validation
rejects a half-configured or missing cert cleanly.
"""

import pytest

from cognita.__main__ import _make_server, _validate_tls_config
from cognita.config import CognitaConfig


def _app():
    from fastapi import FastAPI

    return FastAPI()


def test_make_server_wires_ssl_when_cert_and_key_given(tmp_path):
    cert = tmp_path / "kei.pem"
    key = tmp_path / "kei-key.pem"
    cert.write_text("x")
    key.write_text("y")
    server = _make_server(_app(), "0.0.0.0", 8676, str(cert), str(key))
    assert server.config.ssl_certfile == str(cert)
    assert server.config.ssl_keyfile == str(key)
    assert server.config.is_ssl is True


def test_make_server_plain_http_by_default():
    server = _make_server(_app(), "127.0.0.1", 8676)
    assert server.config.ssl_certfile is None
    assert server.config.is_ssl is False


def test_tls_config_defaults_are_empty():
    cfg = CognitaConfig()
    assert cfg.admin_tls_certfile == "" and cfg.admin_tls_keyfile == ""
    _validate_tls_config(cfg)  # both empty -> no error (plain HTTP)


def test_validate_rejects_half_configured():
    with pytest.raises(SystemExit, match="BOTH"):
        _validate_tls_config(CognitaConfig(admin_tls_certfile="only-cert.pem"))
    with pytest.raises(SystemExit, match="BOTH"):
        _validate_tls_config(CognitaConfig(admin_tls_keyfile="only-key.pem"))


def test_validate_rejects_missing_files(tmp_path):
    with pytest.raises(SystemExit, match="not found"):
        _validate_tls_config(CognitaConfig(
            admin_tls_certfile=str(tmp_path / "nope.pem"),
            admin_tls_keyfile=str(tmp_path / "nope-key.pem"),
        ))


def test_validate_accepts_existing_pair(tmp_path):
    cert = tmp_path / "c.pem"
    cert.write_text("x")
    key = tmp_path / "k.pem"
    key.write_text("y")
    _validate_tls_config(CognitaConfig(
        admin_tls_certfile=str(cert), admin_tls_keyfile=str(key)))
