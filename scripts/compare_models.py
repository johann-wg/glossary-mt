"""モデル・構成ごとの翻訳品質と費用を比較し、Markdown にまとめる.

キャッシュ済みの応答だけを使うため、API を呼ばず費用もかからない。

    python scripts/compare_models.py > docs/model-comparison.md
"""

from __future__ import annotations

import sys

from glossary_mt.evaluate import CONFIG_LABELS, CONFIGS, Metrics, evaluate
from glossary_mt.glossary import Glossary
from glossary_mt.translate import Translator

SETS = [("data/test", "評価専用（test・3 文書）"), ("data/dev", "開発用（dev・3 文書）")]
MODELS = ["claude-haiku-5-5", "claude-sonnet-5-5", "claude-opus-5-5"]


def _ratio(ok: int, total: int) -> str:
    return f"{ok}/{total}" if total else "-"


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    glossary = Glossary.load("data/glossary.csv")
    print("# モデル比較\n")
    print(
        "`scripts/compare_models.py` で生成。各モデル 1 回ずつの実行結果。effort はすべて `medium`。"
    )
    print(
        "費用はキャッシュした実際の API 使用量から算出した、原文 1 万文字あたりの推定額（USD、"
        "「+ QA で検出した誤りを再翻訳」の構成）。\n"
    )
    for directory, title in SETS:
        results: dict[str, dict[str, Metrics]] = {}
        costs: dict[str, float] = {}
        for model in MODELS:
            try:
                per_config = {}
                for config in CONFIGS:
                    translator = Translator(model=model, offline=True)
                    per_config[config] = evaluate(directory, translator, glossary, config)
                    if config == "full":
                        m = per_config[config]
                        costs[model] = translator.usage.estimated_cost_usd / m.source_chars * 10000
            except LookupError:
                continue  # この組み合わせは未測定
            results[model] = per_config
        if not results:
            continue

        print(f"## {title}\n")
        print(
            "| モデル | chrF | 用語集準拠率 | 数値保持率 | 保護部分保持率 | 再翻訳で解消 | 要確認が残った文 | 1 万文字あたり費用 |"
        )
        print("|---|---|---|---|---|---|---|---|")
        for model, per_config in results.items():
            m = per_config["full"]
            print(
                f"| {model} | {m.chrf:.1f} | {_ratio(m.terms_ok, m.terms_total)} | "
                f"{_ratio(m.numbers_ok, m.numbers_total)} | {_ratio(m.protected_ok, m.protected_total)} | "
                f"{m.repaired} | {m.unresolved}/{m.units} | ${costs[model]:.3f} |"
            )
        print("\n### 構成ごとの効果（chrF / 用語集準拠率）\n")
        print("| モデル | " + " | ".join(CONFIG_LABELS[c] for c in CONFIGS) + " |")
        print("|---|" + "---|" * len(CONFIGS))
        for model, per_config in results.items():
            cells = [
                f"{m.chrf:.1f} / {m.terms_ok / m.terms_total:.0%}" for m in per_config.values()
            ]
            print(f"| {model} | " + " | ".join(cells) + " |")
        print()


if __name__ == "__main__":
    main()
