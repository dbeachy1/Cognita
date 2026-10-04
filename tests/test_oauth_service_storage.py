from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

from cognita.oauth_service.bootstrap import _backup_sqlite
from cognita.oauth_service.storage import ensure_private_sqlite


@pytest.mark.skipif(os.name != "posix", reason="POSIX mode bits are not portable to Windows")
def test_new_sqlite_artifact_is_private(tmp_path: Path) -> None:
    path = tmp_path / "oauth-service.sqlite3"
    ensure_private_sqlite(path, create=True)
    assert path.stat().st_mode & 0o777 == 0o600
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE marker (value TEXT)")
    connection.commit()
    connection.close()
    assert path.stat().st_mode & 0o777 == 0o600


@pytest.mark.skipif(os.name != "posix", reason="POSIX mode bits are not portable to Windows")
def test_existing_sqlite_artifact_is_tightened_without_replacement(tmp_path: Path) -> None:
    path = tmp_path / "oauth-service.sqlite3"
    path.write_bytes(b"existing")
    inode = path.stat().st_ino
    path.chmod(0o644)

    ensure_private_sqlite(path)

    assert path.read_bytes() == b"existing"
    assert path.stat().st_ino == inode
    assert path.stat().st_mode & 0o777 == 0o600


@pytest.mark.skipif(os.name != "posix", reason="POSIX file types are not portable to Windows")
def test_non_file_sqlite_path_is_rejected_without_chmod(tmp_path: Path) -> None:
    path = tmp_path / "oauth-service.sqlite3"
    path.mkdir(mode=0o755)

    with pytest.raises(OSError):
        ensure_private_sqlite(path, create=True)

    assert path.stat().st_mode & 0o777 == 0o755


@pytest.mark.skipif(os.name != "posix", reason="POSIX symbolic links are not portable to Windows")
def test_symbolic_link_sqlite_path_is_rejected_without_touching_target(tmp_path: Path) -> None:
    target = tmp_path / "other.sqlite3"
    target.write_bytes(b"other")
    target.chmod(0o644)
    path = tmp_path / "oauth-service.sqlite3"
    path.symlink_to(target)

    with pytest.raises(OSError):
        ensure_private_sqlite(path, create=True)

    assert target.read_bytes() == b"other"
    assert target.stat().st_mode & 0o777 == 0o644


def test_non_posix_mode_leaves_creation_unchanged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "oauth-service.sqlite3"
    monkeypatch.setattr(os, "name", "nt")
    ensure_private_sqlite(path, create=True)
    assert not path.exists()


@pytest.mark.skipif(os.name != "posix", reason="POSIX mode bits are not portable to Windows")
def test_sqlite_backup_is_private(tmp_path: Path) -> None:
    source = tmp_path / "oauth-service.sqlite3"
    connection = sqlite3.connect(source)
    connection.execute("CREATE TABLE marker (value TEXT)")
    connection.execute("INSERT INTO marker VALUES ('ok')")
    connection.commit()
    connection.close()

    _backup_sqlite(source, tmp_path)

    backups = list((tmp_path / "oauth-backups").glob("oauth-service.*.sqlite3"))
    assert len(backups) == 1
    assert backups[0].stat().st_mode & 0o777 == 0o600
