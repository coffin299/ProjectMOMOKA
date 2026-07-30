# MOMOKA/link_fix/link_fix_cog.py
# SNS リンクの公式 embed を抑制し、Fix プロキシ URL で silent 引用置換する。
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

import discord
from discord import app_commands
from discord.ext import commands

from MOMOKA.link_fix.locale_flags import resolve_locale
from MOMOKA.link_fix.presets import (
    get_link_fix_config,
    list_site_ids,
    supports_translation,
)
from MOMOKA.link_fix.settings_store import LinkFixSettingsStore
from MOMOKA.link_fix.settings_view import LinkFixSettingsView
from MOMOKA.link_fix.translation_view import maybe_make_twitter_translation_view
from MOMOKA.link_fix.url_utils import extract_previewable_urls
from MOMOKA.link_fix.websites import (
    MatchedLink,
    format_reply_line,
    match_urls,
)
from MOMOKA.utilities.locale import pick_str, resolve_interaction_lang

logger = logging.getLogger(__name__)


class LinkFixCog(commands.Cog):
    """SNS リンクの公式 embed を壊し、Fix URL の引用返信で置き換える Cog。"""

    def __init__(self, bot: commands.Bot) -> None:
        # Bot 参照を保持する
        self.bot = bot
        # 設定 dict
        self.bot_config: Dict[str, Any] = getattr(bot, "config", None) or {}
        # link_fix セクション
        self.section = get_link_fix_config(self.bot_config)
        # プロジェクトルート（MOMOKA/link_fix → 親の親の親）
        root = Path(__file__).resolve().parents[2]
        # ギルド設定ストア
        self.store = LinkFixSettingsStore(self.bot_config, project_root=root, bot=bot)

    def _cfg(self, key: str, default: Any = None) -> Any:
        """link_fix セクションから値を取る。"""
        return self.section.get(key, default)

    async def _wait_for_embeds(
        self,
        message: discord.Message,
        timeout: float,
    ) -> discord.Message:
        """embed が増えるまで message_edit を待つ。タイムアウト後は再取得。"""
        # 既に embed があればそのまま
        if message.embeds:
            return message

        # 編集チェック
        def _check(before: discord.Message, after: discord.Message) -> bool:
            # 同一メッセージで embed が増えたか
            return after.id == message.id and len(after.embeds) > len(before.embeds)

        # 待つ
        try:
            _, after = await self.bot.wait_for(
                "message_edit",
                check=_check,
                timeout=timeout,
            )
            # 編集後を返す
            return after
        except asyncio.TimeoutError:
            # チャンネルから再取得を試みる
            try:
                return await message.channel.fetch_message(message.id)
            except (discord.NotFound, discord.HTTPException):
                # 失敗時は元を返す
                return message

    def _resolve_footnote(self, preferred_locale: Any = None) -> str:
        """ギルド locale に応じた注記文（旧 footnote キーもフォールバック）。"""
        # locale から ISO 言語を解決する
        locale_info = resolve_locale(preferred_locale)
        # ja なら日本語注記を優先する
        if locale_info and locale_info[0] == "ja":
            # footnote_ja → 旧 footnote
            text = self._cfg("footnote_ja") or self._cfg("footnote") or ""
        else:
            # footnote_en → 旧 footnote
            text = self._cfg("footnote_en") or self._cfg("footnote") or ""
        # 文字列化して返す
        return str(text).strip()

    def _build_reply_content(
        self,
        links: List[MatchedLink],
        *,
        preferred_locale: Any = None,
    ) -> str:
        """返信本文（複数リンク＋注記）。"""
        # 各行
        lines = [format_reply_line(link) for link in links]
        # 注記（locale 別）
        footnote = self._resolve_footnote(preferred_locale)
        # 注記があれば -# 行を付ける
        if footnote:
            lines.append(f"-# {footnote}")
        # 結合する
        return "\n".join(lines)

    def _can_manage_messages(self, message: discord.Message) -> bool:
        """元メッセージの embed 抑制が可能か。"""
        # ギルド必須
        if not message.guild:
            return False
        # me
        me = message.guild.me
        if me is None:
            return False
        # Manage Messages
        return bool(message.channel.permissions_for(me).manage_messages)

    async def _suppress_original(self, message: discord.Message) -> bool:
        """元メッセージの embed を抑制する。成功なら True。"""
        # 権限無ければ失敗扱い
        if not self._can_manage_messages(message):
            return False
        # 抑制を試みる
        try:
            # 公式 embed 付与を少し待つ（無いまま抑制しても後から付くことがある）
            wait_s = float(self._cfg("embed_wait_seconds") or 5.0)
            # 待ちつつ最新を取る
            target = await self._wait_for_embeds(message, wait_s)
            # suppress フラグを立てる
            await target.edit(suppress=True)
            # Discord が再付与することがあるので短く待って再試行
            await asyncio.sleep(1.0)
            # 再取得
            try:
                refreshed = await target.channel.fetch_message(target.id)
            except (discord.NotFound, discord.HTTPException):
                # 取得失敗でも抑制は一度成功している
                return True
            # まだ embed があれば再抑制
            if refreshed.embeds:
                await refreshed.edit(suppress=True)
            # 成功
            return True
        except (discord.Forbidden, discord.HTTPException) as exc:
            # 抑制失敗
            logger.debug("suppress failed: %s", exc)
            return False

    async def _unsuppress_original(self, message: discord.Message) -> None:
        """Fix 失敗時に元メッセージの embed 抑制を戻す。"""
        # 権限無ければ何もしない
        if not self._can_manage_messages(message):
            return
        # 抑制解除を試みる
        try:
            await message.edit(suppress=False)
        except (discord.Forbidden, discord.HTTPException, discord.NotFound) as exc:
            # 戻せなくても致命ではない
            logger.debug("unsuppress failed: %s", exc)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        """対象 URL があれば元 embed を壊し、Fix URL で引用置換する。"""
        # Bot / webhook / システムは無視
        if message.author.bot or message.webhook_id or message.is_system():
            return
        # ギルドのみ（DM は設定が無い）
        if not message.guild or not message.content:
            return
        # ギルド全体オフなら終了
        if not self.store.is_feature_enabled(message.guild.id):
            return
        # 無視キーワード
        ignore_kw = str(self._cfg("ignore_keyword") or "fxignore").strip()
        if ignore_kw and ignore_kw.lower() in message.content.lower():
            return
        # 権限チェック（送信・embed）
        me = message.guild.me
        if me is None:
            return
        perms = message.channel.permissions_for(me)
        if not (perms.send_messages and perms.embed_links):
            return
        # URL 抽出
        urls = extract_previewable_urls(message.content)
        if not urls:
            return
        # ギルドサイト上書き
        guild_sites = self.store.get_all_sites_overrides(message.guild.id)
        # 無効サイトを擬似上書きで落とす
        effective_sites: Dict[str, Any] = dict(guild_sites)
        # 全サイトを走査する
        for site_id in list_site_ids(self.bot_config):
            # サイト無効なら enabled=False を載せる
            if not self.store.is_site_enabled(message.guild.id, site_id):
                entry = dict(effective_sites.get(site_id) or {})
                entry["enabled"] = False
                effective_sites[site_id] = entry
        # locale（Twitter 翻訳用）
        locale_info = resolve_locale(getattr(message.guild, "preferred_locale", None))
        translate_lang = locale_info[0] if locale_info else None
        # 対象 URL をすべてマッチ（壊れているか問わず置換）
        matched = match_urls(
            urls,
            self.bot_config,
            effective_sites,
            translate_lang=translate_lang,
        )
        # 対象無しなら終了
        if not matched:
            return
        # 1) ユーザー公式 embed を破壊（抑制）
        suppressed = await self._suppress_original(message)
        # ギルド preferred_locale（注記言語用）
        guild_locale = getattr(message.guild, "preferred_locale", None)
        # 返信本文
        content = self._build_reply_content(matched, preferred_locale=guild_locale)
        # Twitter 単独かつ翻訳対応なら View を付ける
        view: Optional[discord.ui.View] = None
        if len(matched) == 1 and matched[0].site_id == "twitter":
            link = matched[0]
            view = maybe_make_twitter_translation_view(
                site_id=link.site_id,
                original_url=link.original_url,
                label=link.label,
                fixer_name=link.fixer_name,
                fix_url=link.fix_url,
                fix_domain=link.fix_domain,
                supports_tr=supports_translation(
                    self.bot_config, link.site_id, link.fix_domain
                ),
                locale_info=locale_info,
                footnote=self._resolve_footnote(guild_locale),
                timeout=float(self._cfg("translation_view_timeout") or 3600),
            )
        # 2) silent 引用返信で Fix URL に replace
        silent = bool(self._cfg("silent", True))
        try:
            sent = await message.reply(
                content,
                silent=silent,
                mention_author=False,
                view=view,
            )
        except discord.HTTPException as exc:
            # 送信失敗 → 抑制済みなら元を戻す
            logger.warning("link fix reply failed: %s", exc)
            if suppressed:
                await self._unsuppress_original(message)
            return
        # Fix 側 embed 待ち
        fixed_wait = float(self._cfg("fixed_embed_wait_seconds") or 6.0)
        fixed_msg = await self._wait_for_embeds(sent, fixed_wait)
        # embed が無ければ返信削除＋元 embed 復元
        if not fixed_msg.embeds:
            try:
                await sent.delete()
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass
            # 抑制していた場合は戻す
            if suppressed:
                await self._unsuppress_original(message)
            return

    @app_commands.command(
        name="linkfix",
        description="Configure Link Fix (social embed replacement).",
    )
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def linkfix_settings(self, interaction: discord.Interaction) -> None:
        """ギルド向け Components V2 設定パネルを開く。"""
        # ギルド必須
        if interaction.guild is None:
            lang = resolve_interaction_lang(interaction)
            await interaction.response.send_message(
                pick_str(
                    lang,
                    ja="このコマンドはサーバー内でのみ使えます。",
                    en="This command can only be used in a server.",
                ),
                ephemeral=True,
            )
            return
        # Manage Server
        perms = getattr(interaction.user, "guild_permissions", None)
        if perms is None or not perms.manage_guild:
            # 権限エラーも app → guild → en
            lang = resolve_interaction_lang(interaction)
            await interaction.response.send_message(
                pick_str(
                    lang,
                    ja="サーバー管理権限が必要です。",
                    en="Manage Server permission required.",
                ),
                ephemeral=True,
            )
            return
        # app → guild → en で設定パネル言語を決める
        lang = resolve_interaction_lang(interaction)
        # View を作る
        view = LinkFixSettingsView(
            self.bot,
            self.store,
            interaction.guild.id,
            lang=lang,
        )
        # ephemeral で送る
        await interaction.response.send_message(view=view, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    """Cog をロードする。"""
    # 追加する
    await bot.add_cog(LinkFixCog(bot))
