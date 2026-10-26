"""文書 1 件の翻訳パイプライン.

文書 ─> 分解 (Segment) ─> 重複排除 ─> 翻訳メモリ ─┬─> 書き戻し
                                      │ ヒットなし   │
                                      └─> LLM ─> QA ─┤ error → 問題点を伝えて再翻訳
                                                     └─> 翻訳メモリに登録 (QA 通過分のみ)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from glossary_mt import glossary as gl
from glossary_mt import lang, protect, qa
from glossary_mt.formats import Segment, open_document
from glossary_mt.tm import TranslationMemory
from glossary_mt.translate import Item


class TranslatorLike(Protocol):
    model: str

    async def translate(
        self, items: list[Item], src: str, tgt: str, doc_name: str = ""
    ) -> dict[str, str]: ...

    async def aclose(self) -> None: ...


@dataclass
class Unit:
    """同じ原文の Segment をまとめた翻訳単位 (Excel の「対応中」などは 1 回だけ訳す)."""

    text: str
    masked: protect.Masked
    terms: list[gl.Term]
    segments: list[Segment] = field(default_factory=list)
    target: str | None = None
    raw: str | None = None  # LLM の出力 (マーカーを含む). 再翻訳時に previous として渡す
    origin: str = ""  # tm / llm / repair
    attempts: int = 0
    issues: list[qa.Issue] = field(default_factory=list)

    @property
    def errors(self) -> int:
        return sum(1 for i in self.issues if i.severity == "error")


@dataclass
class DocumentReport:
    source: Path
    output: Path
    src: str
    tgt: str
    units: list[Unit]
    total_segments: int  # 文書内の全 Segment
    skipped: int  # 翻訳元の言語を含まないため対象外 (英数字のみ・保護部分のみ・韓国人の人名など)

    @property
    def translated_segments(self) -> int:
        return sum(len(u.segments) for u in self.units)

    def count(self, origin: str) -> int:
        return sum(1 for u in self.units if u.origin == origin)

    @property
    def with_errors(self) -> list[Unit]:
        return [u for u in self.units if u.errors]

    @property
    def with_warnings(self) -> list[Unit]:
        return [u for u in self.units if not u.errors and u.issues]


def output_path(path: Path, out_dir: Path, tgt: str) -> Path:
    """設計書.md → out/設計書.ko.md, 設計書.ja.md → out/設計書.ko.md."""
    stem = path.stem
    for code in ("ja", "ko"):
        if stem.endswith(f".{code}"):
            stem = stem[: -len(code) - 1]
    return out_dir / f"{stem}.{tgt}{path.suffix}"


class Pipeline:
    def __init__(
        self,
        translator: TranslatorLike,
        glossary: gl.Glossary | None = None,
        tm: TranslationMemory | None = None,
        *,
        glossary_in_prompt: bool = True,
        max_repairs: int = 1,
    ):
        self.translator = translator
        self.glossary = glossary or gl.Glossary()
        self.tm = tm
        self.glossary_in_prompt = glossary_in_prompt
        self.max_repairs = max_repairs

    def _units(self, segments: list[Segment], src: str) -> tuple[list[Unit], int]:
        units: dict[str, Unit] = {}
        skipped = 0
        for seg in segments:
            masked = protect.mask(seg.text)
            if not lang.has_script(masked.text, src):
                skipped += 1
                continue
            unit = units.get(seg.text)
            if unit is None:
                unit = Unit(seg.text, masked, self.glossary.matches(seg.text, src))
                units[seg.text] = unit
            unit.segments.append(seg)
        return list(units.values()), skipped

    async def _translate(
        self, units: list[Unit], src: str, tgt: str, doc_name: str, repair: bool
    ) -> None:
        items = []
        for n, u in enumerate(units):
            item = Item(
                id=f"u{n + 1}",
                text=u.masked.text,
                context=u.segments[0].context,
                terms=u.terms if self.glossary_in_prompt else [],
                protected=u.masked.tokens,
            )
            if repair:
                item.previous = u.raw
                item.problems = [i.message for i in u.issues if i.severity == "error"]
            items.append(item)

        out = await self.translator.translate(items, src, tgt, doc_name)
        for item, u in zip(items, units, strict=True):
            raw = out.get(item.id)
            if raw is None:
                candidate, issues = None, [qa.Issue("missing", "訳文が返されませんでした")]
            else:
                try:
                    candidate = protect.unmask(raw, u.masked.tokens)
                    issues = []
                except protect.ProtectionError as e:
                    candidate = protect.restore_partial(raw, u.masked.tokens)
                    issues = [qa.Issue("placeholder", f"保護部分が崩れています ({e})")]
                issues += qa.check(u.text, candidate, src, tgt, u.terms, u.masked.tokens)

            new_errors = sum(1 for i in issues if i.severity == "error")
            u.attempts += 1
            # 再翻訳で悪化した場合は前回の訳を残す
            if u.target is None or new_errors < u.errors:
                u.target, u.raw, u.issues = candidate, raw, issues
                u.origin = "repair" if repair else "llm"

    async def run(self, path: str | Path, out_path: str | Path, tgt: str) -> DocumentReport:
        path, out_path = Path(path), Path(out_path)
        src = lang.other(tgt)
        doc = open_document(path)
        segments = doc.segments()
        units, skipped = self._units(segments, src)

        pending: list[Unit] = []
        for u in units:
            hit = None
            if self.tm is not None:
                hit = self.tm.get(src, tgt, u.text, gl.signature(u.terms))
            if hit is not None:
                u.target, u.origin = hit, "tm"
            else:
                pending.append(u)

        if pending:
            await self._translate(pending, src, tgt, path.name, repair=False)
            for _ in range(self.max_repairs):
                failed = [u for u in pending if u.errors and u.raw is not None]
                if not failed:
                    break
                await self._translate(failed, src, tgt, path.name, repair=True)
            # 一度も訳文が返らなかったものは、再翻訳の対象外になるため最後にもう一度だけ依頼する
            missing = [u for u in pending if u.target is None]
            if missing:
                await self._translate(missing, src, tgt, path.name, repair=False)

        if self.tm is not None:
            for u in pending:
                if u.target is not None and not u.errors:
                    self.tm.put(
                        src, tgt, u.text, gl.signature(u.terms), u.target, self.translator.model
                    )

        translations = {
            seg.id: u.target for u in units if u.target is not None for seg in u.segments
        }
        out_path.parent.mkdir(parents=True, exist_ok=True)
        doc.save(translations, out_path)
        return DocumentReport(path, out_path, src, tgt, units, len(segments), skipped)
