"""パイプラインのテスト. LLM の代わりに辞書で訳す偽の Translator を使う."""

import asyncio
from pathlib import Path

from glossary_mt.glossary import Glossary, Term, signature
from glossary_mt.pipeline import Pipeline, output_path
from glossary_mt.tm import TranslationMemory
from glossary_mt.translate import Item
from glossary_mt.watch import Watcher

GLOSSARY = Glossary([Term("障害", "장애"), Term("監視", "모니터링")])


class FakeTranslator:
    """1 回目は responses[0]、再翻訳では responses[1] … を返す."""

    model = "fake"

    def __init__(self, responses: dict[str, list[str | None]]):
        self.responses = responses
        self.calls: list[list[Item]] = []
        self._count: dict[str, int] = {}

    async def translate(self, items, src, tgt, doc_name=""):
        self.calls.append(items)
        out = {}
        for item in items:
            n = self._count.get(item.text, 0)
            self._count[item.text] = n + 1
            answers = self.responses[item.text]
            answer = answers[min(n, len(answers) - 1)]
            if answer is not None:
                out[item.id] = answer
        return out

    async def aclose(self):
        pass


def run(pipeline, path, out, tgt="ko"):
    return asyncio.run(pipeline.run(path, out, tgt))


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "doc.ja.md"
    path.write_text(text, encoding="utf-8")
    return path


def test_dedup_skip_and_tm_reuse(tmp_path: Path):
    path = write(tmp_path, "| 状態 | 状態 |\n|---|---|\n| 完了 | 完了 |\n| API | 김민수 |\n")
    translator = FakeTranslator({"状態": ["상태"], "完了": ["완료"]})
    tm = TranslationMemory(tmp_path / "tm.sqlite")
    report = run(Pipeline(translator, GLOSSARY, tm), path, tmp_path / "out.md")

    # 同じ原文は 1 回だけ依頼する. 英字のみ・ハングルのみのセルは対象外
    assert [i.text for i in translator.calls[0]] == ["状態", "完了"]
    assert report.translated_segments == 4 and len(report.units) == 2 and report.skipped == 2
    assert (tmp_path / "out.md").read_text(encoding="utf-8").splitlines()[2] == "| 완료 | 완료 |"

    # 2 回目は翻訳メモリから返り、API (Translator) は呼ばれない
    translator.calls.clear()
    report = run(Pipeline(translator, GLOSSARY, tm), path, tmp_path / "out.md")
    assert translator.calls == [] and report.count("tm") == 2


def test_repair_fixes_glossary_violation(tmp_path: Path):
    path = write(tmp_path, "障害を 5 分以内に報告する\n")
    translator = FakeTranslator(
        {"障害を 5 分以内に報告する": ["문제를 5분 이내에 보고한다", "장애를 5분 이내에 보고한다"]}
    )
    tm = TranslationMemory(":memory:")
    report = run(Pipeline(translator, GLOSSARY, tm), path, tmp_path / "out.md")

    unit = report.units[0]
    assert (
        unit.target == "장애를 5분 이내에 보고한다"
        and unit.origin == "repair"
        and unit.issues == []
    )
    # 再翻訳の依頼には前回の訳と問題点を付ける
    retry = translator.calls[1][0]
    assert retry.previous == "문제를 5분 이내에 보고한다"
    assert retry.problems == ["用語集: 「障害」は「장애」と訳す"]
    assert [t.ja for t in retry.terms] == ["障害"]
    sig = signature(unit.terms)
    assert tm.get("ja", "ko", "障害を 5 分以内に報告する", sig) == "장애를 5분 이내에 보고한다"


def test_unresolved_errors_are_reported_and_not_stored(tmp_path: Path):
    path = write(tmp_path, "`deploy.sh` を実行する\n")
    # 2 回ともマーカーを落とす
    translator = FakeTranslator({"⟦1⟧ を実行する": ["실행한다"]})
    tm = TranslationMemory(":memory:")
    report = run(Pipeline(translator, GLOSSARY, tm), path, tmp_path / "out.md")
    unit = report.units[0]
    assert [i.kind for i in unit.issues if i.severity == "error"] == ["placeholder"]
    assert report.with_errors == [unit] and unit.attempts == 2
    assert tm.stats() == []


def test_repair_keeps_better_previous_translation(tmp_path: Path):
    path = write(tmp_path, "監視の閾値は 80%\n")
    translator = FakeTranslator(
        {"監視の閾値は 80%": ["감시 임계치는 80%", "감시의 임계치는 8%"]}  # 再翻訳で数値まで崩れる
    )
    report = run(Pipeline(translator, GLOSSARY), path, tmp_path / "out.md")
    assert report.units[0].target == "감시 임계치는 80%"


def test_missing_translation_is_requested_again(tmp_path: Path):
    path = write(tmp_path, "確認する\n")
    translator = FakeTranslator({"確認する": [None, "확인한다"]})
    report = run(Pipeline(translator, GLOSSARY), path, tmp_path / "out.md")
    assert report.units[0].target == "확인한다"


def test_glossary_can_be_left_out_of_prompt(tmp_path: Path):
    path = write(tmp_path, "障害\n")
    translator = FakeTranslator({"障害": ["장애"]})
    run(Pipeline(translator, GLOSSARY, glossary_in_prompt=False), path, tmp_path / "out.md")
    assert translator.calls[0][0].terms == []


def test_output_path():
    assert output_path(Path("a/設計書.ja.md"), Path("out"), "ko") == Path("out/設計書.ko.md")
    assert output_path(Path("表.xlsx"), Path("out"), "ko") == Path("out/表.ko.xlsx")


def test_watcher_waits_until_file_is_stable(tmp_path: Path):
    out = tmp_path / "out"
    out.mkdir()
    (tmp_path / "old.md").write_text("既存", encoding="utf-8")
    w = Watcher(tmp_path, exclude=[out])
    w.prime()
    assert w.poll() == []  # 起動時点のファイルは対象外

    new = tmp_path / "new.md"
    new.write_text("新規", encoding="utf-8")
    (out / "ignored.md").write_text("出力", encoding="utf-8")
    (tmp_path / "~$lock.xlsx").write_text("", encoding="utf-8")
    assert w.poll() == []  # 1 回目の検出では保存中の可能性があるため待つ
    assert w.poll() == [new]
    assert w.poll() == []  # 処理済み
