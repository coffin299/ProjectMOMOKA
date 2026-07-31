# PLANA/utilities/slash_command_cog.py
import datetime
import logging
import random
import re
import time
from typing import Optional, List, Dict, Any

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

# /help /invite 用 Components V2 LayoutView と招待 URL 解決
from MOMOKA.utilities.help_view import (
    HelpLayoutView,
    InviteLayoutView,
    resolve_invite_urls,
)
from MOMOKA.utilities.locale import pick_str, resolve_interaction_lang
# フィードバック Modal / 複数チャンネル投稿
from MOMOKA.utilities.feedback import (
    CATEGORIES,
    FeedbackModal,
    FeedbackService,
    category_label,
    create_support_report_view,
    support_footer_text,
)

logger = logging.getLogger(__name__)


class SlashCommandsCog(commands.Cog, name="slash_commands"):
    # get_cog 用の正式名
    COG_NAME = "slash_commands"

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.session = aiohttp.ClientSession()
        # フィードバック投稿サービスを初期化する
        self.feedback_service = FeedbackService(bot)

        # slash_commands 配下とトップレベル両方から読む（後方互換）
        slash_cfg = self.bot.config.get("slash_commands") or {}

        def _cfg(key: str, default: Optional[str] = None) -> Optional[str]:
            # ネスト設定を優先し、無ければトップレベル、それも無ければ default
            if slash_cfg.get(key) is not None:
                return slash_cfg.get(key)
            if self.bot.config.get(key) is not None:
                return self.bot.config.get(key)
            return default

        # /updates 用リポジトリ（MOMOKA本体のコミット履歴）
        self.updates_repository = _cfg(
            "updates_repository_url",
            "https://github.com/coffin299/ProjectMOMOKA",
        )
        self.support_x_url = _cfg("support_x_url", "https://x.com/coffin299")
        self.support_discord_id = _cfg("support_discord_id", "coffin299")

        # bots.*.invite_url の有無を起動時に確認する（単一 bot_invite_url は廃止）
        plana_invite, arona_invite = resolve_invite_urls(bot)
        if not plana_invite and not arona_invite:
            logger.error(
                "CRITICAL: bots.plana.invite_url / bots.arona.invite_url が未設定です。"
                "/invite コマンドは機能しません。"
            )
        # feedback.channel_ids 未設定を起動時に警告する
        if not self.feedback_service.is_configured():
            logger.warning(
                "feedback.channel_ids が空です。/feedback と LLM feedback ツールは投稿できません。"
            )

    async def cog_unload(self) -> None:
        await self.session.close()

    async def get_prefix_from_config(self) -> str:
        prefix = "!!"
        if hasattr(self.bot, 'config') and self.bot.config:
            cfg_prefix = self.bot.config.get('prefix')
            if isinstance(cfg_prefix, str) and cfg_prefix:
                prefix = cfg_prefix
        return prefix

    def _add_support_footer(self, embed: discord.Embed) -> None:
        """embedにサポート誘導フッターを追加"""
        current_footer = embed.footer.text if embed.footer else ""
        # フォーム + GitHub 誘導文言を付ける
        support_text = "\n" + support_footer_text()
        embed.set_footer(text=current_footer + support_text if current_footer else support_footer_text())

    def _create_support_view(self) -> discord.ui.View:
        """フィードバック Modal ボタンと GitHub リンクを含む View を作成"""
        # 共有 SupportReportView を返す
        return create_support_report_view(self.bot)

    def _get_single_recruit(self, guaranteed_star2: bool = False) -> int:
        if guaranteed_star2:
            population = [3, 2]
            weights = [3.0, 18.5]
            return random.choices(population, weights=weights, k=1)[0]
        else:
            population = [3, 2, 1]
            weights = [3.0, 18.5, 78.5]
            return random.choices(population, weights=weights, k=1)[0]

    @app_commands.command(name="gacha",
                          description="Recruits students like in Blue Archive.")
    @app_commands.describe(rolls="Select the number of recruitments.")
    @app_commands.choices(rolls=[
        app_commands.Choice(name="10 Rolls / 10回募集", value=10),
        app_commands.Choice(name="1 Roll / 1回募集", value=1),
    ])
    async def gacha(self, interaction: discord.Interaction, rolls: app_commands.Choice[int]):
        await interaction.response.defer(ephemeral=False)
        num_rolls = rolls.value
        results = []
        if num_rolls == 10:
            for _ in range(9):
                results.append(self._get_single_recruit())
            results.append(self._get_single_recruit(guaranteed_star2=True))
            random.shuffle(results)
        else:
            results.append(self._get_single_recruit())

        has_star_3 = 3 in results
        embed_color = discord.Color.from_rgb(230, 13, 138) if has_star_3 else discord.Color.gold()

        rarity_to_emoji = {1: "🟦", 2: "🟨", 3: "🟪"}
        emoji_results = [rarity_to_emoji[r] for r in results]

        if num_rolls == 10:
            result_text = "".join(emoji_results[:5]) + "\n" + "".join(emoji_results[5:])
        else:
            result_text = emoji_results[0]

        embed = discord.Embed(title="生徒募集 結果 / Recruitment Results",
                              description=f"{interaction.user.mention} 先生の募集結果です。",
                              color=embed_color)
        embed.add_field(name="結果 / Results", value=result_text, inline=False)
        embed.set_footer(text="提供割合: 🟪(☆3): 3.0%, 🟨(☆2): 18.5%, 🟦(☆1): 78.5%")
        self._add_support_footer(embed)
        await interaction.followup.send(embed=embed, view=self._create_support_view())
    @app_commands.command(
        name="ping",
        description="Shows Gateway (WebSocket) and API round-trip latency.",
    )
    async def ping(self, interaction: discord.Interaction):
        # Discord Gateway WebSocket の HEARTBEAT 往復（秒→ms）
        gateway_ms = round(self.bot.latency * 1000)
        # スラッシュ応答の往復計測開始
        t0 = time.perf_counter()
        # 先に defer してから計測用に edit（ACK + 本文送信の体感に近い）
        await interaction.response.defer(ephemeral=False, thinking=False)
        # Voice WebSocket（接続中があれば平均）
        voice_samples: List[float] = []
        for vc in self.bot.voice_clients:
            try:
                if not vc.is_connected():
                    continue
                lat = float(vc.latency)
                if lat < 0 or lat == float("inf"):
                    continue
                voice_samples.append(lat * 1000.0)
            except Exception:
                continue
        if voice_samples:
            voice_ms: Optional[int] = round(
                sum(voice_samples) / len(voice_samples)
            )
            voice_text = f"`{voice_ms}ms`"
            if len(voice_samples) > 1:
                voice_text += f" _(avg of {len(voice_samples)})_"
        else:
            voice_ms = None
            voice_text = "`N/A`"
        # 色は Gateway / Voice の大きい方（Voice 無しなら Gateway）
        color_ms = gateway_ms if voice_ms is None else max(gateway_ms, voice_ms)
        embed_color = (
            discord.Color.green()
            if color_ms < 150
            else (
                discord.Color.orange()
                if color_ms < 300
                else discord.Color.red()
            )
        )
        embed = discord.Embed(title="Pong! 🏓", color=embed_color)
        embed.add_field(
            name="Gateway",
            value=f"`{gateway_ms}ms`\nDiscord Gateway WebSocket HEARTBEAT",
            inline=True,
        )
        embed.add_field(
            name="WebSocket (Voice)",
            value=f"{voice_text}\nVoice server HEARTBEAT",
            inline=True,
        )
        # 実行日時をフッター先頭に記載（JST）
        executed_at = datetime.datetime.now(
            datetime.timezone(datetime.timedelta(hours=9))
        )
        embed.set_footer(
            text=f"Executed at {executed_at.strftime('%Y-%m-%d %H:%M:%S')} JST"
        )
        self._add_support_footer(embed)
        await interaction.followup.send(
            embed=embed,
            view=self._create_support_view(),
        )
        # defer〜followup の所要（参考ログ）
        api_ms = round((time.perf_counter() - t0) * 1000)
        logger.info(
            "/ping gateway=%sms voice=%s api=%sms (User: %s)",
            gateway_ms,
            f"{voice_ms}ms" if voice_ms is not None else "N/A",
            api_ms,
            interaction.user.id,
        )

    @app_commands.command(name="serverinfo",
                          description="Displays information about the current server.")
    async def serverinfo(self, interaction: discord.Interaction):
        if not interaction.guild:
            await interaction.response.send_message(
                "このコマンドはサーバー内でのみ使用できます。\nThis command can only be used within a server.",
                ephemeral=False)
            return
        guild = interaction.guild
        embed = discord.Embed(title=f"{guild.name} のサーバー情報 / Server Information", color=discord.Color.blue())
        if guild.icon: embed.set_thumbnail(url=guild.icon.url)
        embed.add_field(name="サーバーID / Server ID", value=guild.id, inline=True)
        owner_display = "不明 / Unknown"
        if guild.owner:
            owner_display = guild.owner.mention
        elif guild.owner_id:
            try:
                owner_user = await self.bot.fetch_user(guild.owner_id)
                owner_display = owner_user.mention if owner_user else f"ID: {guild.owner_id}"
            except discord.NotFound:
                owner_display = f"ID: {guild.owner_id} (取得不可 / Not found)"
            except Exception as e:
                logger.warning(f"オーナー情報の取得に失敗 (ID: {guild.owner_id}): {e}")
                owner_display = f"ID: {guild.owner_id} (エラー / Error)"
        embed.add_field(name="オーナー / Owner", value=owner_display, inline=True)
        embed.add_field(name="メンバー数 / Member Count", value=guild.member_count, inline=True)
        embed.add_field(name="テキストチャンネル数 / Text Channels", value=len(guild.text_channels), inline=True)
        embed.add_field(name="ボイスチャンネル数 / Voice Channels", value=len(guild.voice_channels), inline=True)
        embed.add_field(name="ロール数 / Roles", value=len(guild.roles), inline=True)
        created_at_text = discord.utils.format_dt(guild.created_at, style='F')
        embed.add_field(name="作成日時 / Created At", value=created_at_text, inline=False)
        verification_level_str_en = guild.verification_level.name.replace('_', ' ').capitalize()
        embed.add_field(name="認証レベル / Verification Level", value=f"{verification_level_str_en}", inline=True)
        if guild.features:
            features_str = ", ".join(f"`{f.replace('_', ' ').title()}`" for f in guild.features)
            embed.add_field(name="サーバー機能 / Server Features", value=features_str, inline=False)
        self._add_support_footer(embed)
        await interaction.response.send_message(embed=embed, view=self._create_support_view(), ephemeral=False)
        logger.info(f"/serverinfo が実行されました。 (Server: {guild.id}, User: {interaction.user.id})")

    @app_commands.command(name="userinfo",
                          description="Displays information about the specified user.")
    @app_commands.describe(
        user="User to display information for (optional, defaults to you).")
    async def userinfo(self, interaction: discord.Interaction, user: Optional[discord.User] = None):
        target_user = user or interaction.user
        embed = discord.Embed(title=f"{target_user.display_name} のユーザー情報 / User Information",
                              color=target_user.accent_color or discord.Color.blurple())
        if target_user.display_avatar: embed.set_thumbnail(url=target_user.display_avatar.url)
        username_display = f"{target_user.name}#{target_user.discriminator}" if target_user.discriminator != '0' else target_user.name
        embed.add_field(name="ユーザー名 / Username", value=username_display, inline=True)
        embed.add_field(name="ユーザーID / User ID", value=target_user.id, inline=True)
        is_bot, is_bot_en = ("はい", "Yes") if target_user.bot else ("いいえ", "No")
        embed.add_field(name="Botアカウントか / Bot Account?", value=f"{is_bot} / {is_bot_en}", inline=True)
        created_at_text = discord.utils.format_dt(target_user.created_at, style='F')
        embed.add_field(name="アカウント作成日時 / Account Created", value=created_at_text, inline=False)
        if interaction.guild and isinstance(target_user, discord.Member):
            member: discord.Member = target_user
            joined_at_text = discord.utils.format_dt(member.joined_at,
                                                     style='F') if member.joined_at else "不明 / Unknown"
            embed.add_field(name="サーバー参加日時 / Joined Server", value=joined_at_text, inline=False)
            roles = [r.mention for r in reversed(member.roles) if r.name != "@everyone"]
            roles_count = len(roles)
            roles_display_value = "なし / None"
            if roles:
                roles_str = ", ".join(roles)
                roles_display_value = roles_str[:1017] + "..." if len(roles_str) > 1020 else roles_str
            embed.add_field(name=f"ロール ({roles_count}) / Roles ({roles_count})", value=roles_display_value,
                            inline=False)
            if member.bot:
                evaluation_lines = [
                    "✅ **認証済みBot** / Verified Bot" if member.public_flags.verified_bot else "❌ **未認証Bot** / Unverified Bot",
                    "👑 **管理者権限** / Administrator Privileges" if member.guild_permissions.administrator else "🔧 **標準権限** / Standard Privileges"]
                embed.add_field(name="Botの評価 / Bot Evaluation", value="\n".join(evaluation_lines), inline=False)
            else:
                # メンバーインテントと完全なキャッシュがある場合だけ順位を信用する
                can_calculate_join_rank = (
                    member.joined_at is not None
                    and self.bot.intents.members
                    and interaction.guild.chunked
                )
                # 信頼できない場合もフィールドを残して不明と明示する
                join_rank_value = "不明 / Unknown"
                if can_calculate_join_rank:
                    # 参加日時の昇順で参加順位を計算する
                    sorted_members = sorted(interaction.guild.members,
                                            key=lambda m: m.joined_at or datetime.datetime.max.replace(
                                                tzinfo=datetime.timezone.utc))
                    # 対象メンバーが完全なキャッシュ内にあれば順位を表示する
                    if member in sorted_members:
                        join_position = sorted_members.index(member) + 1
                        join_rank_value = f"{join_position}番目 / th"
                # 常に参加順位フィールドを表示し、算出不可時は不明とする
                embed.add_field(
                    name="参加順位 / Join Rank",
                    value=join_rank_value,
                    inline=True,
                )
                perms = member.guild_permissions
                notable_perms_ja = {"管理者": perms.administrator, "サーバー管理": perms.manage_guild,
                                    "ロール管理": perms.manage_roles, "追放": perms.kick_members,
                                    "BAN": perms.ban_members}
                user_perms = [name for name, has_perm in notable_perms_ja.items() if has_perm]
                perms_display = "なし / None"
                if user_perms: perms_display = "✅ **管理者**" if "管理者" in user_perms else ", ".join(user_perms)
                embed.add_field(name="重要な権限 / Key Permissions", value=perms_display, inline=False)
                if member.timed_out_until:
                    timeout_text = discord.utils.format_dt(member.timed_out_until, style='R')
                    embed.add_field(name="⏳ タイムアウト中 / Timed Out", value=f"終了: {timeout_text}", inline=True)
            if member.nick: embed.add_field(name="ニックネーム / Nickname", value=member.nick, inline=True)
            if member.premium_since:
                premium_text = discord.utils.format_dt(member.premium_since, style='R')
                embed.add_field(name="サーバーブースト開始 / Server Boosting Since", value=premium_text, inline=True)
        self._add_support_footer(embed)
        await interaction.response.send_message(embed=embed, view=self._create_support_view(), ephemeral=False)
        logger.info(f"/userinfo が実行されました。 (TargetUser: {target_user.id}, Requester: {interaction.user.id})")

    @app_commands.command(name="avatar",
                          description="Displays the avatar of the specified user.")
    @app_commands.describe(
        user="User whose avatar to display (optional, defaults to you).")
    async def avatar_command(self, interaction: discord.Interaction, user: Optional[discord.User] = None):
        target_user = user or interaction.user
        avatar_url = target_user.display_avatar.url
        embed = discord.Embed(title=f"{target_user.display_name} のアバター / Avatar",
                              color=target_user.accent_color or discord.Color.default())
        embed.set_image(url=avatar_url)
        embed.add_field(name="画像URL / Image URL", value=f"[リンク / Link]({avatar_url})")
        self._add_support_footer(embed)
        await interaction.response.send_message(embed=embed, view=self._create_support_view(), ephemeral=False)
        logger.info(f"/avatar が実行されました。 (TargetUser: {target_user.id}, Requester: {interaction.user.id})")

    @app_commands.command(
        name="feedback",
        description="Send a bug report or feature request.",
    )
    @app_commands.describe(
        category="Report category.",
    )
    @app_commands.choices(
        category=[
            app_commands.Choice(name="Bug report / 不具合報告", value="bug"),
            app_commands.Choice(name="Feature request / 機能リクエスト", value="feature_request"),
            app_commands.Choice(name="Other / その他", value="other"),
        ]
    )
    async def feedback_slash(
        self,
        interaction: discord.Interaction,
        category: app_commands.Choice[str],
    ) -> None:
        # 投稿先未設定なら Modal を開かず案内する
        if not self.feedback_service.is_configured():
            await interaction.response.send_message(
                "❌ フィードバック送信先が未設定です。"
                "管理者に `feedback.channel_ids` の設定を依頼してください。\n"
                "❌ Feedback destination is not configured. "
                "Ask an admin to set `feedback.channel_ids`.",
                ephemeral=True,
            )
            return
        # クールダウン中なら案内する
        remaining = self.feedback_service.check_cooldown(interaction.user.id)
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
        # カテゴリ ID を確定する
        category_id = category.value if category.value in CATEGORIES else "other"
        # Modal を開く（Interaction 必須）
        modal = FeedbackModal(
            service=self.feedback_service,
            category_id=category_id,
            requester_id=interaction.user.id,
            lang=resolve_interaction_lang(interaction),
        )
        await interaction.response.send_modal(modal)
        # 実行ログを残す
        logger.info(
            "/feedback opened modal category=%s user=%s",
            category_id,
            interaction.user.id,
        )

    @app_commands.command(name="support",
                          description="Shows how to contact the developer.")
    async def support_contact_slash(self, interaction: discord.Interaction) -> None:
        # GitHubリポジトリURLを問い合わせ先として使用
        github_url = "https://github.com/coffin299/ProjectMOMOKA"
        github_issues_url = "https://github.com/coffin299/ProjectMOMOKA/issues"

        embed = discord.Embed(
            title="💬 サポート / Support",
            description=(
                "Botに関するご質問・ご要望・不具合報告などは、ボット内フォーム・GitHub・以下の方法でお気軽にお問い合わせください。\n\n"
                "For questions, requests, or bug reports, use the in-bot form, GitHub, or the contacts below."
            ),
            color=discord.Color.blurple()
        )

        # GitHubリポジトリのアイコン等は省略（サーバーアイコンの代わり）
        bot_user = self.bot.user
        if bot_user and bot_user.avatar:
            embed.set_thumbnail(url=bot_user.avatar.url)

        embed.add_field(
            name="📋 ボット内フォーム / In-bot form",
            value=(
                f"`/feedback` — カテゴリを選んで Modal から送信（開発者サーバーへ配信）\n"
                f"`/feedback` — pick a category and submit via Modal (delivered to developer servers)\n"
                f"カテゴリ例: {category_label('bug')}, {category_label('feature_request')}, {category_label('other')}"
            ),
            inline=False,
        )

        embed.add_field(
            name="🐙 GitHub リポジトリ / GitHub Repository",
            value=f"不具合報告・機能要望はIssueで受け付けています！\nBug reports & feature requests are welcome via Issues!\n\n**[GitHub Issues]({github_issues_url})**",
            inline=False
        )

        embed.add_field(
            name="🐦 X (Twitter)",
            value=f"[**@coffin299**]({self.support_x_url})\nDMまたはメンションでお問い合わせください。\nContact via DM or mention.",
            inline=True
        )

        embed.add_field(
            name="💬 Discord DM",
            value=f"**`{self.support_discord_id}`**\nDiscordのDMでお問い合わせください。\nContact via Discord DM.",
            inline=True
        )

        embed.add_field(
            name="📝 ご連絡時のお願い / When Contacting",
            value="• Botを使用しているサーバー名\n• 具体的な問題や要望の内容\n• スクリーンショット（あれば）\n\n• Server name where you're using the bot\n• Specific issue or request details\n• Screenshots (if available)",
            inline=False
        )

        embed.set_footer(text="お気軽にお問い合わせください！ / Feel free to contact us!")

        view = discord.ui.View()
        view.add_item(discord.ui.Button(
            label="GitHub で報告 / Report on GitHub",
            style=discord.ButtonStyle.link,
            url=github_issues_url,
            emoji="🐙"
        ))
        view.add_item(discord.ui.Button(
            label="X (Twitter)で連絡 / Contact on X",
            style=discord.ButtonStyle.link,
            url=self.support_x_url,
            emoji="🐦"
        ))

        await interaction.response.send_message(embed=embed, view=view, ephemeral=False)
        logger.info(f"/support が実行されました。 (User: {interaction.user.id})")

    @app_commands.command(name="invite",
                          description="Shows invite links for PLANA and ARONA.")
    async def invite_bot_slash(self, interaction: discord.Interaction) -> None:
        # app → guild → en で表示言語を決める
        lang = resolve_interaction_lang(interaction)
        # Components V2 は embed 併用不可のため view のみ送信する
        view = InviteLayoutView(self.bot, lang=lang)
        # LayoutView メッセージを返す
        await interaction.response.send_message(view=view)
        # 実行ログ
        logger.info(
            f"/invite が実行されました。 (User: {interaction.user.id}, lang={lang})"
        )

    @app_commands.command(name="updates",
                          description="Shows the bot's latest update history (commit log).")
    async def updates(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=False)

        if not self.updates_repository:
            await interaction.followup.send(
                "エラー: リポジトリのURLが設定されていません。\nError: The repository URL is not configured.",
                ephemeral=False)
            logger.warning(f"/updates が実行されましたが、リポジトリURLが未設定です。 (User: {interaction.user.id})")
            return

        repo_match = re.match(r"https://github\.com/([^/]+)/([^/]+)", self.updates_repository)
        if not repo_match:
            await interaction.followup.send(
                "エラー: 設定されているリポジトリURLの形式が正しくありません。\nError: The configured repository URL format is invalid.",
                ephemeral=False)
            logger.warning(
                f"/updates が実行されましたが、リポジトリURLの形式が不正です: {self.updates_repository} (User: {interaction.user.id})")
            return

        owner, repo = repo_match.groups()
        api_url = f"https://api.github.com/repos/{owner}/{repo}/commits"

        try:
            async with self.session.get(api_url) as response:
                if response.status == 200:
                    commits: List[Dict[str, Any]] = await response.json()
                    embed = discord.Embed(
                        title="📜 アップデート履歴 / Update History",
                        description=f"最新のコミット25件を表示しています。\nShowing the 25 most recent commits from the [{repo}]({self.updates_repository}) repository.",
                        color=discord.Color.blue()
                    )

                    for commit_data in commits[:25]:
                        sha = commit_data['sha'][:7]
                        message = commit_data['commit']['message'].split('\n')[0]
                        author = commit_data['commit']['author']['name']
                        html_url = commit_data['html_url']

                        date_str = commit_data['commit']['author']['date']
                        commit_date = datetime.datetime.fromisoformat(date_str.replace('Z', '+00:00'))

                        timestamp = discord.utils.format_dt(commit_date, style='R')

                        if len(message) > 80:
                            message = message[:77] + "..."

                        embed.add_field(
                            name=f"📝 `{sha}` by {author} ({timestamp})",
                            value=f"[{message}]({html_url})",
                            inline=False
                        )

                    self._add_support_footer(embed)
                    await interaction.followup.send(embed=embed, view=self._create_support_view())
                    logger.info(f"/updates が正常に実行されました。 (User: {interaction.user.id})")

                else:
                    error_data = await response.json()
                    error_message = error_data.get("message", "Unknown error")
                    await interaction.followup.send(
                        f"エラー: GitHub APIからの情報取得に失敗しました (ステータス: {response.status})。\n`{error_message}`\n\nError: Failed to fetch data from GitHub API (Status: {response.status}).",
                        ephemeral=False)
                    logger.error(
                        f"/updates の実行中にGitHub APIエラーが発生しました (Status: {response.status}): {error_message}")

        except aiohttp.ClientError as e:
            await interaction.followup.send(
                "エラー: GitHub APIへの接続中に問題が発生しました。\nError: An issue occurred while connecting to the GitHub API.",
                ephemeral=False)
            logger.error(f"/updates の実行中に接続エラーが発生しました: {e}")

    @app_commands.command(name="help",
                          description="Displays help information for the bot.")
    async def help_slash_command(self, interaction: discord.Interaction):
        # app → guild → en で初期表示言語を決める
        initial_lang = resolve_interaction_lang(interaction)
        # Components V2 LayoutView のみ送信（embed 非併用）
        view = HelpLayoutView(self.bot, page=0, lang=initial_lang)
        # ヘルプパネルを返す
        await interaction.response.send_message(view=view)
        # 実行ログ
        logger.info(
            f"/help が実行されました。 (User: {interaction.user.id}, lang={initial_lang})"
        )


async def setup(bot: commands.Bot):
    if not hasattr(bot, 'config') or not bot.config:
        logger.error("SlashCommandsCog: Botインスタンスに 'config' 属性が見つからないか空です。Cogをロードできません。")
        raise commands.ExtensionFailed("SlashCommandsCog", "Botのconfigがロードされていません。")

    cog = SlashCommandsCog(bot)
    await bot.add_cog(cog)
    logger.info("SlashCommandsCogが正常にロードされました。")