# MOMOKA/utilities/locale.py
# Components V2 / Modal 用 UI 言語解決（app locale → guild locale → en）。
from __future__ import annotations

from typing import Any, Optional

import discord


def lang_from_discord_locale(locale: Any) -> str:
    """Discord locale から UI 用言語コード（ja / en）を返す。"""
    # 未指定は英語へフォールバックする
    if locale is None:
        return "en"
    # Locale enum 等は value を優先する
    raw = getattr(locale, "value", None)
    # value が無ければ文字列化する
    key = str(raw if raw is not None else locale).strip().lower()
    # 空文字は未取得扱いとする
    if not key:
        return "en"
    # ja* なら日本語
    if key.startswith("ja"):
        return "ja"
    # それ以外は英語
    return "en"


def resolve_ui_lang(
    *,
    app_locale: Any = None,
    guild_locale: Any = None,
) -> str:
    """app locale → guild locale → en の順で UI 言語を決める。"""
    # app locale が取れたらそれを最優先する（非 ja でも guild へは落とさない）
    if app_locale is not None and str(
        getattr(app_locale, "value", app_locale) or ""
    ).strip():
        return lang_from_discord_locale(app_locale)
    # guild locale があれば次に使う
    if guild_locale is not None and str(
        getattr(guild_locale, "value", guild_locale) or ""
    ).strip():
        return lang_from_discord_locale(guild_locale)
    # どちらも無ければ英語
    return "en"


def resolve_interaction_lang(interaction: discord.Interaction) -> str:
    """Interaction から UI 言語を解決する（app → guild → en）。"""
    # クライアント言語（app locale）
    app_locale = getattr(interaction, "locale", None)
    # ギルドの優先言語
    guild = getattr(interaction, "guild", None)
    guild_locale = getattr(guild, "preferred_locale", None) if guild else None
    # カスケードで決める
    return resolve_ui_lang(app_locale=app_locale, guild_locale=guild_locale)


def resolve_guild_lang(guild: Optional[discord.Guild]) -> str:
    """ギルドのみから UI 言語を解決する（メッセージ起点 UI 用）。"""
    # ギルドが無ければ英語
    if guild is None:
        return "en"
    # preferred_locale を読む
    return resolve_ui_lang(guild_locale=getattr(guild, "preferred_locale", None))


def pick_str(lang: str, *, ja: str, en: str) -> str:
    """lang に応じて日英どちらかの文字列を返す。"""
    # ja 以外はすべて en 扱い
    return ja if lang == "ja" else en
