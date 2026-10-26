"""フォルダの監視.

外部ライブラリを使わず、更新日時とサイズのポーリングで変更を検出する。
保存の途中のファイルを読まないよう、サイズと更新日時が 2 回続けて変わらなかったものだけを処理する。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from glossary_mt.formats import SUFFIXES


@dataclass
class Watcher:
    root: Path
    exclude: list[Path] = field(default_factory=list)
    _seen: dict[Path, tuple[float, int]] = field(default_factory=dict)  # 処理済みの状態
    _pending: dict[Path, tuple[float, int]] = field(default_factory=dict)  # 変更を検出した状態

    def _targets(self) -> dict[Path, tuple[float, int]]:
        result = {}
        for path in self.root.rglob("*"):
            if (
                path.suffix.lower() not in SUFFIXES
                or path.name.startswith("~$")  # Office の一時ファイル
                or not path.is_file()
                or any(path.is_relative_to(ex) for ex in self.exclude)
            ):
                continue
            st = path.stat()
            result[path] = (st.st_mtime, st.st_size)
        return result

    def prime(self) -> None:
        """起動時点のファイルを処理済みとして記録する (既存ファイルは翻訳しない)."""
        self._seen = self._targets()

    def poll(self) -> list[Path]:
        """前回から変更がなく、かつ処理済みの状態と異なるファイル (= 保存が完了した変更)."""
        ready = []
        current = self._targets()
        for path, state in current.items():
            if self._seen.get(path) == state:
                self._pending.pop(path, None)
            elif self._pending.get(path) == state:
                ready.append(path)
                self._seen[path] = state
                del self._pending[path]
            else:
                self._pending[path] = state
        return sorted(ready)
