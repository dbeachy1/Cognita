"""Unit tests for cognita.chunking — the 3.x chunker semantics must carry over."""

from cognita.chunking import chunk_markdown, chunk_text


def test_short_text_is_one_chunk():
    chunks = chunk_text("hello world")
    assert len(chunks) == 1
    assert chunks[0].index == 0
    assert chunks[0].content == "hello world"


def test_empty_text_yields_no_chunks():
    assert chunk_text("") == []
    assert chunk_markdown("") == []


def test_long_text_chunks_with_overlap():
    # 100 paragraphs, far beyond one chunk
    text = "\n\n".join(f"Paragraph {i} " + "word " * 30 for i in range(100))
    chunks = chunk_text(text, chunk_size=1000, chunk_overlap=200)
    assert len(chunks) > 5
    assert [c.index for c in chunks] == list(range(len(chunks)))
    # Every chunk respects the size budget (post-strip)
    assert all(len(c.content) <= 1000 for c in chunks)
    # Overlap: consecutive chunks share text
    assert chunks[0].content[-50:].split()[0] in chunks[1].content


def test_chunk_text_prefers_paragraph_breaks():
    # A paragraph break inside the last 20% of the window should win.
    text = "a" * 850 + "\n\n" + "b" * 500
    chunks = chunk_text(text, chunk_size=1000, chunk_overlap=100)
    assert chunks[0].content == "a" * 850


def test_markdown_splits_on_sections():
    text = (
        "# Title\n\nintro text here that is long enough to stand alone as a chunk body\n\n"
        "## Install\n\n" + "install instructions " * 10 + "\n\n"
        "## Configure\n\n" + "configuration details " * 10 + "\n\n"
        "### Advanced\n\n" + "advanced settings " * 10
    )
    chunks = chunk_markdown(text)
    sections = [c.section for c in chunks]
    # 3.x parity: the title+intro (<100 chars) merges INTO the Install section,
    # and a merged section starting with "# Title" gets no section label —
    # labels only attach when a section literally starts with ##/###.
    assert any("## Install" in c.content for c in chunks)
    assert any(s and s.startswith("## Configure") for s in sections)
    assert any(s and s.startswith("### Advanced") for s in sections)
    assert [c.index for c in chunks] == list(range(len(chunks)))


def test_markdown_code_blocks_do_not_split_sections():
    text = (
        "## First Section\n\n"
        + "first section body text " * 10
        + "\n\n## Code Section\n\n"
        + "before code " * 10
        + "\n\n```bash\n## not a header\n### also not a header\necho hi\n```\n\n"
        + "after code " * 10
    )
    chunks = chunk_markdown(text)
    # The fake headers inside the fence must not create new sections — only
    # the two real ones exist...
    assert {c.section for c in chunks} == {"## First Section", "## Code Section"}
    # ...and the code block survives intact, unsplit, in its section's chunk
    code_chunk = next(c for c in chunks if c.section == "## Code Section")
    assert "## not a header\n### also not a header\necho hi" in code_chunk.content


def test_markdown_small_sections_merge():
    text = "## A\n\ntiny\n\n## B\n\n" + "large enough section body " * 20
    chunks = chunk_markdown(text)
    # "## A" alone is under the 100-char minimum, so it merges with "## B"
    assert len(chunks) == 1
    assert "## A" in chunks[0].content and "## B" in chunks[0].content


def test_markdown_oversized_section_subchunks_with_header_prefix():
    # Two sections so the section-splitting path engages (a single-section doc
    # falls back to plain text chunking — 3.x parity, covered below)
    text = (
        "## Small Section\n\n" + "small section body text " * 10 + "\n\n"
        "## Big Section\n\n" + "filler sentence goes here. " * 200
    )
    chunks = chunk_markdown(text, chunk_size=1000, chunk_overlap=100)
    big = [c for c in chunks if c.section == "## Big Section"]
    assert len(big) > 1
    # Sub-chunks after the first carry the header re-prefixed for context
    assert not big[0].content.startswith("## Big Section\n\n## Big Section")
    assert all(c.content.startswith("## Big Section") for c in big[1:])


def test_markdown_single_section_falls_back_to_text_chunking():
    # 3.x parity: one lone ##-section means "no structure worth splitting on"
    text = "## Only Section\n\n" + "body text " * 200
    chunks = chunk_markdown(text, chunk_size=500, chunk_overlap=50)
    assert len(chunks) > 1
    assert all(c.section is None for c in chunks)


def test_markdown_without_headers_falls_back_to_text_chunking():
    text = "no headers here " * 100
    chunks = chunk_markdown(text, chunk_size=500, chunk_overlap=50)
    assert len(chunks) > 1
    assert all(c.section is None for c in chunks)


# ------------------------------------- 5.1: the duplicated tail chunk


def test_no_chunk_is_wholly_contained_in_its_predecessor():
    """Every multi-chunk document used to end with a redundant chunk.

    After the final real window reached the end of the text, the overlap step
    still rewound to text_len - chunk_overlap and emitted one more chunk — the
    last 200 characters, already wholly inside the chunk before it. So the tail
    of every document was embedded and indexed TWICE: doubled retrieval odds for
    that one passage, two search results that are the same text, and ~7% wasted
    chunks and embedding spend on a 14-chunk document.

    Distinct tokens, so "contained in" means what it says — a fixture of
    repeated characters makes every chunk a substring of every other.
    """
    text = " ".join(f"w{i:05d}" for i in range(3000))
    chunks = chunk_text(text)
    assert len(chunks) > 2
    for i in range(1, len(chunks)):
        assert chunks[i].content not in chunks[i - 1].content, f"chunk {i} is redundant"


def test_chunking_still_covers_every_token():
    """The fix removes a chunk, so prove it removed only a REDUNDANT one."""
    words = [f"w{i:05d}" for i in range(3000)]
    chunks = chunk_text(" ".join(words))
    seen = {w for c in chunks for w in c.content.split()}
    assert not [w for w in words if w not in seen]
    assert [c.index for c in chunks] == list(range(len(chunks)))


def test_prose_containing_the_code_placeholder_is_not_substituted():
    """A document whose PROSE contained the old literal placeholder had that
    text swapped for an unrelated code block from elsewhere in the file, so the
    indexed copy held text appearing nowhere in the document. Narrow trigger,
    but it is index corruption rather than a formatting wobble."""
    doc = (
        "## A\n\nsome prose __CODE_BLOCK_0__ more prose\n\n"
        "## B\n\n```\ncode here\n```\n"
    )
    body = "".join(c.content for c in chunk_markdown(doc))
    assert "some prose __CODE_BLOCK_0__ more prose" in body
    assert "some prose ```" not in body
    assert "code here" in body  # the REAL fence still round-trips
    assert "\ue000" not in body  # and the sentinel never leaks into the index
