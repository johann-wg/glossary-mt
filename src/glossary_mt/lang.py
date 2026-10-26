"""日本語・韓国語の文字種の判定."""

from __future__ import annotations

import re

HANGUL = re.compile(r"[가-힣ㄱ-ㆎ]")
KANA = re.compile(r"[ぁ-ヿㇰ-ㇿｦ-ﾝ]")
KANJI = re.compile(r"[㐀-䶿一-鿿]")

NAMES = {"ja": "日本語", "ko": "韓国語"}


def has_script(text: str, lang: str) -> bool:
    """text に lang の文字 (日本語なら仮名・漢字, 韓国語ならハングル) が含まれるか."""
    if lang == "ja":
        return bool(KANA.search(text) or KANJI.search(text))
    if lang == "ko":
        return bool(HANGUL.search(text))
    raise ValueError(f"未対応の言語: {lang}")


def other(lang: str) -> str:
    """翻訳先の言語から翻訳元の言語を決める (日韓の 2 言語のみ対応)."""
    return {"ja": "ko", "ko": "ja"}[lang]
