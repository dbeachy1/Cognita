# DOCX content controls in retrieval

## Behavior

General retrieval parsing includes text inside Word content controls (`w:sdt`) by unwrapping each control to its `w:sdtContent` in the in-memory document XML before using the existing `python-docx` extraction. Block paragraphs and tables therefore retain the established heading, table, whitespace, and ordering behavior. Inline controls contribute their normal runs, including nested controls. Empty controls contribute no text, and control properties such as aliases are metadata rather than document content. The source DOCX bytes remain unchanged.

## Scope

This applies to the general retrieval DOCX extractor in `cognita.parsing`. It does not change the stricter speech-oriented parser in `cognita.books.docx`, nor does it add extraction from headers, footers, deleted text, drawing metadata, or other non-content XML.

## Regression acceptance

- A DOCX containing only a block content control parses successfully and returns its paragraph text.
- Block paragraphs, inline runs (including nested runs), and tables inside controls appear in extracted content with existing formatting.
- Empty controls and `w:sdtPr` metadata do not appear in extracted content.
- Parsing leaves the input DOCX bytes unchanged.
