"""用語集の読み込み・照合.

用語集は ja, ko, note の 3 列の CSV。1 行が 1 組の対訳で、日→韓・韓→日の両方向に使う。
"""

from __future__ import annotations

import csv
import hashlib
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path


def compact(text: str) -> str:
    """照合用の正規化: NFKC (全角半角の統一)・英字の大小無視・空白の除去.

    韓国語は分かち書きの揺れ (「운영 환경」と「운영환경」) があるため空白を無視する。
    """
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text).casefold())


@dataclass(frozen=True)
class Term:
    ja: str
    ko: str
    note: str = ""

    def get(self, lang: str) -> str:
        return {"ja": self.ja, "ko": self.ko}[lang]


class Glossary:
    def __init__(self, terms: list[Term] | None = None):
        self.terms = list(terms or [])
        for lang in ("ja", "ko"):
            seen: dict[str, Term] = {}
            for t in self.terms:
                key = compact(t.get(lang))
                if key in seen:
                    raise ValueError(f"用語集の重複 ({lang}): {seen[key]} / {t}")
                seen[key] = t

    @classmethod
    def load(cls, path: str | Path) -> Glossary:
        with open(path, encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            if not reader.fieldnames or not {"ja", "ko"} <= set(reader.fieldnames):
                raise ValueError(f"{path}: ja, ko の列が必要です")
            terms = [
                Term(row["ja"].strip(), row["ko"].strip(), (row.get("note") or "").strip())
                for row in reader
                if row["ja"].strip() and row["ko"].strip()
            ]
        return cls(terms)

    def __len__(self) -> int:
        return len(self.terms)

    def matches(self, text: str, src: str) -> list[Term]:
        """text に現れる用語. 長い用語に含まれる短い用語は数えない.

        例: 用語集に「ジョブネット」と「ジョブ」があるとき、「ジョブネット」の中の「ジョブ」は
        「ジョブ」の出現として扱わない (訳語の指示が矛盾するため)。
        """
        norm = compact(text)
        spans: list[tuple[int, int]] = []
        found: list[Term] = []
        for term in sorted(self.terms, key=lambda t: len(compact(t.get(src))), reverse=True):
            key = compact(term.get(src))
            hit = False
            start = norm.find(key)
            while start != -1:
                end = start + len(key)
                if not any(s <= start and end <= e for s, e in spans):
                    spans.append((start, end))
                    hit = True
                start = norm.find(key, start + 1)
            if hit:
                found.append(term)
        return sorted(found, key=lambda t: t.get(src))


def violations(target: str, terms: list[Term], tgt: str) -> list[Term]:
    """訳文に指定の訳語が含まれていない用語."""
    norm = compact(target)
    return [t for t in terms if compact(t.get(tgt)) not in norm]


def signature(terms: list[Term]) -> str:
    """適用される用語の組を表すハッシュ. 翻訳メモリのキーに使う.

    用語集の該当部分が変わったときだけ、その文が再翻訳される。
    """
    if not terms:
        return ""
    joined = "|".join(sorted(f"{t.ja}={t.ko}" for t in terms))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:16]
