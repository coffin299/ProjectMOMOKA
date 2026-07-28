# ルーター LLM による mode / lang 分類。
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, TYPE_CHECKING

from MOMOKA.llm.router.json_extract import (
    completion_has_null_message,
    extract_completion_text,
    parse_llm_json_object,
)

if TYPE_CHECKING:
    # llm_cog への循環 import を避ける
    from MOMOKA.llm.llm_cog import LLMCog

logger = logging.getLogger(__name__)

# 許可モード一覧
_VALID_MODES = frozenset({"conversation", "coding", "command", "unsupported"})
# 許可言語コード
_VALID_LANGS = frozenset({"ja", "en", "ko", "vi", "zh-CN", "zh-TW"})
# NSFW 系キーワード（unsupported → conversation へ矯正する）
_NSFW_ROUTE_MARKERS = (
    "nsfw",
    "r18",
    "r-18",
    "adult",
    "explicit",
    "lewd",
    "hentai",
    "nude",
    "porn",
    "erotic",
    "エロ",
    "性的",
    "成人向",
    "アダルト",
    "ヌード",
    "過激",
    "r18g",
)


def _is_nsfw_related_request(user_text: str, reason: str = "") -> bool:
    """ユーザー文またはルーター reason が NSFW 系かどうか。"""
    # 判定用に小文字化した結合文字列を作る
    combined = f"{user_text}\n{reason}".lower()
    # いずれかのマーカーが含まれれば NSFW 系とみなす
    return any(marker in combined for marker in _NSFW_ROUTE_MARKERS)


def _normalize_route_mode(mode: str, user_text: str, reason: str) -> str:
    """mode を正規化し、NSFW は unsupported から conversation へ矯正する。"""
    # 未知 mode は conversation へ
    if mode not in _VALID_MODES:
        return "conversation"
    # NSFW が unsupported に振られた場合は conversation へ上書き
    if mode == "unsupported" and _is_nsfw_related_request(user_text, reason):
        return "conversation"
    # それ以外はそのまま
    return mode


@dataclass
class RouteResult:
    """ルーター分類結果。"""

    # 実行モード
    mode: str
    # 応答言語
    lang: str
    # 短い理由
    reason: str
    # ルーター自体が失敗して conversation へ落ちたか
    router_failed: bool = False


def _heuristic_lang(text: str) -> str:
    """ルーター失敗時の簡易言語推定。"""
    # 空なら英語
    if not text or not text.strip():
        return "en"
    # ハングル
    if re.search(r"[\uac00-\ud7af]", text):
        return "ko"
    # ひらがな・カタカナが多ければ日本語
    if re.search(r"[\u3040-\u30ff]", text):
        return "ja"
    # 漢字のみ寄りは簡体寄りとする（厳密判定はしない）
    if re.search(r"[\u4e00-\u9fff]", text):
        return "zh-CN"
    # ベトナム語っぽい声調記号
    if re.search(r"[ăâêôơưđĂÂÊÔƠƯĐ]", text):
        return "vi"
    # それ以外は英語
    return "en"


def _parse_route_json(raw: str) -> Optional[Dict[str, Any]]:
    """モデル出力から JSON オブジェクトを取り出す（thought 除去込み）。"""
    # 共有抽出ロジックへ委譲する
    return parse_llm_json_object(raw)


async def classify_request(
    cog: "LLMCog",
    *,
    user_text: str,
    channel_id: int,
) -> RouteResult:
    """ユーザー文を mode / lang に分類する。失敗時は conversation へフォールスルー。"""
    # llm 設定を取る
    llm_cfg = cog.llm_config or {}
    # router セクション
    router_cfg = llm_cfg.get("router") or {}
    # 無効なら conversation 固定
    if not router_cfg.get("enabled", True):
        # 言語だけヒューリスティック
        return RouteResult(
            mode="conversation",
            lang=_heuristic_lang(user_text),
            reason="router disabled",
            router_failed=True,
        )
    # 分類プロンプト
    classify_prompt = (router_cfg.get("classify_prompt") or "").strip()
    # 未設定なら安全側
    if not classify_prompt:
        logger.warning("[%s] router.classify_prompt missing; fallback conversation", cog._bot_tag())
        return RouteResult(
            mode="conversation",
            lang=_heuristic_lang(user_text),
            reason="missing classify_prompt",
            router_failed=True,
        )
    # 試行モデル列: [model] + fallback_models
    primary = router_cfg.get("model")
    chain: List[str] = []
    if primary:
        chain.append(str(primary))
    for m in router_cfg.get("fallback_models") or []:
        if m and str(m) not in chain:
            chain.append(str(m))
    # モデルが無ければ失敗扱い
    if not chain:
        return RouteResult(
            mode="conversation",
            lang=_heuristic_lang(user_text),
            reason="no router models",
            router_failed=True,
        )
    # ユーザー向けメッセージ
    messages = [
        {"role": "system", "content": classify_prompt},
        {"role": "user", "content": user_text[:4000]},
    ]
    # 各モデルを順に試す（各モデル内で API キー全巡回）
    last_error: Optional[Exception] = None
    for model_string in chain:
        try:
            # クライアント取得
            client = cog._get_or_create_llm_client(model_string)
            if not client:
                continue
            # Gemini 形式が必要なら変換
            api_messages = cog._ensure_messages_for_model(messages, model_string)
            # キー巡回付きの短め完了呼び出し（Gemma thought 抑制付き）
            resp, client = await cog._chat_completion_with_key_rotation(
                client,
                messages=api_messages,
                max_tokens=4096,
                temperature=0.0,
                suppress_thinking=True,
            )
            # 本文取り出し（message が null のモデル応答にも対応）
            raw = extract_completion_text(resp)
            # message 自体が null なら次モデルへ
            if completion_has_null_message(resp):
                logger.warning(
                    "[%s] router model %s returned null message; trying next",
                    cog._bot_tag(),
                    model_string,
                )
                continue
            # JSON 解釈（パース失敗はキー問題ではないので次モデルへ）
            data = _parse_route_json(raw)
            if not data:
                # thought のみ残った切り捨てっぽいかをログに残す
                thought_only = bool(
                    re.search(r"<(thought|think)\b", raw or "", re.IGNORECASE)
                ) and not re.search(r'"mode"\s*:', raw or "", re.IGNORECASE)
                logger.warning(
                    "[%s] router JSON parse failed for %s%s: %r",
                    cog._bot_tag(),
                    model_string,
                    " (thought-only/truncated?)" if thought_only else "",
                    raw[:200],
                )
                continue
            # mode 正規化
            mode = str(data.get("mode") or "conversation").strip().lower()
            # reason（NSFW 矯正判定にも使う）
            reason = str(data.get("reason") or "")[:80]
            # NSFW は unsupported ではなく conversation へ
            normalized_mode = _normalize_route_mode(mode, user_text, reason)
            if normalized_mode != mode:
                logger.info(
                    "[%s] router remapped %s→conversation (NSFW request)",
                    cog._bot_tag(),
                    mode,
                )
                mode = normalized_mode
            if mode not in _VALID_MODES:
                mode = "conversation"
            # lang 正規化
            lang = str(data.get("lang") or "").strip()
            if lang not in _VALID_LANGS:
                lang = _heuristic_lang(user_text)
            logger.info(
                "[%s] router classified mode=%s lang=%s via %s (%s)",
                cog._bot_tag(),
                mode,
                lang,
                model_string,
                reason,
            )
            return RouteResult(mode=mode, lang=lang, reason=reason, router_failed=False)
        except Exception as e:
            # キー尽きた／未処理例外 → 次モデルへ
            last_error = e
            logger.warning(
                "[%s] router model %s failed: %s",
                cog._bot_tag(),
                model_string,
                e,
            )
            continue
    # 全失敗 → conversation
    logger.error(
        "[%s] router all models failed (%s); falling through to conversation",
        cog._bot_tag(),
        last_error,
    )
    return RouteResult(
        mode="conversation",
        lang=_heuristic_lang(user_text),
        reason="router failed",
        router_failed=True,
    )
