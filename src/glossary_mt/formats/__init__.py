"""文書形式ごとのアダプター.

各アダプターは文書を翻訳単位 (Segment) に分解し、訳文を元の位置に書き戻す。
パイプラインは形式を意識せず Document プロトコルだけを使う。
"""

from __future__ import annotations

from pathlib import Path

from glossary_mt.formats.base import Document, Segment

SUFFIXES = {".md", ".markdown", ".xlsx", ".pptx"}


def open_document(path: str | Path) -> Document:
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix in (".md", ".markdown"):
        from glossary_mt.formats.markdown import MarkdownDocument

        return MarkdownDocument(path)
    if suffix == ".xlsx":
        from glossary_mt.formats.excel import ExcelDocument

        return ExcelDocument(path)
    if suffix == ".pptx":
        from glossary_mt.formats.powerpoint import PowerPointDocument

        return PowerPointDocument(path)
    raise ValueError(f"未対応の形式です: {path.name} (対応: {', '.join(sorted(SUFFIXES))})")


__all__ = ["SUFFIXES", "Document", "Segment", "open_document"]
