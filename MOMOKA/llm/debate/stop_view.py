# 討論中止ボタン付き LayoutView（Components V2）。
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Callable, Optional, Set

import discord

from MOMOKA.llm.debate.accents import initiator_accent_color
from MOMOKA.utilities.locale import pick_str, resolve_interaction_lang

if TYPE_CHECKING:
    from MOMOKA.llm.debate.orchestrator import DebateSession

logger = logging.getLogger(__name__)


class DebateStopView(discord.ui.LayoutView):
    """討論パネル: テーマ / 進捗 / 中止ボタン。"""

    def __init__(
        self,
        *,
        session: "DebateSession",
        admin_ids: Set[int],
        on_stop: Callable,
        timeout: Optional[float] = None,
        lang: Optional[str] = None,
    ) -> None:
        # LayoutView は長時間表示するため timeout=None
        super().__init__(timeout=timeout)
        # セッション参照
        self.session = session
        # 管理者 ID 集合
        self.admin_ids = admin_ids
        # 中止コールバック
        self.on_stop = on_stop
        # セッション保存言語があればそれを優先する
        session_lang = getattr(session, "lang", None)
        # 明示 lang → session.lang → en
        self.lang = "ja" if (lang or session_lang or "en") == "ja" else "en"
        # UI を組み立てる
        self._rebuild()

    def _rebuild(self) -> None:
        """進捗テキストとボタンを再構築する。"""
        # 既存子をクリアする（LayoutView 再構築）
        self.clear_items()
        # セッション状態を読む
        topic = self.session.topic
        status = self.session.status
        round_i = self.session.current_round
        max_r = self.session.max_rounds
        # 状態に応じた本文
        if status == "cancelled":
            body = pick_str(
                self.lang,
                ja=f"**討論を中断しました**\nテーマ: {topic}",
                en=f"**Debate cancelled**\nTopic: {topic}",
            )
            accent = discord.Color.dark_grey()
            show_button = False
        elif status == "finished":
            body = pick_str(
                self.lang,
                ja=(
                    f"**討論終了**\n"
                    f"テーマ: {topic}\n"
                    f"ラウンド: {max_r}/{max_r}"
                ),
                en=(
                    f"**Debate finished**\n"
                    f"Topic: {topic}\n"
                    f"Rounds: {max_r}/{max_r}"
                ),
            )
            accent = discord.Color.green()
            show_button = False
        else:
            body = pick_str(
                self.lang,
                ja=(
                    f"**討論中**\n"
                    f"テーマ: {topic}\n"
                    f"ラウンド: {round_i}/{max_r}\n"
                    f"中止する場合は下のボタンを押してください。"
                ),
                en=(
                    f"**Debate in progress**\n"
                    f"Topic: {topic}\n"
                    f"Round: {round_i}/{max_r}\n"
                    f"Press the button below to stop."
                ),
            )
            # 進行中は起動 Bot 色（PLANA 紫 / ARONA 水色）
            accent = initiator_accent_color(self.session.initiator_bot_id)
            show_button = True
        # コンテナを作る
        container = discord.ui.Container(accent_color=accent)
        # 本文を載せる
        container.add_item(discord.ui.TextDisplay(body))
        # 中止ボタンが必要なら ActionRow を付ける
        if show_button:
            # ActionRow を用意する
            row = discord.ui.ActionRow()
            # 中止ボタン
            btn = discord.ui.Button(
                label=pick_str(
                    self.lang,
                    ja="討論を中止",
                    en="Stop debate",
                ),
                style=discord.ButtonStyle.danger,
                custom_id=f"debate_stop:{self.session.session_id}",
            )
            # コールバックを結ぶ
            btn.callback = self._stop_callback
            # 行に追加する
            row.add_item(btn)
            # コンテナへ
            container.add_item(row)
        # ルートにコンテナを追加する
        self.add_item(container)

    async def _stop_callback(self, interaction: discord.Interaction) -> None:
        """中止ボタン押下時。開始者または管理者のみ。"""
        # 押したユーザー ID
        uid = interaction.user.id
        # 許可判定
        allowed = uid == self.session.starter_user_id or uid in self.admin_ids
        # 拒否メッセージは押した人の locale を優先する
        msg_lang = resolve_interaction_lang(interaction)
        # 拒否なら ephemeral
        if not allowed:
            await interaction.response.send_message(
                pick_str(
                    msg_lang,
                    ja="討論の開始者または管理者のみ中止できます。",
                    en="Only the starter or an admin can stop this debate.",
                ),
                ephemeral=True,
            )
            return
        # 既に終了済みなら何もしない
        if self.session.status in ("cancelled", "finished"):
            await interaction.response.send_message(
                pick_str(
                    msg_lang,
                    ja="この討論は既に終了しています。",
                    en="This debate has already ended.",
                ),
                ephemeral=True,
            )
            return
        # 応答を先に返す
        await interaction.response.defer()
        # コールバックでセッションをキャンセルする
        await self.on_stop(self.session)
        # UI を再構築してメッセージを更新する
        self._rebuild()
        try:
            # パネルメッセージを edit
            if self.session.panel_message is not None:
                await self.session.panel_message.edit(view=self)
        except Exception as e:
            # 編集失敗はログのみ
            logger.warning("Failed to update debate stop panel: %s", e)

    async def refresh_panel(self) -> None:
        """進捗更新時に呼ぶ。"""
        # UI を組み直す
        self._rebuild()
        # メッセージが無ければ終わり
        if self.session.panel_message is None:
            return
        try:
            # パネルを edit する
            await self.session.panel_message.edit(view=self)
        except Exception as e:
            # 失敗は警告
            logger.warning("Failed to refresh debate panel: %s", e)
