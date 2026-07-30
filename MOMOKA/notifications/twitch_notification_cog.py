# MOMOKA/notification/twitch_notification.py
import asyncio
import json
import logging
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks

# エラーハンドラをインポート
from MOMOKA.notifications.error.twitch_errors import (ConfigError, DataParsingError,
                                  NotificationError, TwitchAPIError,
                                  TwitchExceptionHandler)
from MOMOKA.storage import NS_TWITCH_SETTINGS, resolve_settings_db

# ロガーの設定
logger = logging.getLogger(__name__)

# --- 定数 ---
TWITCH_API_BASE_URL = "https://api.twitch.tv/helix"
TWITCH_AUTH_URL = "https://id.twitch.tv/oauth2/token"


def _is_valid_twitch_credential(value: Any) -> bool:
    """Twitch 認証情報が実運用可能な値かを判定する。"""
    # 文字列以外は認証情報として使えないため無効にする
    if not isinstance(value, str):
        return False
    # 空白だけの値とテンプレート用プレースホルダーを拒否する
    return bool(value.strip()) and not value.strip().upper().startswith("YOUR_")


class TwitchNotification(commands.Cog):
    """Twitchの配信開始を通知するCog"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.handler = TwitchExceptionHandler(self)
        self.session: aiohttp.ClientSession = aiohttp.ClientSession()
        # 設定ファイルの同時保存を直列化する
        self._settings_lock = asyncio.Lock()
        # SettingsDB
        self.settings_db = resolve_settings_db(bot)

        # Twitch API認証情報をconfigから取得
        twitch_config = bot.config.get('twitch', {})
        configured_client_id = twitch_config.get('client_id')
        configured_client_secret = twitch_config.get('client_secret')
        # YOUR_* を含む雛形値では Twitch API へ接続しない
        self.client_id = (
            configured_client_id
            if _is_valid_twitch_credential(configured_client_id)
            else None
        )
        self.client_secret = (
            configured_client_secret
            if _is_valid_twitch_credential(configured_client_secret)
            else None
        )
        self.access_token: Optional[str] = None
        self.token_expires_at: int = 0

        # 設定の読み込み
        ### 変更箇所: データ構造の変更に対応 (型ヒントをより具体的に) ###
        # 構造: {guild_id: {twitch_user_id: {setting_data}}}
        self.settings: Dict[int, Dict[str, Dict[str, Any]]] = self._load_settings()

        # 認証情報がなければタスクを開始しない
        if not self.client_id or not self.client_secret:
            pass
        else:
            self.check_streams.start()

    # --- Cogのライフサイクルイベント ---
    async def cog_unload(self):
        """Cogがアンロードされるときに呼ばれる"""
        self.check_streams.cancel()
        await self.session.close()

    # --- 設定管理 ---
    def _load_settings(self) -> Dict[int, Dict[str, Dict[str, Any]]]:
        """設定を SettingsDB から読み込む"""
        try:
            # namespace から取る
            raw = self.settings_db.load(NS_TWITCH_SETTINGS)
            # 無ければ空
            if raw is None:
                return {}
            # dict でなければ空
            if not isinstance(raw, dict):
                return {}
            # JSONのキーは文字列なので、guild_id(トップレベルのキー)をintに変換
            return {int(k): v for k, v in raw.items()}
        except Exception as e:  # noqa: BLE001
            logger.error("Twitch設定の読み込みに失敗しました: %s", e)
            return {}

    async def _save_settings(self) -> None:
        """設定を SettingsDB へ排他的に保存する。"""
        # 同時実行されるコマンドと定期チェックの保存競合を防ぐ
        async with self._settings_lock:
            try:
                # ギルド ID キーを文字列にして保存する（JSON 互換）
                payload = {str(k): v for k, v in self.settings.items()}
                await self.settings_db.save_async(NS_TWITCH_SETTINGS, payload)
            except Exception as e:  # noqa: BLE001
                logger.error("Twitch設定の保存に失敗しました: %s", e)

    # --- Twitch API 関連 ---
    # (このセクションのコードは変更ありません)
    async def _get_twitch_access_token(self):
        """Twitch APIのアプリアクセストークンを取得・更新する"""
        if self.access_token and time.time() < self.token_expires_at:
            return

        logger.info("Twitch APIのアクセストークンを更新します。")
        params = {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "grant_type": "client_credentials",
        }
        try:
            async with self.session.post(TWITCH_AUTH_URL, params=params) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    self.access_token = data["access_token"]
                    # 期限の1分前に更新するようにマージンを設定
                    self.token_expires_at = time.time() + data["expires_in"] - 60
                    logger.info("Twitch APIのアクセストークンを更新しました。")
                else:
                    text = await resp.text()
                    raise self.handler.handle_api_response_error(resp.status, TWITCH_AUTH_URL, text)
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            raise self.handler.handle_api_error(e, "アクセストークン取得")

    async def _api_request(self, endpoint: str, params: Optional[Dict] = None) -> Dict:
        """Twitch APIへのリクエストを共通化する"""
        await self._get_twitch_access_token()
        headers = {
            "Client-ID": self.client_id,
            "Authorization": f"Bearer {self.access_token}",
        }
        url = f"{TWITCH_API_BASE_URL}/{endpoint}"

        try:
            async with self.session.get(url, headers=headers, params=params) as resp:
                if resp.status == 200:
                    return await resp.json()
                if resp.status == 401:
                    self.access_token = None
                text = await resp.text()
                raise self.handler.handle_api_response_error(resp.status, url, text)
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            raise self.handler.handle_api_error(e, f"APIリクエスト: {endpoint}")
        except json.JSONDecodeError as e:
            raise self.handler.handle_json_decode_error(e, f"APIリクエスト: {endpoint}")

    async def get_user_data(self, login_name: str) -> Optional[Dict]:
        """Twitchのログイン名からユーザーデータを取得する"""
        response = await self._api_request("users", params={"login": login_name})
        if response and response.get("data"):
            return response["data"][0]
        return None

    async def get_stream_data(self, user_id: str) -> Optional[Dict]:
        """ユーザーIDから現在の配信データを取得する"""
        response = await self._api_request("streams", params={"user_id": user_id})
        if response and response.get("data"):
            return response["data"][0]
        return None

    # --- バックグラウンドタスク ---
    @tasks.loop(minutes=1)
    async def check_streams(self):
        """定期的に配信ステータスをチェックする"""
        if not self.settings:
            return

        ### 変更箇所: 複数チャンネル対応 ###
        # チェック対象の全ユーザーIDを重複なく収集する
        user_ids_to_check = set()
        for guild_settings in self.settings.values():
            for user_id in guild_settings.keys():
                user_ids_to_check.add(user_id)

        if not user_ids_to_check:
            return

        try:
            # Helix の user_id 指定上限である100件ごとに配信情報を取得する
            live_streams = {}
            user_ids = sorted(user_ids_to_check)
            for start_index in range(0, len(user_ids), 100):
                # 現在の100件だけを同じキーのクエリパラメータで渡す
                user_id_chunk = user_ids[start_index:start_index + 100]
                response = await self._api_request(
                    "streams",
                    params=[("user_id", user_id) for user_id in user_id_chunk],
                )
                # 取得済みの配信情報を全ギルドで再利用できるよう ID をキーにする
                live_streams.update(
                    {
                        stream["user_id"]: stream
                        for stream in response.get("data", [])
                    }
                )

            settings_changed = False
            # 全てのサーバー、全てのチャンネル設定をチェック
            for guild_id, guild_settings in self.settings.items():
                for user_id, stream_config in guild_settings.items():
                    channel = self.bot.get_channel(stream_config["notification_channel_id"])
                    if not channel:
                        logger.warning(
                            f"ギルド {guild_id} の通知チャンネル {stream_config['notification_channel_id']} が見つかりません。設定をスキップします。")
                        continue

                    last_status = stream_config.get("last_status", "offline")
                    stream_data = live_streams.get(user_id)

                    # 配信が開始された場合
                    if stream_data and last_status == "offline":
                        logger.info(
                            f"{stream_data['user_name']} が配信を開始しました。ギルド {guild_id} のチャンネル {channel.id} に通知します。")
                        await self._send_notification(channel, stream_data)
                        stream_config["last_status"] = "online"
                        settings_changed = True

                    # 配信が終了した場合
                    elif not stream_data and last_status == "online":
                        logger.info(f"{stream_config['twitch_display_name']} の配信が終了しました。")
                        stream_config["last_status"] = "offline"
                        settings_changed = True

            # 変更があった場合のみファイルに保存する
            if settings_changed:
                await self._save_settings()

        except TwitchAPIError as e:
            logger.warning(f"配信チェック中にAPIエラーが発生しました: {e}")
        except Exception as e:
            self.handler.log_generic_error(e, "配信チェックタスク")

    @check_streams.before_loop
    async def before_check_streams(self):
        await self.bot.wait_until_ready()

    # --- 通知機能 ---
    # (このセクションのコードは変更ありません)
    async def _send_notification(self, channel: discord.TextChannel, stream_data: Dict):
        """通知メッセージを送信する"""
        embed = discord.Embed(
            title=f"🔴LIVE: {stream_data['title']}",
            url=f"https://www.twitch.tv/{stream_data['user_login']}",
            color=discord.Color.purple()
        )
        embed.set_author(
            name=stream_data['user_name'],
            url=f"https://www.twitch.tv/{stream_data['user_login']}"
        )
        embed.add_field(name="ゲーム", value=stream_data.get('game_name', 'N/A'), inline=True)
        embed.add_field(name="視聴者数", value=stream_data.get('viewer_count', 'N/A'), inline=True)

        thumbnail_url = stream_data['thumbnail_url'].replace('{width}', '1280').replace('{height}', '720')
        embed.set_image(url=f"{thumbnail_url}?t={int(time.time())}")

        embed.set_footer(text="Twitch配信通知")
        embed.timestamp = discord.utils.utcnow()

        try:
            guild_settings = self.settings.get(channel.guild.id, {})
            # 該当する設定を探してカスタムメッセージを取得
            custom_message = f"{stream_data['user_name']}が配信を開始しました！"  # デフォルト
            for config in guild_settings.values():
                if config.get('twitch_login_name') == stream_data['user_login']:
                    custom_message = config.get("message", custom_message)
                    break

            await channel.send(custom_message, embed=embed)
        except discord.Forbidden:
            logger.error(f"チャンネル {channel.id} への通知送信に失敗しました: 権限がありません。")
        except discord.HTTPException as e:
            logger.error(f"チャンネル {channel.id} への通知送信に失敗しました: {e}")

    ### 変更箇所: コマンド引数の入力補完用メソッドを追加 ###
    async def twitch_channel_autocomplete(
            self, interaction: discord.Interaction, current: str
    ) -> List[app_commands.Choice[str]]:
        """設定済みのTwitchチャンネルを候補として表示する"""
        guild_id = interaction.guild_id
        if guild_id not in self.settings:
            return []

        choices = []
        for user_id, config in self.settings[guild_id].items():
            name = config.get("twitch_display_name", user_id)
            # 入力された文字がチャンネル名に含まれていれば候補に出す
            if current.lower() in name.lower():
                choices.append(app_commands.Choice(name=name, value=user_id))

        # Discordの制限は25件まで
        return choices[:25]

    # --- スラッシュコマンド ---
    @app_commands.command(name="twitch_set", description="Set up Twitch stream notifications.")
    @app_commands.describe(
        twitch_url="Twitch channel URL to watch (e.g. https://www.twitch.tv/twitch).",
        notification_channel="Discord channel to send notifications to.",
        message="Optional custom message (e.g. mentions) for notifications."
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    async def set_notification(self, interaction: discord.Interaction, twitch_url: str,
                               notification_channel: discord.TextChannel, message: Optional[str] = None):
        """配信通知を設定するコマンド"""
        await interaction.response.defer()
        guild_id = interaction.guild_id

        try:
            parsed_url = urlparse(twitch_url)
            if parsed_url.netloc not in ("www.twitch.tv", "twitch.tv"):
                raise ConfigError("無効なTwitchチャンネルURLです。")
            # URL の後続パスを API のログイン名として誤用しない
            login_name = parsed_url.path.strip("/").split("/", maxsplit=1)[0]
            if not login_name:
                raise ConfigError("URLからチャンネル名を特定できませんでした。")

            user_data = await self.get_user_data(login_name)
            if not user_data:
                raise TwitchAPIError(f"Twitchユーザー '{login_name}' が見つかりませんでした。")

            # サーバー用の設定辞書がなければ作成
            if guild_id not in self.settings:
                self.settings[guild_id] = {}

            # ユーザーIDをキーにして設定を保存（または上書き）
            new_setting = {
                "twitch_login_name": user_data["login"],
                "twitch_display_name": user_data["display_name"],
                "notification_channel_id": notification_channel.id,
                "last_status": "offline",
            }
            if message:
                new_setting["message"] = message

            self.settings[guild_id][user_data["id"]] = new_setting
            await self._save_settings()

            embed = discord.Embed(
                title="✅ Twitch通知設定完了",
                description=f"**{user_data['display_name']}** の配信が開始されたら、{notification_channel.mention} に通知します。",
                color=discord.Color.green()
            )
            if message:
                embed.add_field(name="カスタムメッセージ", value=message, inline=False)

            await interaction.followup.send(embed=embed)

        except Exception as e:
            message = self.handler.get_user_friendly_message(e)
            await interaction.followup.send(message)

    ### 変更箇所: twitch_removeコマンドを修正 ###
    @app_commands.command(name="twitch_remove", description="Remove Twitch stream notification settings.")
    @app_commands.describe(twitch_channel="Twitch channel to remove.")
    @app_commands.autocomplete(twitch_channel=twitch_channel_autocomplete)
    @app_commands.checks.has_permissions(manage_guild=True)
    async def remove_notification(self, interaction: discord.Interaction, twitch_channel: str):
        """配信通知を解除するコマンド"""
        guild_id = interaction.guild_id
        # twitch_channel引数にはautocompleteからtwitch_user_idが渡される

        if guild_id in self.settings and twitch_channel in self.settings[guild_id]:
            removed_channel_name = self.settings[guild_id][twitch_channel].get("twitch_display_name", twitch_channel)

            # 設定を削除
            del self.settings[guild_id][twitch_channel]

            # もしサーバーの設定が空になったら、サーバー自体のキーも削除
            if not self.settings[guild_id]:
                del self.settings[guild_id]

            await self._save_settings()
            await interaction.response.send_message(
                f"✅ **{removed_channel_name}** のTwitch配信通知の設定を解除しました。")
        else:
            await interaction.response.send_message("ℹ️ 指定されたTwitchチャンネルの通知設定は見つかりませんでした。")

    @app_commands.command(name="twitch_test", description="Send a Twitch notification test message.")
    @app_commands.describe(twitch_channel="Twitch channel to test.")
    @app_commands.autocomplete(twitch_channel=twitch_channel_autocomplete)
    @app_commands.checks.has_permissions(manage_guild=True)
    async def test_notification(self, interaction: discord.Interaction, twitch_channel: str):
        """通知のテストを行うコマンド"""
        await interaction.response.defer()
        guild_id = interaction.guild_id
        # twitch_channel引数にはautocompleteからtwitch_user_idが渡される

        if guild_id not in self.settings or twitch_channel not in self.settings[guild_id]:
            await interaction.followup.send("❌ 指定されたTwitchチャンネルの通知設定が見つかりません。")
            return

        config = self.settings[guild_id][twitch_channel]
        channel = self.bot.get_channel(config["notification_channel_id"])
        if not channel:
            await interaction.followup.send(f"❌ 通知チャンネルが見つかりません。ID: {config['notification_channel_id']}")
            return

        test_stream_data = {
            'user_name': config['twitch_display_name'],
            'user_login': config['twitch_login_name'],
            'title': 'これはテスト配信です！',
            'game_name': 'Just Chatting',
            'viewer_count': 1234,
            'thumbnail_url': 'https://static-cdn.jtvnw.net/previews-ttv/live_user_{user_login}-{width}x{height}'.format(
                user_login=config['twitch_login_name'], width=1280, height=720
            )
        }

        try:
            await self._send_notification(channel, test_stream_data)
            await interaction.followup.send(f"✅ {channel.mention} にテスト通知を送信しました。")
        except Exception as e:
            message = self.handler.get_user_friendly_message(e)
            await interaction.followup.send(message)

    @app_commands.command(name="twitch_list", description="List configured Twitch notifications.")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def list_notifications(self, interaction: discord.Interaction):
        """設定されている通知の一覧を表示するコマンド"""
        guild_id = interaction.guild_id
        if guild_id not in self.settings or not self.settings[guild_id]:
            await interaction.response.send_message("ℹ️ このサーバーにはTwitch配信通知が設定されていません。")
            return

        embed = discord.Embed(
            title=f"Twitch通知設定一覧 ({interaction.guild.name})",
            color=discord.Color.purple()
        )

        description_lines = []
        for user_id, config in self.settings[guild_id].items():
            channel = self.bot.get_channel(config.get("notification_channel_id"))
            channel_mention = channel.mention if channel else f"ID: `{config.get('notification_channel_id')}` (不明)"
            display_name = config.get('twitch_display_name', 'N/A')
            login_name = config.get('twitch_login_name', 'N/A')
            description_lines.append(f"📺 **[{display_name}](https://www.twitch.tv/{login_name})** → {channel_mention}")

        embed.description = "\n".join(description_lines)
        await interaction.response.send_message(embed=embed)


async def setup(bot: commands.Bot):
    if not hasattr(bot, 'config'):
        logger.critical("Botにconfig属性が見つかりません。config.yamlをロードしてください。")
        return

    twitch_config = bot.config.get('twitch', {})
    if not (
        _is_valid_twitch_credential(twitch_config.get("client_id"))
        and _is_valid_twitch_credential(twitch_config.get("client_secret"))
    ):
        logger.critical(
            "Twitch の認証情報(client_id, client_secret)が未設定または "
            "YOUR_* プレースホルダーです。Cogをロードしません。"
        )
        return

    await bot.add_cog(TwitchNotification(bot))