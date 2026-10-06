"""Pure effective-index and managed-book-path decisions.

This module owns no persistence or filesystem access. Callers provide the
current, already validated policy/config snapshot and apply its decisions under
their existing project locks and publication protocol.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Mapping, Sequence

from .config import (
    BookBinding,
    BookLayout,
    FolderRule,
    normalize_project_path,
)

IndexRule = Literal[
    "hard_exclusion", "folder_exclusion", "per_file_exclusion", "global_exclusion",
    "book_role_exclusion", "folder_inclusion", "global_inclusion",
]


@dataclass(frozen=True, slots=True)
class IndexDecision:
    indexed: bool
    reason: IndexRule
    matched_path: str | None = None


@dataclass(frozen=True, slots=True)
class MutationDecision:
    allowed: bool
    disposition: Literal[
        "ordinary", "preserve_original", "protected", "guarded_configuration",
        "configuration_conflict",
    ]
    reason: str
    preserve_original: bool = False


def _casefold_path(path: str) -> str:
    return normalize_project_path(path, allow_root=True).casefold()


def _is_at_or_below(path: str, root: str) -> bool:
    return path == root or (root == "" or path.startswith(root.rstrip("/") + "/"))


def _ancestors(path: str) -> tuple[str, ...]:
    if not path:
        return ("",)
    parts = path.split("/")
    return tuple("/".join(parts[:index]) for index in range(len(parts), -1, -1))


class EffectiveIndexPolicy:
    """Compose folder, hard, per-file, global, and optional book-role decisions.

    A false ancestor always wins. Explicit true folder rules only express the
    folder-specific decision; they never bypass hard/global/per-file/book gates.
    """

    def __init__(
        self,
        folder_rules: Sequence[FolderRule | Mapping[str, object]],
        *,
        hard_exclusion_roots: Sequence[str] = (),
        deindexed_paths: Sequence[str] = (),
        book_layout: BookLayout | None = None,
    ) -> None:
        parsed = [
            rule if isinstance(rule, FolderRule) else FolderRule.model_validate(rule, strict=True)
            for rule in folder_rules
        ]
        folded = [rule.path.casefold() for rule in parsed]
        if len(folded) != len(set(folded)):
            raise ValueError("folder rules must have unique normalized paths")
        self._rules = {rule.path.casefold(): rule.indexed for rule in parsed}
        self._hard_roots = tuple(sorted({_casefold_path(p) for p in hard_exclusion_roots}))
        self._deindexed = frozenset(_casefold_path(p) for p in deindexed_paths)
        self._book_layout = book_layout
        self._book_roles, self._book_hard_roots = self._book_paths(book_layout)

    @property
    def layout(self) -> BookLayout | None:
        """The validated book layout used to constrain role admission, if enabled."""
        return self._book_layout

    @staticmethod
    def _book_paths(
        layout: BookLayout | None,
    ) -> tuple[dict[str, str], tuple[str, ...]]:
        if layout is None:
            return {}, ()
        roles: dict[str, str] = {}
        hard = [layout.shared_paths.book_audio_root, ".cognita-storage"]
        if layout.source_master_filepath is not None:
            hard.append(layout.source_master_filepath)
        for chapter in layout.chapters:
            roles[chapter.working_filepath.casefold()] = "chapter"
            if chapter.summary_filepath:
                roles[chapter.summary_filepath.casefold()] = "summary"
            roles[chapter.tagged_filepath.casefold()] = "tagged"
            hard.extend((chapter.originals_root, chapter.audio_root, chapter.tagged_filepath))
        for item in layout.indexed_references:
            roles[item.filepath.casefold()] = item.role
        for item in layout.indexed_instructions:
            roles[item.filepath.casefold()] = "instructions"
        for item in layout.indexed_workflow_documents:
            roles[item.filepath.casefold()] = "workflow"
        return roles, tuple(sorted({_casefold_path(path) for path in hard}))

    def decision(
        self,
        path: str,
        *,
        globally_eligible: bool = True,
        per_file_indexed: bool = True,
        book_role: str | None = None,
    ) -> IndexDecision:
        normalized = _casefold_path(path)
        for root in (*self._hard_roots, *self._book_hard_roots):
            if _is_at_or_below(normalized, root):
                return IndexDecision(False, "hard_exclusion", root)
        for ancestor in _ancestors(normalized):
            if self._rules.get(ancestor) is False:
                return IndexDecision(False, "folder_exclusion", ancestor)
        if not per_file_indexed or normalized in self._deindexed:
            return IndexDecision(False, "per_file_exclusion", normalized)
        if not globally_eligible:
            return IndexDecision(False, "global_exclusion")
        if self._book_layout is not None:
            registered_role = self._book_roles.get(normalized)
            if registered_role is None:
                return IndexDecision(False, "book_role_exclusion", normalized)
            if book_role is not None and registered_role != book_role:
                return IndexDecision(False, "book_role_exclusion", normalized)
        for ancestor in _ancestors(normalized):
            if self._rules.get(ancestor) is True:
                return IndexDecision(True, "folder_inclusion", ancestor)
        return IndexDecision(True, "global_inclusion")

    def is_indexed(self, path: str, **kwargs: object) -> bool:
        return self.decision(path, **kwargs).indexed


class BookMutationPolicy:
    """Classify generic mutations against one validated book layout snapshot."""

    STATE_ROOT = ".cognita-storage"
    BINDING_PATH = ".cognita-book-binding.json"
    LAYOUT_PATH = "Project Files/Book_Layout.json"

    def __init__(
        self,
        layout: BookLayout | None,
        *,
        config_state: Literal[
            "never_enabled", "bootstrap_pending", "enabled", "configuration_conflict"
        ],
        binding: BookBinding | None = None,
    ) -> None:
        self.layout = layout
        self.config_state = config_state
        self.binding = binding
        if config_state == "enabled" and layout is None:
            raise ValueError("enabled book policy requires its validated layout")
        if binding is not None and binding.state_root != self.STATE_ROOT:
            raise ValueError("book state root must remain fixed at .cognita-storage")

    def decide(
        self,
        path: str,
        *,
        operation: Literal["write", "remove", "move", "copy", "restore"],
        source_exists: bool = False,
        first_original_exists: bool = False,
    ) -> MutationDecision:
        normalized = _casefold_path(path)
        state_root = self.STATE_ROOT.casefold()
        binding_path = self.BINDING_PATH.casefold()
        layout_path = self.LAYOUT_PATH.casefold()

        if self.config_state == "configuration_conflict":
            if normalized in (binding_path, layout_path, state_root) or _is_at_or_below(
                normalized, state_root
            ):
                return MutationDecision(False, "configuration_conflict", "configuration_conflict")
        if normalized == binding_path:
            return MutationDecision(
                False, "guarded_configuration", "binding_requires_validated_bootstrap"
            )
        if normalized == state_root or _is_at_or_below(normalized, state_root):
            return MutationDecision(False, "protected", "managed_project_state")
        if normalized == layout_path:
            return MutationDecision(False, "guarded_configuration", "layout_requires_validated_guarded_write")

        if self.config_state in ("never_enabled", "bootstrap_pending") or self.layout is None:
            return MutationDecision(True, "ordinary", "legacy_project_path")

        layout = self.layout
        if layout.source_master_filepath is not None and normalized == layout.source_master_filepath.casefold():
            return MutationDecision(False, "protected", "registered_source_master")
        if normalized == layout.shared_paths.production_settings_filepath.casefold():
            return MutationDecision(
                False, "guarded_configuration", "production_settings_require_raw_guard"
            )
        if normalized == layout.shared_paths.book_audio_root.casefold() or _is_at_or_below(
            normalized, layout.shared_paths.book_audio_root.casefold()
        ):
            return MutationDecision(False, "protected", "managed_audio_root")

        for chapter in layout.chapters:
            if normalized in {
                chapter.chapter_state_filepath.casefold(),
            }:
                return MutationDecision(
                    False, "guarded_configuration", "chapter_state_requires_raw_guard"
                )
            if any(
                _is_at_or_below(normalized, root.casefold())
                for root in (chapter.originals_root, chapter.audio_root)
            ):
                return MutationDecision(False, "protected", "immutable_book_root")
            if normalized == chapter.working_filepath.casefold():
                if source_exists and not first_original_exists and operation in (
                    "write", "restore", "copy"
                ):
                    return MutationDecision(
                        True, "preserve_original", "first_registered_working_write",
                        preserve_original=True,
                    )
                return MutationDecision(True, "ordinary", "registered_working_chapter")
            if normalized == chapter.tagged_filepath.casefold():
                return MutationDecision(True, "ordinary", "registered_tagged_copy")

        return MutationDecision(True, "ordinary", "unmanaged_project_path")


__all__ = [
    "IndexDecision", "MutationDecision", "EffectiveIndexPolicy", "BookMutationPolicy",
]
