"""翻訳してはいけない部分 (コード・URL・パス・変数など) の保護.

LLM に渡す前に ⟦1⟧ のようなマーカーに置き換え、翻訳後に元に戻す。
保護部分は LLM に一切書き換えられないことが保証され、マーカーが消えた・増えた場合は
ProtectionError として検出できる。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# 先頭から順に試す. 同じ位置で複数が一致する場合は先に書いたものが優先される
_PATTERNS = [
    r"`[^`\n]+`",  # インラインコード
    r"<[A-Za-z/!][^<>\n]*>",  # HTML タグ
    r"(?<=\])\([^()\s]+\)",  # Markdown リンクの URL 部分 [text](url)
    r"https?://[A-Za-z0-9\-._~:/?#@!$&*+,;=%]*[A-Za-z0-9/_\-#=&%~]",  # URL (末尾の句読点は含めない)
    r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+",  # メールアドレス
    r"\$\{[^}\n]+\}|\{\{[^}\n]+\}\}|\{[A-Za-z_][A-Za-z0-9_.]*\}",  # ${VAR} {{var}} {name}
    r"%(?:\d+\$)?[-+ 0#]*\d*(?:\.\d+)?[sdfixX]",  # printf 形式
    r"(?<![A-Za-z0-9_./])(?:/[A-Za-z0-9_.-]+){2,}/?",  # Unix のパス (2 階層以上)
    r"\b[A-Za-z]:\\[A-Za-z0-9_.\\-]+",  # Windows のパス
]
_PROTECTED = re.compile("|".join(f"(?:{p})" for p in _PATTERNS))
MARKER = re.compile(r"⟦(\d+)⟧")


class ProtectionError(ValueError):
    def __init__(self, missing: list[str], extra: list[str]):
        self.missing = missing
        self.extra = extra
        parts = []
        if missing:
            parts.append("消えた保護部分: " + ", ".join(missing))
        if extra:
            parts.append("不明なマーカー: " + ", ".join(extra))
        super().__init__(" / ".join(parts))


@dataclass(frozen=True)
class Masked:
    text: str
    tokens: tuple[str, ...]


def mask(text: str) -> Masked:
    tokens: list[str] = []

    def repl(m: re.Match[str]) -> str:
        tokens.append(m.group(0))
        return f"⟦{len(tokens)}⟧"

    return Masked(_PROTECTED.sub(repl, text), tuple(tokens))


def unmask(text: str, tokens: tuple[str, ...]) -> str:
    """マーカーを元に戻す. 各マーカーがちょうど 1 回ずつ現れない場合は ProtectionError.

    訳文では語順に合わせてマーカーの位置が動いてよい.
    """
    found = [int(n) for n in MARKER.findall(text)]
    expected = range(1, len(tokens) + 1)
    missing = [tokens[i - 1] for i in expected if found.count(i) == 0]
    extra = [f"⟦{n}⟧" for n in sorted(set(found)) if n not in expected or found.count(n) > 1]
    if missing or extra:
        raise ProtectionError(missing, extra)
    return MARKER.sub(lambda m: tokens[int(m.group(1)) - 1], text)


def restore_partial(text: str, tokens: tuple[str, ...]) -> str:
    """戻せるマーカーだけ戻す (QA レポートの表示用)."""

    def repl(m: re.Match[str]) -> str:
        n = int(m.group(1))
        return tokens[n - 1] if 1 <= n <= len(tokens) else m.group(0)

    return MARKER.sub(repl, text)
