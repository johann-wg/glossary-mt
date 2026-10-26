"""コマンドラインインターフェース."""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

from glossary_mt import report
from glossary_mt.glossary import Glossary
from glossary_mt.pipeline import DocumentReport, Pipeline, output_path
from glossary_mt.tm import TranslationMemory
from glossary_mt.translate import DEFAULT_MODEL, Translator, Usage


def _translator(args: argparse.Namespace) -> Translator:
    return Translator(
        model=args.model,
        effort=args.effort,
        cache_dir=args.cache_dir,
        offline=args.offline,
        concurrency=args.concurrency,
    )


def _glossary(args: argparse.Namespace) -> Glossary:
    return Glossary.load(args.glossary) if args.glossary else Glossary()


def _print_usage(usage: Usage) -> None:
    print(
        f"API 呼び出し {usage.calls} 回 / キャッシュ利用 {usage.cache_hits} 回 / "
        f"費用 約 ${usage.cost_usd:.4f} (キャッシュなしの場合 約 ${usage.estimated_cost_usd:.4f})",
        file=sys.stderr,
    )


async def _translate_files(
    pipeline: Pipeline, files: list[Path], out_dir: Path, tgt: str
) -> list[DocumentReport]:
    # 文書ごとの処理も並列に走らせる (API の同時実行数は Translator 側で制限される)
    try:
        return list(
            await asyncio.gather(
                *(pipeline.run(f, output_path(f, out_dir, tgt), tgt) for f in files)
            )
        )
    finally:
        await pipeline.translator.aclose()


def _write_reports(reports: list[DocumentReport], usage: Usage, qa_report: str | None) -> None:
    for r in reports:
        print(report.summary_line(r), file=sys.stderr)
    if qa_report:
        Path(qa_report).write_text(report.to_markdown(reports, usage), encoding="utf-8")
        print(f"saved: {qa_report}", file=sys.stderr)


def _cmd_translate(args: argparse.Namespace) -> int:
    translator = _translator(args)
    tm = None if args.no_tm else TranslationMemory(args.tm)
    pipeline = Pipeline(translator, _glossary(args), tm, max_repairs=args.max_repairs)
    out_dir = Path(args.out_dir)
    reports = asyncio.run(
        _translate_files(pipeline, [Path(f) for f in args.files], out_dir, args.to)
    )
    _write_reports(reports, translator.usage, args.qa_report or str(out_dir / "qa_report.md"))
    _print_usage(translator.usage)
    return 1 if args.strict and any(r.with_errors for r in reports) else 0


def _cmd_watch(args: argparse.Namespace) -> int:
    from glossary_mt.watch import Watcher

    translator = _translator(args)
    tm = TranslationMemory(args.tm)
    pipeline = Pipeline(translator, _glossary(args), tm, max_repairs=args.max_repairs)
    root, out_dir = Path(args.dir), Path(args.out_dir)
    watcher = Watcher(root.resolve(), exclude=[out_dir.resolve(), Path(args.tm).resolve().parent])
    watcher.prime()
    print(f"監視中: {watcher.root} (→ {out_dir}, Ctrl+C で終了)", file=sys.stderr)
    try:
        while True:
            changed = watcher.poll()
            if changed:
                print("変更を検出: " + ", ".join(p.name for p in changed), file=sys.stderr)
                reports = asyncio.run(_translate_files(pipeline, changed, out_dir, args.to))
                _write_reports(reports, translator.usage, str(out_dir / "qa_report.md"))
                _print_usage(translator.usage)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        return 0


def _cmd_eval(args: argparse.Namespace) -> int:
    from glossary_mt.evaluate import CONFIGS, evaluate, summarize

    translator = _translator(args)
    glossary = _glossary(args)
    configs = list(CONFIGS) if args.config == "all" else [args.config]
    results = {c: evaluate(args.data, translator, glossary, c) for c in configs}
    table = summarize(results)
    print(table)
    if args.verbose:
        for config, m in results.items():
            for failure in m.failures:
                print(f"  [{config}] {failure}")
    if args.report:
        Path(args.report).write_text(table + "\n", encoding="utf-8")
    _print_usage(translator.usage)
    return 0


def _cmd_tm(args: argparse.Namespace) -> int:
    tm = TranslationMemory(args.tm)
    rows = tm.stats()
    if not rows:
        print("翻訳メモリは空です")
    for src, tgt, n in rows:
        print(f"{src} → {tgt}: {n} 件")
    return 0


def _add_llm_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--glossary", help="用語集の CSV (列: ja, ko, note)")
    p.add_argument("--model", default=DEFAULT_MODEL, help="Claude のモデル ID")
    p.add_argument("--effort", default="medium", choices=["low", "medium", "high", "xhigh", "max"])
    p.add_argument("--concurrency", type=int, default=4, help="API の同時リクエスト数")
    p.add_argument("--cache-dir", default="llm-cache", help="API 応答のキャッシュ先")
    p.add_argument(
        "--offline", action="store_true", help="API を呼ばずキャッシュのみ使う (CI・再現用)"
    )


def _add_output_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--to", required=True, choices=["ja", "ko"], help="翻訳先の言語")
    p.add_argument("--out-dir", default="out", help="訳文の出力先")
    p.add_argument("--tm", default=".glossary-mt/tm.sqlite", help="翻訳メモリのファイル")
    p.add_argument(
        "--max-repairs", type=int, default=1, help="QA で誤りが見つかった文を再翻訳する回数"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="glossary-mt",
        description="用語集を守る日韓の技術文書翻訳 (Markdown / Excel / PowerPoint)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("translate", help="文書を翻訳する")
    p.add_argument("files", nargs="+")
    _add_output_args(p)
    _add_llm_args(p)
    p.add_argument("--no-tm", action="store_true", help="翻訳メモリを使わない")
    p.add_argument("--qa-report", help="QA レポートの出力先 (既定: <out-dir>/qa_report.md)")
    p.add_argument("--strict", action="store_true", help="要確認の文が残ったら終了コード 1 (CI 用)")
    p.set_defaults(func=_cmd_translate)

    p = sub.add_parser("watch", help="フォルダを監視し、保存された文書を自動で翻訳する")
    p.add_argument("dir")
    _add_output_args(p)
    _add_llm_args(p)
    p.add_argument("--interval", type=float, default=2.0, help="確認の間隔 (秒)")
    p.set_defaults(func=_cmd_watch)

    p = sub.add_parser("eval", help="参照訳と比較して評価する")
    p.add_argument("data", help="評価データのフォルダ (SOURCES と原文・参照訳)")
    _add_llm_args(p)
    p.add_argument("--config", default="all", choices=["all", "baseline", "glossary", "full"])
    p.add_argument("--report", help="結果の表を Markdown で保存する")
    p.add_argument("-v", "--verbose", action="store_true", help="用語・数値・保護部分の誤りを表示")
    p.set_defaults(func=_cmd_eval)

    p = sub.add_parser("tm", help="翻訳メモリの件数を表示する")
    p.add_argument("--tm", default=".glossary-mt/tm.sqlite")
    p.set_defaults(func=_cmd_tm)
    return parser


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
