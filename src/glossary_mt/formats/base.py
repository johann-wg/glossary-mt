from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class Segment:
    id: str  # 文書内の位置 (例: "L12", "L20c2", "課題一覧!C3", "s2:5")
    text: str
    context: str = ""  # 見出し・シート名・列名など. 翻訳の参考として LLM に渡す


class Document(Protocol):
    def segments(self) -> list[Segment]:
        """文字を含む翻訳候補のすべて (言語による絞り込みはパイプライン側で行う)."""
        ...

    def save(self, translations: dict[str, str], path: Path) -> None:
        """訳文 (Segment.id → 訳文) を書き戻して path に保存する. 含まれない id は原文のまま."""
        ...
