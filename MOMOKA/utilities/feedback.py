# MOMOKA/utilities/feedback.py
# バグ報告・機能リクエスト用の共有ロジック（Modal / View / 複数チャンネル投稿）。
from __future__ import annotations

import logging
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

import discord
from discord.ext import commands

from MOMOKA.utilities.locale import pick_str, resolve_interaction_lang

logger = logging.getLogger(__name__)

# View / Modal の有効期限（秒）— 画像生成フローに合わせる
VIEW_TIMEOUT_SECONDS = 300

# 同一ユーザーの連続投稿クールダウン（秒）
COOLDOWN_SECONDS = 60

# カテゴリ定義（id → 日英ラベル）
CATEGORIES: Dict[str, Dict[str, str]] = {
    "bug": {"ja": "不具合報告", "en": "Bug report"},
    "feature_request": {"ja": "機能リクエスト", "en": "Feature request"},
    "other": {"ja": "その他", "en": "Other"},
}

# カテゴリボタンの見た目
_CATEGORY_STYLES: Dict[str, discord.ButtonStyle] = {
    "bug": discord.ButtonStyle.danger,
    "feature_request": discord.ButtonStyle.primary,
    "other": discord.ButtonStyle.secondary,
}


def category_label(category_id: str, *, lang: Optional[str] = None) -> str:
    """カテゴリラベルを返す（lang 指定時は単一言語、未指定時は日英併記）。"""
    # 未知 ID は other にフォールバックする
    meta = CATEGORIES.get(category_id) or CATEGORIES["other"]
    # lang 指定なら単一言語
    if lang is not None:
        return pick_str(lang, ja=meta["ja"], en=meta["en"])
    # 未指定は後方互換の併記
    return f"{meta['ja']} / {meta['en']}"


def normalize_category(raw: Optional[str]) -> Optional[str]:
    """カテゴリ文字列を正規化し、未知なら None。"""
    # 空は無効とする
    if not raw:
        return None
    # 小文字化して比較する
    key = str(raw).strip().lower()
    # 定義済み ID なら採用する
    if key in CATEGORIES:
        return key
    # 未知は無効とする
    return None


class FeedbackService:
    """設定読込・クールダウン・複数チャンネルへの Embed 投稿。"""

    def __init__(self, bot: commands.Bot) -> None:
        # Bot 参照を保持する
        self.bot = bot
        # user_id → 最終投稿時刻（monotonic）
        self._last_submit: Dict[int, float] = {}

    def _feedback_config(self) -> Dict[str, Any]:
        """マージ済み config から feedback セクションを取る。"""
        # bot.config が無い場合は空にする
        cfg = getattr(self.bot, "config", None) or {}
        # feedback キーを読む
        section = cfg.get("feedback") or {}
        # dict 以外は空扱いする
        if not isinstance(section, dict):
            return {}
        # セクションを返す
        return section

    def channel_ids(self) -> List[int]:
        """投稿先チャンネル ID のリストを返す。"""
        # 設定から raw リストを取る
        raw = self._feedback_config().get("channel_ids") or []
        # リストでなければ空にする
        if not isinstance(raw, list):
            logger.warning("feedback.channel_ids must be a list; got %r", type(raw))
            return []
        # 整数化できるものだけ集める
        result: List[int] = []
        # 各要素を走査する
        for item in raw:
            # 既に int ならそのまま追加する
            if isinstance(item, int):
                result.append(item)
                continue
            # 文字列の数字も許容する
            try:
                result.append(int(item))
            except (TypeError, ValueError):
                logger.warning("Ignoring invalid feedback channel id: %r", item)
        # 重複を除いた順序保持リストを返す
        seen = set()
        unique: List[int] = []
        for cid in result:
            if cid in seen:
                continue
            seen.add(cid)
            unique.append(cid)
        return unique

    def is_configured(self) -> bool:
        """投稿先が1つ以上あるか。"""
        return bool(self.channel_ids())

    def check_cooldown(self, user_id: int) -> Optional[int]:
        """クールダウン中なら残り秒、否则 None。"""
        # 最終投稿時刻を取る
        last = self._last_submit.get(user_id)
        # 未投稿なら通過する
        if last is None:
            return None
        # 経過秒を計算する
        elapsed = time.monotonic() - last
        # クールダウン超過なら通過する
        if elapsed >= COOLDOWN_SECONDS:
            return None
        # 残り秒（切り上げ）を返す
        return max(1, int(COOLDOWN_SECONDS - elapsed) + 1)

    def mark_submitted(self, user_id: int) -> None:
        """投稿成功時にクールダウンを記録する。"""
        self._last_submit[user_id] = time.monotonic()

    def build_embed(
        self,
        *,
        category_id: str,
        title: str,
        body: str,
        requester: discord.abc.User,
        source_guild: Optional[discord.Guild],
        source_channel: Optional[discord.abc.GuildChannel],
        submission_id: str,
    ) -> discord.Embed:
        """スタッフ向け Embed を組み立てる。"""
        # カテゴリ色を決める
        color = {
            "bug": discord.Color.red(),
            "feature_request": discord.Color.blurple(),
            "other": discord.Color.greyple(),
        }.get(category_id, discord.Color.blurple())
        # Embed 本体を作る
        embed = discord.Embed(
            title=category_label(category_id),
            description=body[:4000] if body else "(empty / 空)",
            color=color,
            timestamp=discord.utils.utcnow(),
        )
        # 提出者フィールドを追加する
        embed.add_field(
            name="提出者 / Requester",
            value=f"{requester.mention} (`{requester}` / `{requester.id}`)",
            inline=False,
        )
        # ギルド情報を追加する
        if source_guild is not None:
            guild_text = f"{source_guild.name} (`{source_guild.id}`)"
        else:
            guild_text = "DM / Unknown"
        embed.add_field(name="サーバー / Guild", value=guild_text, inline=True)
        # 元チャンネル情報を追加する
        if source_channel is not None:
            channel_text = f"{source_channel.mention} (`{source_channel.id}`)"
        else:
            channel_text = "—"
        embed.add_field(name="チャンネル / Channel", value=channel_text, inline=True)
        # タイトル（件名）を追加する
        embed.add_field(
            name="タイトル / Title",
            value=(title[:256] if title else "(none / なし)"),
            inline=False,
        )
        # フッターに submission ID を載せる
        embed.set_footer(text=f"submission_id={submission_id}")
        # アバターがあればサムネにする
        avatar = getattr(requester, "display_avatar", None)
        if avatar is not None:
            embed.set_thumbnail(url=avatar.url)
        # 完成した Embed を返す
        return embed

    async def post_to_channels(
        self,
        *,
        category_id: str,
        title: str,
        body: str,
        requester: discord.abc.User,
        source_guild: Optional[discord.Guild],
        source_channel: Optional[discord.abc.GuildChannel],
        lang: str = "en",
    ) -> Tuple[bool, str, Optional[str]]:
        """
        全 feedback チャンネルへ投稿する。

        Returns:
            (ok, user_message, submission_id_or_none)
        """
        # UI 言語を正規化する
        ui_lang = "ja" if lang == "ja" else "en"
        # 投稿先が無ければ失敗する
        ids = self.channel_ids()
        if not ids:
            return (
                False,
                pick_str(
                    ui_lang,
                    ja=(
                        "❌ フィードバック送信先が未設定です。"
                        "`configs/utilities_config.yaml` の `feedback.channel_ids` を設定してください。"
                    ),
                    en=(
                        "❌ Feedback destination is not configured. "
                        "Set `feedback.channel_ids` in `configs/utilities_config.yaml`."
                    ),
                ),
                None,
            )
        # クールダウンを確認する
        remaining = self.check_cooldown(requester.id)
        if remaining is not None:
            return (
                False,
                pick_str(
                    ui_lang,
                    ja=f"⏳ 連続投稿は少し待ってください（残り約 {remaining} 秒）。",
                    en=f"⏳ Please wait before submitting again (~{remaining}s remaining).",
                ),
                None,
            )
        # カテゴリを正規化する
        cat = normalize_category(category_id) or "other"
        # 提出 ID を発行する
        submission_id = uuid.uuid4().hex[:12]
        # Embed を組み立てる
        embed = self.build_embed(
            category_id=cat,
            title=title.strip(),
            body=body.strip(),
            requester=requester,
            source_guild=source_guild,
            source_channel=source_channel,
            submission_id=submission_id,
        )
        # 成功件数を数える
        sent = 0
        # 各チャンネルへ送る
        for channel_id in ids:
            # キャッシュから取得を試みる
            channel = self.bot.get_channel(channel_id)
            # 無ければ fetch する
            if channel is None:
                try:
                    channel = await self.bot.fetch_channel(channel_id)
                except Exception as exc:  # noqa: BLE001
                    logger.error("Failed to fetch feedback channel %s: %s", channel_id, exc)
                    continue
            # 送信可能なチャンネルか確認する
            if not hasattr(channel, "send"):
                logger.error("Feedback channel %s is not sendable", channel_id)
                continue
            # Embed を送る
            try:
                await channel.send(embed=embed)
                sent += 1
            except Exception as exc:  # noqa: BLE001
                logger.error("Failed to post feedback to channel %s: %s", channel_id, exc)
        # 1件も送れなければ失敗する
        if sent == 0:
            return (
                False,
                pick_str(
                    ui_lang,
                    ja="❌ フィードバックの投稿に失敗しました。管理者に連絡してください。",
                    en="❌ Failed to deliver feedback. Please contact an administrator.",
                ),
                None,
            )
        # クールダウンを記録する
        self.mark_submitted(requester.id)
        # 成功メッセージを返す
        return (
            True,
            pick_str(
                ui_lang,
                ja=f"✅ フィードバックを送信しました（{sent} 件）。\n`submission_id={submission_id}`",
                en=f"✅ Feedback submitted ({sent} destination(s)).\n`submission_id={submission_id}`",
            ),
            submission_id,
        )


class FeedbackModal(discord.ui.Modal):
    """タイトル + 本文の入力 Modal。"""

    def __init__(
        self,
        service: FeedbackService,
        category_id: str,
        requester_id: int,
        *,
        prefill_title: str = "",
        prefill_body: str = "",
        lang: str = "en",
    ) -> None:
        # UI 言語を正規化する
        ui_lang = "ja" if lang == "ja" else "en"
        # カテゴリ単一言語をタイトルに載せる
        cat = category_label(category_id, lang=ui_lang)
        super().__init__(
            title=f"Feedback — {cat}"[:45],
            timeout=VIEW_TIMEOUT_SECONDS,
        )
        # 依存を保持する
        self.service = service
        self.category_id = category_id
        self.requester_id = requester_id
        self.lang = ui_lang
        # タイトル入力欄を作る
        self.title_input = discord.ui.TextInput(
            label=pick_str(ui_lang, ja="タイトル", en="Title"),
            placeholder=pick_str(ui_lang, ja="短い要約", en="Short summary"),
            style=discord.TextStyle.short,
            required=True,
            max_length=100,
            default=prefill_title[:100] if prefill_title else None,
        )
        # 本文入力欄を作る
        self.body_input = discord.ui.TextInput(
            label=pick_str(ui_lang, ja="本文", en="Details"),
            placeholder=pick_str(
                ui_lang,
                ja="再現手順・期待動作など",
                en="Steps to reproduce, expected behavior, etc.",
            ),
            style=discord.TextStyle.paragraph,
            required=True,
            max_length=1500,
            default=prefill_body[:1500] if prefill_body else None,
        )
        # Modal に追加する
        self.add_item(self.title_input)
        self.add_item(self.body_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        # 依頼者以外は拒否する
        if interaction.user.id != self.requester_id:
            msg_lang = resolve_interaction_lang(interaction)
            await interaction.response.send_message(
                pick_str(
                    msg_lang,
                    ja="❌ このフォームは依頼したユーザーのみ送信できます。",
                    en="❌ Only the original requester can submit this form.",
                ),
                ephemeral=True,
            )
            return
        # 応答を遅延する（投稿に時間がかかる可能性）
        await interaction.response.defer(ephemeral=True)
        # ギルド / チャンネルを取る
        guild = interaction.guild
        channel = interaction.channel if isinstance(interaction.channel, discord.abc.GuildChannel) else None
        # 投稿する
        ok, message, _sid = await self.service.post_to_channels(
            category_id=self.category_id,
            title=str(self.title_input.value),
            body=str(self.body_input.value),
            requester=interaction.user,
            source_guild=guild,
            source_channel=channel,
            lang=self.lang,
        )
        # 結果を ephemeral で返す
        await interaction.followup.send(message, ephemeral=True)
        # ログを残す
        logger.info(
            "Feedback modal submit: ok=%s user=%s category=%s",
            ok,
            interaction.user.id,
            self.category_id,
        )


class _CategoryButton(discord.ui.Button):
    """カテゴリ選択ボタン（親 View 経由で Modal を開く）。"""

    def __init__(self, category_id: str) -> None:
        # メタを取る
        meta = CATEGORIES[category_id]
        # ボタンを初期化する
        super().__init__(
            label=f"{meta['ja']} / {meta['en']}"[:80],
            style=_CATEGORY_STYLES.get(category_id, discord.ButtonStyle.secondary),
            custom_id=f"feedback_cat:{category_id}",
        )
        # カテゴリ ID を保持する
        self.category_id = category_id

    async def callback(self, interaction: discord.Interaction) -> None:
        # 親 View を取る
        view = self.view
        # FeedbackCategoryView でなければ何もしない
        if not isinstance(view, FeedbackCategoryView):
            return
        # 認可チェックする
        if not view._is_authorized(interaction):
            await interaction.response.send_message(
                "❌ このボタンは依頼したユーザーのみ操作できます。\n"
                "❌ Only the original requester can use these buttons.",
                ephemeral=True,
            )
            return
        # Modal を開く
        modal = FeedbackModal(
            service=view.service,
            category_id=self.category_id,
            requester_id=view.requester_id,
            lang=resolve_interaction_lang(interaction),
        )
        await interaction.response.send_modal(modal)


class FeedbackCategoryView(discord.ui.View):
    """LLM form モード用: カテゴリボタン → Modal。"""

    def __init__(self, service: FeedbackService, requester_id: int) -> None:
        # タイムアウト付き View を作る
        super().__init__(timeout=VIEW_TIMEOUT_SECONDS)
        # 依存を保持する
        self.service = service
        self.requester_id = requester_id
        # 各カテゴリボタンを追加する
        for category_id in CATEGORIES:
            self.add_item(_CategoryButton(category_id))

    def _is_authorized(self, interaction: discord.Interaction) -> bool:
        """依頼者本人かどうか。"""
        return interaction.user.id == self.requester_id

    async def on_timeout(self) -> None:
        # 子コンポーネントを無効化する
        for item in self.children:
            item.disabled = True  # type: ignore[attr-defined]


class FeedbackConfirmView(discord.ui.View):
    """LLM submit モード用: 確認後に投稿。"""

    def __init__(
        self,
        service: FeedbackService,
        requester_id: int,
        *,
        category_id: str,
        title: str,
        body: str,
        source_guild_id: Optional[int],
        source_channel_id: Optional[int],
    ) -> None:
        # タイムアウト付き View を作る
        super().__init__(timeout=VIEW_TIMEOUT_SECONDS)
        # 依存・下書きを保持する
        self.service = service
        self.requester_id = requester_id
        self.category_id = category_id
        self.title_text = title
        self.body_text = body
        self.source_guild_id = source_guild_id
        self.source_channel_id = source_channel_id
        self._done = False

    def _is_authorized(self, interaction: discord.Interaction) -> bool:
        """依頼者本人かどうか。"""
        return interaction.user.id == self.requester_id

    async def _deny(self, interaction: discord.Interaction) -> None:
        """権限なし応答。"""
        await interaction.response.send_message(
            "❌ この確認は依頼したユーザーのみ操作できます。\n"
            "❌ Only the original requester can confirm.",
            ephemeral=True,
        )

    @discord.ui.button(
        label="送信する / Submit",
        style=discord.ButtonStyle.success,
        custom_id="feedback_confirm_submit",
    )
    async def confirm_submit(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        # 依頼者以外は拒否する
        if not self._is_authorized(interaction):
            await self._deny(interaction)
            return
        # 二重送信を防ぐ
        if self._done:
            await interaction.response.send_message(
                "ℹ️ 既に処理済みです。 / Already processed.",
                ephemeral=True,
            )
            return
        # 遅延応答する
        await interaction.response.defer(ephemeral=True)
        # 完了フラグを立てる
        self._done = True
        # ギルドを解決する
        guild = None
        if self.source_guild_id is not None:
            guild = self.service.bot.get_guild(self.source_guild_id)
        # チャンネルを解決する
        channel = None
        if self.source_channel_id is not None:
            ch = self.service.bot.get_channel(self.source_channel_id)
            if isinstance(ch, discord.abc.GuildChannel):
                channel = ch
        # 投稿する
        ok, message, _sid = await self.service.post_to_channels(
            category_id=self.category_id,
            title=self.title_text,
            body=self.body_text,
            requester=interaction.user,
            source_guild=guild,
            source_channel=channel,
        )
        # ボタンを無効化する
        for item in self.children:
            item.disabled = True  # type: ignore[attr-defined]
        # 元メッセージを更新できる場合は更新する
        try:
            if interaction.message is not None:
                await interaction.message.edit(view=self)
        except Exception:  # noqa: BLE001
            pass
        # 結果を返す
        await interaction.followup.send(message, ephemeral=True)
        # ログを残す
        logger.info(
            "Feedback confirm: ok=%s user=%s category=%s",
            ok,
            interaction.user.id,
            self.category_id,
        )
        # View を停止する
        self.stop()

    @discord.ui.button(
        label="キャンセル / Cancel",
        style=discord.ButtonStyle.secondary,
        custom_id="feedback_confirm_cancel",
    )
    async def confirm_cancel(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        # 依頼者以外は拒否する
        if not self._is_authorized(interaction):
            await self._deny(interaction)
            return
        # 完了フラグを立てる
        self._done = True
        # ボタンを無効化する
        for item in self.children:
            item.disabled = True  # type: ignore[attr-defined]
        # キャンセルを通知する
        await interaction.response.edit_message(
            content="キャンセルしました。 / Cancelled.",
            embed=None,
            view=self,
        )
        # View を停止する
        self.stop()

    @discord.ui.button(
        label="フォームで編集 / Edit in form",
        style=discord.ButtonStyle.primary,
        custom_id="feedback_confirm_edit",
    )
    async def confirm_edit(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        # 依頼者以外は拒否する
        if not self._is_authorized(interaction):
            await self._deny(interaction)
            return
        # 下書きを Modal に載せて開く
        modal = FeedbackModal(
            service=self.service,
            category_id=self.category_id,
            requester_id=self.requester_id,
            prefill_title=self.title_text,
            prefill_body=self.body_text,
            lang=resolve_interaction_lang(interaction),
        )
        await interaction.response.send_modal(modal)

    async def on_timeout(self) -> None:
        # 子コンポーネントを無効化する
        for item in self.children:
            item.disabled = True  # type: ignore[attr-defined]


def preview_embed_for_confirm(
    *,
    category_id: str,
    title: str,
    body: str,
    requester: discord.abc.User,
) -> discord.Embed:
    """ユーザー確認用のプレビュー Embed。"""
    # プレビュー用 Embed を作る
    embed = discord.Embed(
        title="フィードバック確認 / Confirm feedback",
        description=(
            "内容を確認し、**送信する**を押すと開発者サーバーへ届きます。\n"
            "Press **Submit** to send this to the developer servers."
        ),
        color=discord.Color.orange(),
    )
    # カテゴリを載せる
    embed.add_field(name="カテゴリ / Category", value=category_label(category_id), inline=False)
    # タイトルを載せる
    embed.add_field(name="タイトル / Title", value=title[:256] or "(none)", inline=False)
    # 本文を載せる（長すぎる場合は切る）
    body_preview = body if len(body) <= 1000 else body[:997] + "..."
    embed.add_field(name="本文 / Details", value=body_preview or "(empty)", inline=False)
    # 提出者をフッターに載せる
    embed.set_footer(text=f"Requester: {requester} ({requester.id})")
    # Embed を返す
    return embed


# サポート誘導用の既定 URL
GITHUB_REPO_URL = "https://github.com/coffin399/ProjectMOMOKA"
GITHUB_ISSUES_URL = "https://github.com/coffin399/ProjectMOMOKA/issues"

# エラー表示フッター（日英）
SUPPORT_FOOTER_TEXT = (
    "問題がありますか？フォームまたは GitHub で報告できます！ "
    "/ Having issues? Report via the form or GitHub!"
)


class SupportReportView(discord.ui.View):
    """エラー・サポート誘導用: フィードバック Modal ボタン + GitHub リンク。"""

    def __init__(
        self,
        bot: commands.Bot,
        *,
        default_category: str = "bug",
        timeout: Optional[float] = VIEW_TIMEOUT_SECONDS,
    ) -> None:
        # タイムアウト付き View を初期化する
        super().__init__(timeout=timeout)
        # FeedbackService を保持する
        self.service = FeedbackService(bot)
        # エラー報告向けの既定カテゴリを保持する
        self.default_category = (
            default_category if default_category in CATEGORIES else "bug"
        )
        # フォームを開くボタンを追加する
        self.add_item(_OpenFeedbackModalButton())
        # 既存の GitHub リンクボタンを追加する
        self.add_item(
            discord.ui.Button(
                label="GitHub / 問題報告",
                style=discord.ButtonStyle.link,
                url=GITHUB_REPO_URL,
                emoji="🐙",
            )
        )


class _OpenFeedbackModalButton(discord.ui.Button):
    """押下でフィードバック Modal を開くボタン。"""

    def __init__(self) -> None:
        # プライマリボタンとして初期化する
        super().__init__(
            label="フォームで報告 / Report via form",
            style=discord.ButtonStyle.primary,
            emoji="📋",
            custom_id="support_open_feedback_modal",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        # 親 View を取る
        view = self.view
        # SupportReportView 以外では何もしない
        if not isinstance(view, SupportReportView):
            return
        # 投稿先未設定なら Modal を開かず案内する
        if not view.service.is_configured():
            lang = resolve_interaction_lang(interaction)
            await interaction.response.send_message(
                pick_str(
                    lang,
                    ja=(
                        "❌ フィードバック送信先が未設定です。"
                        "GitHub リンクから Issue を作成するか、管理者に設定を依頼してください。"
                    ),
                    en=(
                        "❌ Feedback destination is not configured. "
                        "Use the GitHub link or ask an admin to set channel_ids."
                    ),
                ),
                ephemeral=True,
            )
            return
        # クールダウン中なら案内する
        remaining = view.service.check_cooldown(interaction.user.id)
        if remaining is not None:
            lang = resolve_interaction_lang(interaction)
            await interaction.response.send_message(
                pick_str(
                    lang,
                    ja=f"⏳ 連続投稿は少し待ってください（残り約 {remaining} 秒）。",
                    en=f"⏳ Please wait before submitting again (~{remaining}s remaining).",
                ),
                ephemeral=True,
            )
            return
        # 押したユーザーを依頼者として Modal を開く
        modal = FeedbackModal(
            service=view.service,
            category_id=view.default_category,
            requester_id=interaction.user.id,
            lang=resolve_interaction_lang(interaction),
        )
        await interaction.response.send_modal(modal)


def create_support_report_view(
    bot: commands.Bot,
    *,
    default_category: str = "bug",
) -> SupportReportView:
    """エラー表示などに付けるサポート View を生成する。"""
    # View を生成して返す
    return SupportReportView(bot, default_category=default_category)


def support_footer_text() -> str:
    """サポート誘導フッター文言を返す。"""
    # 定数を返す
    return SUPPORT_FOOTER_TEXT
