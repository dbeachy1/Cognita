"""Fail-closed discovery of the fixed project book binding and layout."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from .config import (
    BookBinding,
    BookLayout,
    classify_book_config,
    validate_book_binding,
    validate_book_layout,
)

BINDING_PATH = ".cognita-book-binding.json"
LAYOUT_PATH = "Project Files/Book_Layout.json"
STATE_ROOT = ".cognita-storage"


@dataclass(frozen=True)
class BookConfigSnapshot:
    config_state: str
    layout: BookLayout | None
    binding: BookBinding | None
    layout_sha256: str | None
    binding_sha256: str | None


def _read_config(root: Path, relative: str) -> tuple[bool, bytes | None, bool]:
    """Return (present, bytes, valid-file-kind), never follow path symlinks."""
    target = root
    # The canonical path itself and every parent must be ordinary project
    # directories. Checking only the leaf would follow a substituted
    # ``Project Files`` junction while reading the supposedly fixed layout.
    parts = relative.split("/")
    for parent in parts[:-1]:
        target = target / parent
        try:
            mode = target.lstat().st_mode
        except FileNotFoundError:
            return False, None, True
        except OSError:
            return True, None, False
        import stat
        if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
            return True, None, False
    target = target / parts[-1]
    try:
        facts = target.lstat()
    except FileNotFoundError:
        return False, None, True
    except OSError:
        return True, None, False
    if target.is_symlink() or not target.is_file():
        return True, None, False
    try:
        return True, target.read_bytes(), True
    except OSError:
        return True, None, False


def load_book_config(project_root: Path, state: object | None = None) -> BookConfigSnapshot:
    """Read and validate only the canonical configuration paths.

    A valid layout without its fixed binding remains `bootstrap_pending`; it is
    not allowed to activate indexing policy or audiobook operations by itself.
    """
    root = Path(project_root).resolve(strict=True)
    binding_present, binding_bytes, binding_file = _read_config(root, BINDING_PATH)
    layout_present, layout_bytes, layout_file = _read_config(root, LAYOUT_PATH)
    binding: BookBinding | None = None
    layout: BookLayout | None = None
    binding_valid = False
    layout_valid = False
    if binding_file and binding_bytes is not None:
        try:
            binding = validate_book_binding(binding_bytes)
            binding_valid = (
                binding.state_root == STATE_ROOT
                and binding.layout_filepath == LAYOUT_PATH
                and state is not None
            )
        except Exception:
            binding_valid = False
    if layout_file and layout_bytes is not None:
        try:
            layout = validate_book_layout(layout_bytes)
            layout_valid = True
        except Exception:
            layout_valid = False
    config_state = classify_book_config(
        binding_present=binding_present,
        layout_present=layout_present,
        binding_valid=binding_valid,
        layout_valid=layout_valid,
        binding_book_id=binding.book_id if binding is not None else None,
        layout_book_id=layout.book_id if layout is not None else None,
        binding_layout_filepath=binding.layout_filepath if binding is not None else None,
    )
    # The DB's fixed root is part of the binding's validity. A layout may be
    # returned for diagnostics during bootstrap, but consumers must consult
    # config_state before using it as policy.
    return BookConfigSnapshot(
        config_state=config_state,
        layout=layout,
        binding=binding,
        layout_sha256=(hashlib.sha256(layout_bytes).hexdigest()
                       if layout_bytes is not None else None),
        binding_sha256=(hashlib.sha256(binding_bytes).hexdigest()
                        if binding_bytes is not None else None),
    )
