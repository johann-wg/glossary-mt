"""Markdown.

行単位で解析し、見出し記号・リスト記号・表の区切りなどの構文はそのまま残して本文だけを訳す。
コードブロックと front matter は翻訳しない。改行コード・BOM も元のファイルに合わせる。
"""

from __future__ import annotations

import re
from pathlib import Path

from glossary_mt.formats.base import Segment

_FENCE = re.compile(r"^\s*(```|~~~)")
_TABLE_SEP = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?\s*$")
# 行頭の構文: 見出し / 引用 / 箇条書き (チェックボックス含む) / 番号付きリスト. 入れ子も可
_PREFIX = re.compile(r"^(\s*(?:#{1,6}\s+|>\s?|[-*+]\s+(?:\[[ xX]\]\s+)?|\d+[.)]\s+)*)")
_HEADING = re.compile(r"^\s*#{1,6}\s+")
_PIPE = re.compile(r"(?<!\\)\|")


class MarkdownDocument:
    def __init__(self, path: Path):
        raw = path.read_bytes()
        self._bom = raw.startswith(b"\xef\xbb\xbf")
        # newline="" 相当: CRLF の \r は各行の末尾に残し、保存時にそのまま戻す
        self._lines = raw.decode("utf-8-sig").split("\n")
        # id → (行番号, 開始, 終了, 表のセルか)
        self._spans: dict[str, tuple[int, int, int, bool]] = {}
        self._segments = self._parse()

    def _add(self, sid: str, line_no: int, start: int, end: int, cell: bool, ctx: str) -> Segment:
        self._spans[sid] = (line_no, start, end, cell)
        return Segment(sid, self._lines[line_no][start:end], ctx)

    def _parse(self) -> list[Segment]:
        segments: list[Segment] = []
        in_fence = False
        in_front_matter = bool(self._lines) and self._lines[0].strip() == "---"
        heading = ""
        for i, line in enumerate(self._lines):
            if in_front_matter:
                if i > 0 and line.strip() == "---":
                    in_front_matter = False
                continue
            if _FENCE.match(line):
                in_fence = not in_fence
                continue
            stripped = line.strip()
            if in_fence or not stripped or stripped.startswith("<!--") or _TABLE_SEP.match(line):
                continue

            if stripped.startswith("|"):
                pipes = [m.start() for m in _PIPE.finditer(line)]
                bounds = list(zip(pipes, pipes[1:] + [len(line.rstrip())], strict=True))
                for col, (a, b) in enumerate(bounds):
                    cell = line[a + 1 : b]
                    if not cell.strip():
                        continue
                    start = a + 1 + (len(cell) - len(cell.lstrip()))
                    end = a + 1 + len(cell.rstrip())
                    segments.append(self._add(f"L{i + 1}c{col + 1}", i, start, end, True, heading))
                continue

            start = _PREFIX.match(line).end()
            end = len(line.rstrip())
            if start < end:
                seg = self._add(f"L{i + 1}", i, start, end, False, heading)
                segments.append(seg)
                if _HEADING.match(line):
                    heading = seg.text
        return segments

    def segments(self) -> list[Segment]:
        return list(self._segments)

    def save(self, translations: dict[str, str], path: Path) -> None:
        lines = list(self._lines)
        by_line: dict[int, list[tuple[int, int, str]]] = {}
        for sid, text in translations.items():
            line_no, start, end, cell = self._spans[sid]
            text = " ".join(text.splitlines())
            if cell:
                text = _PIPE.sub(r"\\|", text)
            by_line.setdefault(line_no, []).append((start, end, text))
        for line_no, edits in by_line.items():
            line = lines[line_no]
            for start, end, text in sorted(edits, reverse=True):
                line = line[:start] + text + line[end:]
            lines[line_no] = line
        data = "\n".join(lines).encode("utf-8")
        path.write_bytes(b"\xef\xbb\xbf" + data if self._bom else data)
