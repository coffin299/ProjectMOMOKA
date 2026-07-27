# 分類結果に基づくモード実行ヘルパー。
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, TYPE_CHECKING

from MOMOKA.llm.router.classifier import RouteResult, classify_request
from MOMOKA.llm.router.command_executor import run_command_mode

if TYPE_CHECKING:
    from MOMOKA.llm.llm_cog import LLMCog

logger = logging.getLogger(__name__)

# 待機 UI 用カスタム絵文字
EMOJI_ROUTER = "<a:loading:1531129757234757744>"
EMOJI_CODING = "<a:coding:1531206254574178346>"
EMOJI_STREAM = "<:stream:1313474295372058758>"


def waiting_phase_title(phase: str, lang: str) -> str:
    """フェーズ見出し文言。"""
    ja = lang.startswith("ja")
    if phase == "router":
        return (
            f"{EMOJI_ROUTER} リクエストを振り分け中…"
            if ja
            else f"{EMOJI_ROUTER} Routing your request…"
        )
    if phase == "coding":
        return (
            f"{EMOJI_CODING} コードを生成中…"
            if ja
            else f"{EMOJI_CODING} Generating code…"
        )
    if phase == "command":
        return (
            f"{EMOJI_STREAM} コマンドを処理中…"
            if ja
            else f"{EMOJI_STREAM} Processing command…"
        )
    # conversation / default
    return (
        f"{EMOJI_STREAM} 考え中…"
        if ja
        else f"{EMOJI_STREAM} Thinking…"
    )


def mode_model_chain(llm_config: Dict[str, Any], mode: str) -> List[str]:
    """modes.<mode> の [model]+fallback_models。無ければトップレベルへフォールバック。"""
    modes = llm_config.get("modes") or {}
    entry = modes.get(mode) if isinstance(modes, dict) else None
    chain: List[str] = []
    if isinstance(entry, dict):
        primary = entry.get("model")
        if primary:
            chain.append(str(primary))
        for m in entry.get("fallback_models") or []:
            if m and str(m) not in chain:
                chain.append(str(m))
    # conversation で空ならトップレベル
    if not chain:
        top = llm_config.get("model")
        if top:
            chain.append(str(top))
        for m in llm_config.get("fallback_models") or []:
            if m and str(m) not in chain:
                chain.append(str(m))
    return chain


def coding_attach_settings(llm_config: Dict[str, Any]) -> Dict[str, Any]:
    """coding モードの添付関連設定。"""
    modes = llm_config.get("modes") or {}
    coding = modes.get("coding") if isinstance(modes, dict) else {}
    if not isinstance(coding, dict):
        coding = {}
    return {
        "attach_as_file_threshold": int(coding.get("attach_as_file_threshold", 1800)),
        "prefer_py_extension": bool(coding.get("prefer_py_extension", True)),
        "ask_attach_if_over": bool(coding.get("ask_attach_if_over", True)),
        "max_attached_text_chars": int(coding.get("max_attached_text_chars", 50000)),
    }


def unsupported_message(lang: str, reason: str = "") -> str:
    """能力外の正直な拒否文。"""
    if lang.startswith("ja"):
        base = (
            "すみません、その操作はこのボットでは実行できません。"
            "できること・できないことを偽って完了したようには言いません。"
        )
    else:
        base = (
            "Sorry — I cannot perform that action with this bot's capabilities. "
            "I will not pretend it was completed."
        )
    if reason:
        return f"{base}\n({reason})"
    return base


def image_gen_disabled_message(lang: str) -> str:
    """画像生成無効時の案内。"""
    if lang.startswith("ja"):
        return (
            "画像生成機能は現在無効です（設定でオフになっています）。"
            "代わりに画像検索などのスラッシュコマンドをご利用ください。"
            "PLANA の `/help` または `/invite` からコマンド一覧を確認できます。"
        )
    return (
        "Image generation is currently disabled in config. "
        "Please use related slash commands (e.g. image search) instead. "
        "Check PLANA `/help` or `/invite` for available commands."
    )


async def run_routed_response(
    cog: "LLMCog",
    *,
    user_text: str,
    channel_id: int,
) -> RouteResult:
    """分類のみ実行して RouteResult を返す（実行本体は llm_cog 側）。"""
    # 分類を呼ぶ
    return await classify_request(cog, user_text=user_text, channel_id=channel_id)


async def execute_command_baton(
    cog: "LLMCog",
    *,
    user_text: str,
    lang: str,
    channel: Any,
    author: Any,
) -> str:
    """command モードのバトンタッチ実行。"""
    return await run_command_mode(
        cog,
        user_text=user_text,
        lang=lang,
        channel=channel,
        author=author,
    )
