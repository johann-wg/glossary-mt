"""訳文の機械的な品質チェック.

LLM の自己申告ではなく、コードで確認できることだけを確認する。
severity が error の問題は、問題点を伝えて自動で再翻訳させる。
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter
from dataclasses import dataclass

from glossary_mt import lang
from glossary_mt.glossary import Term, compact, violations


@dataclass(frozen=True)
class Issue:
    kind: str  # glossary / number / placeholder / script / length / missing
    message: str
    severity: str = "error"  # error: 自動再翻訳の対象, warning: 報告のみ


_NUMBER = re.compile(r"\d+(?:\.\d+)?")
_THOUSANDS = re.compile(r"(?<=\d),(?=\d{3}(?!\d))")
# 序数は「一次」「1次」「1차」「일차」のどの表記にも訳されるため、算用数字にそろえてから比べる
_ORDINALS = {
    "ja": (re.compile(r"([一二三四五六七八九])(?=次|回目|段階|号機)"), "一二三四五六七八九"),
    "ko": (re.compile(r"(?<![가-힣])([일이삼사오육칠팔구])(?=차)"), "일이삼사오육칠팔구"),
}

# 韓国語の助詞は直前の語の最後の音にパッチム (終声) があるかで形が変わる.
# 数字で終わる語は読みが一意に決まるので機械的に確認できる
# (0 영・1 일・3 삼・6 육・7 칠・8 팔 はパッチムあり. 10 십・100 백・1000 천・10000 만 も同じ)
_HAS_BATCHIM = set("013678")
_PARTICLES = {  # パッチムあり, なし
    "을": ("을", "를"),
    "를": ("을", "를"),
    "이": ("이", "가"),
    "가": ("이", "가"),
    "은": ("은", "는"),
    "는": ("은", "는"),
    "과": ("과", "와"),
    "와": ("과", "와"),
    "으로": ("으로", "로"),
    "로": ("으로", "로"),
}
# 括弧の後の助詞は括弧の前の語に合わせるため対象外 (「버전(v2.3.2)으로」は正しい)
_DIGIT_PARTICLE = re.compile(r"(\d)[`'\"]?(으로|로|을|를|이|가|은|는|과|와)(?![가-힣])")


def particle_errors(text: str) -> list[str]:
    """数字の直後の助詞の誤り (例: 「2.4.0를」→「2.4.0을」)."""
    errors = []
    for m in _DIGIT_PARTICLE.finditer(text):
        digit, particle = m.group(1), m.group(2)
        with_batchim, without = _PARTICLES[particle]
        if digit == "1" and particle in ("으로", "로"):
            expected = "로"  # ㄹ パッチム (일) の後は「로」
        else:
            expected = with_batchim if digit in _HAS_BATCHIM else without
        if particle != expected:
            errors.append(f"{m.group(0)} → {m.group(0)[: -len(particle)]}{expected}")
    return errors


def numbers(text: str, lang_code: str = "") -> Counter[str]:
    """文中の数値の多重集合. 全角数字・桁区切りの違いは無視する."""
    t = unicodedata.normalize("NFKC", text)
    if lang_code in _ORDINALS:
        pattern, digits = _ORDINALS[lang_code]
        t = pattern.sub(lambda m: str(digits.index(m.group(1)) + 1), t)
    return Counter(_NUMBER.findall(_THOUSANDS.sub("", t)))


def _without(text: str, protected: tuple[str, ...]) -> str:
    for token in sorted(set(protected), key=len, reverse=True):
        text = text.replace(token, " ")
    return text


def check(
    source: str,
    target: str,
    src: str,
    tgt: str,
    terms: list[Term],
    protected: tuple[str, ...] = (),
) -> list[Issue]:
    """訳文の問題. protected (コード・URL など) は原文と同一であることを別に保証しているため、
    文字種・用語・数値の確認からは除く (コード中の漢字や用語を誤って指摘しないように)。
    """
    issues: list[Issue] = []
    if not target.strip():
        return [Issue("missing", "訳文が空です")]
    full_target = target
    source, target = _without(source, protected), _without(target, protected)

    src_nums, tgt_nums = numbers(source, src), numbers(target, tgt)
    missing = sorted((src_nums - tgt_nums).elements())
    extra = sorted((tgt_nums - src_nums).elements())
    if missing:
        issues.append(
            Issue(
                "number", f"原文の数値が訳文にありません: {', '.join(missing)} (数値は変えずに残す)"
            )
        )
    if extra:
        issues.append(
            Issue("number", f"原文にない数値が訳文にあります: {', '.join(extra)}", "warning")
        )

    for t in violations(target, terms, tgt):
        issues.append(Issue("glossary", f"用語集: 「{t.get(src)}」は「{t.get(tgt)}」と訳す"))

    if tgt == "ko":
        # 中黒は見た目が似ているが、日本語は U+30FB「・」、韓国語は U+00B7「·」
        punct = sorted(set(re.findall(r"[。、・]", target)))
        if punct:
            issues.append(
                Issue(
                    "script", f"訳文に日本語の記号 ({' '.join(punct)}) が残っています (中黒は「·」)"
                )
            )
        for fix in particle_errors(full_target):  # 助詞は保護部分の末尾の音で決まる
            issues.append(Issue("particle", f"助詞の誤り: {fix}"))

    if tgt == "ko" and lang.KANA.search(target.replace("・", "")):
        issues.append(Issue("script", "訳文に仮名が残っています"))
    elif tgt == "ko" and lang.KANJI.search(target):
        issues.append(Issue("script", "訳文に漢字が残っています", "warning"))
    elif tgt == "ja" and lang.HANGUL.search(target):
        # 韓国人の人名などは原文のまま残すことがあるため警告にとどめる
        issues.append(Issue("script", "訳文にハングルが残っています", "warning"))

    src_len, tgt_len = len(compact(source)), len(compact(target))
    if src_len >= 15 and not 0.4 <= tgt_len / src_len <= 2.5:
        issues.append(
            Issue(
                "length",
                f"訳文の長さが原文と大きく違います ({src_len} → {tgt_len} 文字)",
                "warning",
            )
        )
    return issues


def has_error(issues: list[Issue]) -> bool:
    return any(i.severity == "error" for i in issues)
