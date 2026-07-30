# MOMOKA/utilities/guild_events_cog.py
# サーバー（ギルド）加入・脱退を GUI 向けにログする Cog。
from __future__ import annotations

import logging

import discord
from discord.ext import commands

# 本モジュール用ロガー
logger = logging.getLogger(__name__)


class GuildEventsCog(commands.Cog):
    """ギルド加入・脱退イベントを [GUILD_EVENT] 付きで記録する。"""

    def __init__(self, bot: commands.Bot) -> None:
        # Bot 参照を保持する
        self.bot = bot

    def _bot_role(self) -> str:
        """primary / companion などの役割文字列を返す。"""
        # Momoka.bot_role を読み、無ければ unknown
        return getattr(self.bot, "bot_role", "unknown")

    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild) -> None:
        """Bot が新規サーバーへ加入したときにログする。"""
        # primary / companion を文言用に取る
        role = self._bot_role()
        # メンバー数（キャッシュ上の概算）
        member_count = guild.member_count if guild.member_count is not None else "?"
        # GUI が #0000ff で拾うマーカー付きで INFO 出力する
        logger.info(
            "[GUILD_EVENT] [%s] Joined guild: %s (id=%s) members=%s",
            role,
            guild.name,
            guild.id,
            member_count,
        )

    @commands.Cog.listener()
    async def on_guild_remove(self, guild: discord.Guild) -> None:
        """Bot がサーバーから脱退したときにログする。"""
        # primary / companion を文言用に取る
        role = self._bot_role()
        # GUI が #0000ff で拾うマーカー付きで INFO 出力する
        logger.info(
            "[GUILD_EVENT] [%s] Left guild: %s (id=%s)",
            role,
            guild.name,
            guild.id,
        )


async def setup(bot: commands.Bot) -> None:
    """Cog を Bot へ登録する。"""
    # GuildEventsCog をロードする
    await bot.add_cog(GuildEventsCog(bot))
