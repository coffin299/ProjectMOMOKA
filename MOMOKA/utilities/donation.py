# Ko-fi など寄付リンク表示の共通ヘルパ。
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional

import discord


@dataclass(frozen=True)
class DonationSettings:
    """寄付表示設定。"""

    enabled: bool
    url: str
    subtle_label: str
    help_button_label: str
    help_text_ja: str
    help_text_en: str


# 既定値（config 欠落時・個人 URL は置かない）
_DEFAULT = DonationSettings(
    enabled=False,
    url="",
    subtle_label="Buy me a coffee...",
    help_button_label="Ko-fi / Support",
    help_text_ja=(
        "このBotはボランティアで開発・運用されています。"
        "サーバー代の足しになるので、よければKo-fiで支援していただけると嬉しいです。"
    ),
    help_text_en=(
        "This bot is developed and hosted as a volunteer project. "
        "If you'd like to help cover server costs, a small tip on Ko-fi is always appreciated."
    ),
)


def load_donation_settings(config: Optional[Dict[str, Any]]) -> DonationSettings:
    """bot.config / マージ済み config から donation 設定を読む。"""
    # config が無ければ既定
    raw = (config or {}).get("donation") or {}
    if not isinstance(raw, dict):
        return _DEFAULT
    # URL を正規化する
    url = str(raw.get("url") or _DEFAULT.url).strip()
    # http で始まらなければ無効扱い（enabled でもボタンを出さない）
    enabled = bool(raw.get("enabled", _DEFAULT.enabled)) and url.startswith("http")
    # 各文言
    return DonationSettings(
        enabled=enabled,
        url=url,
        subtle_label=str(raw.get("subtle_label") or _DEFAULT.subtle_label).strip()
        or _DEFAULT.subtle_label,
        help_button_label=str(
            raw.get("help_button_label") or _DEFAULT.help_button_label
        ).strip()
        or _DEFAULT.help_button_label,
        help_text_ja=str(raw.get("help_text_ja") or _DEFAULT.help_text_ja).strip()
        or _DEFAULT.help_text_ja,
        help_text_en=str(raw.get("help_text_en") or _DEFAULT.help_text_en).strip()
        or _DEFAULT.help_text_en,
    )


def donation_from_bot(bot: Any) -> DonationSettings:
    """commands.Bot の config 属性から読む。"""
    # bot.config を優先する
    return load_donation_settings(getattr(bot, "config", None) or {})


def make_subtle_link_button(settings: DonationSettings) -> Optional[discord.ui.Button]:
    """控えめなリンクボタン（LLM / 音楽用）。無効時は None。"""
    # 無効なら出さない
    if not settings.enabled:
        return None
    # リンクボタンを返す
    return discord.ui.Button(
        label=settings.subtle_label[:80],
        style=discord.ButtonStyle.link,
        url=settings.url,
    )


def make_subtle_donation_view(settings: DonationSettings) -> Optional[discord.ui.View]:
    """クラシック View に控えめリンクだけ載せたもの。無効時は None。"""
    # ボタンを作る
    btn = make_subtle_link_button(settings)
    if btn is None:
        return None
    # View に載せる
    view = discord.ui.View(timeout=None)
    view.add_item(btn)
    return view


def make_help_link_button(settings: DonationSettings) -> Optional[discord.ui.Button]:
    """help 用のやや目立つリンクボタン。無効時は None。"""
    # 無効なら出さない
    if not settings.enabled:
        return None
    # ラベルは help 用
    return discord.ui.Button(
        label=settings.help_button_label[:80],
        style=discord.ButtonStyle.link,
        url=settings.url,
        emoji="☕",
    )


def help_donation_body(settings: DonationSettings, lang: str = "en") -> str:
    """/help に載せる支援文（選択言語のみ）。"""
    # 日本語なら JA 文
    if lang == "ja":
        # 見出し＋日本語本文
        return f"**☕ 支援**\n{settings.help_text_ja}"
    # それ以外は英語
    return f"**☕ Support**\n{settings.help_text_en}"
