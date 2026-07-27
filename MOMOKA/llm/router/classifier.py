# ルーター LLM による mode / lang 分類。
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    # llm_cog への循環 import を避ける
    from MOMOKA.llm.llm_cog import LLMCog

logger = logging.getLogger(__name__)

# 許可モード一覧
_VALID_MODES = frozenset({"conversation", "coding", "command", "unsupported"})
# 許可言語コード
_VALID_LANGS = frozenset({"ja", "en", "ko", "vi", "zh-CN", "zh-TW"})


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
    """モデル出力から JSON オブジェクトを取り出す。"""
    # 前後空白を落とす
    text = (raw or "").strip()
    # 空なら失敗
    if not text:
        return None
    # フェンス除去
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL | re.IGNORECASE)
    if fence:
        text = fence.group(1)
    # 最初の {…} を拾う
    brace = re.search(r"\{.*\}", text, re.DOTALL)
    if brace:
        text = brace.group(0)
    try:
        # JSON パース
        data = json.loads(text)
    except json.JSONDecodeError:
        # パース失敗
        return None
    # dict 以外は無効
    if not isinstance(data, dict):
        return None
    return data


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
    # 各モデルを順に試す
    last_error: Optional[Exception] = None
    for model_string in chain:
        try:
            # クライアント取得（キー回転は既存経路に任せる）
            client = cog._get_or_create_llm_client(model_string)
            if not client:
                continue
            # Gemini 形式が必要なら変換
            api_messages = cog._ensure_messages_for_model(messages, model_string)
            # 短めの完了呼び出し
            resp = await client.chat.completions.create(
                model=client.model_name_for_api_calls,
                messages=api_messages,
                stream=False,
                max_tokens=256,
                temperature=0.0,
            )
            # 本文取り出し
            raw = ""
            if resp.choices:
                raw = (resp.choices[0].message.content or "").strip()
            # JSON 解釈
            data = _parse_route_json(raw)
            if not data:
                logger.warning(
                    "[%s] router JSON parse failed for %s: %r",
                    cog._bot_tag(),
                    model_string,
                    raw[:200],
                )
                continue
            # mode 正規化
            mode = str(data.get("mode") or "conversation").strip().lower()
            if mode not in _VALID_MODES:
                mode = "conversation"
            # lang 正規化
            lang = str(data.get("lang") or "").strip()
            if lang not in _VALID_LANGS:
                lang = _heuristic_lang(user_text)
            # reason
            reason = str(data.get("reason") or "")[:80]
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
            # 次モデルへ
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
