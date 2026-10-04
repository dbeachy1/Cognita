"""Text chunking for the 4.0 retrieval core (DESIGN-4.0-vector-engine.md D4.3).

Faithful port of the 3.x engine's chunkers (mcp_server/ingestion.py) so 4.0
indexes the same corpus into near-identical chunks — the retrieval-quality
baseline Doug eyeballs in M2 assumes the chunk shapes didn't move under it.

Two strategies, as in 3.x:
- Markdown: split on ##/### section headers (never # — code comments), with
  fenced code blocks masked during the split, sections under 100 chars merged
  forward, and oversized sections sub-chunked with the header re-prefixed.
- Plain text: fixed-size windows (chunk_size) with overlap, breaking at the
  best boundary (paragraph > line > sentence > word) in the last 20%.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass

DEFAULT_CHUNK_SIZE = 1000  # chars, matching the 3.x engine defaults
DEFAULT_CHUNK_OVERLAP = 200
MIN_MARKDOWN_SECTION = 100  # sections shorter than this merge into the next


@dataclass(slots=True)
class TextChunk:
    """One chunk of document text, pre-embedding."""

    index: int
    content: str
    section: str | None = None  # the ##/### header this chunk falls under


def chunk_text(
    text: str,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    chunk_overlap: int = DEFAULT_CHUNK_OVERLAP,
) -> list[TextChunk]:
    """Overlapping fixed-size chunks with natural break points."""
    if not text:
        return []
    chunks: list[TextChunk] = []
    text_len = len(text)
    start = 0
    index = 0
    previous_start = -1
    while start < text_len:
        if start <= previous_start:  # safety: never loop in place
            break
        previous_start = start
        end = min(start + chunk_size, text_len)
        if end < text_len:
            # Prefer a natural break within the last 20% of the window.
            break_zone_start = start + int(chunk_size * 0.8)
            break_zone = text[break_zone_start:end]
            for pattern in ("\n\n", "\n", ". ", " "):
                last_break = break_zone.rfind(pattern)
                if last_break != -1:
                    end = break_zone_start + last_break + len(pattern)
                    break
        content = text[start:end].strip()
        if content:
            chunks.append(TextChunk(index=index, content=content))
            index += 1
        if end >= text_len:
            # The window reached the end, so there is nothing left to cover.
            # Without this, the overlap step below rewound to text_len -
            # chunk_overlap and emitted ONE more chunk holding the last 200
            # characters — text already wholly inside its predecessor. Every
            # multi-chunk document therefore indexed and embedded its own tail
            # twice: doubled retrieval odds for that passage, two search results
            # showing the same text, and ~7% wasted chunks and embedding cost on
            # a 14-chunk document.
            break
        new_start = end - chunk_overlap
        start = end if new_start <= start else new_start
    return chunks


def chunk_markdown(
    text: str,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    chunk_overlap: int = DEFAULT_CHUNK_OVERLAP,
) -> list[TextChunk]:
    """Markdown-aware chunking aligned to ##/### sections."""
    if not text:
        return []

    # Mask fenced code blocks so a "# comment" inside one can't split a section.
    # The placeholder uses a private-use codepoint plus a per-call nonce, because
    # a plain "__CODE_BLOCK_0__" is text a document can legitimately CONTAIN —
    # and restore_code's str.replace would then swap that prose for an unrelated
    # code block from elsewhere in the file, putting text in the index that
    # appears nowhere in the document. Index corruption, not a formatting wobble.
    code_blocks: list[str] = []
    nonce = uuid.uuid4().hex

    def _placeholder(i: int) -> str:
        return f"CODE{nonce}_{i}"

    def mask_code(match: re.Match) -> str:
        code_blocks.append(match.group(0))
        return _placeholder(len(code_blocks) - 1)

    masked = re.sub(r"```.*?```", mask_code, text, flags=re.DOTALL)
    sections = [s for s in re.split(r"(?=^#{2,3}\s+)", masked, flags=re.MULTILINE) if s.strip()]
    if len(sections) <= 1:
        return chunk_text(text, chunk_size, chunk_overlap)

    def restore_code(section: str) -> str:
        for i, block in enumerate(code_blocks):
            section = section.replace(_placeholder(i), block)
        return section

    sections = [restore_code(s) for s in sections]

    # Merge undersized sections forward so no chunk is a lone header.
    merged: list[str] = []
    buffer = ""
    for section in sections:
        if buffer:
            buffer += "\n\n" + section
            if len(buffer.strip()) >= MIN_MARKDOWN_SECTION:
                merged.append(buffer)
                buffer = ""
        elif len(section.strip()) < MIN_MARKDOWN_SECTION:
            buffer = section
        else:
            merged.append(section)
    if buffer:
        if merged:
            merged[-1] += "\n\n" + buffer
        else:
            merged.append(buffer)
    if not merged:
        return chunk_text(text, chunk_size, chunk_overlap)

    chunks: list[TextChunk] = []
    index = 0
    for section in merged:
        section = section.strip()
        if not section:
            continue
        header_match = re.match(r"^(#{2,3}\s+.+)$", section, re.MULTILINE)
        header = header_match.group(1) if header_match else None
        if len(section) <= chunk_size:
            chunks.append(TextChunk(index=index, content=section, section=header))
            index += 1
        else:
            # Oversized section: window it, re-prefixing the header from the
            # second sub-chunk on so every chunk keeps its section context.
            for i, sub in enumerate(chunk_text(section, chunk_size, chunk_overlap)):
                content = sub.content if i == 0 or not header else f"{header}\n\n{sub.content}"
                chunks.append(TextChunk(index=index, content=content, section=header))
                index += 1
    return chunks
