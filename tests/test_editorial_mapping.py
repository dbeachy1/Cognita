from __future__ import annotations

import copy

from cognita.books.editorial_mapping import dry_run_editorial_layout


def test_editorial_dry_run_maps_legacy_sources_reports_collisions_and_changes_nothing(tmp_path):
    prologue = tmp_path / "Prologue.docx"
    chapter = tmp_path / "Chapter02.docx"
    prologue.write_bytes(b"prologue bytes")
    chapter.write_bytes(b"chapter bytes")
    collision = tmp_path / "Chapters/Chapter02/Chapter02.docx"
    collision.parent.mkdir(parents=True)
    collision.write_bytes(b"existing")
    layout = {
        "chapters": [
            {"working_filepath": "Prologue.docx", "tagged_filepath": "Prologue.tags.docx"},
            {"working_filepath": "Chapter02.docx", "tagged_filepath": "Chapter02.tags.docx"},
        ],
        "indexed_references": [{"filepath": "Chapter02.docx"}],
    }
    original = copy.deepcopy(layout)
    before = {path.relative_to(tmp_path).as_posix(): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}

    plan = dry_run_editorial_layout(tmp_path, layout)

    assert [(move["source"], move["destination"], move["blocked"]) for move in plan["moves"]] == [
        ("Chapter02.docx", "Chapters/Chapter02/Chapter02.docx", True),
        ("Prologue.docx", "Chapters/Prologue/Prologue.docx", False),
    ]
    chapter_move = plan["moves"][0]
    assert chapter_move["registered_reference_updates"] == [
        {"json_pointer": "/chapters/1/working_filepath", "from": "Chapter02.docx", "to": "Chapters/Chapter02/Chapter02.docx"},
        {"json_pointer": "/indexed_references/0/filepath", "from": "Chapter02.docx", "to": "Chapters/Chapter02/Chapter02.docx"},
    ]
    assert plan["collisions"] == [{"source": "Chapter02.docx", "destination": "Chapters/Chapter02/Chapter02.docx", "reason": "destination_exists"}]
    assert layout == original
    assert {path.relative_to(tmp_path).as_posix(): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()} == before
