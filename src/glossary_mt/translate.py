"""Claude API による翻訳.

文を最大 batch_size 件ずつまとめて 1 リクエストにし、複数のリクエストを asyncio で並列に送る
(同時実行数は Semaphore で制限)。応答は JSON スキーマ (Structured Outputs) で id と訳文の組に固定する。

応答は入力のハッシュをキーにキャッシュする。同じ評価を再実行するときは API を呼ばないため、
CI や第三者は費用ゼロで結果を再現できる。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from glossary_mt.glossary import Term
from glossary_mt.lang import NAMES

DEFAULT_MODEL = "claude-opus-5-5"
# プロンプトを変えたら上げる. キャッシュキーに含まれるため古い応答は使われなくなる
PROMPT_VERSION = "2"

# 100 万トークンあたりの料金 (USD, 入力 / 出力). Anthropic 公式の API 料金
PRICES = {
    "claude-opus-5-5": (4.00, 20.00),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-haiku-5-5": (0.10, 0.50),
}
# 拒否時にサーバー側で別モデルに切り替える機能 (fallbacks) が使えるモデル
FALLBACK_MODELS = {"claude-opus-5-5", "claude-sonnet-5-5"}

SYSTEM_PROMPT = """\
あなたは日本語と韓国語の IT 技術文書 (設計書・手順書・報告書・管理表) を専門とする翻訳者です。
入力の JSON 配列の各要素の text を、指定された方向に翻訳してください。

翻訳のルール:
- 要素ごとに 1 件ずつ翻訳し、同じ id で返す。要素の統合・分割・省略はしない
- ⟦1⟧ のようなマーカーはコード・URL・パスなどの保護部分。記号も番号も変えずに訳文に残す
  (語順に合わせて位置は動かしてよい)。protected は各マーカーの元の内容で、
  韓国語の助詞 (을/를・이/가・으로/로 など) を選ぶときは、この内容の読みの最後の音に合わせる
- 数字は変えない (漢数字と算用数字の変換、単位の換算、桁区切りの追加・削除をしない)
- 用語集の訳語は必ず使う (助詞・語尾を付けたり、複合語の一部にしたりするのはよい)
- 製品名・コマンド・設定項目名などの英字はそのまま残す
- 文体は原文に合わせる: 「です・ます」は「-습니다/-ㅂ니다」、「だ・である」は「-다」、
  表のセルや見出しの体言止めは名詞で終える (韓国語から日本語への翻訳も同様に対応させる)
- 日本人の人名は韓国語ではハングル表記にし、韓国人の人名は原文のハングル表記のまま残す
- context は翻訳の参考情報で、翻訳しない
- previous と problems がある要素は、前回の訳の問題を直した訳を返す
- 原文にない説明や注釈を加えない
"""

OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "translations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"id": {"type": "string"}, "text": {"type": "string"}},
                "required": ["id", "text"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["translations"],
    "additionalProperties": False,
}


@dataclass
class Item:
    """1 件の翻訳依頼. text は保護部分をマーカーに置き換え済みのもの."""

    id: str
    text: str
    context: str = ""
    terms: list[Term] = field(default_factory=list)
    protected: tuple[str, ...] = ()  # マーカー ⟦1⟧, ⟦2⟧ … の元の内容 (助詞の選択の参考)
    previous: str | None = None  # 再翻訳のとき: 前回の訳
    problems: list[str] = field(default_factory=list)  # 再翻訳のとき: QA で見つかった問題


@dataclass
class Usage:
    """API 呼び出しの集計.

    キャッシュから返した分も、実際に API を呼んだ場合の費用として estimated_cost_usd に含める。
    """

    calls: int = 0
    cache_hits: int = 0
    by_model: dict[str, list[int]] = field(default_factory=dict)
    cached_by_model: dict[str, list[int]] = field(default_factory=dict)

    def add(self, model: str, input_tokens: int, output_tokens: int, cached: bool = False) -> None:
        if cached:
            self.cache_hits += 1
            target = self.cached_by_model
        else:
            self.calls += 1
            target = self.by_model
        tokens = target.setdefault(model, [0, 0])
        tokens[0] += input_tokens
        tokens[1] += output_tokens

    @staticmethod
    def _cost(by_model: dict[str, list[int]]) -> float:
        total = 0.0
        for model, (inp, out) in by_model.items():
            pin, pout = PRICES.get(model, (0.0, 0.0))
            total += inp / 1e6 * pin + out / 1e6 * pout
        return total

    @property
    def cost_usd(self) -> float:
        """今回実際に API で使った費用."""
        return self._cost(self.by_model)

    @property
    def estimated_cost_usd(self) -> float:
        """キャッシュがなかった場合の費用."""
        return self.cost_usd + self._cost(self.cached_by_model)


def batches(items: list[Item], max_items: int, max_chars: int) -> list[list[Item]]:
    """文書の順序を保ったまま、件数と文字数の上限でまとめる."""
    result: list[list[Item]] = []
    current: list[Item] = []
    chars = 0
    for item in items:
        if current and (len(current) >= max_items or chars + len(item.text) > max_chars):
            result.append(current)
            current, chars = [], 0
        current.append(item)
        chars += len(item.text)
    if current:
        result.append(current)
    return result


class Translator:
    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        effort: str = "medium",
        cache_dir: str | Path = "llm-cache",
        offline: bool = False,
        concurrency: int = 4,
        batch_size: int = 30,
        batch_chars: int = 4000,
    ):
        self.model = model
        self.effort = effort
        self.cache_dir = Path(cache_dir)
        self.offline = offline
        self.batch_size = batch_size
        self.batch_chars = batch_chars
        self.concurrency = concurrency
        self.usage = Usage()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._semaphore: asyncio.Semaphore | None = None
        self._client = None

    def _bind_loop(self) -> None:
        # watch モードでは変更のたびに asyncio.run するため、イベントループごとに作り直す
        loop = asyncio.get_running_loop()
        if loop is not self._loop:
            self._loop = loop
            self._semaphore = asyncio.Semaphore(self.concurrency)
            self._client = None

    def _user_message(self, batch: list[Item], src: str, tgt: str, doc_name: str) -> str:
        terms = sorted({t for item in batch for t in item.terms}, key=lambda t: t.get(src))
        glossary = "\n".join(f"- {t.get(src)} → {t.get(tgt)}" for t in terms) or "(なし)"
        payload = []
        for item in batch:
            entry: dict[str, Any] = {"id": item.id, "text": item.text}
            if item.context:
                entry["context"] = item.context
            if item.protected:
                entry["protected"] = {f"⟦{n}⟧": tok for n, tok in enumerate(item.protected, 1)}
            if item.previous is not None:
                entry["previous"] = item.previous
                entry["problems"] = item.problems
            payload.append(entry)
        return (
            f"翻訳方向: {NAMES[src]} → {NAMES[tgt]}\n"
            f"文書: {doc_name}\n\n"
            f"用語集:\n{glossary}\n\n"
            f"入力:\n{json.dumps(payload, ensure_ascii=False, indent=1)}"
        )

    def _cache_path(self, message: str) -> Path:
        key = hashlib.sha256(
            json.dumps(
                {
                    "model": self.model,
                    "effort": self.effort,
                    "prompt": PROMPT_VERSION,
                    "input": message,
                },
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()[:24]
        return self.cache_dir / self.model / f"{key}.json"

    async def _call(self, message: str) -> dict[str, Any]:
        if self._client is None:
            import anthropic

            # 429・5xx・接続エラーは SDK が指数バックオフで再試行する
            self._client = anthropic.AsyncAnthropic(max_retries=5)
        params: dict[str, Any] = {
            "model": self.model,
            "max_tokens": 16000,
            "system": SYSTEM_PROMPT,
            "messages": [{"role": "user", "content": message}],
            "output_config": {
                "effort": self.effort,
                "format": {"type": "json_schema", "schema": OUTPUT_SCHEMA},
            },
        }
        async with self._semaphore:
            if self.model in FALLBACK_MODELS:
                response = await self._client.beta.messages.create(
                    betas=["server-side-fallback-2026-07-01"], fallbacks="default", **params
                )
            else:
                response = await self._client.messages.create(**params)

        usage = response.usage
        self.usage.add(response.model, usage.input_tokens, usage.output_tokens)
        record: dict[str, Any] = {
            "model": response.model,
            "stop_reason": response.stop_reason,
            "usage": {"input_tokens": usage.input_tokens, "output_tokens": usage.output_tokens},
            "result": None,
        }
        if response.stop_reason not in ("refusal", "max_tokens"):
            text = next(b.text for b in response.content if b.type == "text")
            record["result"] = json.loads(text)
        return record

    async def aclose(self) -> None:
        """HTTP 接続を閉じる. イベントループの終了前に呼ぶ."""
        if self._client is not None:
            await self._client.close()
            self._client = None

    async def _request(self, message: str) -> dict[str, Any]:
        path = self._cache_path(message)
        if path.exists():
            record = json.loads(path.read_text(encoding="utf-8"))
            u = record["usage"]
            self.usage.add(self.model, u["input_tokens"], u["output_tokens"], cached=True)
            return record
        if self.offline:
            raise LookupError(f"キャッシュがありません (offline): {path.name}")
        record = await self._call(message)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return record

    async def _translate_batch(
        self, batch: list[Item], src: str, tgt: str, doc_name: str
    ) -> dict[str, str]:
        record = await self._request(self._user_message(batch, src, tgt, doc_name))
        if record["result"] is None:
            if record["stop_reason"] == "max_tokens" and len(batch) > 1:
                # 出力が上限に達した: 半分に分けて送り直す
                mid = len(batch) // 2
                left, right = await asyncio.gather(
                    self._translate_batch(batch[:mid], src, tgt, doc_name),
                    self._translate_batch(batch[mid:], src, tgt, doc_name),
                )
                return {**left, **right}
            return {}  # 拒否など. 返らなかった文はパイプライン側で missing として扱う
        wanted = {item.id for item in batch}
        out: dict[str, str] = {}
        for t in record["result"]["translations"]:
            if t["id"] in wanted and t["id"] not in out:
                out[t["id"]] = t["text"]
        return out

    async def translate(
        self, items: list[Item], src: str, tgt: str, doc_name: str = ""
    ) -> dict[str, str]:
        """id → 訳文. 訳文が返らなかった id は含まれない."""
        self._bind_loop()
        groups = batches(items, self.batch_size, self.batch_chars)
        results = await asyncio.gather(
            *(self._translate_batch(g, src, tgt, doc_name) for g in groups)
        )
        merged: dict[str, str] = {}
        for r in results:
            merged.update(r)
        return merged
