# MOMOKA/link_fix/settings_view.py
# /linkfix 用 Components V2 多ページ設定 UI。
from __future__ import annotations

import logging
from enum import Enum
from typing import Any, Dict, List, Optional

import discord
from discord.ext import commands

from MOMOKA.link_fix.presets import (
    get_fixer_presets,
    get_site_meta,
    list_site_ids,
    normalize_domain,
    parse_domain_list,
    resolve_fix_domain,
    resolve_match_domains,
)
from MOMOKA.link_fix.settings_store import LinkFixSettingsStore

logger = logging.getLogger(__name__)

# サイト一覧の1ページあたり件数
_SITES_PER_PAGE = 4


class _Page(str, Enum):
    """設定 UI のページ種別。"""

    OVERVIEW = "overview"
    SITES = "sites"
    SITE_DETAIL = "site_detail"


class CustomFixDomainModal(discord.ui.Modal, title="Custom fix domain"):
    """任意の Fix 先ドメイン入力。"""

    domain = discord.ui.TextInput(
        label="Fix destination domain",
        placeholder="example.com",
        required=True,
        max_length=120,
    )

    def __init__(self, parent: "LinkFixSettingsView") -> None:
        # Modal を初期化する
        super().__init__()
        # 親 View を保持する
        self.parent = parent

    async def on_submit(self, interaction: discord.Interaction) -> None:
        # サイト未選択なら拒否する
        if not self.parent.site_id:
            await interaction.response.send_message("No site selected.", ephemeral=True)
            return
        # 正規化する
        normalized = normalize_domain(str(self.domain.value))
        # 不正ならエラー
        if not normalized:
            await interaction.response.send_message("Invalid domain.", ephemeral=True)
            return
        # 保存する
        ok = self.parent.store.set_fix_domain(
            self.parent.guild_id, self.parent.site_id, normalized
        )
        # 失敗時
        if not ok:
            await interaction.response.send_message("Failed to save domain.", ephemeral=True)
            return
        # UI を再構築する
        self.parent._rebuild()
        # メッセージを更新する
        await interaction.response.edit_message(view=self.parent)


class EditMatchDomainsModal(discord.ui.Modal, title="Edit match domains"):
    """Fix 元（マッチ）ドメインのカンマ区切り編集。"""

    domains = discord.ui.TextInput(
        label="Source domains (comma-separated)",
        style=discord.TextStyle.paragraph,
        placeholder="x.com, twitter.com",
        required=True,
        max_length=500,
    )

    def __init__(self, parent: "LinkFixSettingsView", initial: str) -> None:
        # Modal を初期化する
        super().__init__()
        # 親 View
        self.parent = parent
        # 初期値を入れる
        self.domains.default = initial

    async def on_submit(self, interaction: discord.Interaction) -> None:
        # サイト未選択
        if not self.parent.site_id:
            await interaction.response.send_message("No site selected.", ephemeral=True)
            return
        # パースする
        parsed, err = parse_domain_list(str(self.domains.value))
        # エラー
        if err:
            await interaction.response.send_message(err, ephemeral=True)
            return
        # 保存する
        ok = self.parent.store.set_match_domains(
            self.parent.guild_id, self.parent.site_id, parsed
        )
        # 失敗
        if not ok:
            await interaction.response.send_message("Failed to save domains.", ephemeral=True)
            return
        # UI 更新
        self.parent._rebuild()
        await interaction.response.edit_message(view=self.parent)


class LinkFixSettingsView(discord.ui.LayoutView):
    """Overview / Sites / Site detail の最強 CV2 設定パネル。"""

    def __init__(
        self,
        bot: commands.Bot,
        store: LinkFixSettingsStore,
        guild_id: int,
        *,
        timeout: Optional[float] = 600,
    ) -> None:
        # タイムアウト付きで初期化する
        super().__init__(timeout=timeout)
        # Bot 参照
        self.bot = bot
        # 設定ストア
        self.store = store
        # 対象ギルド
        self.guild_id = guild_id
        # bot.config
        self.bot_config: Dict[str, Any] = getattr(bot, "config", None) or {}
        # 現在ページ
        self.page = _Page.OVERVIEW
        # サイト一覧のページ番号
        self.sites_page = 0
        # 詳細表示中のサイト id
        self.site_id: Optional[str] = None
        # UI 構築
        self._rebuild()

    def _site_ids(self) -> List[str]:
        """サイト id 一覧。"""
        return list_site_ids(self.bot_config)

    def _rebuild(self) -> None:
        """現在状態から LayoutView を組み直す。"""
        # 全アイテムを消す
        self.clear_items()
        # アクセント色
        accent = discord.Color.from_rgb(29, 155, 240)
        # コンテナ
        container = discord.ui.Container(accent_color=accent)
        # ページ分岐
        if self.page == _Page.OVERVIEW:
            self._build_overview(container)
        elif self.page == _Page.SITES:
            self._build_sites(container)
        else:
            self._build_site_detail(container)
        # ルートに追加する
        self.add_item(container)

    def _build_overview(self, container: discord.ui.Container) -> None:
        """Overview ページ。"""
        # 全体フラグ
        enabled = self.store.is_feature_enabled(self.guild_id)
        # 有効サイト数
        on_count, total = self.store.count_enabled_sites(self.guild_id)
        # 状態文言
        status = "ENABLED" if enabled else "DISABLED"
        # 本文
        body = (
            "### Link Fix Settings\n"
            f"**Feature:** `{status}`\n"
            f"**Sites enabled:** `{on_count}/{total}`\n\n"
            "Suppress original social embeds and quote-replace with fixer URLs.\n"
            "Default: **disabled**. Toggle below or open **Sites**."
        )
        # TextDisplay
        container.add_item(discord.ui.TextDisplay(body))
        # 区切り
        container.add_item(discord.ui.Separator())
        # マスター行（機能全体）
        master = discord.ui.ActionRow()
        # Enable / Disable トグル
        toggle = discord.ui.Button(
            label="Disable Feature" if enabled else "Enable Feature",
            style=discord.ButtonStyle.danger if enabled else discord.ButtonStyle.success,
            custom_id="lf_toggle_feature",
        )
        toggle.callback = self._toggle_feature
        master.add_item(toggle)
        # 全サイト一括有効化
        enable_all = discord.ui.Button(
            label="Enable All Sites",
            style=discord.ButtonStyle.success,
            custom_id="lf_enable_all_sites",
        )
        enable_all.callback = self._enable_all_sites
        master.add_item(enable_all)
        # 全サイト一括無効化
        disable_all = discord.ui.Button(
            label="Disable All Sites",
            style=discord.ButtonStyle.danger,
            custom_id="lf_disable_all_sites",
        )
        disable_all.callback = self._disable_all_sites
        master.add_item(disable_all)
        # 行を追加
        container.add_item(master)
        # ナビ行（Sites / Reset）
        nav = discord.ui.ActionRow()
        # Sites へ
        to_sites = discord.ui.Button(
            label="Sites",
            style=discord.ButtonStyle.primary,
            custom_id="lf_goto_sites",
        )
        to_sites.callback = self._goto_sites
        nav.add_item(to_sites)
        # Reset
        reset = discord.ui.Button(
            label="Reset Guild",
            style=discord.ButtonStyle.secondary,
            custom_id="lf_reset_guild",
        )
        reset.callback = self._reset_guild
        nav.add_item(reset)
        # 行を追加
        container.add_item(nav)

    def _build_sites(self, container: discord.ui.Container) -> None:
        """サイト一覧ページ。"""
        # 全 id
        ids = self._site_ids()
        # ページ数
        pages = max(1, (len(ids) + _SITES_PER_PAGE - 1) // _SITES_PER_PAGE)
        # ページ正規化
        self.sites_page = max(0, min(self.sites_page, pages - 1))
        # スライス
        start = self.sites_page * _SITES_PER_PAGE
        chunk = ids[start : start + _SITES_PER_PAGE]
        # ヘッダ
        lines = [
            "### Link Fix — Sites",
            f"Page `{self.sites_page + 1}/{pages}`",
            "",
        ]
        # 各サイトの状態行
        for sid in chunk:
            meta = get_site_meta(self.bot_config, sid)
            label = str(meta.get("label") or sid)
            on = self.store.is_site_enabled(self.guild_id, sid)
            guild_site = self.store.get_site(self.guild_id, sid)
            fix = resolve_fix_domain(self.bot_config, sid, guild_site)
            mark = "ON" if on else "OFF"
            lines.append(f"• **{label}** — `{mark}` → `{fix}`")
        # TextDisplay
        container.add_item(discord.ui.TextDisplay("\n".join(lines)))
        container.add_item(discord.ui.Separator())
        # サイトごとの On/Off + Configure
        for sid in chunk:
            meta = get_site_meta(self.bot_config, sid)
            label = str(meta.get("label") or sid)
            on = self.store.is_site_enabled(self.guild_id, sid)
            row = discord.ui.ActionRow()
            # トグル
            btn = discord.ui.Button(
                label=f"{'ON' if on else 'OFF'} {label}"[:80],
                style=discord.ButtonStyle.success if on else discord.ButtonStyle.secondary,
                custom_id=f"lf_site_toggle:{sid}",
            )
            # クロージャ用に sid を束縛する
            btn.callback = self._make_site_toggle_cb(sid)
            row.add_item(btn)
            # Configure
            cfg = discord.ui.Button(
                label="Configure",
                style=discord.ButtonStyle.primary,
                custom_id=f"lf_site_cfg:{sid}",
            )
            cfg.callback = self._make_site_cfg_cb(sid)
            row.add_item(cfg)
            container.add_item(row)
        # 一括 on/off 行
        bulk = discord.ui.ActionRow()
        # 全サイト有効化
        enable_all = discord.ui.Button(
            label="Enable All",
            style=discord.ButtonStyle.success,
            custom_id="lf_sites_enable_all",
        )
        enable_all.callback = self._enable_all_sites
        bulk.add_item(enable_all)
        # 全サイト無効化
        disable_all = discord.ui.Button(
            label="Disable All",
            style=discord.ButtonStyle.danger,
            custom_id="lf_sites_disable_all",
        )
        disable_all.callback = self._disable_all_sites
        bulk.add_item(disable_all)
        container.add_item(bulk)
        # ナビ行
        nav = discord.ui.ActionRow()
        prev_btn = discord.ui.Button(
            label="◀ Prev",
            style=discord.ButtonStyle.secondary,
            custom_id="lf_sites_prev",
            disabled=self.sites_page <= 0,
        )
        prev_btn.callback = self._sites_prev
        nav.add_item(prev_btn)
        back = discord.ui.Button(
            label="Overview",
            style=discord.ButtonStyle.secondary,
            custom_id="lf_back_overview",
        )
        back.callback = self._goto_overview
        nav.add_item(back)
        next_btn = discord.ui.Button(
            label="Next ▶",
            style=discord.ButtonStyle.secondary,
            custom_id="lf_sites_next",
            disabled=self.sites_page >= pages - 1,
        )
        next_btn.callback = self._sites_next
        nav.add_item(next_btn)
        container.add_item(nav)

    def _build_site_detail(self, container: discord.ui.Container) -> None:
        """サイト詳細（宗派・マッチ元）。"""
        # サイト id
        sid = self.site_id or ""
        # メタ
        meta = get_site_meta(self.bot_config, sid)
        label = str(meta.get("label") or sid)
        # ギルド上書き
        guild_site = self.store.get_site(self.guild_id, sid)
        # 解決済み
        fix = resolve_fix_domain(self.bot_config, sid, guild_site)
        sources = resolve_match_domains(self.bot_config, sid, guild_site)
        on = self.store.is_site_enabled(self.guild_id, sid)
        # 本文
        body = (
            f"### {label}\n"
            f"**Status:** `{'ON' if on else 'OFF'}`\n"
            f"**Fix destination:** `{fix}`\n"
            f"**Match sources:** `{', '.join(sources)}`\n\n"
            "Pick a fixer denomination below, or set a custom domain."
        )
        container.add_item(discord.ui.TextDisplay(body))
        container.add_item(discord.ui.Separator())
        # 宗派 Select
        presets = get_fixer_presets(self.bot_config, sid)
        options: List[discord.SelectOption] = []
        # プリセット
        for domain in presets[:24]:
            options.append(
                discord.SelectOption(
                    label=domain[:100],
                    value=f"preset:{domain}",
                    default=(domain == fix),
                )
            )
        # Custom
        options.append(
            discord.SelectOption(
                label="Custom…",
                value="custom",
                description="Enter any fix domain",
            )
        )
        select = discord.ui.Select(
            placeholder="Fix destination (denomination)",
            options=options,
            custom_id=f"lf_fix_select:{sid}",
            min_values=1,
            max_values=1,
        )
        select.callback = self._on_fix_select
        sel_row = discord.ui.ActionRow()
        sel_row.add_item(select)
        container.add_item(sel_row)
        # マッチ元編集行
        src_row = discord.ui.ActionRow()
        edit_src = discord.ui.Button(
            label="Edit sources",
            style=discord.ButtonStyle.primary,
            custom_id="lf_edit_sources",
        )
        edit_src.callback = self._edit_sources
        src_row.add_item(edit_src)
        reset_src = discord.ui.Button(
            label="Reset sources",
            style=discord.ButtonStyle.secondary,
            custom_id="lf_reset_sources",
        )
        reset_src.callback = self._reset_sources
        src_row.add_item(reset_src)
        container.add_item(src_row)
        # 戻る
        back_row = discord.ui.ActionRow()
        back = discord.ui.Button(
            label="◀ Back to Sites",
            style=discord.ButtonStyle.secondary,
            custom_id="lf_back_sites",
        )
        back.callback = self._goto_sites
        back_row.add_item(back)
        container.add_item(back_row)

    def _make_site_toggle_cb(self, site_id: str):
        """サイトトグル用コールバック工場。"""

        async def _cb(interaction: discord.Interaction) -> None:
            # 権限チェック
            if not await self._ensure_manage(interaction):
                return
            # 反転する
            current = self.store.is_site_enabled(self.guild_id, site_id)
            self.store.set_site_enabled(self.guild_id, site_id, not current)
            # 再描画
            self._rebuild()
            await interaction.response.edit_message(view=self)

        return _cb

    def _make_site_cfg_cb(self, site_id: str):
        """サイト詳細へ遷移するコールバック工場。"""

        async def _cb(interaction: discord.Interaction) -> None:
            if not await self._ensure_manage(interaction):
                return
            # 詳細ページへ
            self.site_id = site_id
            self.page = _Page.SITE_DETAIL
            self._rebuild()
            await interaction.response.edit_message(view=self)

        return _cb

    async def _ensure_manage(self, interaction: discord.Interaction) -> bool:
        """Manage Guild が無ければ ephemeral で拒否。"""
        # メンバー権限を見る
        perms = getattr(interaction.user, "guild_permissions", None)
        # 無ければ拒否
        if perms is None or not perms.manage_guild:
            await interaction.response.send_message(
                "Manage Server permission required.",
                ephemeral=True,
            )
            return False
        # OK
        return True

    async def _toggle_feature(self, interaction: discord.Interaction) -> None:
        """全体 on/off。"""
        if not await self._ensure_manage(interaction):
            return
        # 反転
        current = self.store.is_feature_enabled(self.guild_id)
        self.store.set_feature_enabled(self.guild_id, not current)
        self._rebuild()
        await interaction.response.edit_message(view=self)

    async def _enable_all_sites(self, interaction: discord.Interaction) -> None:
        """全サイトを一括で有効化する。"""
        # 権限チェック
        if not await self._ensure_manage(interaction):
            return
        # 全サイトを ON にする
        self.store.set_all_sites_enabled(self.guild_id, True)
        # 再描画する
        self._rebuild()
        await interaction.response.edit_message(view=self)

    async def _disable_all_sites(self, interaction: discord.Interaction) -> None:
        """全サイトを一括で無効化する。"""
        # 権限チェック
        if not await self._ensure_manage(interaction):
            return
        # 全サイトを OFF にする
        self.store.set_all_sites_enabled(self.guild_id, False)
        # 再描画する
        self._rebuild()
        await interaction.response.edit_message(view=self)

    async def _reset_guild(self, interaction: discord.Interaction) -> None:
        """ギルド設定リセット。"""
        if not await self._ensure_manage(interaction):
            return
        self.store.reset_guild(self.guild_id)
        self.page = _Page.OVERVIEW
        self.site_id = None
        self._rebuild()
        await interaction.response.edit_message(view=self)

    async def _goto_sites(self, interaction: discord.Interaction) -> None:
        """Sites ページへ。"""
        if not await self._ensure_manage(interaction):
            return
        self.page = _Page.SITES
        self._rebuild()
        await interaction.response.edit_message(view=self)

    async def _goto_overview(self, interaction: discord.Interaction) -> None:
        """Overview へ。"""
        if not await self._ensure_manage(interaction):
            return
        self.page = _Page.OVERVIEW
        self.site_id = None
        self._rebuild()
        await interaction.response.edit_message(view=self)

    async def _sites_prev(self, interaction: discord.Interaction) -> None:
        """サイト一覧前ページ。"""
        if not await self._ensure_manage(interaction):
            return
        self.sites_page = max(0, self.sites_page - 1)
        self._rebuild()
        await interaction.response.edit_message(view=self)

    async def _sites_next(self, interaction: discord.Interaction) -> None:
        """サイト一覧次ページ。"""
        if not await self._ensure_manage(interaction):
            return
        self.sites_page += 1
        self._rebuild()
        await interaction.response.edit_message(view=self)

    async def _on_fix_select(self, interaction: discord.Interaction) -> None:
        """Fix 先 Select。"""
        if not await self._ensure_manage(interaction):
            return
        # 値
        values = interaction.data.get("values") if interaction.data else None
        if not values:
            await interaction.response.defer()
            return
        value = values[0]
        # Custom Modal
        if value == "custom":
            await interaction.response.send_modal(CustomFixDomainModal(self))
            return
        # preset:domain
        if value.startswith("preset:"):
            domain = value.split(":", 1)[1]
            if self.site_id:
                self.store.set_fix_domain(self.guild_id, self.site_id, domain)
            self._rebuild()
            await interaction.response.edit_message(view=self)
            return
        await interaction.response.defer()

    async def _edit_sources(self, interaction: discord.Interaction) -> None:
        """マッチ元 Modal を開く。"""
        if not await self._ensure_manage(interaction):
            return
        if not self.site_id:
            await interaction.response.send_message("No site selected.", ephemeral=True)
            return
        guild_site = self.store.get_site(self.guild_id, self.site_id)
        sources = resolve_match_domains(self.bot_config, self.site_id, guild_site)
        modal = EditMatchDomainsModal(self, ", ".join(sources))
        await interaction.response.send_modal(modal)

    async def _reset_sources(self, interaction: discord.Interaction) -> None:
        """マッチ元をデフォルトに戻す。"""
        if not await self._ensure_manage(interaction):
            return
        if self.site_id:
            self.store.clear_match_domains(self.guild_id, self.site_id)
        self._rebuild()
        await interaction.response.edit_message(view=self)
