"""翻訳メモリ (SQLite).

QA を通過した訳文を「原文 + 適用された用語の組」をキーに保存する。
文書の一部だけを修正して再実行したとき、変更のない文は API を呼ばずに再利用される。
用語集の該当部分を変えた文は、キーが変わるため再翻訳される。
"""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tm (
    src_lang    TEXT NOT NULL,
    tgt_lang    TEXT NOT NULL,
    source_hash TEXT NOT NULL,
    glossary    TEXT NOT NULL,
    source      TEXT NOT NULL,
    target      TEXT NOT NULL,
    model       TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (src_lang, tgt_lang, source_hash, glossary)
)
"""


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class TranslationMemory:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path))
        self._db.execute(_SCHEMA)

    def get(self, src: str, tgt: str, source: str, glossary_sig: str) -> str | None:
        row = self._db.execute(
            "SELECT target FROM tm WHERE src_lang=? AND tgt_lang=? AND source_hash=? AND glossary=?",
            (src, tgt, _hash(source), glossary_sig),
        ).fetchone()
        return row[0] if row else None

    def put(
        self, src: str, tgt: str, source: str, glossary_sig: str, target: str, model: str
    ) -> None:
        with self._db:
            self._db.execute(
                "INSERT OR REPLACE INTO tm (src_lang, tgt_lang, source_hash, glossary, source, target, model)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (src, tgt, _hash(source), glossary_sig, source, target, model),
            )

    def stats(self) -> list[tuple[str, str, int]]:
        return self._db.execute(
            "SELECT src_lang, tgt_lang, COUNT(*) FROM tm GROUP BY src_lang, tgt_lang ORDER BY 1, 2"
        ).fetchall()

    def close(self) -> None:
        self._db.close()
