"""参照訳との比較による評価.

data/<split>/ には原文 (名前.ja.md など) と参照訳 (名前.ko.md など) を同じ構造で置く。
パイプラインの出力を参照訳と Segment の位置で突き合わせ、次を測定する。

- chrF: 文字 n-gram の F スコア (n=1..6, β=2, 空白は無視). 参照訳との近さ
- 用語集準拠率: 原文に用語集の語が現れた箇所のうち、指定の訳語が使われた割合
- 数値保持率: 数値を含む文のうち、原文の数値がすべて訳文に残った割合
- 保護部分保持率: コード・URL などを含む文のうち、保護部分がすべてそのまま残った割合

用語集準拠率・数値保持率・保護部分保持率は、パイプライン内部の判定ではなく
最終的な訳文から独立に計算する。
"""

from __future__ import annotations

import asyncio
import tempfile
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from glossary_mt import protect, qa
from glossary_mt.formats import SUFFIXES, open_document
from glossary_mt.glossary import Glossary, compact, violations
from glossary_mt.pipeline import DocumentReport, Pipeline, TranslatorLike


def _ngrams(text: str, n: int) -> Counter[str]:
    return Counter(text[i : i + n] for i in range(len(text) - n + 1))


def chrf(hypotheses: list[str], references: list[str], order: int = 6, beta: float = 2.0) -> float:
    """コーパス単位の chrF (0〜100). 各 n の一致数をコーパス全体で合計してから適合率・再現率を出す."""
    matches = [0] * order
    hyp_total = [0] * order
    ref_total = [0] * order
    for hyp, ref in zip(hypotheses, references, strict=True):
        h, r = compact(hyp), compact(ref)
        for n in range(1, order + 1):
            hc, rc = _ngrams(h, n), _ngrams(r, n)
            matches[n - 1] += sum((hc & rc).values())
            hyp_total[n - 1] += sum(hc.values())
            ref_total[n - 1] += sum(rc.values())
    precisions = [m / t for m, t in zip(matches, hyp_total, strict=True) if t]
    recalls = [m / t for m, t in zip(matches, ref_total, strict=True) if t]
    if not precisions or not recalls:
        return 0.0
    p, r = sum(precisions) / len(precisions), sum(recalls) / len(recalls)
    if p + r == 0:
        return 0.0
    return 100 * (1 + beta**2) * p * r / (beta**2 * p + r)


@dataclass
class Metrics:
    hyps: list[str] = field(default_factory=list)
    refs: list[str] = field(default_factory=list)
    terms_ok: int = 0
    terms_total: int = 0
    numbers_ok: int = 0
    numbers_total: int = 0
    protected_ok: int = 0
    protected_total: int = 0
    unresolved: int = 0  # QA の error が残った翻訳単位
    repaired: int = 0  # QA で誤りが見つかり、再翻訳で解消した翻訳単位
    units: int = 0
    source_chars: int = 0  # 翻訳対象の原文の文字数 (費用の換算用)
    failures: list[str] = field(default_factory=list)  # 用語・数値・保護部分の誤り (レポート用)

    @property
    def chrf(self) -> float:
        return chrf(self.hyps, self.refs)


def find_pairs(data_dir: str | Path) -> list[tuple[Path, Path, str]]:
    """(原文, 参照訳, 翻訳先の言語) の組.

    原文は data/<split>/SOURCES に 1 行 1 ファイルで列挙する (名前.ja.md と 名前.ko.md の
    どちらが原文かはファイル名だけでは決まらないため)。参照訳は言語コードを入れ替えた名前のファイル。
    """
    data_dir = Path(data_dir)
    pairs = []
    for name in (data_dir / "SOURCES").read_text(encoding="utf-8").split():
        path = data_dir / name
        stem, _, code = path.stem.rpartition(".")
        if path.suffix.lower() not in SUFFIXES or code not in ("ja", "ko"):
            raise ValueError(f"SOURCES: 名前.ja.md / 名前.ko.xlsx の形式にしてください: {name}")
        tgt = "ko" if code == "ja" else "ja"
        pairs.append((path, path.with_name(f"{stem}.{tgt}{path.suffix}"), tgt))
    return pairs


def score(report: DocumentReport, ref_path: Path, glossary: Glossary, m: Metrics) -> None:
    refs = {s.id: s.text for s in open_document(ref_path).segments()}
    src, tgt = report.src, report.tgt
    for u in report.units:
        m.units += 1
        if u.errors:
            m.unresolved += 1
        elif u.origin == "repair":
            m.repaired += 1
        target = u.target or ""
        for seg in u.segments:
            if seg.id not in refs:
                raise ValueError(f"{ref_path.name}: 参照訳に {seg.id} がありません")
            m.hyps.append(target)
            m.refs.append(refs[seg.id])
            m.source_chars += len(seg.text)

        weight = len(u.segments)
        terms = glossary.matches(u.text, src)
        bad_terms = violations(target, terms, tgt)
        m.terms_total += len(terms) * weight
        m.terms_ok += (len(terms) - len(bad_terms)) * weight
        for t in bad_terms:
            m.failures.append(f"用語 {t.get(src)}→{t.get(tgt)}: {target}")

        src_nums = qa.numbers(u.text, src)
        if src_nums:
            m.numbers_total += weight
            if not src_nums - qa.numbers(target, tgt):
                m.numbers_ok += weight
            else:
                m.failures.append(f"数値: {u.text} → {target}")

        tokens = protect.mask(u.text).tokens
        if tokens:
            m.protected_total += weight
            if all(target.count(tok) >= u.text.count(tok) for tok in set(tokens)):
                m.protected_ok += weight
            else:
                m.failures.append(f"保護部分: {u.text} → {target}")


async def _evaluate(data_dir: Path, pipeline: Pipeline, glossary: Glossary) -> Metrics:
    m = Metrics()
    try:
        with tempfile.TemporaryDirectory() as tmp:
            for src_path, ref_path, tgt in find_pairs(data_dir):
                out = Path(tmp) / f"out{src_path.suffix}"
                report = await pipeline.run(src_path, out, tgt)
                score(report, ref_path, glossary, m)
    finally:
        await pipeline.translator.aclose()
    return m


# 評価する構成. 用語集をプロンプトに入れる効果と、QA による再翻訳の効果を分けて測る
CONFIGS = {
    "baseline": {"glossary_in_prompt": False, "max_repairs": 0},
    "glossary": {"glossary_in_prompt": True, "max_repairs": 0},
    "full": {"glossary_in_prompt": True, "max_repairs": 1},
}
CONFIG_LABELS = {
    "baseline": "用語集なし",
    "glossary": "+ 用語集をプロンプトに指定",
    "full": "+ QA で検出した誤りを再翻訳",
}


def evaluate(
    data_dir: str | Path, translator: TranslatorLike, glossary: Glossary, config: str
) -> Metrics:
    # 翻訳メモリは使わない (前の構成の訳文が混ざらないようにする)
    pipeline = Pipeline(translator, glossary, tm=None, **CONFIGS[config])
    return asyncio.run(_evaluate(Path(data_dir), pipeline, glossary))


def _ratio(ok: int, total: int) -> str:
    return f"{ok}/{total} ({ok / total:.0%})" if total else "-"


def summarize(results: dict[str, Metrics]) -> str:
    lines = [
        "| 構成 | chrF | 用語集準拠率 | 数値保持率 | 保護部分保持率 | 要確認が残った文 |",
        "|---|---|---|---|---|---|",
    ]
    for config, m in results.items():
        lines.append(
            f"| {CONFIG_LABELS[config]} | {m.chrf:.1f} | {_ratio(m.terms_ok, m.terms_total)} | "
            f"{_ratio(m.numbers_ok, m.numbers_total)} | {_ratio(m.protected_ok, m.protected_total)} | "
            f"{m.unresolved}/{m.units} |"
        )
    return "\n".join(lines)
