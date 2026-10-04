"""scripts/set-admin-credentials.py — writes ONLY the password hash, never the
plaintext; sets the username too."""

import importlib.util
import io
import json
from pathlib import Path

import yaml
from argon2 import PasswordHasher

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "set-admin-credentials.py"


def _load():
    spec = importlib.util.spec_from_file_location("set_admin_credentials", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run(mod, cfg, monkeypatch, *, username_input, passwords):
    monkeypatch.setattr("builtins.input", lambda *a, **k: username_input)
    pwds = iter(passwords)
    monkeypatch.setattr(mod.getpass, "getpass", lambda *a, **k: next(pwds))
    monkeypatch.setattr("sys.argv", ["set-admin-credentials.py", "--config", str(cfg)])
    return mod.main()


def _run_stdin(mod, cfg, monkeypatch, *, username, password):
    monkeypatch.setattr("sys.argv", ["set-admin-credentials.py", "--config", str(cfg), "--stdin-json"])
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"username": username, "password": password})))
    return mod.main()


def test_writes_hash_and_username_not_plaintext(tmp_path, monkeypatch):
    mod = _load()
    cfg = tmp_path / "cognita.yaml"
    assert _run(mod, cfg, monkeypatch, username_input="operator",
                passwords=["s3cret-pw!", "s3cret-pw!"]) == 0
    text = cfg.read_text(encoding="utf-8")
    data = yaml.safe_load(text)
    assert data["admin_username"] == "operator"
    assert PasswordHasher().verify(data["admin_password_hash"], "s3cret-pw!")
    assert "admin_password_sha256" not in data
    assert "s3cret-pw!" not in text  # plaintext must never be written


def test_blank_username_keeps_current(tmp_path, monkeypatch):
    mod = _load()
    cfg = tmp_path / "cognita.yaml"
    cfg.write_text("admin_username: existinguser\n", encoding="utf-8")
    # empty input -> keep the current username
    assert _run(mod, cfg, monkeypatch, username_input="", passwords=["pw12345", "pw12345"]) == 0
    data = yaml.safe_load(cfg.read_text(encoding="utf-8"))
    assert data["admin_username"] == "existinguser"


def test_preserves_existing_keys(tmp_path, monkeypatch):
    mod = _load()
    cfg = tmp_path / "cognita.yaml"
    cfg.write_text('public_base_url: "https://x.example.com"\nadmin_host: "0.0.0.0"\n',
                   encoding="utf-8")
    assert _run(mod, cfg, monkeypatch, username_input="operator",
                passwords=["pw12345", "pw12345"]) == 0
    data = yaml.safe_load(cfg.read_text(encoding="utf-8"))
    assert data["public_base_url"] == "https://x.example.com"  # untouched
    assert data["admin_host"] == "0.0.0.0"  # untouched
    assert PasswordHasher().verify(data["admin_password_hash"], "pw12345")


def test_mismatch_aborts_without_writing(tmp_path, monkeypatch):
    mod = _load()
    cfg = tmp_path / "cognita.yaml"
    assert _run(mod, cfg, monkeypatch, username_input="operator", passwords=["one", "two"]) == 2
    assert not cfg.exists()


def test_stdin_json_writes_argon2id_without_echoing_credentials(tmp_path, monkeypatch, capsys):
    mod = _load()
    cfg = tmp_path / "cognita.yaml"
    cfg.write_text("public_base_url: https://localhost\nadmin_password_sha256: legacy\n",
                   encoding="utf-8")
    username = "windows-admin"
    password = "synthetic-secret-from-stdin"

    assert _run_stdin(mod, cfg, monkeypatch, username=username, password=password) == 0

    output = capsys.readouterr()
    assert password not in output.out + output.err
    assert username not in output.out + output.err
    data = yaml.safe_load(cfg.read_text(encoding="utf-8"))
    assert data["admin_username"] == username
    assert PasswordHasher().verify(data["admin_password_hash"], password)
    assert "admin_password_sha256" not in data
    assert data["public_base_url"] == "https://localhost"
    assert password not in cfg.read_text(encoding="utf-8")


def test_stdin_json_rejects_extra_fields_without_writing(tmp_path, monkeypatch, capsys):
    mod = _load()
    cfg = tmp_path / "cognita.yaml"
    password = "synthetic-secret"
    monkeypatch.setattr("sys.argv", ["set-admin-credentials.py", "--config", str(cfg), "--stdin-json"])
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({
        "username": "admin", "password": password, "mode": "core",
    })))

    assert mod.main() == 2

    output = capsys.readouterr()
    assert password not in output.out + output.err
    assert not cfg.exists()
