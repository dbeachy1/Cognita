"""Filesystem permissions for package-owned OAuth SQLite artifacts."""

from __future__ import annotations

import os
import stat
from pathlib import Path

_PRIVATE_MODE = 0o600


def ensure_private_sqlite(path: Path, *, create: bool = False) -> None:
    """Keep a package-owned SQLite file private on POSIX systems.

    SQLite creates a missing database using the process umask, which can leave
    token checksums readable when the service runs with a permissive umask. A
    precreated file closes that window; the explicit chmod also tightens an
    existing store imported from an older release. Windows file permissions are
    intentionally left to the platform's existing behavior.
    """
    if os.name != "posix":
        return
    path = Path(path)
    if path.is_symlink():
        raise OSError(f"OAuth SQLite path must not be a symbolic link: {path}")
    if not create and not path.exists():
        return
    flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    if create:
        flags |= os.O_CREAT
    descriptor = os.open(path, flags, _PRIVATE_MODE)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise OSError(f"OAuth SQLite path is not a regular file: {path}")
        os.fchmod(descriptor, _PRIVATE_MODE)
    finally:
        os.close(descriptor)
