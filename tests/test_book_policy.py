from __future__ import annotations

from cognita.books.config import BookLayout, FolderRule
from cognita.books.policy import BookMutationPolicy, EffectiveIndexPolicy

from test_book_config import _layout


def test_folder_exclusion_ancestor_wins_and_explicit_inclusion_cannot_override_it():
    policy = EffectiveIndexPolicy([
        FolderRule(path="Archive", indexed=False),
        FolderRule(path="Archive/Keep", indexed=True),
    ])
    excluded = policy.decision("Archive/Keep/story.docx")
    assert not excluded.indexed
    assert excluded.reason == "folder_exclusion"
    assert excluded.matched_path == "archive"


def test_folder_inclusion_does_not_override_hard_global_or_per_file_exclusions():
    policy = EffectiveIndexPolicy(
        [FolderRule(path="Included", indexed=True)],
        hard_exclusion_roots=("Included/Managed",),
        deindexed_paths=("Included/suppressed.docx",),
    )
    assert not policy.is_indexed("Included/Managed/item.md")
    assert not policy.is_indexed("Included/suppressed.docx")
    assert not policy.is_indexed("Included/file.bin", globally_eligible=False)
    assert policy.is_indexed("Included/file.md")


def test_book_role_admission_and_audio_source_exclusion_are_separate_from_exact_reads():
    layout = BookLayout.model_validate(_layout(), strict=True)
    policy = EffectiveIndexPolicy([], book_layout=layout)
    assert policy.is_indexed("Chapters/1/chapter.docx")
    assert policy.is_indexed("Project Files/ref.docx")
    assert not policy.is_indexed("Project Files/Source/Version1.docx")
    assert not policy.is_indexed("Audiobook/Chapters/1/chunk.pcm")
    assert not policy.is_indexed("Chapters/1/chapter_audio-tags.docx")
    # EffectiveIndexPolicy reports retrieval admission only; exact-path reads
    # are governed by existing read permissions and are outside this decision.


def test_mutation_policy_protects_master_and_managed_roots_and_preserves_first_original():
    layout = BookLayout.model_validate(_layout(), strict=True)
    policy = BookMutationPolicy(layout, config_state="enabled")
    master = policy.decide(
        "Project Files/Source/Version1.docx", operation="write", source_exists=True
    )
    assert not master.allowed and master.reason == "registered_source_master"
    managed = policy.decide("Audiobook/Chapters/1/chunk.pcm", operation="remove")
    assert not managed.allowed and managed.reason == "managed_audio_root"
    initial = policy.decide(
        "Chapters/1/chapter.docx", operation="write", source_exists=True,
        first_original_exists=False,
    )
    assert initial.allowed and initial.preserve_original
    later = policy.decide(
        "Chapters/1/chapter.docx", operation="write", source_exists=True,
        first_original_exists=True,
    )
    assert later.allowed and not later.preserve_original


def test_binding_and_invalid_config_paths_never_fall_back_to_legacy_writes():
    pristine = BookMutationPolicy(None, config_state="never_enabled")
    assert pristine.decide("ordinary/file.txt", operation="write").allowed
    assert not pristine.decide(
        ".cognita-book-binding.json", operation="write"
    ).allowed
    damaged = BookMutationPolicy(None, config_state="configuration_conflict")
    result = damaged.decide(".cognita-storage/state.sqlite", operation="write")
    assert not result.allowed
    assert result.disposition == "configuration_conflict"
