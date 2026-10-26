"""保護・用語集・QA・翻訳メモリ・評価指標のテスト (API は呼ばない)."""

import pytest
from hypothesis import given
from hypothesis import strategies as st

from glossary_mt import protect, qa
from glossary_mt.evaluate import chrf
from glossary_mt.glossary import Glossary, Term, signature, violations
from glossary_mt.tm import TranslationMemory
from glossary_mt.translate import Item, batches

# --- protect ---------------------------------------------------------------


@pytest.mark.parametrize(
    "text, tokens",
    [
        ("`deploy.sh --env prod` を実行する", ["`deploy.sh --env prod`"]),
        (
            "詳細は https://wiki.example.com/dms/rollback を参照",
            ["https://wiki.example.com/dms/rollback"],
        ),
        # URL の直後の句読点・日本語は含めない
        ("https://example.com/a。次へ", ["https://example.com/a"]),
        # パスの直後の日本語は含めない
        (
            "ログ領域 /var/log を対象とし、/etc/api/config.yamlを修正",
            ["/var/log", "/etc/api/config.yaml"],
        ),
        ("[手順書](docs/runbook.md) を参照", ["(docs/runbook.md)"]),
        ("support@example.com までご連絡", ["support@example.com"]),
        (
            "{name} 様、${HOME} と {{count}} 件、%s と %05.2f",
            ["{name}", "${HOME}", "{{count}}", "%s", "%05.2f"],
        ),
        ("1 行目<br>2 行目", ["<br>"]),
        ("5 分以内に起票する", []),
        # 1 階層だけのスラッシュは日付や「A/B」と区別できないため保護しない
        ("2026/10/26 と 運用/保守", []),
    ],
)
def test_mask_patterns(text, tokens):
    assert list(protect.mask(text).tokens) == tokens


def test_unmask_allows_reordering():
    m = protect.mask("`a` を `b` に変更")
    assert protect.unmask("⟦2⟧로 ⟦1⟧을 변경", m.tokens) == "`b`로 `a`을 변경"


def test_unmask_detects_missing_and_duplicated_markers():
    m = protect.mask("`a` と `b`")
    with pytest.raises(protect.ProtectionError) as e:
        protect.unmask("⟦1⟧와 ⟦1⟧", m.tokens)
    assert e.value.missing == ["`b`"]
    assert e.value.extra == ["⟦1⟧"]
    with pytest.raises(protect.ProtectionError):
        protect.unmask("⟦1⟧와 ⟦2⟧와 ⟦3⟧", m.tokens)


@given(st.text(alphabet=st.characters(blacklist_characters="⟦⟧")))
def test_mask_roundtrip(text):
    m = protect.mask(text)
    assert protect.unmask(m.text, m.tokens) == text


# --- glossary --------------------------------------------------------------

GLOSSARY = Glossary(
    [
        Term("ジョブネット", "잡넷"),
        Term("ジョブ", "작업"),
        Term("本番環境", "운영 환경"),
        Term("障害", "장애"),
    ]
)


def test_longer_term_shadows_contained_term():
    assert [t.ja for t in GLOSSARY.matches("ジョブネットを再実行", "ja")] == ["ジョブネット"]
    assert {t.ja for t in GLOSSARY.matches("ジョブネットと個別のジョブ", "ja")} == {
        "ジョブネット",
        "ジョブ",
    }


def test_korean_spacing_is_ignored():
    assert [t.ja for t in GLOSSARY.matches("운영환경의 장애", "ko")] == ["本番環境", "障害"]
    terms = GLOSSARY.matches("本番環境の障害", "ja")
    assert violations("운영환경의 장애", terms, "ko") == []
    assert [t.ja for t in violations("프로덕션 환경의 장애", terms, "ko")] == ["本番環境"]


def test_duplicate_terms_are_rejected():
    with pytest.raises(ValueError):
        Glossary([Term("障害", "장애"), Term("故障", "장애")])


def test_signature_depends_only_on_terms():
    assert signature([]) == ""
    a = signature([Term("障害", "장애"), Term("監視", "모니터링")])
    assert a == signature([Term("監視", "모니터링"), Term("障害", "장애")])
    assert a != signature([Term("障害", "장애"), Term("監視", "감시")])


def test_load_sample_glossary():
    g = Glossary.load("data/glossary.csv")
    assert len(g) >= 20


# --- qa --------------------------------------------------------------------


def kinds(issues):
    return sorted((i.kind, i.severity) for i in issues)


def test_numbers_ignore_width_and_separators():
    assert qa.numbers("約１，２００件、95%") == qa.numbers("약 1200건, 95%")
    assert qa.numbers("一次切り分け", "ja") == qa.numbers("1차 원인 분석", "ko")
    assert qa.numbers("一次切り分け", "ja") == qa.numbers("일차 원인 분석", "ko")
    assert qa.numbers("一部の帳票", "ja") == qa.numbers("일부 리포트", "ko")  # 序数以外は変換しない


def test_check_number_and_glossary():
    terms = GLOSSARY.matches("障害は 30 分以内に報告", "ja")
    assert qa.check("障害は 30 分以内に報告", "장애는 30분 이내에 보고", "ja", "ko", terms) == []
    issues = qa.check("障害は 30 分以内に報告", "문제는 3분 이내에 보고", "ja", "ko", terms)
    assert kinds(issues) == [("glossary", "error"), ("number", "error"), ("number", "warning")]


@pytest.mark.parametrize(
    "text, errors",
    [
        ("`deploy.sh --version 2.4.0`를 실행한다", ["0`를 → 0`을"]),
        ("`deploy.sh --version 2.4.0`을 실행한다", []),
        ("판단 기한은 1:00으로 하고", []),
        ("1으로 설정", ["1으로 → 1로"]),  # ㄹ パッチムの後は「로」
        ("2을 입력, 5가 아닌 3이 맞다", ["2을 → 2를"]),
        ("90%로 낮추고, 2가지 방법", []),  # 記号の後や後続がハングルの場合は対象外
        ("이전 버전(v2.3.2)으로 되돌린다", []),  # 括弧の後は括弧の前の語 (버전) に合わせる
    ],
)
def test_particle_errors(text, errors):
    assert qa.particle_errors(text) == errors


def test_check_ignores_protected_parts():
    src = "ログ `/var/log/<日付>.log` と `v2.4.0` を確認"
    tokens = protect.mask(src).tokens
    tgt = "로그 `/var/log/<日付>.log`와 `v2.4.0`를 확인"
    assert kinds(qa.check(src, tgt, "ja", "ko", [], tokens)) == [("particle", "error")]
    assert kinds(qa.check(src, tgt, "ja", "ko", [])) == [
        ("particle", "error"),
        ("script", "warning"),
    ]


def test_check_leftover_script():
    assert kinds(qa.check("完了。", "완료。", "ja", "ko", [])) == [("script", "error")]
    # 日本語の中黒「・」(U+30FB) は韓国語の「·」(U+00B7) と見分けにくい
    issues = qa.check("運用チーム・お客様", "운영팀・고객사", "ja", "ko", [])
    assert [i.message for i in issues] == ["訳文に日本語の記号 (・) が残っています (中黒は「·」)"]
    assert qa.check("運用チーム・お客様", "운영팀·고객사", "ja", "ko", []) == []
    assert kinds(qa.check("確認する", "확인する", "ja", "ko", [])) == [("script", "error")]
    # 韓国人の人名などが残る場合があるため、日本語訳のハングルは警告
    assert kinds(qa.check("김민수 확인", "김민수確認", "ko", "ja", [])) == [("script", "warning")]


# --- tm / batching / chrF --------------------------------------------------


def test_translation_memory_key_includes_glossary():
    tm = TranslationMemory(":memory:")
    tm.put("ja", "ko", "障害", "sig1", "장애", "m")
    assert tm.get("ja", "ko", "障害", "sig1") == "장애"
    assert tm.get("ja", "ko", "障害", "sig2") is None
    assert tm.get("ko", "ja", "障害", "sig1") is None
    assert tm.stats() == [("ja", "ko", 1)]


def test_batches_respect_limits_and_order():
    items = [Item(f"u{i}", "あ" * 10) for i in range(7)]
    groups = batches(items, max_items=3, max_chars=25)
    assert [[i.id for i in g] for g in groups] == [["u0", "u1"], ["u2", "u3"], ["u4", "u5"], ["u6"]]
    assert len(batches([Item("u", "あ" * 100)], 3, 25)) == 1  # 上限を超える 1 件も送る


def test_chrf():
    assert chrf(["장애 보고"], ["장애 보고"]) == pytest.approx(100)
    assert chrf(["abc"], ["xyz"]) == 0
    assert 0 < chrf(["장애 보고서"], ["장애 보고"]) < 100
