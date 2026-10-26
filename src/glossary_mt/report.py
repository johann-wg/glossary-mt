"""QA レポート (Markdown)."""

from __future__ import annotations

from glossary_mt.pipeline import DocumentReport, Unit
from glossary_mt.translate import Usage


def _cell(text: str | None) -> str:
    if text is None:
        return "(なし)"
    return text.replace("|", "\\|").replace("\n", "<br>")


def summary_line(r: DocumentReport) -> str:
    return (
        f"{r.source.name} → {r.output.name}: 翻訳対象 {r.translated_segments} 件"
        f" (重複を除き {len(r.units)} 件: 翻訳メモリ {r.count('tm')} / 新規翻訳 {r.count('llm') + r.count('repair')}"
        f", うち再翻訳で修正 {r.count('repair')}) / 要確認 {len(r.with_errors)} / 警告 {len(r.with_warnings)}"
    )


def _rows(units: list[Unit]) -> list[str]:
    rows = ["| 位置 | 原文 | 訳文 | 問題 |", "|---|---|---|---|"]
    for u in units:
        where = ", ".join(s.id for s in u.segments[:3]) + (" ほか" if len(u.segments) > 3 else "")
        problems = "<br>".join(f"[{i.kind}] {i.message}" for i in u.issues)
        rows.append(f"| {where} | {_cell(u.text)} | {_cell(u.target)} | {_cell(problems)} |")
    return rows


def to_markdown(reports: list[DocumentReport], usage: Usage | None = None) -> str:
    lines = [
        "# 翻訳 QA レポート",
        "",
        "| 文書 | 翻訳対象 | 翻訳メモリ | 新規翻訳 | 再翻訳で修正 | 要確認 | 警告 |",
    ]
    lines.append("|---|---|---|---|---|---|---|")
    for r in reports:
        lines.append(
            f"| {r.source.name} | {r.translated_segments} | {r.count('tm')} | "
            f"{r.count('llm') + r.count('repair')} | {r.count('repair')} | {len(r.with_errors)} | {len(r.with_warnings)} |"
        )
    if usage is not None:
        lines += [
            "",
            f"API 呼び出し {usage.calls} 回 / キャッシュ利用 {usage.cache_hits} 回 / "
            f"費用 約 ${usage.cost_usd:.4f}",
        ]
    for r in reports:
        if not r.with_errors and not r.with_warnings:
            continue
        lines += ["", f"## {r.source.name}"]
        if r.with_errors:
            lines += [
                "",
                "### 要確認 (自動の再翻訳でも解消しなかった問題)",
                "",
                *_rows(r.with_errors),
            ]
        if r.with_warnings:
            lines += ["", "### 警告", "", *_rows(r.with_warnings)]
    return "\n".join(lines) + "\n"
