# MOMOKA/notifications/earthquake_settings_view.py
# /earthquake_settings 用 Components V2（簡易 / 詳細）設定 UI。
from __future__ import annotations

import logging
from enum import Enum
from typing import TYPE_CHECKING, List, Optional

import discord
from discord.ext import commands

from MOMOKA.notifications.earthquake_notification_cog import (
    ALL_NOTIFY_SCALES,
    InfoType,
    NOTIFY_SCALE_LABELS,
)

if TYPE_CHECKING:
    from MOMOKA.notifications.earthquake_notification_cog import EarthquakeTsunamiCog

logger = logging.getLogger(__name__)


class _Page(str, Enum):
    """設定 UI のページ種別。"""

    SIMPLE = "simple"
    DETAILED = "detailed"


class EarthquakeSettingsView(discord.ui.LayoutView):
    """地震・津波通知の簡易 / 詳細設定画面。"""

    def __init__(
        self,
        bot: commands.Bot,
        cog: "EarthquakeTsunamiCog",
        guild: discord.Guild,
        *,
        timeout: Optional[float] = 600,
    ) -> None:
        # タイムアウト付きで初期化する
        super().__init__(timeout=timeout)
        # Bot 参照
        self.bot = bot
        # 地震 cog 参照
        self.cog = cog
        # 対象ギルド
        self.guild = guild
        # ギルド ID 文字列
        self.guild_id = str(guild.id)
        # 初期は簡易ページ
        self.page = _Page.SIMPLE
        # UI を組み立てる
        self._rebuild()

    def _rebuild(self) -> None:
        """現在状態から LayoutView を組み直す。"""
        # 既存アイテムを消す
        self.clear_items()
        # 地震向けのアクセント色（橙）
        accent = discord.Color.from_rgb(230, 126, 34)
        # コンテナ
        container = discord.ui.Container(accent_color=accent)
        # ページ分岐
        if self.page == _Page.SIMPLE:
            # 簡易ページ
            self._build_simple(container)
        else:
            # 詳細ページ
            self._build_detailed(container)
        # ルートに載せる
        self.add_item(container)

    def _channel_label(self, info_type: str) -> str:
        """設定済みチャンネルを表示用文字列にする。"""
        # ギルド設定
        guild_config = self.cog.config.get(self.guild_id) or {}
        # チャンネル ID
        channel_id = guild_config.get(info_type)
        # 未設定
        if not channel_id:
            return "未設定"
        # 解決を試みる
        channel = self.guild.get_channel(int(channel_id))
        # 見つかればメンション
        if channel is not None:
            return channel.mention
        # 削除済み
        return f"削除済み (`{channel_id}`)"

    def _build_simple(self, container: discord.ui.Container) -> None:
        """簡易設定ページ（通知先統一）。"""
        # 現状テキスト
        body = (
            "### 地震通知設定（簡易）\n"
            "3種類の通知先を **同じチャンネル** にまとめて設定します。\n"
            "震度フィルタ・津波オンオフは **詳細設定** で変更できます。\n\n"
            f"- EEW: {self._channel_label(InfoType.EEW.value)}\n"
            f"- 地震情報: {self._channel_label(InfoType.QUAKE.value)}\n"
            f"- 津波: {self._channel_label(InfoType.TSUNAMI.value)}\n"
            f"- 津波通知: {'オン' if self.cog.get_notify_tsunami(self.guild_id) else 'オフ'}"
        )
        # TextDisplay
        container.add_item(discord.ui.TextDisplay(body))
        # 区切り
        container.add_item(discord.ui.Separator())
        # 統一チャンネル選択
        ch_row = discord.ui.ActionRow()
        # ChannelSelect
        ch_select = discord.ui.ChannelSelect(
            placeholder="通知先チャンネル（3種まとめて）",
            channel_types=[
                discord.ChannelType.text,
                discord.ChannelType.news,
            ],
            min_values=1,
            max_values=1,
            custom_id="eq_simple_channel",
        )
        # コールバック
        ch_select.callback = self._on_simple_channel
        # 行に追加
        ch_row.add_item(ch_select)
        # コンテナへ
        container.add_item(ch_row)
        # ナビ
        nav = discord.ui.ActionRow()
        # 詳細へ
        to_detail = discord.ui.Button(
            label="詳細設定へ",
            style=discord.ButtonStyle.primary,
            custom_id="eq_goto_detailed",
        )
        # コールバック
        to_detail.callback = self._goto_detailed
        # 行に追加
        nav.add_item(to_detail)
        # コンテナへ
        container.add_item(nav)

    def _build_detailed(self, container: discord.ui.Container) -> None:
        """詳細設定ページ（チャンネル分離・震度・津波）。"""
        # EEW / 通常の震度要約
        eew_scales = self.cog.format_scales_summary(self.guild_id, InfoType.EEW.value)
        quake_scales = self.cog.format_scales_summary(self.guild_id, InfoType.QUAKE.value)
        # 津波状態
        tsunami_on = self.cog.get_notify_tsunami(self.guild_id)
        # 本文
        body = (
            "### 地震通知設定（詳細）\n"
            "通知先・震度レベル・津波通知を個別に設定します。\n\n"
            f"- EEW: {self._channel_label(InfoType.EEW.value)}\n"
            f"- 地震情報: {self._channel_label(InfoType.QUAKE.value)}\n"
            f"- 津波: {self._channel_label(InfoType.TSUNAMI.value)}\n"
            f"- EEW 震度: {eew_scales}\n"
            f"- 地震情報 震度: {quake_scales}\n"
            f"- 津波通知: {'オン' if tsunami_on else 'オフ'}"
        )
        # TextDisplay
        container.add_item(discord.ui.TextDisplay(body))
        # 区切り
        container.add_item(discord.ui.Separator())
        # EEW チャンネル
        self._add_channel_select(
            container,
            placeholder="EEW の通知チャンネル",
            custom_id="eq_ch_eew",
            info_type=InfoType.EEW.value,
        )
        # 通常チャンネル
        self._add_channel_select(
            container,
            placeholder="地震情報の通知チャンネル",
            custom_id="eq_ch_quake",
            info_type=InfoType.QUAKE.value,
        )
        # 津波チャンネル
        self._add_channel_select(
            container,
            placeholder="津波の通知チャンネル",
            custom_id="eq_ch_tsunami",
            info_type=InfoType.TSUNAMI.value,
        )
        # EEW 震度 Multi Select
        self._add_scale_select(
            container,
            placeholder="EEW で通知する震度",
            custom_id="eq_scales_eew",
            info_type=InfoType.EEW.value,
        )
        # 通常震度 Multi Select
        self._add_scale_select(
            container,
            placeholder="地震情報で通知する震度",
            custom_id="eq_scales_quake",
            info_type=InfoType.QUAKE.value,
        )
        # 津波トグル + 簡易へ戻る
        action = discord.ui.ActionRow()
        # 津波トグル
        tsu_btn = discord.ui.Button(
            label=f"津波通知: {'オン' if tsunami_on else 'オフ'}",
            style=discord.ButtonStyle.success if tsunami_on else discord.ButtonStyle.secondary,
            custom_id="eq_toggle_tsunami",
        )
        # コールバック
        tsu_btn.callback = self._toggle_tsunami
        # 行に追加
        action.add_item(tsu_btn)
        # 簡易へ
        to_simple = discord.ui.Button(
            label="簡易設定へ戻る",
            style=discord.ButtonStyle.primary,
            custom_id="eq_goto_simple",
        )
        # コールバック
        to_simple.callback = self._goto_simple
        # 行に追加
        action.add_item(to_simple)
        # コンテナへ
        container.add_item(action)

    def _add_channel_select(
        self,
        container: discord.ui.Container,
        *,
        placeholder: str,
        custom_id: str,
        info_type: str,
    ) -> None:
        """種別ごとの ChannelSelect を1行追加する。"""
        # ActionRow
        row = discord.ui.ActionRow()
        # ChannelSelect
        select = discord.ui.ChannelSelect(
            placeholder=placeholder,
            channel_types=[
                discord.ChannelType.text,
                discord.ChannelType.news,
            ],
            min_values=1,
            max_values=1,
            custom_id=custom_id,
        )
        # コールバックを種別付きで生成
        select.callback = self._make_channel_cb(info_type)
        # 行へ
        row.add_item(select)
        # コンテナへ
        container.add_item(row)

    def _add_scale_select(
        self,
        container: discord.ui.Container,
        *,
        placeholder: str,
        custom_id: str,
        info_type: str,
    ) -> None:
        """震度 Multi Select を1行追加する。"""
        # 現在の選択
        current = set(self.cog.get_notify_scales(self.guild_id, info_type))
        # オプション生成
        options: List[discord.SelectOption] = []
        for code in ALL_NOTIFY_SCALES:
            # ラベル
            label = NOTIFY_SCALE_LABELS.get(code, str(code))
            # value は文字列（-1 も可）
            options.append(
                discord.SelectOption(
                    label=label,
                    value=str(code),
                    default=(code in current),
                )
            )
        # ActionRow
        row = discord.ui.ActionRow()
        # String Select（複数）
        select = discord.ui.Select(
            placeholder=placeholder,
            options=options,
            min_values=0,
            max_values=len(options),
            custom_id=custom_id,
        )
        # コールバック
        select.callback = self._make_scales_cb(info_type)
        # 行へ
        row.add_item(select)
        # コンテナへ
        container.add_item(row)

    async def _ensure_manage(self, interaction: discord.Interaction) -> bool:
        """Manage Guild 権限を確認する。"""
        # メンバー権限
        perms = getattr(interaction.user, "guild_permissions", None)
        # 無ければ拒否
        if perms is None or not perms.manage_guild:
            # エラー返信
            await interaction.response.send_message(
                "この操作には「サーバー管理」権限が必要です。",
                ephemeral=True,
            )
            return False
        return True

    async def _on_simple_channel(self, interaction: discord.Interaction) -> None:
        """簡易モード: 3種を同一チャンネルに設定する。"""
        # 権限チェック
        if not await self._ensure_manage(interaction):
            return
        # 選択 ID 一覧
        raw_values = interaction.data.get("values", [])  # type: ignore[union-attr]
        # 未選択なら拒否
        if not raw_values:
            await interaction.response.send_message("チャンネルが選択されていません。", ephemeral=True)
            return
        # 先頭 ID を保存
        self.cog.set_channels_unified(self.guild_id, int(raw_values[0]))
        # UI 再構築
        self._rebuild()
        # メッセージ更新
        await interaction.response.edit_message(view=self)

    def _make_channel_cb(self, info_type: str):
        """種別付きチャンネル選択コールバックを返す。"""

        async def _callback(interaction: discord.Interaction) -> None:
            # 権限チェック
            if not await self._ensure_manage(interaction):
                return
            # 選択 ID
            raw_values = interaction.data.get("values", [])  # type: ignore[union-attr]
            if not raw_values:
                await interaction.response.send_message("チャンネルが選択されていません。", ephemeral=True)
                return
            # 保存
            self.cog.set_channel_for_type(self.guild_id, info_type, int(raw_values[0]))
            # 再構築
            self._rebuild()
            # 更新
            await interaction.response.edit_message(view=self)

        return _callback

    def _make_scales_cb(self, info_type: str):
        """種別付き震度 Multi Select コールバックを返す。"""

        async def _callback(interaction: discord.Interaction) -> None:
            # 権限チェック
            if not await self._ensure_manage(interaction):
                return
            # 選択値（文字列）
            raw_values = interaction.data.get("values", [])  # type: ignore[union-attr]
            # int 化
            scales = []
            for v in raw_values:
                try:
                    scales.append(int(v))
                except (TypeError, ValueError):
                    continue
            # 保存
            self.cog.set_notify_scales(self.guild_id, info_type, scales)
            # 再構築
            self._rebuild()
            # 更新
            await interaction.response.edit_message(view=self)

        return _callback

    async def _toggle_tsunami(self, interaction: discord.Interaction) -> None:
        """津波通知のオン/オフを切り替える。"""
        # 権限チェック
        if not await self._ensure_manage(interaction):
            return
        # 現在値を反転
        current = self.cog.get_notify_tsunami(self.guild_id)
        # 保存
        self.cog.set_notify_tsunami(self.guild_id, not current)
        # 再構築
        self._rebuild()
        # 更新
        await interaction.response.edit_message(view=self)

    async def _goto_detailed(self, interaction: discord.Interaction) -> None:
        """詳細ページへ移動する。"""
        # 権限チェック
        if not await self._ensure_manage(interaction):
            return
        # ページ切替
        self.page = _Page.DETAILED
        # 再構築
        self._rebuild()
        # 更新
        await interaction.response.edit_message(view=self)

    async def _goto_simple(self, interaction: discord.Interaction) -> None:
        """簡易ページへ戻る。"""
        # 権限チェック
        if not await self._ensure_manage(interaction):
            return
        # ページ切替
        self.page = _Page.SIMPLE
        # 再構築
        self._rebuild()
        # 更新
        await interaction.response.edit_message(view=self)
