# MOMOKA/utilities/help_view.py
# /help と /invite 用 Components V2 LayoutView（日英切替・ページング）。
from __future__ import annotations

import logging
from typing import Any, Dict, Optional, Tuple

import discord
from discord.ext import commands

from MOMOKA.utilities.donation import (
    donation_from_bot,
    help_donation_body,
    make_help_link_button,
)

logger = logging.getLogger(__name__)

# ページ総数（0..7）
_HELP_PAGE_COUNT = 8

# 無効とみなす招待 URL プレースホルダ
_INVALID_INVITE_PLACEHOLDERS = frozenset(
    {
        "",
        "YOUR_BOT_INVITE_LINK_HERE",
        "HOGE_FUGA_PIYO",
    }
)


def _is_valid_invite_url(url: Optional[str]) -> bool:
    """招待 URL が実運用可能か判定する。"""
    # 空や非文字列は無効
    if not url or not isinstance(url, str):
        return False
    # 前後空白を除去する
    cleaned = url.strip()
    # http(s) で始まらなければリンクボタンに使えない
    if not cleaned.startswith("http"):
        return False
    # 既知プレースホルダは無効
    if cleaned in _INVALID_INVITE_PLACEHOLDERS:
        return False
    # YOUR_ を含む未設定値も無効
    if "YOUR_" in cleaned.upper():
        return False
    # ここまで来たら有効
    return True


def resolve_invite_urls(bot: commands.Bot) -> Tuple[str, str]:
    """bots.plana / bots.arona の invite_url を返す（無ければ空文字）。"""
    # config 辞書を取得する（無ければ空）
    config: Dict[str, Any] = getattr(bot, "config", None) or {}
    # bots セクションを読む
    bots = config.get("bots") or {}
    # PLANA エントリ
    plana = bots.get("plana") or {}
    # ARONA エントリ
    arona = bots.get("arona") or {}
    # 各 invite_url（無ければ空）
    plana_url = str(plana.get("invite_url") or "").strip()
    arona_url = str(arona.get("invite_url") or "").strip()
    # タプルで返す
    return plana_url, arona_url


def _is_companion(bot: commands.Bot) -> bool:
    """companion（ARONA）ロールかどうか。"""
    # bot_role 属性を読む（未設定時は primary）
    return getattr(bot, "bot_role", "primary") == "companion"


def lang_from_discord_locale(locale: Any) -> str:
    """Discord locale から help 用言語コード（ja / en）を返す。"""
    # 未指定は英語
    if locale is None:
        return "en"
    # Locale enum 等は value を優先する
    raw = getattr(locale, "value", None)
    # value が無ければ文字列化
    key = str(raw if raw is not None else locale).strip().lower()
    # ja* なら日本語
    if key.startswith("ja"):
        return "ja"
    # それ以外は英語
    return "en"


class HelpLayoutView(discord.ui.LayoutView):
    """ /help 用ページング LayoutView（Components V2・日英切替）。"""

    def __init__(
        self,
        bot: commands.Bot,
        *,
        page: int = 0,
        lang: str = "en",
    ) -> None:
        # 長時間表示するためタイムアウトなし
        super().__init__(timeout=None)
        # Bot 参照を保持する
        self.bot = bot
        # 招待 URL を config から解決する
        self.plana_invite, self.arona_invite = resolve_invite_urls(bot)
        # companion なら PLANA 誘導注記を出す
        self.is_companion = _is_companion(bot)
        # 言語を正規化する（ja 以外は en）
        self.lang = "ja" if lang == "ja" else "en"
        # ページ番号を範囲内に正規化する
        self.page = max(0, min(int(page), _HELP_PAGE_COUNT - 1))
        # UI を組み立てる
        self._rebuild()

    def _rebuild(self) -> None:
        """現在ページ・言語の TextDisplay / ボタンを再構築する。"""
        # 既存アイテムを全部消す
        self.clear_items()
        # アクセント色（ティール系）
        accent = discord.Color.teal()
        # コンテナを作る
        container = discord.ui.Container(accent_color=accent)
        # ページ本文を取得する
        body = self._page_text(self.page)
        # TextDisplay で本文を載せる
        container.add_item(discord.ui.TextDisplay(body))
        # Overview のみ招待リンク行を付ける
        if self.page == 0:
            # 招待用 ActionRow
            invite_row = discord.ui.ActionRow()
            # ボタン追加有無フラグ
            has_invite_btn = False
            # PLANA 招待が有効ならリンクボタンを追加
            if _is_valid_invite_url(self.plana_invite):
                invite_row.add_item(
                    discord.ui.Button(
                        label="Invite PLANA",
                        style=discord.ButtonStyle.link,
                        url=self.plana_invite,
                        emoji="💌",
                    )
                )
                # 追加済みにする
                has_invite_btn = True
            # ARONA 招待が有効ならリンクボタンを追加
            if _is_valid_invite_url(self.arona_invite):
                invite_row.add_item(
                    discord.ui.Button(
                        label="Invite ARONA",
                        style=discord.ButtonStyle.link,
                        url=self.arona_invite,
                        emoji="🎵",
                    )
                )
                # 追加済みにする
                has_invite_btn = True
            # ボタンが1つ以上あれば行を載せる
            if has_invite_btn:
                container.add_item(invite_row)
            # 寄付（enabled 時は堂々と本文＋ボタン）
            donation = donation_from_bot(self.bot)
            if donation.enabled:
                # 選択言語の支援文を載せる
                container.add_item(
                    discord.ui.TextDisplay(help_donation_body(donation, self.lang))
                )
                # Ko-fi リンクボタン行
                donation_row = discord.ui.ActionRow()
                help_btn = make_help_link_button(donation)
                if help_btn is not None:
                    donation_row.add_item(help_btn)
                    container.add_item(donation_row)
        # 言語切替行（絵文字ボタン）
        lang_row = discord.ui.ActionRow()
        # 日本語ボタン（選択中は primary）
        ja_btn = discord.ui.Button(
            emoji="🇯🇵",
            style=(
                discord.ButtonStyle.primary
                if self.lang == "ja"
                else discord.ButtonStyle.secondary
            ),
            custom_id="help_lang_ja",
        )
        # コールバックを結ぶ
        ja_btn.callback = self._lang_ja_callback
        # 行に追加
        lang_row.add_item(ja_btn)
        # 英語ボタン（選択中は primary）
        en_btn = discord.ui.Button(
            emoji="🇺🇸",
            style=(
                discord.ButtonStyle.primary
                if self.lang == "en"
                else discord.ButtonStyle.secondary
            ),
            custom_id="help_lang_en",
        )
        # コールバックを結ぶ
        en_btn.callback = self._lang_en_callback
        # 行に追加
        lang_row.add_item(en_btn)
        # 言語行をコンテナへ
        container.add_item(lang_row)
        # Prev / Next 用 ActionRow
        nav_row = discord.ui.ActionRow()
        # 前へラベル（言語別）
        prev_label = "◀ 前へ" if self.lang == "ja" else "◀ Prev"
        # 前へボタン（先頭ページでは無効）
        prev_btn = discord.ui.Button(
            label=prev_label,
            style=discord.ButtonStyle.secondary,
            custom_id="help_prev",
            disabled=(self.page <= 0),
        )
        # コールバックを結ぶ
        prev_btn.callback = self._prev_callback
        # 行に追加
        nav_row.add_item(prev_btn)
        # ページ表示用の無効ボタン（ラベルのみ）
        page_btn = discord.ui.Button(
            label=f"{self.page + 1}/{_HELP_PAGE_COUNT}",
            style=discord.ButtonStyle.primary,
            custom_id="help_page_indicator",
            disabled=True,
        )
        # 行に追加
        nav_row.add_item(page_btn)
        # 次へラベル（言語別）
        next_label = "次へ ▶" if self.lang == "ja" else "Next ▶"
        # 次へボタン（最終ページでは無効）
        next_btn = discord.ui.Button(
            label=next_label,
            style=discord.ButtonStyle.secondary,
            custom_id="help_next",
            disabled=(self.page >= _HELP_PAGE_COUNT - 1),
        )
        # コールバックを結ぶ
        next_btn.callback = self._next_callback
        # 行に追加
        nav_row.add_item(next_btn)
        # ナビ行をコンテナへ
        container.add_item(nav_row)
        # ルートにコンテナを追加
        self.add_item(container)

    def _page_text(self, page: int) -> str:
        """ページ番号に応じた単一言語本文。"""
        # ページ分岐
        if page == 0:
            return self._overview_text()
        if page == 1:
            return self._llm_text()
        if page == 2:
            return self._music_download_text()
        if page == 3:
            return self._linkfix_text()
        if page == 4:
            return self._twitch_text()
        if page == 5:
            return self._more_plana_text()
        if page == 6:
            return self._utilities_text()
        return self._guidelines_text()

    def _overview_text(self) -> str:
        """Page 0: Overview（主要4機能を先頭に）。"""
        # Bot 表示名
        bot_name = self.bot.user.name if self.bot.user else "MOMOKA"
        # 日本語 Overview
        if self.lang == "ja":
            # companion 向け
            if self.is_companion:
                features = (
                    f"**この Bot（{bot_name} / ARONA）で使える機能**\n"
                    "1. **AI / LLM** — メンション・`/chat`\n"
                    "2. **Music** — `/play` など\n"
                    "3. ユーティリティ — `/help`・`/invite` など\n\n"
                    "**PLANA 専用（こちらでは使えません）**\n"
                    "• Link Fix（Embed）/ Twitch 配信通知 / メディアダウンロード\n"
                    "• TTS / 画像検索 / 地震通知 など\n"
                    "→ `/invite` から PLANA を追加してください。"
                )
            else:
                # PLANA: 主要4つを先に
                features = (
                    f"**この Bot（{bot_name} / PLANA）の主な機能**\n"
                    "1. **AI / LLM** — メンション対話・ツール\n"
                    "2. **Music** — `/play` 再生 ＋ `/download_*` 取得\n"
                    "3. **Link Fix** — SNS の Embed を Fix URL で置換\n"
                    "4. **Twitch** — 配信開始の Discord 通知\n\n"
                    "ほか: TTS / 画像検索 / 地震通知 / タイマー など"
                )
            # 表紙を返す
            return (
                "### 📜 MOMOKA Help — 概要\n"
                "**MOMOKA** は PLANA + ARONA のマルチ Bot 基盤です。\n\n"
                f"{features}\n\n"
                "🇯🇵 / 🇺🇸 で言語切替  |  前へ / 次へ でページ移動"
            )
        # 英語 Overview
        if self.is_companion:
            features_en = (
                f"**Available on this bot ({bot_name} / ARONA)**\n"
                "1. **AI / LLM** — mention / `/chat`\n"
                "2. **Music** — `/play`, etc.\n"
                "3. Utilities — `/help`, `/invite`, …\n\n"
                "**PLANA-only (not on ARONA)**\n"
                "• Link Fix (Embed) / Twitch stream alerts / media download\n"
                "• TTS / image search / earthquake alerts, etc.\n"
                "→ Invite PLANA via `/invite`."
            )
        else:
            features_en = (
                f"**Main features on this bot ({bot_name} / PLANA)**\n"
                "1. **AI / LLM** — mention chat & tools\n"
                "2. **Music** — `/play` plus `/download_*`\n"
                "3. **Link Fix** — replace SNS embeds with Fix URLs\n"
                "4. **Twitch** — Discord alerts when a stream goes live\n\n"
                "Also: TTS / image search / earthquake alerts / timers, …"
            )
        return (
            "### 📜 MOMOKA Help — Overview\n"
            "**MOMOKA** is the multi-bot platform powering **PLANA** and **ARONA**.\n\n"
            f"{features_en}\n\n"
            "🇯🇵 / 🇺🇸 switch language  |  Prev / Next to browse pages"
        )

    def _llm_text(self) -> str:
        """Page 1: LLM。"""
        # companion 注記
        if self.lang == "ja":
            note = ""
            if self.is_companion:
                note = (
                    "\n\n⚠️ 画像生成などの高度ツールは **PLANA 専用**です。"
                    "\n→ `/invite` から PLANA を追加してください。"
                )
            return (
                "### 🤖 AI / LLM\n"
                "**使い方**\n"
                "• Bot をメンションして話しかける\n"
                "• Bot の返信にリプライで会話を続ける\n"
                "• `/chat <message>` — メンションなしの単発対話（履歴なし）\n\n"
                "**コマンド**\n"
                "• `/switch-models` — チャンネルの AI モデル切替\n"
                "• `/switch-models-default-server` — サーバー既定モデル\n"
                "• `/clear_history` — 会話履歴リセット\n"
                "• `/switch-image-model` `/show-image-model` `/list-image-models`"
                " — 画像モデル（**PLANA**）\n\n"
                "**メモ**\n"
                "• 対応モデルなら画像添付を認識できます\n"
                "• 履歴はチャンネル単位で保持されます\n"
                "• 討論（debate）/ クロスチェックはメンション経由のツール呼び出し"
                f"{note}"
            )
        note_en = ""
        if self.is_companion:
            note_en = (
                "\n\n⚠️ Advanced tools such as image generation are **PLANA-only**."
                "\n→ Invite PLANA via `/invite`."
            )
        return (
            "### 🤖 AI / LLM\n"
            "**How to use**\n"
            "• Mention the bot to chat\n"
            "• Reply to bot messages to continue\n"
            "• `/chat <message>` — one-shot chat without mention (no history)\n\n"
            "**Commands**\n"
            "• `/switch-models` — switch channel AI model\n"
            "• `/switch-models-default-server` — server default model\n"
            "• `/clear_history` — reset conversation history\n"
            "• `/switch-image-model` `/show-image-model` `/list-image-models`"
            " — image models (**PLANA**)\n\n"
            "**Notes**\n"
            "• Vision-capable models can read attached images\n"
            "• History is kept per channel\n"
            "• Debate / cross_check run as mention-triggered tools"
            f"{note_en}"
        )

    def _music_download_text(self) -> str:
        """Page 2: Music + Download。"""
        if self.lang == "ja":
            return (
                "### 🎵 Music / Download\n"
                "音楽再生は **PLANA / ARONA** 両方で利用できます。\n"
                "メディアダウンロードは **PLANA 専用**です。\n\n"
                "**再生**\n"
                "`/play <名前|URL>` — 再生 / キュー追加\n"
                "`/pause` `/resume` `/stop` `/skip` — 一時停止・再開・停止・スキップ\n"
                "`/seek <time>` `/volume <0-200>` — シーク・音量\n\n"
                "**キュー**\n"
                "`/queue` `/nowplaying` `/shuffle` `/clear` `/remove` `/loop`\n\n"
                "**ボイス**\n"
                "`/join` `/leave` — VC 接続・切断\n\n"
                "Now Playing: Pause / Skip / Stop（確認）/ Loop / QLoop。"
                "次曲があるときだけキュー表示（最大5曲＋ページング）。\n\n"
                "**ダウンロード（PLANA）**\n"
                "• `/download_video <query>` — 動画取得 → Google Drive 共有\n"
                "• `/download_audio <query> <format>` — 音声抽出"
                "（mp3 / m4a / opus / flac / wav）"
            )
        return (
            "### 🎵 Music / Download\n"
            "Music works on **both PLANA and ARONA**.\n"
            "Media download is **PLANA-only**.\n\n"
            "**Playback**\n"
            "`/play <name|URL>` — play or enqueue\n"
            "`/pause` `/resume` `/stop` `/skip` — pause, resume, stop, skip\n"
            "`/seek <time>` `/volume <0-200>` — seek / volume\n\n"
            "**Queue**\n"
            "`/queue` `/nowplaying` `/shuffle` `/clear` `/remove` `/loop`\n\n"
            "**Voice**\n"
            "`/join` `/leave` — join or leave VC\n\n"
            "Now Playing: Pause / Skip / Stop (confirm) / Loop / QLoop. "
            "Queue (up to 5 + paging) only when upcoming tracks exist.\n\n"
            "**Download (PLANA)**\n"
            "• `/download_video <query>` — fetch video → Google Drive share\n"
            "• `/download_audio <query> <format>` — extract audio "
            "(mp3 / m4a / opus / flac / wav)"
        )

    def _linkfix_text(self) -> str:
        """Page 3: Link Fix。"""
        if self.lang == "ja":
            note = ""
            if self.is_companion:
                note = (
                    "\n\n⚠️ Link Fix は **PLANA 専用**です。"
                    "\n→ `/invite` から PLANA を追加してください。"
                )
            return (
                "### 🔗 Link Fix（Embed）\n"
                "SNS の公式埋め込みを抑制し、Fix URL で引用置換します（**PLANA**）。\n"
                "デフォルト無効。設定は `/linkfix`（サーバー管理権限）。\n\n"
                "**一時スキップ**\n"
                "• `fxignore` を本文に含める / URL を `<>` で囲む"
                f"{note}"
            )
        note_en = ""
        if self.is_companion:
            note_en = (
                "\n\n⚠️ Link Fix is **PLANA-only**."
                "\n→ Invite PLANA via `/invite`."
            )
        return (
            "### 🔗 Link Fix (Embed)\n"
            "Suppresses original SNS embeds and quote-replies with Fix URLs "
            "(**PLANA**).\n"
            "Disabled by default. Manage via `/linkfix` (Manage Server).\n\n"
            "**Skip one message**\n"
            "• Include `fxignore`, or wrap the URL in `<>`"
            f"{note_en}"
        )

    def _twitch_text(self) -> str:
        """Page 4: Twitch（＋地震の短記）。"""
        if self.lang == "ja":
            note = ""
            if self.is_companion:
                note = (
                    "\n\n⚠️ 通知機能は **PLANA 専用**です。"
                    "\n→ `/invite` から PLANA を追加してください。"
                )
            return (
                "### 📺 Twitch 配信開始通知\n"
                "指定チャンネルの配信開始を Discord に通知します（**PLANA**）。\n\n"
                "**コマンド**\n"
                "• `/twitch_set` — Twitch URL と通知先チャンネルを設定\n"
                "• `/twitch_remove` — 設定を削除\n"
                "• `/twitch_list` — 設定一覧\n"
                "• `/twitch_test` — テスト通知\n\n"
                "**その他の通知**\n"
                "地震・津波: `/earthquake_channel` `/earthquake_status` "
                "`/earthquake_help` など"
                f"{note}"
            )
        note_en = ""
        if self.is_companion:
            note_en = (
                "\n\n⚠️ Notifications are **PLANA-only**."
                "\n→ Invite PLANA via `/invite`."
            )
        return (
            "### 📺 Twitch stream-start alerts\n"
            "Posts to Discord when a watched channel goes live (**PLANA**).\n\n"
            "**Commands**\n"
            "• `/twitch_set` — set Twitch URL and Discord channel\n"
            "• `/twitch_remove` — remove a setting\n"
            "• `/twitch_list` — list settings\n"
            "• `/twitch_test` — send a test notification\n\n"
            "**Other notifications**\n"
            "Earthquake/tsunami: `/earthquake_channel` `/earthquake_status` "
            "`/earthquake_help`, …"
            f"{note_en}"
        )

    def _more_plana_text(self) -> str:
        """Page 5: その他 PLANA 機能（トラッカー除外）。"""
        if self.lang == "ja":
            note = ""
            if self.is_companion:
                note = (
                    "\n\n⚠️ このページの機能は **PLANA 専用**です。"
                    "\n→ `/invite` から PLANA を追加してください。"
                )
            return (
                "### 🧩 その他（PLANA）\n"
                "**TTS（読み上げ）**\n"
                "• `/say` — テキスト読み上げ\n"
                "• `/speech enable|disable|skip` — チャンネル読み上げ\n"
                "• `/tts volume` `/autojoin` `/dictionary`\n\n"
                "**画像検索**\n"
                "• `/meow` `/yandere-safe` `/danbooru-safe`\n\n"
                "**タイマー・対戦時間**\n"
                "• `/timer start` `/timer stop`\n"
                "• `/match_time` — 対戦時間の調整"
                f"{note}"
            )
        note_en = ""
        if self.is_companion:
            note_en = (
                "\n\n⚠️ Features on this page are **PLANA-only**."
                "\n→ Invite PLANA via `/invite`."
            )
        return (
            "### 🧩 More (PLANA)\n"
            "**TTS**\n"
            "• `/say` — speak text\n"
            "• `/speech enable|disable|skip` — channel read-aloud\n"
            "• `/tts volume` `/autojoin` `/dictionary`\n\n"
            "**Image search**\n"
            "• `/meow` `/yandere-safe` `/danbooru-safe`\n\n"
            "**Timer & match time**\n"
            "• `/timer start` `/timer stop`\n"
            "• `/match_time` — match time helper"
            f"{note_en}"
        )

    def _utilities_text(self) -> str:
        """Page 6: Utilities。"""
        if self.lang == "ja":
            return (
                "### 🛠️ ユーティリティ\n"
                "**招待・サポート**\n"
                "• `/invite` — PLANA / ARONA の招待リンク\n"
                "• `/updates` — GitHub コミット履歴\n"
                "• `/feedback` — 不具合・要望を Modal から送信\n"
                "• `/support` — 開発者への連絡方法\n"
                "• Overview の Ko-fi — サーバー代の支援\n\n"
                "**情報・その他**\n"
                "• `/gacha` `/serverinfo` `/userinfo` `/avatar`\n"
                "• `/help` — このパネル（🇯🇵/🇺🇸・ページング）"
            )
        return (
            "### 🛠️ Utilities\n"
            "**Invite & support**\n"
            "• `/invite` — PLANA / ARONA invite links\n"
            "• `/updates` — GitHub commit history\n"
            "• `/feedback` — bug/request via Modal\n"
            "• `/support` — how to contact developers\n"
            "• Ko-fi on Overview — server-cost support\n\n"
            "**Info & other**\n"
            "• `/gacha` `/serverinfo` `/userinfo` `/avatar`\n"
            "• `/help` — this panel (🇯🇵/🇺🇸 + paging)"
        )

    def _guidelines_text(self) -> str:
        """Page 7: Guidelines。"""
        if self.lang == "ja":
            if self.is_companion:
                role_note = (
                    "\n\n**ARONA について**\n"
                    "Link Fix・Twitch・TTS・画像検索・通知などは **PLANA 専用**です。\n"
                    "`/invite` または Overview の Invite PLANA から追加してください。"
                )
            else:
                role_note = (
                    "\n\n**PLANA / ARONA**\n"
                    "高度機能の多くは PLANA で提供されます。\n"
                    "ARONA は LLM + 音楽中心のコンパニオンです。"
                )
            return (
                "### 📋 ガイドライン\n"
                "**お願い**\n"
                "• 法令・Discord ToS・サーバー規則を守ってください\n"
                "• 個人情報や秘密情報をむやみに送らないでください\n"
                "• AI の回答は誤る可能性があります。重要判断は自己責任で\n"
                "• 生成コンテンツの公開・商用利用は各モデル規約に従ってください"
                f"{role_note}\n\n"
                "リポジトリ: https://github.com/coffin399/ProjectMOMOKA"
            )
        if self.is_companion:
            role_note_en = (
                "\n\n**About ARONA**\n"
                "Link Fix, Twitch, TTS, image search, and notifications "
                "are **PLANA-only**.\n"
                "Invite PLANA from `/invite` or the Overview buttons."
            )
        else:
            role_note_en = (
                "\n\n**PLANA / ARONA**\n"
                "Most advanced features are on PLANA.\n"
                "ARONA is a companion focused on LLM + music."
            )
        return (
            "### 📋 Guidelines\n"
            "**Please**\n"
            "• Follow laws, Discord ToS, and your server rules\n"
            "• Do not share personal or confidential data casually\n"
            "• AI output can be wrong; verify critical decisions yourself\n"
            "• Follow each model provider's terms for generated content"
            f"{role_note_en}\n\n"
            "Repository: https://github.com/coffin399/ProjectMOMOKA"
        )

    async def _lang_ja_callback(self, interaction: discord.Interaction) -> None:
        """日本語に切り替えてメッセージを編集する。"""
        # 既に日本語なら応答だけ
        if self.lang == "ja":
            await interaction.response.defer()
            return
        # 言語を日本語にする
        self.lang = "ja"
        # UI を組み直す
        self._rebuild()
        # 元メッセージを更新する
        await interaction.response.edit_message(view=self)

    async def _lang_en_callback(self, interaction: discord.Interaction) -> None:
        """英語に切り替えてメッセージを編集する。"""
        # 既に英語なら応答だけ
        if self.lang == "en":
            await interaction.response.defer()
            return
        # 言語を英語にする
        self.lang = "en"
        # UI を組み直す
        self._rebuild()
        # 元メッセージを更新する
        await interaction.response.edit_message(view=self)

    async def _prev_callback(self, interaction: discord.Interaction) -> None:
        """前ページへ移動してメッセージを編集する。"""
        # 先頭なら何もしない
        if self.page <= 0:
            # 応答だけ返す
            await interaction.response.defer()
            return
        # ページを1つ戻す
        self.page -= 1
        # UI を組み直す
        self._rebuild()
        # 元メッセージを view のみで更新する（V2 は embed 併用不可）
        await interaction.response.edit_message(view=self)

    async def _next_callback(self, interaction: discord.Interaction) -> None:
        """次ページへ移動してメッセージを編集する。"""
        # 末尾なら何もしない
        if self.page >= _HELP_PAGE_COUNT - 1:
            # 応答だけ返す
            await interaction.response.defer()
            return
        # ページを1つ進める
        self.page += 1
        # UI を組み直す
        self._rebuild()
        # 元メッセージを view のみで更新する
        await interaction.response.edit_message(view=self)


class InviteLayoutView(discord.ui.LayoutView):
    """ /invite 用 LayoutView（PLANA / ARONA リンク）。"""

    def __init__(self, bot: commands.Bot) -> None:
        # 長時間表示
        super().__init__(timeout=None)
        # Bot 参照
        self.bot = bot
        # 招待 URL 解決
        self.plana_invite, self.arona_invite = resolve_invite_urls(bot)
        # UI 構築
        self._rebuild()

    def _rebuild(self) -> None:
        """本文とリンクボタンを組み立てる。"""
        # クリア
        self.clear_items()
        # 有効判定
        plana_ok = _is_valid_invite_url(self.plana_invite)
        arona_ok = _is_valid_invite_url(self.arona_invite)
        # 本文（日英）
        if plana_ok or arona_ok:
            body = (
                "### 💌 Invite PLANA / ARONA\n"
                "下のボタンから Bot をサーバーに招待できます。\n"
                "Use the buttons below to invite the bots to your server.\n\n"
                "• **PLANA** — フル機能（LLM / 音楽 / TTS / Link Fix / 通知 など）\n"
                "• **ARONA** — コンパニオン（LLM / 音楽 / ユーティリティ）"
            )
        else:
            body = (
                "### 💌 Invite\n"
                "招待 URL が `bots.plana.invite_url` / `bots.arona.invite_url` に"
                "正しく設定されていません。管理者にご連絡ください。\n\n"
                "Invite URLs are not set correctly in "
                "`bots.plana.invite_url` / `bots.arona.invite_url`. "
                "Please contact the bot administrator."
            )
        # コンテナ
        container = discord.ui.Container(accent_color=discord.Color.og_blurple())
        # 本文
        container.add_item(discord.ui.TextDisplay(body))
        # リンク行
        if plana_ok or arona_ok:
            row = discord.ui.ActionRow()
            # PLANA
            if plana_ok:
                row.add_item(
                    discord.ui.Button(
                        label="Invite PLANA",
                        style=discord.ButtonStyle.link,
                        url=self.plana_invite,
                        emoji="💌",
                    )
                )
            # ARONA
            if arona_ok:
                row.add_item(
                    discord.ui.Button(
                        label="Invite ARONA",
                        style=discord.ButtonStyle.link,
                        url=self.arona_invite,
                        emoji="🎵",
                    )
                )
            # 行を載せる
            container.add_item(row)
        # ルートへ
        self.add_item(container)
