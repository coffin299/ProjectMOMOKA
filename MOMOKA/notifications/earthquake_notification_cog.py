# MOMOKA/notifications/earthquake_notification_cog.py

import asyncio
import io
import json
import logging
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Literal, Optional

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks

from MOMOKA.notifications.error.earthquake_errors import (
    EarthquakeTsunamiExceptionHandler,
    APIError,
    DataParsingError,
    ConfigError,
    NotificationError
)
# コマンドは Cog 本体に残す（不完全 mixin による上書きを避ける）
from MOMOKA.notifications.earthquake_constants import (
    ALL_NOTIFY_SCALES,
    InfoType,
    NOTIFY_SCALE_LABELS,
    NOT_FOUND_DELETE_THRESHOLD,
    notification_embed_footer,
)
from MOMOKA.notifications.earthquake_embeds import EarthquakeEmbedsMixin
from MOMOKA.notifications.earthquake_map import (
    CARTOPY_AVAILABLE,
    calculate_smart_map_extent,
    generate_map_sync,
    generate_single_map_sync,
)
from MOMOKA.notifications.earthquake_protocol import EarthquakeProtocolMixin
from MOMOKA.storage import NS_EARTHQUAKE_CONFIG, resolve_settings_db

# 既存のログ名を維持して監視設定との互換性を保つ
logger = logging.getLogger('EarthquakeTsunamiCog')


class EarthquakeTsunamiCog(
    EarthquakeEmbedsMixin,
    EarthquakeProtocolMixin,
    commands.Cog,
    name="earthquake_notifications",
):
    # get_cog 用の正式名
    COG_NAME = "earthquake_notifications"

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        logger.info("🔄 EarthquakeTsunamiCog 初期化開始...")

        # SettingsDB
        self.settings_db = resolve_settings_db(bot)
        self.config = self.load_config()

        self.last_ids: Dict[str, Optional[str]] = {
            InfoType.EEW.value: None, InfoType.QUAKE.value: None, InfoType.TSUNAMI.value: None
        }
        self.processed_ids: Dict[str, OrderedDict[str, None]] = {
            InfoType.EEW.value: OrderedDict(),
            InfoType.QUAKE.value: OrderedDict(),
            InfoType.TSUNAMI.value: OrderedDict(),
        }
        self.max_processed_ids = 1000
        # 設定ファイルの同時書き込みを直列化する
        self.config_save_lock = asyncio.Lock()
        # Discord API が返す NotFound の連続回数を通知先ごとに保持する
        self.not_found_counts: Dict[tuple[str, str], int] = {}

        self.ws_session = None
        self.ws_connection = None
        self.ws_reconnect_delay = 5
        self.ws_max_reconnect_delay = 300
        self.ws_running = False

        self.http_session = None
        self.jst = timezone(timedelta(hours=+9), 'JST')
        self.api_base_url = "https://api.p2pquake.net/v2"
        self.ws_url = "wss://api.p2pquake.net/v2/ws"
        self.request_headers = {'User-Agent': 'Discord-Bot-EarthquakeTsunami/3.0', 'Accept': 'application/json'}

        self.error_stats = {'api_errors': 0, 'parsing_errors': 0, 'network_errors': 0, 'ws_disconnects': 0,
                            'last_error_time': None}
        self.processing_stats = {'eew_processed': 0, 'quake_processed': 0, 'tsunami_processed': 0, 'unknown_skipped': 0,
                                 'last_stats_output': datetime.now(self.jst)}
        self.stats_interval = 3600

        self.exception_handler = EarthquakeTsunamiExceptionHandler(self)
        logger.info("✅ EarthquakeTsunamiCog 初期化完了")

    async def cog_load(self):
        logger.info("🔄 EarthquakeTsunamiCog セットアップ開始...")
        try:
            await self.recreate_http_session()
            logger.info("🔄 最新情報のIDを初期化中...")
            await self.initialize_processed_ids()

            self.ws_running = True
            asyncio.create_task(self.websocket_listener())

            self.output_stats_task.start()

            logger.info("✅ EarthquakeTsunamiCog セットアップ完了")
        except Exception as e:
            self.exception_handler.log_generic_error(e, "Cogのセットアップ")
            logger.critical(f"❌ セットアップに失敗しました: {e}")

    async def cog_unload(self):
        logger.info("🔄 EarthquakeTsunamiCog アンロード中...")

        self.ws_running = False
        if self.ws_connection and not self.ws_connection.closed:
            await self.ws_connection.close()
        if self.ws_session and not self.ws_session.closed:
            await self.ws_session.close()

        if self.http_session and not self.http_session.closed:
            await self.http_session.close()

        if hasattr(self, 'output_stats_task'):
            self.output_stats_task.cancel()

        logger.info("✅ EarthquakeTsunamiCog アンロード完了")

    def load_config(self) -> Dict[str, Any]:
        try:
            # SettingsDB から設定全体を読む
            config = self.settings_db.load(NS_EARTHQUAKE_CONFIG)
            # 無ければ空
            if config is None:
                return {}
            # dict でなければ空
            if not isinstance(config, dict):
                return {}
            for guild_id, value in list(config.items()):
                # ギルド設定を正規化する
                if isinstance(value, dict):
                    # フィルタ系キーの欠落を埋める
                    self._normalize_guild_config(value)
            return config
        except Exception as e:  # noqa: BLE001
            logger.warning(f"設定読み込みエラー: {e}")
        return {}

    async def save_config(self) -> None:
        """設定を排他制御付きで SettingsDB へ保存する。"""
        # 同時実行される設定変更を直列化する
        async with self.config_save_lock:
            try:
                # 設定全体を namespace に書く
                await self.settings_db.save_async(NS_EARTHQUAKE_CONFIG, self.config)
            except Exception as error:
                # 呼び出し元が扱える設定エラーとして送出する
                raise ConfigError(f"設定の保存に失敗しました: {error}") from error

    def _normalize_guild_config(self, guild_config: Dict[str, Any]) -> None:
        """ギルド設定にフィルタ用キーのデフォルトを補完する。"""
        # EEW 震度リストが無ければ全レベル＋不明
        if "notify_scales_eew" not in guild_config:
            # デフォルトは全通知
            guild_config["notify_scales_eew"] = list(ALL_NOTIFY_SCALES)
        # 通常地震も同様
        if "notify_scales_quake" not in guild_config:
            # デフォルトは全通知
            guild_config["notify_scales_quake"] = list(ALL_NOTIFY_SCALES)
        # 津波通知フラグが無ければオン
        if "notify_tsunami" not in guild_config:
            # 既存互換で true
            guild_config["notify_tsunami"] = True

    def ensure_guild_config(self, guild_id: str) -> Dict[str, Any]:
        """ギルド設定辞書を取得し、無ければ作成して正規化する。"""
        # 未作成なら空 dict
        if guild_id not in self.config or not isinstance(self.config.get(guild_id), dict):
            # 新規ギルド枠
            self.config[guild_id] = {}
        # 正規化する
        self._normalize_guild_config(self.config[guild_id])
        # 参照を返す
        return self.config[guild_id]

    def get_notify_scales(self, guild_id: str, info_type: str) -> list:
        """通知する震度コード一覧を返す（欠落時は全レベル）。"""
        # キーを決める
        key = f"notify_scales_{info_type}"
        # ギルド設定
        guild_config = self.config.get(guild_id) or {}
        # 生の値
        raw = guild_config.get(key)
        # 無ければデフォルト
        if raw is None:
            return list(ALL_NOTIFY_SCALES)
        # int 化して返す
        result = []
        for item in raw:
            try:
                result.append(int(item))
            except (TypeError, ValueError):
                continue
        return result

    async def set_notify_scales(
        self,
        guild_id: str,
        info_type: str,
        scales: list,
    ) -> None:
        """通知する震度コード一覧を保存する。"""
        # ギルド枠を確保
        guild_config = self.ensure_guild_config(guild_id)
        # 許可コードだけ残す
        allowed = set(ALL_NOTIFY_SCALES)
        cleaned = []
        for item in scales:
            try:
                code = int(item)
            except (TypeError, ValueError):
                continue
            if code in allowed and code not in cleaned:
                cleaned.append(code)
        # 安定した並びにする
        cleaned.sort(key=lambda c: ALL_NOTIFY_SCALES.index(c) if c in ALL_NOTIFY_SCALES else c)
        # 書き込む
        guild_config[f"notify_scales_{info_type}"] = cleaned
        # 永続化
        await self.save_config()

    def get_notify_tsunami(self, guild_id: str) -> bool:
        """津波通知が有効かどうかを返す。"""
        # ギルド設定
        guild_config = self.config.get(guild_id) or {}
        # デフォルト true
        return bool(guild_config.get("notify_tsunami", True))

    async def set_notify_tsunami(self, guild_id: str, enabled: bool) -> None:
        """津波通知の有効/無効を保存する。"""
        # ギルド枠を確保
        guild_config = self.ensure_guild_config(guild_id)
        # フラグを書く
        guild_config["notify_tsunami"] = bool(enabled)
        # 永続化
        await self.save_config()

    async def set_channels_unified(self, guild_id: str, channel_id: int) -> None:
        """EEW / 地震 / 津波の通知先を同一チャンネルに揃える。"""
        # ギルド枠を確保
        guild_config = self.ensure_guild_config(guild_id)
        # 3種に同じ ID
        for key in (InfoType.EEW.value, InfoType.QUAKE.value, InfoType.TSUNAMI.value):
            guild_config[key] = int(channel_id)
        # 永続化
        await self.save_config()

    async def set_channel_for_type(
        self,
        guild_id: str,
        info_type: str,
        channel_id: int,
    ) -> None:
        """指定種別の通知チャンネルを保存する。"""
        # ギルド枠を確保
        guild_config = self.ensure_guild_config(guild_id)
        # チャンネル ID を書く
        guild_config[info_type] = int(channel_id)
        # 永続化
        await self.save_config()

    def normalize_max_scale(self, max_scale: Any) -> int:
        """maxScale を比較用 int に正規化する（欠落は -1）。"""
        # None は不明
        if max_scale is None:
            return -1
        try:
            return int(max_scale)
        except (TypeError, ValueError):
            return -1

    def should_notify_by_scale(self, guild_id: str, info_type: str, max_scale: Any) -> bool:
        """震度フィルタに基づき通知すべきか判定する。"""
        # 正規化
        code = self.normalize_max_scale(max_scale)
        # 許可リストに含まれるか
        return code in self.get_notify_scales(guild_id, info_type)

    def format_scales_summary(self, guild_id: str, info_type: str) -> str:
        """設定中の震度一覧を短い日本語にする。"""
        # 現在値
        scales = self.get_notify_scales(guild_id, info_type)
        # 空ならミュート
        if not scales:
            return "なし（すべて非通知）"
        # 全選択なら略記
        if set(scales) == set(ALL_NOTIFY_SCALES):
            return "すべて"
        # ラベル連結
        return "、".join(NOTIFY_SCALE_LABELS.get(s, str(s)) for s in scales)

    async def send_eew_notification(self, data: Dict[str, Any]) -> None:
        """code 556 の EEW スキーマを専用 embed として配信する。"""
        # API のテスト情報は本番通知チャンネルへ送信しない
        if data.get("test") is True:
            logger.info("EEW テスト情報を本番チャンネルへ送信せずスキップしました")
            return
        # 取消フラグを取得する
        cancelled = data.get("cancelled") is True
        # 取消情報では earthquake が欠落し得るため、安全な辞書だけを使う
        earthquake = data.get("earthquake")
        earthquake = earthquake if isinstance(earthquake, dict) else {}
        # issue は API 情報番号と発表時刻の取得に使う
        issue = data.get("issue")
        issue = issue if isinstance(issue, dict) else {}
        # 地域予測から EEW 専用の最大予測震度を計算する
        max_scale = self.get_eew_max_scale(data)
        # 発生時刻を優先し、取消時などは発表時刻を使う
        event_time = earthquake.get("originTime") or issue.get("time")
        # Discord embed の時刻を JST で生成する
        timestamp = self.parse_earthquake_time(event_time, issue.get("time"))
        # 取消と通常通知でタイトルを切り替える
        title = "🚨 緊急地震速報（取消）" if cancelled else "🚨 緊急地震速報（警報）"
        # 取消では誤った震度を示さない専用の本文を使う
        description = (
            "発表されていた緊急地震速報は**取り消されました**。"
            if cancelled
            else (
                "強い揺れに警戒してください。"
                if max_scale == -1
                else (
                    f"**最大予測震度 {self.scale_to_japanese(max_scale)}** "
                    "程度の揺れが予想されます。"
                )
            )
        )
        # 取消は灰色、通常情報は予測震度に対応する色で embed を作る
        embed = discord.Embed(
            title=title,
            description=description,
            color=(
                discord.Color.light_grey()
                if cancelled
                else self.get_embed_color(max_scale, InfoType.EEW.value)
            ),
            timestamp=timestamp,
        )
        # 取消でない場合だけ earthquake の詳細を表示する
        if not cancelled:
            # 震源情報は EEW スキーマの earthquake 配下にある
            hypocenter = earthquake.get("hypocenter")
            hypocenter = hypocenter if isinstance(hypocenter, dict) else {}
            # 震源地名を欠落時も安全に表示する
            hypocenter_name = hypocenter.get("name") or "調査中"
            # 震源地を embed へ追加する
            embed.add_field(
                name="🌏 震源地",
                value=f"```{hypocenter_name}```",
                inline=True,
            )
            # 推定マグニチュードを embed へ追加する
            embed.add_field(
                name="📊 マグニチュード",
                value=f"```推定 {self.format_magnitude(hypocenter.get('magnitude'))}```",
                inline=True,
            )
            # 推定深さを embed へ追加する
            embed.add_field(
                name="📏 深さ",
                value=f"```{self.format_depth(hypocenter.get('depth'))}```",
                inline=True,
            )
            # EEW の地震発生時刻を表示する
            if earthquake.get("originTime"):
                embed.add_field(
                    name="🕐 発生時刻",
                    value=f"```{earthquake['originTime']}```",
                    inline=True,
                )
            # EEW の主要動到達予測時刻を表示する
            if earthquake.get("arrivalTime"):
                embed.add_field(
                    name="⏱️ 主要動到達予測",
                    value=f"```{earthquake['arrivalTime']}```",
                    inline=True,
                )
            # EEW 専用地域配列を表示用文字列へ変換する
            areas_text = self.format_eew_areas(data)
            # 地域情報があるときだけ予測地域として表示する
            if areas_text:
                embed.add_field(
                    name="📍 予測地域・予測震度",
                    value=areas_text[:1024],
                    inline=False,
                )
            # 地域情報が無い場合は不足を明示する
            else:
                embed.add_field(
                    name="📍 予測地域・予測震度",
                    value="予測地域の詳細は現在取得できていません。",
                    inline=False,
                )
            # 速報の安全行動を案内する
            embed.add_field(
                name="⚠️ 注意",
                value="この情報は速報です。揺れが予想される地域では身の安全を確保してください。",
                inline=False,
            )
        # 情報番号があれば取消を含む通知の識別に使う
        if issue.get("eventId") or issue.get("serial"):
            # 空値を除外して情報番号を連結する
            identifier = " / ".join(
                str(value)
                for value in (issue.get("eventId"), issue.get("serial"))
                if value
            )
            # 情報番号を embed へ追加する
            embed.add_field(name="ℹ️ 情報番号", value=f"`{identifier}`", inline=False)
        # 共通の情報元フッターを設定する
        embed.set_footer(text=notification_embed_footer())
        # P2Pquake のロゴを表示する
        embed.set_thumbnail(url="https://www.p2pquake.net/images/QuakeLogo_100x100.png")
        # 取消は震度フィルタに関わらず、設定済み EEW チャンネルへ通知する
        await self.send_embed_to_channels(
            embed,
            InfoType.EEW.value,
            max_scale=max_scale,
            apply_scale_filter=not cancelled,
        )

    async def send_tsunami_notification(self, data, tsunami_info):
        try:
            warning_level = tsunami_info.get('warning_level', '津波予報')
            emoji_map = {"大津波警報": "🔴", "津波警報": "🟠", "津波注意報": "🟡"}
            embed = discord.Embed(
                title=f"{emoji_map.get(warning_level, '🌊')} {warning_level}",
                description=f"**{warning_level}** が発表されました。",
                color=discord.Color.purple(),
                timestamp=datetime.now(self.jst)
            )
            earthquake = data.get('earthquake', {})
            if earthquake and isinstance(earthquake, dict):
                hypocenter = earthquake.get('hypocenter', {})
                magnitude = hypocenter.get('magnitude', -1)
                depth = hypocenter.get('depth', -1)
                embed.add_field(name="🌏 震源地", value=f"```{hypocenter.get('name', '不明')}```", inline=True)
                embed.add_field(name="📊 マグニチュード", value=f"```{self.format_magnitude(magnitude)}```", inline=True)
                embed.add_field(name="📏 深さ", value=f"```{self.format_depth(depth)}```", inline=True)

            areas = tsunami_info.get('areas', [])
            if areas and isinstance(areas, list):
                area_text = "".join(
                    f"🌊 **{area.get('grade', warning_level)}** - {area.get('name', '不明')}\n"
                    for area in areas[:5] if isinstance(area, dict)
                )
                if area_text:
                    embed.add_field(name="🏖️ 予報区域", value=area_text, inline=False)

            warning_text = (
                "⚠️ **直ちに避難してください** ⚠️\n高台や避難ビルなど安全な場所へ" if warning_level == "大津波警報"
                else "⚠️ **直ちに避難してください**\n海岸や川から離れ、高いところへ" if warning_level == "津波警報"
                else "⚠️ 海の中や海岸付近は危険です\n海から上がって、海岸から離れてください"
            )
            embed.add_field(name="⚠️ 避難指示", value=warning_text, inline=False)
            if tsunami_info.get('description'):
                embed.add_field(name="ℹ️ 詳細情報", value=tsunami_info['description'][:500], inline=False)

            embed.set_footer(text=notification_embed_footer())
            embed.set_thumbnail(url="https://www.p2pquake.net/images/QuakeLogo_100x100.png")
            await self.send_embed_to_channels(embed, InfoType.TSUNAMI.value)
        except Exception as e:
            raise NotificationError(f"津波通知処理エラー: {e}")

    async def generate_single_earthquake_map(self, quake: dict, info_type: str) -> io.BytesIO:
        """単一の地震の位置を地図に表示"""
        # 実行中のイベントループを取得する（3.11 では get_event_loop は非推奨）
        loop = asyncio.get_running_loop()
        # 地図専用モジュールへ同期描画を委譲する
        return await loop.run_in_executor(
            None,
            self._generate_single_map_sync,
            quake,
            info_type,
        )

    def _generate_single_map_sync(
        self,
        quake: dict,
        info_type: str,
    ) -> io.BytesIO:
        """単一震源の地図描画を地図モジュールへ委譲する。"""
        # 地図描画の実装を Cog から分離して単一の実装を利用する
        return generate_single_map_sync(quake, info_type)

    def _calculate_smart_map_extent(
        self,
        lat: float,
        lon: float,
        max_scale: int,
    ) -> tuple[float, float, float, float]:
        """地図範囲の計算を地図モジュールへ委譲する。"""
        # 既存の内部呼び出しとの互換性を保ちながら実装を一本化する
        return calculate_smart_map_extent(lat, lon, max_scale)

    @app_commands.command(
        name="earthquake_channel",
        description="Set the notification channel for earthquake/tsunami alerts.",
    )
    @app_commands.describe(
        channel="Channel to send notifications to.",
        info_type="Type of alert to notify.",
    )
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    async def set_channel(self, interaction: discord.Interaction, channel: discord.TextChannel,
                          info_type: Literal["緊急地震速報", "地震情報", "津波予報", "すべて"]):
        try:
            guild_id = str(interaction.guild.id)
            # 全種別指定は共通の設定保存処理へ委譲する
            if info_type == "すべて":
                await self.set_channels_unified(guild_id, channel.id)
            else:
                # 日本語の選択値を内部種別へ変換する
                type_map = {
                    "緊急地震速報": InfoType.EEW.value,
                    "地震情報": InfoType.QUAKE.value,
                    "津波予報": InfoType.TSUNAMI.value,
                }
                # 選択された1種別だけを保存する
                await self.set_channel_for_type(
                    guild_id,
                    type_map[info_type],
                    channel.id,
                )
            await interaction.response.send_message(
                f"✅ **{info_type}** の通知チャンネルを {channel.mention} に設定しました。\n"
                f"ℹ️ 震度フィルタ等は `/earthquake_settings` でも変更できます。"
            )
        except Exception as e:
            self.exception_handler.log_generic_error(e, "チャンネル設定コマンド")
            await interaction.response.send_message(self.exception_handler.get_user_friendly_message(e),
                                                    ephemeral=False)

    @app_commands.command(
        name="earthquake_settings",
        description="Configure earthquake/tsunami notification filters and channels.",
    )
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    async def earthquake_settings(self, interaction: discord.Interaction):
        """Components V2 の地震通知設定画面を開く。"""
        # ギルド外は拒否
        if interaction.guild is None:
            await interaction.response.send_message("このコマンドはサーバー内でのみ使用できます。", ephemeral=True)
            return
        # 遅延インポート（循環参照回避）
        from MOMOKA.notifications.earthquake_settings_view import EarthquakeSettingsView
        # ギルド設定を正規化しておく
        self.ensure_guild_config(str(interaction.guild.id))
        # View 生成
        view = EarthquakeSettingsView(self.bot, self, interaction.guild)
        # ephemeral で送信（LayoutView は content なし）
        await interaction.response.send_message(view=view, ephemeral=True)

    @app_commands.command(name="earthquake_status", description="Check earthquake/tsunami system status.")
    async def status_system(self, interaction: discord.Interaction):
        try:
            await interaction.response.defer(ephemeral=False)
            embed = discord.Embed(
                title="🔧 地震・津波情報システム状態",
                color=discord.Color.blue(),
                timestamp=datetime.now(self.jst)
            )

            ws_status = "✅ 接続中" if self.ws_connection and not self.ws_connection.closed else "❌ 切断中"
            embed.add_field(name="🔌 WebSocket状態", value=ws_status, inline=True)

            embed.add_field(
                name="🌐 HTTPセッション",
                value="✅ 正常" if self.http_session and not self.http_session.closed else "❌ 無効",
                inline=True
            )

            id_status = ""
            for it, lid in self.last_ids.items():
                count = len(self.processed_ids.get(it, set()))
                id_status += f"**{it.upper()}**: `{lid[:8] if lid else '未取得'}` ({count}件)\n"
            embed.add_field(name="🆔 最後のID", value=id_status, inline=False)

            guild_id = str(interaction.guild.id)
            if guild_id in self.config:
                channel_status = ""
                type_map = {
                    InfoType.EEW.value: '緊急地震速報',
                    InfoType.QUAKE.value: '地震情報',
                    InfoType.TSUNAMI.value: '津波予報'
                }
                for it, name in type_map.items():
                    if it in self.config[guild_id]:
                        channel = interaction.guild.get_channel(self.config[guild_id][it])
                        status = f"✅ {channel.mention}" if channel else "❌ 削除済み"
                    else:
                        status = "⚠️ 未設定"
                    channel_status += f"**{name}**: {status}\n"
            else:
                channel_status = "⚠️ すべて未設定"

            embed.add_field(name="📢 通知チャンネル", value=channel_status, inline=False)

            # フィルタ状態
            filter_status = (
                f"**EEW 震度:** {self.format_scales_summary(guild_id, InfoType.EEW.value)}\n"
                f"**地震情報 震度:** {self.format_scales_summary(guild_id, InfoType.QUAKE.value)}\n"
                f"**津波通知:** {'オン' if self.get_notify_tsunami(guild_id) else 'オフ'}\n"
                f"ℹ️ 変更は `/earthquake_settings`"
            )
            embed.add_field(name="🎚️ 通知フィルタ", value=filter_status, inline=False)

            if self.error_stats['last_error_time']:
                embed.add_field(
                    name="🕐 最後のエラー",
                    value=self.error_stats['last_error_time'].strftime('%m/%d %H:%M:%S'),
                    inline=True
                )

            error_summary = (
                f"API: {self.error_stats['api_errors']} | "
                f"解析: {self.error_stats['parsing_errors']} | "
                f"WS切断: {self.error_stats['ws_disconnects']}"
            )
            embed.add_field(name="📊 エラー統計", value=error_summary, inline=False)

            embed.set_footer(text="システム診断完了 | P2P地震情報 WebSocket API")
            await interaction.followup.send(embed=embed)
        except Exception as e:
            self.exception_handler.log_generic_error(e, "ステータスコマンド")
            msg = self.exception_handler.get_user_friendly_message(e)
            if not interaction.response.is_done():
                await interaction.response.send_message(msg, ephemeral=False)
            else:
                await interaction.followup.send(msg)

    @app_commands.command(name="earthquake_test", description="Send a test earthquake/tsunami notification.")
    @app_commands.describe(
        info_type="Alert type to test.",
        max_scale="Maximum intensity for the test.",
        tsunami_level="Tsunami warning level for the test."
    )
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    async def test_notification(
            self,
            interaction: discord.Interaction,
            info_type: Literal["緊急地震速報", "地震情報", "津波予報"],
            max_scale: Optional[Literal["震度3", "震度5強", "震度7"]] = "震度5強",
            tsunami_level: Optional[Literal["津波注意報", "津波警報", "大津波警報"]] = "津波警報"
    ):
        try:
            await interaction.response.defer(ephemeral=False)
            target_channel, is_configured = interaction.channel, False
            guild_id = str(interaction.guild.id)

            if guild_id in self.config:
                type_map = {
                    "緊急地震速報": InfoType.EEW.value,
                    "地震情報": InfoType.QUAKE.value,
                    "津波予報": InfoType.TSUNAMI.value
                }
                config_key = type_map.get(info_type)
                if config_key and config_key in self.config[guild_id]:
                    channel = interaction.guild.get_channel(self.config[guild_id][config_key])
                    if channel:
                        target_channel, is_configured = channel, True

            map_file = None
            embed = None

            if info_type == "津波予報":
                embed = await self.create_tsunami_test_embed(tsunami_level)
            else:
                scale_code = {"震度3": 30, "震度5強": 50, "震度7": 70}.get(max_scale, 50)
                embed = await self.create_earthquake_test_embed(info_type, max_scale, scale_code)

                if CARTOPY_AVAILABLE:
                    try:
                        test_quake_data = {
                            'lat': 36.0, 'lon': 140.5, 'magnitude': 7.0, 'depth': 30,
                            'max_scale': scale_code, 'name': 'テスト震源地 (関東沖)',
                            'time': datetime.now(self.jst)
                        }
                        info_type_value = "eew" if info_type == "緊急地震速報" else "quake"
                        map_buffer = await self.generate_single_earthquake_map(test_quake_data, info_type_value)
                        map_file = discord.File(fp=map_buffer, filename="earthquake_test_map.png")
                        embed.set_image(url="attachment://earthquake_test_map.png")
                    except Exception as e:
                        logger.warning(f"テスト通知の地図生成に失敗: {e}")

            await target_channel.send(embed=embed, file=map_file)

            msg = (
                f"✅ 設定されたチャンネル {target_channel.mention} に **{info_type}** のテスト通知を送信しました。"
                if is_configured
                else f"✅ このチャンネルに **{info_type}** のテスト通知を送信しました。\nℹ️ 本番の通知は `/earthquake_settings` または `/earthquake_channel` で設定したチャンネルに送信されます。"
            )
            await interaction.followup.send(msg)
        except discord.Forbidden:
            await interaction.followup.send(f"❌ {target_channel.mention} にメッセージを送信する権限がありません。")
        except Exception as e:
            self.exception_handler.log_generic_error(e, "テスト通知コマンド")
            await interaction.followup.send(self.exception_handler.get_user_friendly_message(e))

    async def create_earthquake_test_embed(self, info_type, max_scale, scale_code):
        title = (
            f"🚨【テスト】緊急地震速報 (予報)"
            if info_type == "緊急地震速報"
            else f"📊【テスト】地震情報"
        )
        description = f"**最大震度 {max_scale}** の地震が{'検知されました' if info_type == '緊急地震速報' else '発生しました'}。"

        embed = discord.Embed(
            title=title,
            description=description,
            color=self.get_embed_color(scale_code),
            timestamp=datetime.now(self.jst)
        )
        embed.add_field(name="🌏 震源地", value="```テスト震源地```", inline=True)
        embed.add_field(name="📊 マグニチュード", value="```M7.0```", inline=True)
        embed.add_field(name="📏 深さ", value="```30km```", inline=True)
        embed.add_field(
            name="📍 各地の震度",
            value=f"🔴 **{max_scale}** - テスト県A市\n🟠 **震度4** - テスト県B市\n🟡 **震度3** - テスト県C市",
            inline=False
        )
        embed.set_footer(text=notification_embed_footer(test=True))
        embed.set_thumbnail(url="https://www.p2pquake.net/images/QuakeLogo_100x100.png")
        return embed

    async def create_tsunami_test_embed(self, tsunami_level):
        emoji_map = {"津波注意報": "🟡", "津波警報": "🟠", "大津波警報": "🔴"}
        embed = discord.Embed(
            title=f"{emoji_map.get(tsunami_level, '🌊')}【テスト】{tsunami_level}",
            description=f"**{tsunami_level}** が発表されました。",
            color=discord.Color.purple(),
            timestamp=datetime.now(self.jst)
        )
        embed.add_field(name="🌏 震源地", value="```テスト海域```", inline=True)
        embed.add_field(name="📊 マグニチュード", value="```M7.5```", inline=True)
        embed.add_field(name="📏 深さ", value="```10km```", inline=True)
        embed.add_field(
            name="🏖️ 予報区域",
            value=f"🌊 **{tsunami_level}**\n・テスト県沿岸\n・テスト湾\n・テスト海岸",
            inline=False
        )
        warning_text = (
            "⚠️ **直ちに避難してください** ⚠️"
            if tsunami_level == "大津波警報"
            else "⚠️ 直ちに海岸や川から離れ、高いところに避難してください。"
            if tsunami_level == "津波警報"
            else "⚠️ 海の中や海岸付近は危険です。海から上がって、海岸から離れてください。"
        )
        embed.add_field(name="⚠️ 注意事項", value=warning_text, inline=False)
        embed.set_footer(text=notification_embed_footer(test=True))
        embed.set_thumbnail(url="https://www.p2pquake.net/images/QuakeLogo_100x100.png")
        return embed

    @app_commands.command(name="earthquake_remove", description="Remove earthquake/tsunami notification settings.")
    @app_commands.describe(info_type="Notification setting to remove.")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    async def remove_channel(
            self,
            interaction: discord.Interaction,
            info_type: Literal["緊急地震速報", "地震情報", "津波予報", "すべて"]
    ):
        try:
            guild_id = str(interaction.guild.id)

            if guild_id not in self.config:
                await interaction.response.send_message("❌ このサーバーには通知設定がありません。", ephemeral=False)
                return

            type_map = {
                "緊急地震速報": InfoType.EEW.value,
                "地震情報": InfoType.QUAKE.value,
                "津波予報": InfoType.TSUNAMI.value
            }

            removed_types = []

            if info_type == "すべて":
                # すべての設定を削除
                if guild_id in self.config:
                    del self.config[guild_id]
                    removed_types = ["緊急地震速報", "地震情報", "津波予報"]
                    await self.save_config()
                    await interaction.response.send_message(
                        "✅ **すべての通知設定** を削除しました。",
                        ephemeral=False
                    )
                else:
                    await interaction.response.send_message(
                        "❌ このサーバーには通知設定がありません。",
                        ephemeral=False
                    )
                return
            else:
                # 個別の設定を削除
                config_key = type_map[info_type]
                if config_key in self.config[guild_id]:
                    del self.config[guild_id][config_key]
                    removed_types.append(info_type)

                    # 設定が空になった場合はギルド設定自体も削除
                    if not self.config[guild_id]:
                        del self.config[guild_id]
                        logger.info(f"ギルド '{interaction.guild.name}' の設定が空になったため削除しました")

                    await self.save_config()
                    await interaction.response.send_message(
                        f"✅ **{info_type}** の通知設定を削除しました。",
                        ephemeral=False
                    )
                else:
                    await interaction.response.send_message(
                        f"❌ **{info_type}** の通知設定は存在しません。",
                        ephemeral=False
                    )

        except Exception as e:
            self.exception_handler.log_generic_error(e, "通知削除コマンド")
            await interaction.response.send_message(
                self.exception_handler.get_user_friendly_message(e),
                ephemeral=False
            )

    @app_commands.command(name="earthquake_help", description="Show help for this system.")
    async def help_system(self, interaction: discord.Interaction):
        embed = discord.Embed(
            title="📚 地震・津波情報システム ヘルプ",
            description=(
                "このボットは気象庁の地震・津波情報をリアルタイムで通知します（WebSocket接続）。\n"
                "通知フィルタ・チャンネルは `/earthquake_settings` で設定できます。"
            ),
            color=discord.Color.green(),
            timestamp=datetime.now(self.jst)
        )
        embed.add_field(
            name="🛠️ 利用可能なコマンド",
            value=(
                "**🔧 設定コマンド**\n"
                "`/earthquake_settings` - 通知先・震度・津波の設定（推奨）\n"
                "`/earthquake_channel` - 通知チャンネルを設定\n"
                "`/earthquake_remove` - 通知設定を削除\n"
                "`/earthquake_test` - テスト通知を送信\n\n"
                "**📊 情報表示コマンド**\n"
                "`/earthquake_status` - システム状態を確認\n"
                "`/earthquake_history` - 最近の地震履歴を表示\n"
                "`/earthquake_map` - 地震を地図上に表示\n"
                "`/earthquake_debug` - 詳細診断情報を表示\n\n"
                "**❓ その他**\n"
                "`/earthquake_help` - このヘルプを表示"
            ),
            inline=False
        )
        embed.set_footer(text=notification_embed_footer())
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="earthquake_map", description="Display recent earthquakes on a map of Japan.")
    @app_commands.describe(
        limit="Number of earthquakes to show (1-50).",
        min_scale="Minimum intensity to display.",
        hours="Show earthquakes within the past N hours (1-168 = 7 days)."
    )
    async def show_earthquake_map(
            self,
            interaction: discord.Interaction,
            limit: Optional[int] = 20,
            min_scale: Optional[Literal[
                "震度1", "震度2", "震度3", "震度4", "震度5弱", "震度5強", "震度6弱", "震度6強", "震度7"]] = None,
            hours: Optional[int] = 24
    ):
        try:
            await interaction.response.defer(ephemeral=False)

            if not CARTOPY_AVAILABLE:
                await interaction.followup.send("❌ 地図機能は現在利用できません。Bot管理者にお問い合わせください。")
                return

            limit = max(1, min(limit, 50))
            hours = max(1, min(hours, 168))

            scale_map = {
                "震度1": 10, "震度2": 20, "震度3": 30, "震度4": 40,
                "震度5弱": 45, "震度5強": 50, "震度6弱": 55, "震度6強": 60, "震度7": 70
            }
            min_scale_code = scale_map.get(min_scale, 0) if min_scale else 0

            cutoff_time = datetime.now(self.jst) - timedelta(hours=hours)

            url = f"{self.api_base_url}/history?codes=551&limit=100"
            data = await self.safe_api_request(url)

            if not data or not isinstance(data, list):
                await interaction.followup.send("❌ 地震情報の取得に失敗しました。")
                return

            filtered_quakes = []
            for item in data:
                info_type = self.classify_info_type(item)
                if info_type != InfoType.QUAKE:
                    continue

                earthquake = item.get('earthquake', {})
                max_scale = earthquake.get('maxScale', -1)

                if max_scale < min_scale_code:
                    continue

                issue = item.get('issue', {})
                quake_time = self.parse_earthquake_time(earthquake.get('time', ''), issue.get('time', ''))
                if quake_time < cutoff_time:
                    continue

                hypocenter = earthquake.get('hypocenter', {})
                lat = hypocenter.get('latitude')
                lon = hypocenter.get('longitude')

                if lat is not None and lon is not None:
                    filtered_quakes.append({
                        'lat': lat,
                        'lon': lon,
                        'magnitude': hypocenter.get('magnitude', -1),
                        'depth': hypocenter.get('depth', -1),
                        'max_scale': max_scale,
                        'name': hypocenter.get('name', '不明'),
                        'time': quake_time
                    })

                    if len(filtered_quakes) >= limit:
                        break

            if not filtered_quakes:
                filter_text = f"（{min_scale}以上、過去{hours}時間以内）" if min_scale else f"（過去{hours}時間以内）"
                await interaction.followup.send(f"ℹ️ 該当する地震情報{filter_text}が見つかりませんでした。")
                return

            image_buffer = await self.generate_earthquake_map(filtered_quakes, min_scale, hours)

            file = discord.File(fp=image_buffer, filename="earthquake_map.png")

            embed = discord.Embed(
                title=f"📍 地震発生地点マップ ({len(filtered_quakes)}件)",
                description=f"過去{hours}時間以内、最小震度: {min_scale or '指定なし'}",
                color=discord.Color.red(),
                timestamp=datetime.now(self.jst)
            )
            embed.set_image(url="attachment://earthquake_map.png")
            embed.set_footer(text="データ提供: P2P地震情報 API | PLANA by coffin299")

            await interaction.followup.send(embed=embed, file=file)

        except (APIError, DataParsingError) as e:
            logger.error(f"地図生成エラー: {e}")
            await interaction.followup.send(f"❌ 地震情報の取得中にエラーが発生しました: {e}")
        except Exception as e:
            self.exception_handler.log_generic_error(e, "地図表示コマンド")
            await interaction.followup.send(self.exception_handler.get_user_friendly_message(e))

    async def generate_earthquake_map(self, quakes: list, min_scale: Optional[str], hours: int) -> io.BytesIO:
        """地震マップ画像を生成"""
        # 実行中のイベントループを取得する（3.11 では get_event_loop は非推奨）
        loop = asyncio.get_running_loop()
        # 地図専用モジュールへ同期描画を委譲する
        return await loop.run_in_executor(
            None,
            self._generate_map_sync,
            quakes,
            min_scale,
            hours,
        )

    def _generate_map_sync(
        self,
        quakes: list,
        min_scale: Optional[str],
        hours: Optional[int],
    ) -> io.BytesIO:
        """複数震源の地図描画を地図モジュールへ委譲する。"""
        # 地図描画の実装を Cog から分離して単一の実装を利用する
        return generate_map_sync(quakes, min_scale, hours)

    @app_commands.command(name="earthquake_history", description="Display recent earthquake information.")
    @app_commands.describe(
        limit="Number of earthquakes to show (1-20).",
        min_scale="Minimum intensity to display."
    )
    async def show_history(
            self,
            interaction: discord.Interaction,
            limit: Optional[int] = 10,
            min_scale: Optional[
                Literal["震度1", "震度2", "震度3", "震度4", "震度5弱", "震度5強", "震度6弱", "震度6強", "震度7"]] = None
    ):
        try:
            await interaction.response.defer(ephemeral=False)

            limit = max(1, min(limit, 20))

            scale_map = {
                "震度1": 10, "震度2": 20, "震度3": 30, "震度4": 40,
                "震度5弱": 45, "震度5強": 50, "震度6弱": 55, "震度6強": 60, "震度7": 70
            }
            min_scale_code = scale_map.get(min_scale, 0) if min_scale else 0

            url = f"{self.api_base_url}/history?codes=551&limit=100"
            data = await self.safe_api_request(url)

            if not data or not isinstance(data, list):
                await interaction.followup.send("❌ 地震情報の取得に失敗しました。")
                return

            filtered_quakes = []
            for item in data:
                info_type = self.classify_info_type(item)
                if info_type == InfoType.QUAKE:
                    max_scale = item.get('earthquake', {}).get('maxScale', -1)
                    if max_scale >= min_scale_code:
                        filtered_quakes.append(item)
                        if len(filtered_quakes) >= limit:
                            break

            if not filtered_quakes:
                filter_text = f"（{min_scale}以上）" if min_scale else ""
                await interaction.followup.send(f"ℹ️ 該当する地震情報{filter_text}が見つかりませんでした。")
                return

            map_quakes = []
            for quake in filtered_quakes:
                earthquake = quake.get('earthquake', {})
                hypocenter = earthquake.get('hypocenter', {})
                issue = quake.get('issue', {})

                lat = hypocenter.get('latitude')
                lon = hypocenter.get('longitude')

                if lat is not None and lon is not None:
                    max_scale = earthquake.get('maxScale', -1)
                    quake_time = self.parse_earthquake_time(earthquake.get('time', ''), issue.get('time', ''))
                    magnitude = hypocenter.get('magnitude', -1)
                    depth = hypocenter.get('depth', -1)

                    map_quakes.append({
                        'lat': lat,
                        'lon': lon,
                        'magnitude': magnitude,
                        'depth': depth,
                        'max_scale': max_scale,
                        'name': hypocenter.get('name', '不明'),
                        'time': quake_time
                    })

            embed = discord.Embed(
                title=f"📊 最近の地震情報 ({len(filtered_quakes)}件)",
                description=f"最小震度: {min_scale or '指定なし'}",
                color=discord.Color.blue(),
                timestamp=datetime.now(self.jst)
            )

            for idx, quake in enumerate(filtered_quakes, 1):
                earthquake = quake.get('earthquake', {})
                hypocenter = earthquake.get('hypocenter', {})
                issue = quake.get('issue', {})

                max_scale = earthquake.get('maxScale', -1)
                quake_time = self.parse_earthquake_time(earthquake.get('time', ''), issue.get('time', ''))
                magnitude = hypocenter.get('magnitude', -1)
                depth = hypocenter.get('depth', -1)
                location = hypocenter.get('name', '不明')

                emoji = "🔴" if max_scale >= 55 else "🟠" if max_scale >= 50 else "🟡" if max_scale >= 40 else "🟢" if max_scale >= 30 else "🔵"

                field_value = (
                    f"{emoji} **{self.scale_to_japanese(max_scale)}**\n"
                    f"🌏 {location}\n"
                    f"📊 {self.format_magnitude(magnitude)} / 📏 {self.format_depth(depth)}\n"
                    f"🕐 {quake_time.strftime('%m/%d %H:%M:%S')}"
                )

                embed.add_field(
                    name=f"{idx}. {quake_time.strftime('%m/%d %H:%M')}",
                    value=field_value,
                    inline=True if idx <= 3 else False
                )

                if idx % 3 == 0 and idx < len(filtered_quakes):
                    embed.add_field(name="\u200b", value="\u200b", inline=False)

            embed.set_footer(text="データ提供: P2P地震情報 API | PLANA by coffin299")
            embed.set_thumbnail(url="https://www.p2pquake.net/images/QuakeLogo_100x100.png")

            if map_quakes and CARTOPY_AVAILABLE:
                try:
                    map_buffer = await self.generate_earthquake_map(map_quakes, min_scale, None)
                    map_file = discord.File(fp=map_buffer, filename="earthquake_history_map.png")
                    embed.set_image(url="attachment://earthquake_history_map.png")
                    await interaction.followup.send(embed=embed, file=map_file)
                except Exception as e:
                    logger.warning(f"履歴地図生成に失敗: {e}")
                    await interaction.followup.send(embed=embed)
            else:
                await interaction.followup.send(embed=embed)

        except (APIError, DataParsingError) as e:
            logger.error(f"履歴取得エラー: {e}")
            await interaction.followup.send(f"❌ 地震情報の取得中にエラーが発生しました: {e}")
        except Exception as e:
            self.exception_handler.log_generic_error(e, "履歴表示コマンド")
            await interaction.followup.send(self.exception_handler.get_user_friendly_message(e))

    @app_commands.command(name="earthquake_debug", description="Detailed diagnosis of notification settings.")
    async def debug_config(self, interaction: discord.Interaction):
        try:
            await interaction.response.defer(ephemeral=False)

            guild_id = str(interaction.guild.id)
            embed = discord.Embed(
                title="🔍 通知設定診断",
                color=discord.Color.blue(),
                timestamp=datetime.now(self.jst)
            )

            embed.add_field(
                name="📁 設定ファイル",
                value=f"```json\n{json.dumps(self.config, indent=2, ensure_ascii=False)[:500]}```",
                inline=False
            )

            if guild_id in self.config:
                guild_config = self.config[guild_id]
                config_text = ""
                type_map = {
                    InfoType.EEW.value: '緊急地震速報',
                    InfoType.QUAKE.value: '地震情報',
                    InfoType.TSUNAMI.value: '津波予報',
                }
                for info_type, label in type_map.items():
                    channel_id = guild_config.get(info_type)
                    if not channel_id:
                        config_text += f"**{label}**: ⚠️ 未設定\n"
                        continue
                    channel = interaction.guild.get_channel(int(channel_id))
                    if channel:
                        perms = channel.permissions_for(interaction.guild.me)
                        config_text += f"**{label}**:\n"
                        config_text += f"  チャンネル: {channel.mention} (ID: {channel_id})\n"
                        config_text += f"  メッセージ送信: {'✅' if perms.send_messages else '❌'}\n"
                        config_text += f"  埋め込みリンク: {'✅' if perms.embed_links else '❌'}\n"
                    else:
                        config_text += f"**{label}**: ❌ チャンネル {channel_id} が見つかりません\n"

                filter_text = (
                    f"**EEW 震度:** {self.format_scales_summary(guild_id, InfoType.EEW.value)}\n"
                    f"**地震情報 震度:** {self.format_scales_summary(guild_id, InfoType.QUAKE.value)}\n"
                    f"**津波通知:** {'オン' if self.get_notify_tsunami(guild_id) else 'オフ'}"
                )
                embed.add_field(name="⚙️ このサーバーの設定", value=config_text or "設定なし", inline=False)
                embed.add_field(name="🎚️ 通知フィルタ", value=filter_text, inline=False)
            else:
                embed.add_field(name="⚙️ このサーバーの設定", value="❌ 未設定", inline=False)

            ws_info = "✅ 接続中" if self.ws_connection and not self.ws_connection.closed else "❌ 切断中"
            embed.add_field(
                name="🤖 Bot状態",
                value=(
                    f"ギルド数: {len(self.bot.guilds)}\n"
                    f"WebSocket: {ws_info}\n"
                    f"HTTPセッション: {'✅' if self.http_session and not self.http_session.closed else '❌'}\n"
                    f"WS切断回数: {self.error_stats['ws_disconnects']}"
                ),
                inline=False
            )

            await interaction.followup.send(embed=embed, ephemeral=False)

        except Exception as e:
            logger.error(f"診断コマンドエラー: {e}", exc_info=True)
            await interaction.followup.send(f"❌ エラーが発生しました: {e}", ephemeral=False)


async def setup(bot: commands.Bot):
    try:
        await bot.add_cog(EarthquakeTsunamiCog(bot))
    except Exception as e:
        logger.critical(f"Cogセットアップエラー: {e}", exc_info=True)
        raise