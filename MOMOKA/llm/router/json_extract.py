# ルーター系 LLM 出力から JSON オブジェクトを取り出す。
from __future__ import annotations

import json
import re
from typing import Any, Dict, Optional

# 閉じタグ付きの思考ブロック（Gemma の <thought> 等）
_REASONING_BLOCK_RE = re.compile(
    r"<(thought|think)\b[^>]*>.*?</\1\s*>",
    re.DOTALL | re.IGNORECASE,
)
# 未閉じの思考開始タグ（途中切れ・閉じ無し対応）
_REASONING_OPEN_RE = re.compile(
    r"<(thought|think)\b[^>]*>",
    re.IGNORECASE,
)


def _strip_reasoning_blocks(text: str) -> str:
    """モデルが漏らした thought / think ブロックを除去する。"""
    # 閉じタグ付きブロックを先に落とす
    cleaned = _REASONING_BLOCK_RE.sub("", text)
    # 未閉じタグが残っていればその位置以降を捨てる
    open_match = _REASONING_OPEN_RE.search(cleaned)
    # 未閉じがあればタグ直前まで残す
    if open_match:
        cleaned = cleaned[: open_match.start()]
    # 前後空白を整える
    return cleaned.strip()


def parse_llm_json_object(raw: str) -> Optional[Dict[str, Any]]:
    """モデル出力から JSON オブジェクトを取り出す。失敗時は None。"""
    # 前後空白を落とす
    text = (raw or "").strip()
    # 空なら失敗
    if not text:
        return None
    # 思考タグを除去してから抽出する
    text = _strip_reasoning_blocks(text)
    # 除去後に空なら失敗
    if not text:
        return None
    # Markdown フェンス内の JSON を優先
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL | re.IGNORECASE)
    # フェンスがあればその中身を使う
    if fence:
        text = fence.group(1)
    # 最初の {…} を貪欲に拾う
    brace = re.search(r"\{.*\}", text, re.DOTALL)
    # ブレースがあればその範囲を使う
    if brace:
        text = brace.group(0)
    try:
        # JSON として解釈する
        data = json.loads(text)
    except json.JSONDecodeError:
        # パース失敗
        return None
    # dict 以外は無効
    if not isinstance(data, dict):
        return None
    return data
