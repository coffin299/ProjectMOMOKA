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
# ルーター JSON の必須キーっぽい目印
_MODE_KEY_RE = re.compile(r'"mode"\s*:', re.IGNORECASE)


def extract_completion_text(resp: Any) -> str:
    """Chat completion 応答から assistant 本文を安全に取り出す。"""
    # choices 配列を取り出す
    choices = getattr(resp, "choices", None) or []
    # 空なら本文なし
    if not choices:
        return ""
    # 先頭 choice の message を取る（None のモデルもある）
    message = getattr(choices[0], "message", None)
    # message 自体が null なら空文字
    if message is None:
        return ""
    # content が null でも空文字に正規化する
    return (getattr(message, "content", None) or "").strip()


def completion_has_null_message(resp: Any) -> bool:
    """choices はあるが message が null の異常応答か判定する。"""
    # choices 配列を取り出す
    choices = getattr(resp, "choices", None) or []
    # choice が無ければ null message ではない
    if not choices:
        return False
    # message が明示的に None なら True
    return getattr(choices[0], "message", None) is None


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


def _loads_dict(text: str) -> Optional[Dict[str, Any]]:
    """文字列を dict JSON として読む。失敗時は None。"""
    # 空は無効
    if not text or not text.strip():
        return None
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


def _extract_fence_or_brace(text: str) -> Optional[Dict[str, Any]]:
    """フェンス / ブレース抽出からのパースを試す。"""
    # 作業用コピー
    candidate = text
    # Markdown フェンス内の JSON を優先
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", candidate, re.DOTALL | re.IGNORECASE)
    # フェンスがあればその中身を使う
    if fence:
        parsed = _loads_dict(fence.group(1))
        # 成功なら返す
        if parsed is not None:
            return parsed
    # 最初の {…} を貪欲に拾う
    brace = re.search(r"\{.*\}", candidate, re.DOTALL)
    # ブレースがあればその範囲を試す
    if brace:
        return _loads_dict(brace.group(0))
    return None


def _scan_json_objects_with_mode(text: str) -> Optional[Dict[str, Any]]:
    """本文のどこか（thought 内含む）から mode キー付き JSON を探す。"""
    # 逐次デコーダで { 位置から raw_decode する
    decoder = json.JSONDecoder()
    # 最後に見つかった mode 付きオブジェクトを採用する
    last_hit: Optional[Dict[str, Any]] = None
    # 先頭から走査する
    for index, char in enumerate(text):
        # オブジェクト開始以外はスキップ
        if char != "{":
            continue
        try:
            # この位置から JSON オブジェクトを読む
            obj, _end = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            # この { は JSON ではない
            continue
        # dict かつ mode があるものだけ候補にする
        if isinstance(obj, dict) and "mode" in obj:
            last_hit = obj
    return last_hit


def parse_llm_json_object(raw: str) -> Optional[Dict[str, Any]]:
    """モデル出力から JSON オブジェクトを取り出す。失敗時は None。"""
    # 前後空白を落とす
    text = (raw or "").strip()
    # 空なら失敗
    if not text:
        return None
    # 思考タグ除去後の本文を作る
    stripped = _strip_reasoning_blocks(text)
    # 除去後に中身があれば通常抽出を試す
    if stripped:
        hit = _extract_fence_or_brace(stripped)
        # 除去後テキストから取れたら成功
        if hit is not None:
            return hit
    # thought 内に JSON が埋もれている場合も拾う
    if _MODE_KEY_RE.search(text):
        # 生テキスト全体をスキャンする
        embedded = _scan_json_objects_with_mode(text)
        # 見つかれば返す
        if embedded is not None:
            return embedded
    # 除去前テキストでもフェンス／ブレースを最後に試す
    return _extract_fence_or_brace(text)
