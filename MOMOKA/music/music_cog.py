# MOMOKA/music/music_cog.py
import asyncio
import collections
import gc
import io
import itertools
import logging
import math
import random
import re
import subprocess
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum, auto
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import discord
import yaml
from discord import app_commands
from discord.ext import commands, tasks

from MOMOKA.utilities.donation import donation_from_bot, make_subtle_link_button

try:
    from MOMOKA.music.plugins.ytdlp_wrapper import (
        Track,
        extract as extract_audio_data,
        ensure_stream,
        set_youtube_cookie_path,
        clear_ytdlp_cache,
        UnsupportedMediaError,
        COMMON_YTDL_OPTS,
        YOUTUBE_PLAYER_CLIENT_FALLBACK,
    )
    from MOMOKA.music.error.errors import MusicCogExceptionHandler
    from MOMOKA.music.plugins.audio_mixer import AudioMixer, MusicAudioSource
    from MOMOKA.music.plugins.voice_dave_patch import apply_dave_patch
except ImportError as e:
    print(f"[CRITICAL] MusicCog: 必須コンポーネントのインポートに失敗しました。エラー: {e}")
    Track = None
    extract_audio_data = None
    ensure_stream = None
    set_youtube_cookie_path = None
    clear_ytdlp_cache = None
    UnsupportedMediaError = None
    COMMON_YTDL_OPTS = None
    YOUTUBE_PLAYER_CLIENT_FALLBACK = None
    MusicCogExceptionHandler = None
    AudioMixer = None
    MusicAudioSource = None
    apply_dave_patch = None

logger = logging.getLogger(__name__)

# Now Playing プログレスバーの更新間隔（秒）。Discord rate limit を考慮して 10 秒にする
PROGRESS_UPDATE_INTERVAL = 10
# Now Playing パネル下部に表示するキューの1ページあたり曲数
QUEUE_PAGE_SIZE = 5
# Components V2 上で表示するプログレスバーの長さ（インラインコード1行向け）
PROGRESS_BAR_LENGTH = 28


def format_duration(duration_seconds: int) -> str:
    if duration_seconds is None or duration_seconds < 0:
        return "N/A"
    hours, remainder = divmod(duration_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{int(hours):02}:{int(minutes):02}:{int(seconds):02}" if hours > 0 else f"{int(minutes):02}:{int(seconds):02}"


def parse_time_to_seconds(time_str: str) -> Optional[int]:
    try:
        time_str = time_str.strip()

        if ':' not in time_str:
            return max(0, int(time_str))

        time_str = time_str.rstrip(':')
        parts = [int(p) for p in time_str.split(':')]

        if not parts or any(p < 0 for p in parts):
            return None

        if len(parts) == 2:
            return max(0, parts[0] * 60 + parts[1])
        elif len(parts) == 3:
            return max(0, parts[0] * 3600 + parts[1] * 60 + parts[2])
        else:
            return None
    except (ValueError, AttributeError):
        pass
    return None


class LoopMode(Enum):
    OFF = auto()
    ONE = auto()
    ALL = auto()


class GuildState:
    def __init__(self, bot: commands.Bot, guild_id: int, cog_config: dict):
        self.bot = bot
        self.guild_id = guild_id
        self.voice_client: Optional[discord.VoiceClient] = None
        self.current_track: Optional[Track] = None
        self.queue: asyncio.Queue[Track] = asyncio.Queue()
        self.volume: float = cog_config.get('music', {}).get('default_volume', 20) / 100.0
        self.loop_mode: LoopMode = LoopMode.OFF
        self.is_playing: bool = False
        self.is_paused: bool = False
        self.auto_leave_task: Optional[asyncio.Task] = None
        self.last_text_channel_id: Optional[int] = None
        self.connection_lock = asyncio.Lock()
        self.last_activity = datetime.now()
        self.cleanup_in_progress = False
        self.playback_start_time: Optional[float] = None
        self.seek_position: int = 0
        self.paused_at: Optional[float] = None
        self.is_seeking: bool = False
        self.is_loading: bool = False
        self.mixer: Optional[AudioMixer] = None
        self._playing_next: bool = False  # 次の曲を再生中かどうかのフラグ
        # パイプ 403 による同一曲リトライ回数（最大 1）
        self.stream_403_retries: int = 0
        self.last_now_playing_message: Optional[discord.Message] = None
        # プログレスバー定期更新タスク（未起動時は None）
        self.progress_update_task: Optional[asyncio.Task] = None
        # Now Playing パネル内キュー表示のページ番号（0始まり）
        self.queue_page: int = 0
        # Stop ボタン押下後の確認ダイアログ表示中フラグ
        self.confirming_stop: bool = False
        # Components V2 下部に出すロード失敗バナー（英語・コードブロック用）
        self.ui_load_error: Optional[str] = None
        # 失敗バナーを一度 UI に出したあと、次曲開始で消すためのフラグ
        self.ui_load_error_seen: bool = False
        # /play の query が URL だったときの履歴（停止パネル用・サムネ不要）
        self.last_history_url: Optional[str] = None

    def update_activity(self):
        self.last_activity = datetime.now()

    def update_last_text_channel(self, channel_id: int):
        self.last_text_channel_id = channel_id
        self.update_activity()

    def get_current_position(self) -> int:
        # 再生中でなければシーク位置をそのまま返す
        if not self.is_playing:
            return self.seek_position

        # 一時停止中は paused_at と playback_start_time の両方が必要
        # （片方が None だと減算で TypeError になるためガードする）
        if self.is_paused and self.paused_at and self.playback_start_time:
            # 一時停止時点までの経過秒を算出する
            elapsed = self.paused_at - self.playback_start_time
            # シーク基準位置に加算して返す
            return self.seek_position + int(elapsed)

        # 再生中で開始時刻がある場合は現在時刻との差分を使う
        if self.playback_start_time:
            # 再生開始からの経過秒を算出する
            elapsed = time.time() - self.playback_start_time
            # シーク基準位置に加算して返す
            return self.seek_position + int(elapsed)

        # タイムスタンプ欠損時はシーク位置を返す
        return self.seek_position

    def reset_playback_tracking(self):
        self.playback_start_time = None
        self.seek_position = 0
        self.paused_at = None

    async def clear_queue(self):
        while not self.queue.empty():
            try:
                self.queue.get_nowait()
                self.queue.task_done()
            except asyncio.QueueEmpty:
                break
        self.queue = asyncio.Queue()

    def stop_progress_updater(self):
        # プログレス更新タスクが存在し、まだ完了していないか判定する
        if self.progress_update_task and not self.progress_update_task.done():
            # 定期更新ループをキャンセルする
            self.progress_update_task.cancel()
        # タスク参照をクリアする
        self.progress_update_task = None

    async def cleanup_voice_client(self):
        if self.cleanup_in_progress:
            return
        self.cleanup_in_progress = True
        try:
            # 切断時はプログレスバー更新を止める
            self.stop_progress_updater()
            # Now Playing のグレーアウト表示は MusicCog 側で行うため、ここでは参照のみクリアする
            self.last_now_playing_message = None

            if self.mixer:
                self.mixer.stop()
                self.mixer = None
            if self.voice_client:
                try:
                    if self.voice_client.is_playing():
                        self.voice_client.stop()
                    if self.voice_client.is_connected():
                        await asyncio.wait_for(self.voice_client.disconnect(force=True), timeout=5.0)
                except Exception as e:
                    guild = self.bot.get_guild(self.guild_id)
                    logger.warning(f"Guild {self.guild_id} ({guild.name if guild else ''}): Voice cleanup error: {e}")
                finally:
                    self.voice_client = None
        finally:
            self.cleanup_in_progress = False


class MusicCog(commands.Cog, name="music_cog"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        if not all((Track, extract_audio_data, ensure_stream, MusicCogExceptionHandler, AudioMixer, MusicAudioSource)):
            raise commands.ExtensionFailed(self.qualified_name, "必須コンポーネントのインポート失敗")
        self.config = self._load_bot_config()
        self.music_config = self.config.get('music', {})
        self.guild_states: Dict[int, GuildState] = {}
        self.exception_handler = MusicCogExceptionHandler(self.music_config)
        self.ffmpeg_path = self.music_config.get('ffmpeg_path', 'ffmpeg')
        self.ffmpeg_before_options = self.music_config.get('ffmpeg_before_options',
                                                           "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5")
        self.ffmpeg_options = self.music_config.get('ffmpeg_options', "-vn")
        # YouTube クッキーパスを yt-dlp ラッパーへ反映する
        self._apply_youtube_cookie_config()
        self.auto_leave_timeout = self.music_config.get('auto_leave_timeout', 10)
        self.max_queue_size = self.music_config.get('max_queue_size', 9000)
        # プレイリスト展開の上限（未指定時は 10000。ラッパー既定 50 に依存しない）
        self.max_playlist_items = self.music_config.get('max_playlist_items', 10000)
        self.max_guilds = self.music_config.get('max_guilds', 100000000)
        self.inactive_timeout_minutes = self.music_config.get('inactive_timeout_minutes', 30)
        self.global_connection_lock = asyncio.Lock()
        self.cleanup_task = None

    async def cog_load(self):
        # 起動時に yt-dlp / プロジェクト cache を掃除して古いストリーム情報を残さない
        if clear_ytdlp_cache is not None:
            try:
                # キャッシュ削除を実行する
                clear_ytdlp_cache()
            except Exception as e:
                # キャッシュ削除失敗でも Cog ロードは続行する
                logger.warning(f"yt-dlp cache cleanup failed (non-fatal): {e}")

        # DAVE: 2.6 系向けモンキーパッチ / 2.7+ は discord.py ネイティブ + davey（voice_dave_patch 内で分岐）
        if apply_dave_patch:
            try:
                # ボイスWebSocketの IDENTIFY/RESUME/received_message をパッチ
                result = apply_dave_patch()
                if result:
                    logger.info("DAVE protocol patch applied successfully")
            except Exception as e:
                logger.warning(f"DAVE protocol patch failed (non-fatal): {e}")

        if not self.cleanup_task or self.cleanup_task.done():
            self.cleanup_task = self.cleanup_task_loop.start()
        logger.info("MusicCog loaded and cleanup task started")

    def _apply_youtube_cookie_config(self) -> None:
        """config の youtube_cookie_file を yt-dlp ラッパーへ反映する"""
        # ラッパーが未インポートの場合は何もしない
        if set_youtube_cookie_path is None:
            # 早期リターンする
            return
        # 未指定時は youtube_cookie.txt を既定として渡す（自動検出の優先候補になる）
        set_youtube_cookie_path(self.music_config.get('youtube_cookie_file', 'youtube_cookie.txt'))

    def _load_bot_config(self) -> dict:
        if hasattr(self.bot, 'config') and self.bot.config:
            return self.bot.config
        try:
            with open('config.yaml', 'r', encoding='utf-8') as f:
                loaded_config = yaml.safe_load(f)
                self.bot.config = loaded_config
                return loaded_config
        except Exception:
            return {}

    def cog_unload(self):
        logger.info("Unloading MusicCog...")
        if hasattr(self, 'cleanup_task') and self.cleanup_task:
            self.cleanup_task.cancel()
        if hasattr(self, 'cleanup_task_loop') and self.cleanup_task_loop.is_running():
            self.cleanup_task_loop.cancel()
        for guild_id in list(self.guild_states.keys()):
            try:
                state = self.guild_states[guild_id]
                if state.mixer:
                    state.mixer.stop()
                if state.voice_client and state.voice_client.is_connected():
                    asyncio.create_task(state.voice_client.disconnect(force=True))
                if state.auto_leave_task and not state.auto_leave_task.done():
                    state.auto_leave_task.cancel()
            except Exception as e:
                guild = self.bot.get_guild(guild_id)
                logger.warning(f"Guild {guild_id} ({guild.name if guild else ''}) unload cleanup error: {e}")
        self.guild_states.clear()
        logger.info("MusicCog unloaded.")

    async def notify_admin_restart(self) -> None:
        """再起動前に Now Playing UI を管理者再起動メッセージへ切り替える。"""
        # 共有の再起動文言を遅延インポートする（循環参照回避）
        from MOMOKA.utilities.restart_notice import RESTART_NOTICE_MUSIC
        # Now Playing があるギルドだけを対象にする
        target_guild_ids = [
            guild_id
            for guild_id, state in list(self.guild_states.items())
            if state.last_now_playing_message is not None
        ]
        # 対象が無ければ何もしない
        if not target_guild_ids:
            # 早期リターン
            return
        # ギルドごとに UI を再起動表示へ更新する
        for guild_id in target_guild_ids:
            # 最新のギルド状態を取得する
            state = self._get_guild_state(guild_id)
            # 状態が消えていればスキップする
            if not state:
                # 次のギルドへ
                continue
            try:
                # プログレス更新を止めて編集競合を避ける
                state.stop_progress_updater()
                # LayoutView の終了表示分岐に入るため再生中トラックをクリアする
                state.current_track = None
                # 再生中フラグも下ろす
                state.is_playing = False
                # Now Playing を再起動文言付きグレーアウト UI に更新する
                await self._update_now_playing_message_ui(
                    guild_id,
                    finished_message=RESTART_NOTICE_MUSIC,
                )
            except Exception as e:
                # 1ギルドの失敗で他ギルド通知を止めない
                logger.warning(
                    "Guild %s: failed to notify admin restart on Now Playing: %s",
                    guild_id,
                    e,
                )

    @tasks.loop(minutes=5)
    async def cleanup_task_loop(self):
        try:
            current_time = datetime.now()
            inactive_threshold = timedelta(minutes=self.inactive_timeout_minutes)
            guilds_to_cleanup = []
            for gid, state in list(self.guild_states.items()):
                # 接続済みだが人間がいなくなっているギルドは自動退出（Bot残留の保険）
                if (
                    state.voice_client
                    and state.voice_client.is_connected()
                    and not self._vc_has_humans(state.voice_client.channel)
                ):
                    # タイマー無しでも取り残されないようスケジュールする
                    if not state.auto_leave_task or state.auto_leave_task.done():
                        # 無人なので自動退出を予約
                        self._schedule_auto_leave(gid)
                    # このギルドは disconnect 待ちなのでメモリ掃除リストには入れない
                    continue
                # 長時間非アクティブかつ未接続の状態を破棄対象にする
                if (
                    current_time - state.last_activity > inactive_threshold
                    and not state.is_playing
                    and (not state.voice_client or not state.voice_client.is_connected())
                ):
                    # 破棄リストへ追加
                    guilds_to_cleanup.append(gid)
            for guild_id in guilds_to_cleanup:
                guild = self.bot.get_guild(guild_id)
                logger.info(f"Cleaning up inactive guild: {guild_id} ({guild.name if guild else ''})")
                await self._cleanup_guild_state(guild_id)
            if guilds_to_cleanup:
                gc.collect()
        except Exception as e:
            logger.error(f"Cleanup task error: {e}", exc_info=True)

    @cleanup_task_loop.before_loop
    async def before_cleanup_task(self):
        await self.bot.wait_until_ready()

    def _get_guild_state(self, guild_id: int) -> Optional[GuildState]:
        if guild_id not in self.guild_states:
            if len(self.guild_states) >= self.max_guilds:
                oldest_guild, oldest_time = None, datetime.now()
                for gid, state in self.guild_states.items():
                    if not state.is_playing and state.last_activity < oldest_time:
                        oldest_guild, oldest_time = gid, state.last_activity
                if oldest_guild:
                    asyncio.create_task(self._cleanup_guild_state(oldest_guild))
                    guild = self.bot.get_guild(oldest_guild)
                    logger.info(
                        f"Removed oldest inactive guild {oldest_guild} ({guild.name if guild else ''}) to make room")
            self.guild_states[guild_id] = GuildState(self.bot, guild_id, self.config)
        self.guild_states[guild_id].update_activity()
        return self.guild_states[guild_id]

    def get_active_vc_guild_count(self) -> int:
        """VC に接続中のギルド数を返す（GUI 稼働モニタ用）。"""
        # 接続済み voice_client を持つギルドだけを数える
        return sum(
            1
            for s in self.guild_states.values()
            if s.voice_client and s.voice_client.is_connected()
        )

    @staticmethod
    def _is_http_url(value: Optional[str]) -> bool:
        """文字列が http(s) URL かどうかを判定する。"""
        # 空なら URL ではない
        if not value:
            # 非 URL
            return False
        # 前後空白を除いて小文字化して先頭を見る
        lowered = value.strip().lower()
        # http / https のみ履歴対象にする
        return lowered.startswith("http://") or lowered.startswith("https://")

    def _remember_play_history_url(
        self,
        state: GuildState,
        track: Optional[Track],
    ) -> None:
        """/play の query が URL だったトラックを停止パネル用履歴に残す。"""
        # トラックが無ければ何もしない
        if not track:
            # 更新スキップ
            return
        # ユーザーが入力した元クエリを取得する
        query = (track.original_query or "").strip()
        # URL 再生のときだけ履歴を上書きする（検索再生では消さない）
        if self._is_http_url(query):
            # 停止パネルに出す URL を保存する
            state.last_history_url = query

    @staticmethod
    def _inject_history_url(message: str, history_url: Optional[str]) -> str:
        """終了メッセージの1行目の直後に履歴 URL を差し込む。"""
        # 履歴が無ければ原文のまま返す
        if not history_url:
            # 差し込みなし
            return message
        # 先頭行と残りに分割する
        parts = message.split("\n", 1)
        # 本文がある場合は見出し→URL→本文の順にする
        if len(parts) == 2:
            # 指定レイアウトで結合する
            return f"{parts[0]}\n{history_url}\n{parts[1]}"
        # 1行だけの場合は末尾に URL を付ける
        return f"{message}\n{history_url}"

    @staticmethod
    async def _to_durable_message(
        message: Optional[discord.Message],
    ) -> Optional[discord.Message]:
        """Interaction応答メッセージをチャンネル編集可能な通常Messageへ変換する。

        InteractionMessage.edit は webhook token（約15分で失効）に依存するため、
        長期更新する Now Playing は必ず channel Message 経由で編集する。
        """
        # メッセージが無い場合はそのまま返す
        if message is None:
            # 変換対象なし
            return None
        try:
            # Interaction/Webhook由来のメッセージは fetch で通常Messageに置き換える
            # （どちらも webhook token 依存で約15分後に 50027 になる）
            if isinstance(message, (discord.InteractionMessage, discord.WebhookMessage)):
                # GET /channels/.../messages/... で永続編集可能なMessageを取得する
                return await message.fetch()
            # 既に通常Messageならそのまま使う
            return message
        except Exception as e:
            # fetch失敗時は元メッセージを残し、呼び出し側でフォールバックする
            logger.warning(f"Failed to convert interaction message to durable message: {e}")
            # 失敗時は元オブジェクトを返す
            return message

    async def _send_ctx_message(
            self,
            ctx: commands.Context,
            *,
            content: Optional[str] = None,
            embed: Optional[discord.Embed] = None,
            view: Optional[discord.ui.View] = None,
            ephemeral: bool = False,
            silent: bool = True,
            **kwargs,
    ) -> Optional[discord.Message]:
        # ContextオブジェクトからInteractionを取得する（スラッシュコマンドの場合は存在する）
        interaction = getattr(ctx, "interaction", None)
        try:
            # Interactionが存在する場合の処理
            if interaction:
                # 送信用のパラメータ辞書を構築する（@silent 相当で通知を抑制）
                kwargs_to_send = {
                    "content": content,
                    "embed": embed,
                    "ephemeral": ephemeral,
                    "silent": silent,
                    **kwargs,
                }
                # 表示するView（ボタンなど）が指定されているか判定する
                if view is not None:
                    # 送信用パラメータにViewを追加する
                    kwargs_to_send["view"] = view

                # インタラクションに対する最初の応答が完了していないか判定する
                if not interaction.response.is_done():
                    # 最初のレスポンスメッセージを送信する
                    await interaction.response.send_message(**kwargs_to_send)
                    try:
                        # スラッシュコマンドのオリジナル応答メッセージオブジェクトを取得して返す
                        return await interaction.original_response()
                    # メッセージ取得中に例外が発生した場合のハンドリング
                    except Exception:
                        # 取得できなかった場合はNoneを返す
                        return None
                else:
                    # すでに一度応答している場合は、followupを使ってメッセージを送信して返す
                    return await interaction.followup.send(**kwargs_to_send)
            # Interactionが存在しない（通常のテキストコマンドなどの）場合の処理
            else:
                # メッセージがephemeral（一時表示）指定されているか判定する
                if ephemeral:
                    # プレフィックスコマンドでは一時表示ができないため、ログを出力する
                    logger.debug("Ephemeral messages are not supported for prefix commands; sending normally.")
                # 通常のメッセージ送信を行い、そのメッセージオブジェクトを返す（silent 既定）
                return await ctx.send(
                    content=content,
                    embed=embed,
                    view=view,
                    silent=silent,
                    **kwargs,
                )
        # 送信処理中にエラーが発生した場合のハンドリングを行う
        except Exception as e:
            # エラーが発生したギルド（サーバー）の情報を取得する
            guild = ctx.guild
            # ギルド情報がある場合は「ID (名称)」、ない場合は「Unknown guild」として文字列を構築する
            guild_info = f"{guild.id} ({guild.name})" if guild else "Unknown guild"
            # エラー内容をログに出力する
            logger.error(f"Guild {guild_info}: Response error: {e}")
            # エラー時はNoneを返す
            return None

    async def _send_response(self, ctx: commands.Context, message_key: str, ephemeral: bool = False,
                             **kwargs):
        content = self.exception_handler.get_message(message_key, **kwargs)
        await self._send_ctx_message(ctx, content=content, ephemeral=ephemeral)

    async def _send_background_message(self, channel_id: int, message_key: str, **kwargs):
        try:
            channel = self.bot.get_channel(channel_id)
            if isinstance(channel, discord.TextChannel):
                # バックグラウンド通知も @silent 相当で送る
                await channel.send(
                    self.exception_handler.get_message(message_key, **kwargs),
                    silent=True,
                )
        except discord.Forbidden:
            logger.debug(f"No permission to send to channel {channel_id}")
        except Exception as e:
            logger.error(f"Background message error: {e}")

    async def _handle_error(self, ctx: commands.Context, error: Exception):
        error_message = self.exception_handler.handle_error(error, ctx.guild)
        await self._send_ctx_message(ctx, content=error_message, ephemeral=True)

    async def _ensure_voice(self, ctx: commands.Context, connect_if_not_in: bool = True) -> Optional[
        discord.VoiceClient]:
        state = self._get_guild_state(ctx.guild.id)
        if not state:
            await self._send_ctx_message(ctx, content="サーバーの上限に達しています。", ephemeral=True)
            return None
        state.update_last_text_channel(ctx.channel.id)
        user_voice = ctx.author.voice
        if not user_voice or not user_voice.channel:
            await self._send_response(ctx, "join_voice_channel_first", ephemeral=True)
            return None

        async with state.connection_lock:
            async with self.global_connection_lock:
                active_connections = sum(
                    1 for s in self.guild_states.values() if s.voice_client and s.voice_client.is_connected())
                if active_connections >= self.max_guilds and not state.voice_client:
                    await self._send_response(ctx, "error_playing", ephemeral=True,
                                              error="現在接続数が上限に達しています。")
                    return None

            vc = state.voice_client
            if vc:
                if not vc.is_connected():
                    await state.cleanup_voice_client()
                    vc = None
                elif vc.channel == user_voice.channel:
                    return vc
                else:
                    await state.cleanup_voice_client()
                    await asyncio.sleep(0.5)
                    vc = None

            for voice_client in list(self.bot.voice_clients):
                if voice_client.guild.id == ctx.guild.id and voice_client != state.voice_client:
                    try:
                        await asyncio.wait_for(voice_client.disconnect(force=True), timeout=3.0)
                    except:
                        pass

            if not vc and connect_if_not_in:
                try:
                    # 接続前の短い待機で前回切断との競合を避ける
                    await asyncio.sleep(0.3)
                    # 自己deafは使わず接続し、直後にサーバー側スピーカーミュートへ切り替え
                    state.voice_client = await asyncio.wait_for(
                        user_voice.channel.connect(
                            timeout=30.0, reconnect=True, self_deaf=False),
                        timeout=35.0
                    )
                    # 緑アイコンのサーバー側スピーカーミュートを適用（権限不足時は自己deafへ）
                    await self._apply_server_deafen(ctx.guild)
                    # 接続成功をログに残す
                    logger.info(
                        f"Guild {ctx.guild.id} ({ctx.guild.name}): Connected to {user_voice.channel.name}")
                    # 接続時点ですでに人間がいなければ自動退出を予約する
                    if not self._vc_has_humans(state.voice_client.channel):
                        # 無人VCに留まらないようタイマーを開始する
                        self._schedule_auto_leave(ctx.guild.id)
                    # 接続済み VoiceClient を返す
                    return state.voice_client
                except Exception as e:
                    await self._handle_error(ctx, e)
                    state.voice_client = None
                    return None
            elif not vc:
                await self._send_response(ctx, "bot_not_in_voice_channel", ephemeral=True)
                return None
            return vc

    def mixer_finished_callback(self, error: Optional[Exception], guild_id: int,
                                expected_mixer=None):
        """
        ミキサー（voice_client.play()のafter）が終了した際のコールバック。
        mixer.stop()やvoice_client.stop()で呼ばれる。
        注意: オーディオスレッドから呼ばれるため、asyncio APIはrun_coroutine_threadsafeで実行。

        Args:
            error: 再生中に発生したエラー（あれば）
            guild_id: ギルドID
            expected_mixer: このコールバックが紐づくミキサー参照。
                旧ミキサーのコールバックが新ミキサーのstateを破壊するのを防止する。
        """
        if error:
            logger.error(f"Guild {guild_id}: Mixer unexpectedly finished with error: {error}")
        logger.info(f"Guild {guild_id}: Mixer has finished.")
        state = self._get_guild_state(guild_id)
        if not state or state._playing_next:
            return

        # 旧ミキサーのコールバックが新ミキサーのstateを破壊するのを防止
        # state.mixerが別のミキサーに差し替わっている場合はスキップ
        if expected_mixer is not None and state.mixer is not expected_mixer:
            logger.info(f"Guild {guild_id}: Mixer callback ignored (stale mixer, "
                        f"expected={id(expected_mixer)}, current={id(state.mixer) if state.mixer else 'None'})")
            return

        # ミキサーが既にクリーンアップ済み（意図的な停止）ならスキップ
        # _cleanup_idle_mixer等で事前にmixer=Noneにされている場合
        if state.mixer is None and not state.is_playing:
            logger.info(f"Guild {guild_id}: Mixer callback ignored (already cleaned up)")
            return

        state._playing_next = True

        # 終了時のトラック情報を保存
        finished_track = state.current_track
        # ミキサー参照をクリア
        state.mixer = None
        state.is_playing = False

        # LoopMode.ONEの場合は current_track を保持、それ以外は None にする
        if state.loop_mode != LoopMode.ONE:
            state.current_track = None

        state.reset_playback_tracking()

        # エラーがあればテキストチャンネルに通知
        if error:
            guild = self.bot.get_guild(guild_id)
            error_message = self.exception_handler.handle_error(error, guild)
            if state.last_text_channel_id:
                asyncio.run_coroutine_threadsafe(
                    self._send_background_message(state.last_text_channel_id, "error_message_wrapper",
                                                  error=error_message),
                    self.bot.loop
                )

        # LoopMode.ALLの場合はキューに再追加
        if finished_track and state.loop_mode == LoopMode.ALL:
            asyncio.run_coroutine_threadsafe(state.queue.put(finished_track), self.bot.loop)

        # 次の曲を再生（非同期タスクとしてスケジュール）
        def play_next_and_reset_flag():
            async def _play():
                try:
                    await self._play_next_song(guild_id)
                finally:
                    if state:
                        state._playing_next = False
            asyncio.run_coroutine_threadsafe(_play(), self.bot.loop)

        play_next_and_reset_flag()

    async def _on_music_source_removed(self, guild_id: int, finished_source=None):
        """
        音楽ソースがミキサーから削除されたときに呼ばれる。
        ループモードやキューを考慮して次の曲を再生する。
        AudioMixerのon_source_removed_callbackから各ソース削除ごとに発火される。
        """
        state = self._get_guild_state(guild_id)
        # シーク中や既に次曲処理中の場合はスキップ
        if not state or state.is_seeking or state._playing_next:
            return

        state._playing_next = True

        try:
            # 終了したトラック情報を保存
            finished_track = state.current_track
            # NO audio + 403/途中切断などなら同一曲を 1 回だけ代替 client でリトライする
            should_retry_stream = (
                finished_source is not None
                and getattr(finished_source, "no_audio_failure", False)
                and (
                    getattr(finished_source, "http_forbidden_failure", False)
                    or getattr(finished_source, "stream_retryable_failure", False)
                )
                and finished_track is not None
                and state.stream_403_retries < 1
            )
            # ストリーム失敗リトライ経路
            if should_retry_stream:
                # リトライ回数を加算する
                state.stream_403_retries += 1
                # 再生中フラグを下ろして再起動可能にする
                state.is_playing = False
                # トラッキングをリセットする
                state.reset_playback_tracking()
                # リトライ開始をログする
                logger.warning(
                    "Guild %s: Retrying '%s' once after stream failure / NO audio "
                    "(attempt %s, fallback player_client, forbidden=%s retryable=%s)",
                    guild_id,
                    finished_track.title,
                    state.stream_403_retries,
                    getattr(finished_source, "http_forbidden_failure", False),
                    getattr(finished_source, "stream_retryable_failure", False),
                )
                # 同一曲をフォールバック client で再再生する
                await self._play_next_song(
                    guild_id,
                    retry_track=finished_track,
                    use_fallback_clients=True,
                )
                # リトライ処理を終了する
                return

            # 通常終了・リトライ尽きた場合はカウンタをリセットする
            state.stream_403_retries = 0
            # 再生中フラグを下ろす
            state.is_playing = False

            # NO audio 失敗かどうか
            is_no_audio_fail = (
                finished_source is not None
                and getattr(finished_source, "no_audio_failure", False)
                and finished_track is not None
            )
            # キューに次曲があるか（失敗曲の再投入前に判定）
            has_next_in_queue = not state.queue.empty()

            # NO audio 失敗時の分岐
            if is_no_audio_fail:
                # URL + タイトルを含むバナー文言を組み立てる
                load_error_text = self._format_ui_load_error(
                    url=getattr(finished_track, "url", None),
                    title=getattr(finished_track, "title", None),
                    detail="No audio produced (stream unavailable).",
                )
                # 次曲が無ければ専用パネル、あれば次曲 UI 下へバナー
                should_continue = await self._present_playback_load_error(
                    guild_id,
                    load_error_text,
                    has_next_in_queue=has_next_in_queue,
                )
                # 単発失敗なら次曲再生へ進まない
                if not should_continue:
                    # 早期リターン
                    return

            # LoopMode.ONEの場合は current_track を保持、それ以外は None にする
            # NO audio 失敗時は壊れた曲を ONE で回さない
            if state.loop_mode != LoopMode.ONE or is_no_audio_fail:
                state.current_track = None

            state.reset_playback_tracking()

            # LoopMode.ALLの場合はキューに再追加（NO audio 失敗曲は再投入しない）
            if (
                finished_track
                and state.loop_mode == LoopMode.ALL
                and not is_no_audio_fail
            ):
                await state.queue.put(finished_track)

            # 次の曲を再生（キューが空の場合はミキサーの停止も行う）
            await self._play_next_song(guild_id)
        finally:
            state._playing_next = False

    async def _cleanup_idle_mixer(self, state: GuildState):
        """
        ミキサーにソースが残っていない場合、ミキサーを停止してクリーンアップする。
        TTS等のソースが残っている場合はミキサーを維持する。
        これにより voice_client.is_playing() が False になり、TTS直接再生が可能になる。
        """
        if not state.mixer:
            return
        # ミキサーにソースが残っているか確認
        if state.mixer.has_sources():
            return
        # ソースなし→ミキサーを停止
        logger.info(f"Guild {state.guild_id}: Cleaning up idle mixer (no sources remaining)")
        mixer = state.mixer
        # 先にstate.mixerをNoneにしてmixer_finished_callbackでの二重処理を防止
        state.mixer = None
        # ミキサーを停止（read()がb''を返す→voice_clientのプレイヤーが停止）
        mixer.stop()

    async def _play_next_song(
        self,
        guild_id: int,
        seek_seconds: int = 0,
        play_msg: Optional[discord.Message] = None,
        *,
        retry_track: Optional[Track] = None,
        use_fallback_clients: bool = False,
    ):
        state = self._get_guild_state(guild_id)
        if not state:
            return

        if state.is_playing and not seek_seconds > 0 and retry_track is None:
            return

        is_seek_operation = seek_seconds > 0
        track_to_play: Optional[Track] = None

        # 403 リトライ時は同一トラックを強制再生する
        if retry_track is not None and not is_seek_operation:
            # リトライ対象をそのまま使う
            track_to_play = retry_track
        elif is_seek_operation and state.current_track:
            track_to_play = state.current_track
        elif state.loop_mode == LoopMode.ONE and state.current_track and not is_seek_operation:
            track_to_play = state.current_track
        elif not state.is_playing and not state.queue.empty() and not is_seek_operation:
            try:
                track_to_play = await state.queue.get()
                state.queue.task_done()
            except:
                pass

        if not track_to_play:
            # 終了前に URL 再生履歴を残す（この直後に current_track を消す）
            self._remember_play_history_url(state, state.current_track)
            # 再生対象が無いので再生状態をクリアする
            state.current_track = None
            # 再生中フラグを下ろす
            state.is_playing = False
            # 403 リトライカウンタもリセットする
            state.stream_403_retries = 0
            # 再生位置トラッキングを初期化する
            state.reset_playback_tracking()
            # キュー終了時はプログレスバー更新を停止する
            state.stop_progress_updater()
            # Now Playing メッセージが残っているか判定する
            if state.last_now_playing_message:
                # V2 LayoutView のグレーアウト UI に切り替える（旧 Embed 編集は使わない）
                await self._update_now_playing_message_ui(
                    guild_id,
                    finished_message=(
                        "⏹️ **Queue Finished**\n"
                        "All songs in the queue have been played."
                    ),
                )
            else:
                # メッセージが無い場合のみテキスト通知を送る
                if state.last_text_channel_id:
                    # キュー終了メッセージを送信する
                    await self._send_background_message(state.last_text_channel_id, "queue_ended")
            # キュー終了時：ミキサーにソースが残っていなければ停止してクリーンアップ
            # （TTS等が残っている場合はミキサーを維持する）
            await self._cleanup_idle_mixer(state)
            # 次曲再生処理を終了する
            return

        # 失敗バナーを「次曲の再生中だけ」出すため、
        # バナー表示済みの状態でさらに次の曲へ進むときに消す
        if state.ui_load_error_seen:
            # バナー本文をクリアする
            state.ui_load_error = None
            # 表示済みフラグも戻す
            state.ui_load_error_seen = False

        if not is_seek_operation:
            # 次に再生するトラックを現在曲として設定する
            state.current_track = track_to_play
            # URL 指定の /play なら停止パネル用履歴に残す
            self._remember_play_history_url(state, track_to_play)

        # 新規曲の通常再生開始時は 403 カウンタをリセットする（リトライ中は維持）
        if retry_track is None and not is_seek_operation:
            # カウンタをゼロに戻す
            state.stream_403_retries = 0

        state.is_playing = True
        state.is_paused = False
        state.update_activity()

        state.seek_position = seek_seconds
        state.playback_start_time = time.time()
        state.paused_at = None

        # パイプ / ensure_stream で使う player_client 列を決める
        pipe_clients = None
        # フォールバック client 指定がある場合
        if use_fallback_clients and YOUTUBE_PLAYER_CLIENT_FALLBACK:
            # 代替クライアント列を使う
            pipe_clients = list(YOUTUBE_PLAYER_CLIENT_FALLBACK)

        try:
            is_local_file = False
            if track_to_play.stream_url:
                try:
                    is_local_file = Path(track_to_play.stream_url).is_file()
                except Exception:
                    pass

            if not is_local_file:
                # ensure_stream 用オプション（フォールバック時は client を上書き）
                ensure_opts = None
                if use_fallback_clients and COMMON_YTDL_OPTS and pipe_clients:
                    # 共通オプションをコピーする
                    ensure_opts = COMMON_YTDL_OPTS.copy()
                    # 代替 player_client を注入する
                    ensure_opts["extractor_args"] = {
                        "youtube": {"player_client": list(pipe_clients)}
                    }
                updated_track = await ensure_stream(track_to_play, ytdl_opts_override=ensure_opts)
                if not updated_track or not updated_track.stream_url:
                    raise RuntimeError(f"'{track_to_play.title}' の有効なストリームURLを取得できませんでした。")
                # ストリームURLを最新値へ更新する
                track_to_play.stream_url = updated_track.stream_url
                # FFmpeg が CDN へアクセスできるよう HTTP ヘッダー（Cookie 含む）も同期する
                track_to_play.http_headers = updated_track.http_headers

            ffmpeg_before_opts = self.ffmpeg_before_options
            if seek_seconds > 0:
                # シーク指定位置から再生を開始するための開始オプションを構築する
                ffmpeg_before_opts = f"-ss {seek_seconds} {ffmpeg_before_opts}"

            # ensure_stream で実際に成功した player_client 列を最優先で使う
            # （抽出は通るのにパイプ CLI だけ format 全滅する不整合を防ぐ）
            resolved_clients = getattr(track_to_play, "pipe_player_clients", None) or pipe_clients

            # YouTube は yt-dlp パイプ再生（googlevideo 直読みは 403 になる）
            source = MusicAudioSource(
                track_to_play.stream_url,
                title=track_to_play.title,
                guild_id=guild_id,
                webpage_url=track_to_play.url,
                http_headers=getattr(track_to_play, "http_headers", None),
                player_clients=resolved_clients,
                pipe_format=getattr(track_to_play, "pipe_format", None),
                pipe_use_cookies=getattr(track_to_play, "pipe_use_cookies", None),
                executable=self.ffmpeg_path,
                before_options=ffmpeg_before_opts,
                options=self.ffmpeg_options,
            )

            if state.mixer is None:
                def on_source_removed(name: str, removed_source=None):
                    """ソースが削除されたときのコールバック"""
                    if name == 'music':
                        asyncio.run_coroutine_threadsafe(
                            self._on_music_source_removed(guild_id, removed_source),
                            self.bot.loop,
                        )
                
                state.mixer = AudioMixer(on_source_removed_callback=on_source_removed)

            # ミキサーをローカル変数に保持（awaitの間にstate.mixerがNoneに変更されるのを防止）
            # mixer_finished_callbackの旧ミキサー競合でstate.mixer=Noneにされても、
            # ローカル変数はオブジェクトを保持し続ける
            current_mixer = state.mixer

            await current_mixer.add_source('music', source, volume=state.volume)

            if not state.voice_client or not state.voice_client.is_connected():
                # ボイスクライアントが存在しない、あるいは切断されている場合は再生を中断する
                logger.info(f"Guild {guild_id}: voice_client is None or disconnected, aborting playback")
                # 再生中フラグを初期化する
                state.is_playing = False
                # 再生中トラック情報を初期化する
                state.current_track = None
                # 再生時間追跡情報を初期化する
                state.reset_playback_tracking()
                # アイドルミキサーをクリーンアップする
                await self._cleanup_idle_mixer(state)
                # 再生中メッセージのUIを最新化する（停止状態に更新する）
                await self._update_now_playing_message_ui(guild_id)
                # 切断時はプログレスバー更新も停止する
                state.stop_progress_updater()
                # 処理を正常終了する
                return

            if state.voice_client.source is not current_mixer:
                # 旧AudioPlayerが残留している場合（_cleanup_idle_mixer後のレース等）は
                # 明示的に停止して新しいミキサーで再生開始
                if state.voice_client.is_playing():
                    logger.info(f"Guild {guild_id}: Stopping stale AudioPlayer before starting new mixer")
                    state.voice_client.stop()
                # lambdaにミキサー参照をキャプチャし、mixer_finished_callbackで照合する
                # これにより旧ミキサーのコールバックが新ミキサーのstateを破壊するのを防止
                state.voice_client.play(
                    current_mixer,
                    after=lambda e, m=current_mixer: self.mixer_finished_callback(e, guild_id, m)
                )
                logger.info(f"Guild {guild_id}: Started new AudioPlayer with mixer {id(current_mixer)}")

            # 旧ミキサーのコールバックでstateが破壊された場合の復元処理
            # （mixer_finished_callbackのミキサーID照合で防止されるが、念のため）
            if state.mixer is None and current_mixer is not None:
                logger.warning(f"Guild {guild_id}: state.mixer was cleared during playback setup, restoring")
                state.mixer = current_mixer
            if not state.is_playing:
                logger.warning(f"Guild {guild_id}: state.is_playing was cleared during playback setup, restoring")
                # 再生中フラグを復元する
                state.is_playing = True
            # コールバックで reset_playback_tracking された場合、開始時刻も復元する
            if state.playback_start_time is None:
                logger.warning(
                    f"Guild {guild_id}: state.playback_start_time was cleared during playback setup, restoring"
                )
                # 現在時刻から進捗追跡を再開する
                state.playback_start_time = time.time()
            if state.current_track is None and track_to_play is not None and not is_seek_operation:
                logger.warning(f"Guild {guild_id}: state.current_track was cleared during playback setup, restoring")
                state.current_track = track_to_play

            if is_seek_operation:
                state.is_seeking = False

            # シーク操作以外では Now Playing UI を更新する
            # 次曲移行時も最初の /play 応答メッセージを編集して維持し、誰が再生開始したか分かるようにする
            if state.last_text_channel_id and not is_seek_operation:
                # 再生コントロール一体型UI（LayoutView）を構築する
                view = MusicControllerView(self, guild_id)
                # 送信先チャンネルを取得する
                channel = self.bot.get_channel(state.last_text_channel_id)
                # チャンネルが取れた場合のみ UI を更新する
                if channel:
                    try:
                        # 初回 /play の応答メッセージが渡されているか判定する
                        if play_msg:
                            # webhook期限切れ回避のためチャンネル経由 Message へ変換する
                            play_msg = await self._to_durable_message(play_msg)
                            # /play 応答を Now Playing UI に編集する
                            await play_msg.edit(content=None, embed=None, view=view)
                            # 編集したメッセージを最新の Now Playing として保存する
                            state.last_now_playing_message = play_msg
                        elif state.last_now_playing_message:
                            # 次曲など: 既存の /play 起点メッセージを編集して返信関係を維持する
                            target_message = await self._to_durable_message(
                                state.last_now_playing_message
                            )
                            # 同じメッセージ上で次曲の UI に差し替える
                            await target_message.edit(content=None, embed=None, view=view)
                            # 参照を最新の Message オブジェクトへ更新する
                            state.last_now_playing_message = target_message
                        else:
                            # 既存メッセージが無い場合のみ新規投稿する（フォールバック）
                            state.last_now_playing_message = await channel.send(view=view, silent=True)
                        # Now Playing 表示後にプログレスバーの定期更新を開始する
                        self._start_progress_updater(guild_id)
                    # 送信または編集処理中に例外が発生した場合のハンドリング
                    except Exception as e:
                        # 編集失敗時は新規送信で復旧を試みる
                        logger.error(f"Failed to update now playing message: {e}")
                        try:
                            # 壊れた参照を捨てる
                            state.last_now_playing_message = None
                            # チャンネルへ新規 Now Playing を投稿する（silent）
                            state.last_now_playing_message = await channel.send(view=view, silent=True)
                            # 復旧後もプログレス更新を開始する
                            self._start_progress_updater(guild_id)
                        except Exception as send_error:
                            # 復旧にも失敗した旨をログへ残す
                            logger.error(
                                f"Failed to recover now playing message: {send_error}"
                            )
        except Exception as e:
            guild = self.bot.get_guild(guild_id)
            # UnsupportedMediaError は想定内のためフルスタックを出さない
            if UnsupportedMediaError is not None and isinstance(e, UnsupportedMediaError):
                # 短い WARNING のみ
                logger.warning(
                    "Guild %s (%s): Playback skipped (unsupported): %s",
                    guild_id,
                    guild.name if guild else "",
                    e,
                )
            else:
                # 想定外は従来どおり ERROR + traceback
                logger.error(
                    f"Guild {guild_id} ({guild.name if guild else ''}): Playback error: {e}",
                    exc_info=True,
                )
            # 再生状態を一旦落とす（次曲再生や専用パネルの前準備）
            state.is_seeking = False
            state.is_playing = False
            # シーク失敗は別経路。通常再生のロード失敗は Components V2 へ出す
            if not is_seek_operation and track_to_play is not None:
                # URL + yt-dlp 等のエラー文言をバナー用に組み立てる
                load_error_text = self._format_ui_load_error(
                    url=getattr(track_to_play, "url", None),
                    title=getattr(track_to_play, "title", None),
                    error=e,
                )
                # 次曲の有無を失敗曲クリア前に判定する
                has_next_in_queue = not state.queue.empty()
                # 壊れた曲を Loop ALL へ再投入しない
                # （従来は再投入していたが利用不可 URL で無限ループになる）
                state.current_track = None
                # 再生位置をリセットする
                state.reset_playback_tracking()
                # Components V2（次曲下バナー or 専用パネル）で通知する
                should_continue = await self._present_playback_load_error(
                    guild_id,
                    load_error_text,
                    has_next_in_queue=has_next_in_queue,
                    preferred_message=play_msg,
                )
                # 次曲があればスキップ再生する
                if should_continue:
                    # /play の searching 応答を次曲 Now Playing の編集先として残す
                    if play_msg is not None and state.last_now_playing_message is None:
                        # 次曲 UI がこのメッセージを上書きできるようにする
                        state.last_now_playing_message = play_msg
                    # 次曲再生をスケジュールする
                    asyncio.create_task(self._play_next_song(guild_id))
                # エラー表示まで完了したので終了する
                return
            # シーク失敗など: 従来どおりテキスト通知（稀な経路）
            error_message = self.exception_handler.handle_error(e, guild)
            if state.last_text_channel_id:
                await self._send_background_message(
                    state.last_text_channel_id,
                    "error_message_wrapper",
                    error=error_message,
                )
            # Loop ALL のシーク失敗時のみ再投入を維持する
            if state.loop_mode == LoopMode.ALL and track_to_play and not is_seek_operation:
                await state.queue.put(track_to_play)
            state.current_track = None
            state.reset_playback_tracking()
            # 再生エラー時はプログレスバー更新を停止する
            state.stop_progress_updater()
            asyncio.create_task(self._play_next_song(guild_id))

    @staticmethod
    def _vc_has_humans(channel: Optional[discord.abc.Connectable]) -> bool:
        # チャンネル未取得時は人間なしとして扱う
        if channel is None:
            # 退出判定側で「無人」とみなす
            return False
        # members を持つチャンネルのみ人間有無を判定する
        members = getattr(channel, "members", None)
        # members が取れない場合も無人扱い（安全側に倒す）
        if members is None:
            # 退出してハング回避を優先する
            return False
        # Bot以外（人間）が1人でもいれば True
        return any(not m.bot for m in members)

    async def _apply_server_deafen(self, guild: discord.Guild) -> None:
        """サーバー側スピーカーミュート（緑）を適用。権限が無ければ自己deafへフォールバック。"""
        # 自Botの Member を取得する
        me = guild.me
        # Member が取れない場合は何もしない
        if me is None:
            # 早期リターン
            return
        try:
            # サーバー側 deafen（緑色アイコン）で自身をスピーカーミュートする
            await me.edit(deafen=True, reason="Music bot: server deafen while connected")
            # 成功時は自己deafが残っていても問題ないが、見た目をサーバー側に寄せる
            logger.debug("Guild %s: Applied server deafen to bot", guild.id)
        except (discord.Forbidden, discord.HTTPException) as e:
            # Mute/Deafen Members 権限不足などで失敗した場合は自己deafへフォールバック
            logger.warning(
                "Guild %s: Server deafen failed (%s); falling back to self_deaf",
                guild.id,
                e,
            )
            try:
                # 接続中チャンネルに対して自己スピーカーミュートを立てる
                if me.voice and me.voice.channel:
                    # Voice Identify 相当の自己deafフラグを送る
                    await guild.change_voice_state(
                        channel=me.voice.channel, self_mute=False, self_deaf=True)
            except Exception as fallback_error:
                # フォールバック失敗もログのみ（再生自体は継続させる）
                logger.warning(
                    "Guild %s: self_deaf fallback also failed: %s",
                    guild.id,
                    fallback_error,
                )

    def _schedule_auto_leave(self, guild_id: int):
        # 対象ギルドの再生状態を取得する
        state = self._get_guild_state(guild_id)
        # 状態が無ければスケジュールできない
        if not state:
            # 早期リターン
            return
        # 既存タイマーがあればキャンセルして差し替える（再スケジュール漏れ防止）
        if state.auto_leave_task and not state.auto_leave_task.done():
            # 進行中の自動退出タスクを中断する
            state.auto_leave_task.cancel()
        # まだVCに接続しているときだけ新しいタイマーを起動する
        if state.voice_client and state.voice_client.is_connected():
            # 無人確認付きの自動退出コルーチンを起動する
            state.auto_leave_task = asyncio.create_task(self._auto_leave_coroutine(guild_id))

    async def _auto_leave_coroutine(self, guild_id: int):
        try:
            # 設定された猶予秒だけ待機する（直後の再入室に対応）
            await asyncio.sleep(self.auto_leave_timeout)
        except asyncio.CancelledError:
            # 人間が戻った等でキャンセルされた場合はそのまま終了
            raise
        # 待機後に最新のギルド状態を再取得する
        state = self._get_guild_state(guild_id)
        # 状態または接続が無い場合は何もしない
        if not state or not state.voice_client or not state.voice_client.is_connected():
            # 既に切断済み
            return
        # 人間がいまだ居ない場合のみ退出する（Bot同士だけの残留を防ぐ）
        if self._vc_has_humans(state.voice_client.channel):
            # 人間が戻っているので退出不要
            return
        # テキストチャンネルがあれば退出メッセージを送る
        if state.last_text_channel_id:
            # バックグラウンド通知を送る
            await self._send_background_message(state.last_text_channel_id, "auto_left_empty_channel")
        # disconnect単体ではなく状態ごとクリーンアップして再接続ハングを防ぐ
        await self._cleanup_guild_state(guild_id)

    async def _cleanup_guild_state(self, guild_id: int):
        # 破棄前に状態を取得する（UI 更新に必要）
        state = self._get_guild_state(guild_id)
        # 状態が存在するか判定する
        if state:
            # ギルド破棄前にプログレスバー更新を停止する
            state.stop_progress_updater()
            # 切断前に再生中トラックをクリアしてグレーアウト表示できるようにする
            state.current_track = None
            # 再生中フラグも下ろす
            state.is_playing = False
            # Now Playing メッセージがある場合は切断用のグレーアウト UI に更新する
            if state.last_now_playing_message:
                # V2 LayoutView で Playback Ended 表示に切り替える
                await self._update_now_playing_message_ui(
                    guild_id,
                    finished_message=(
                        "⏹️ **Playback Ended**\n"
                        "The bot has disconnected from the voice channel."
                    ),
                )
        # ギルド状態を辞書から取り出す
        state = self.guild_states.pop(guild_id, None)
        # 取り出した状態が存在するか判定する
        if state:
            # ボイス接続とミキサーをクリーンアップする
            await state.cleanup_voice_client()
            # 自動退出タスクが動いていればキャンセルする（自分自身以外）
            if (
                state.auto_leave_task
                and not state.auto_leave_task.done()
                and state.auto_leave_task is not asyncio.current_task()
            ):
                # 他経路から呼ばれた場合のみタイマーを止める
                state.auto_leave_task.cancel()
            # キューを空にする
            await state.clear_queue()
            # ギルド名解決用にギルドオブジェクトを取得する
            guild = self.bot.get_guild(guild_id)
            # クリーンアップ完了をログに残す
            logger.info(f"Guild {guild_id} ({guild.name if guild else ''}): State cleaned up")

    def _start_progress_updater(self, guild_id: int):
        # 対象ギルドの再生状態を取得する
        state = self._get_guild_state(guild_id)
        # 状態が無ければ開始できないので終了する
        if not state:
            # 早期リターン
            return
        # 既存の更新タスクがあれば先に止める（二重起動防止）
        state.stop_progress_updater()
        # 10秒間隔のプログレス更新ループをバックグラウンドで開始する
        state.progress_update_task = asyncio.create_task(
            self._progress_updater_loop(guild_id),
            name=f"music_progress_{guild_id}",
        )

    async def _progress_updater_loop(self, guild_id: int):
        try:
            # 再生中は一定間隔で Now Playing UI を更新し続ける
            while True:
                # Discord rate limit を避けるため更新間隔だけ待機する
                await asyncio.sleep(PROGRESS_UPDATE_INTERVAL)
                # 待機後に最新のギルド状態を再取得する
                state = self._get_guild_state(guild_id)
                # 状態・再生・メッセージのいずれかが無効ならループを終了する
                if (
                    not state
                    or not state.is_playing
                    or not state.current_track
                    or not state.last_now_playing_message
                ):
                    # 更新対象が無いのでループを抜ける
                    break
                # 一時停止中は位置が変わらないため API 呼び出しをスキップする
                if state.is_paused:
                    # 次の間隔まで待つ
                    continue
                # Stop 確認中はダイアログを上書きしない
                if state.confirming_stop:
                    # 次の間隔まで待つ
                    continue
                # プログレスバー込みで Now Playing UI を再描画する
                await self._update_now_playing_message_ui(guild_id)
        except asyncio.CancelledError:
            # タスクキャンセルは正常終了として扱う
            pass
        except Exception as e:
            # 想定外エラーをログに残し、ループは終了する
            logger.error(f"Guild {guild_id}: Progress updater error: {e}", exc_info=True)
        finally:
            # ループ終了時にタスク参照をクリアする（生存中の state がある場合のみ）
            state = self._get_guild_state(guild_id)
            # 状態が残っており、かつ自分自身のタスク参照ならクリアする
            if state and state.progress_update_task is asyncio.current_task():
                # 参照を None にして再利用可能にする
                state.progress_update_task = None

    @commands.Cog.listener()
    async def on_ready(self):
        logger.info(f"{self.bot.user.name} の MusicCog が正常にロードされました。")

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState,
                                    after: discord.VoiceState):
        # 自BotがVCから切断されたらギルド状態を破棄する
        if member.id == self.bot.user.id and before.channel and not after.channel:
            # 再生状態・VoiceClient・自動退出タスクをまとめて掃除
            await self._cleanup_guild_state(member.guild.id)
            # 以降の無人判定は不要
            return

        # 対象ギルドIDを取得する
        guild_id = member.guild.id
        # Music 状態が無いギルドは無視する
        if guild_id not in self.guild_states:
            # 早期リターン
            return

        # ギルド再生状態を取得する
        state = self._get_guild_state(guild_id)
        # 未接続なら自動退出判定の対象外
        if not state or not state.voice_client or not state.voice_client.is_connected():
            # 早期リターン
            return

        # Botが現在いるVCを基準にする
        current_vc_channel = state.voice_client.channel
        # 自BotのVCと無関係な移動は無視する
        if before.channel != current_vc_channel and after.channel != current_vc_channel:
            # 早期リターン
            return

        # 人間がいなければ自動退出を（再）スケジュールする
        # NOTE: 以前は「タスク未完了なら再スケジュールしない」条件があり、
        # cancel直後に done() が遅れるとBot同士残留バグになっていた
        if not self._vc_has_humans(current_vc_channel):
            # タイムアウト付きで退出コルーチンを起動／差し替え
            self._schedule_auto_leave(guild_id)
        elif state.auto_leave_task and not state.auto_leave_task.done():
            # 人間が戻ったので予定されていた自動退出を取り消す
            state.auto_leave_task.cancel()

    @commands.hybrid_command(name="play", description="Play a song or add it to the queue.")
    @app_commands.describe(query="Song title or URL to play.")
    async def play(self, ctx: commands.Context, *, query: str):
        # レスポンス送信を保留（defer）にし、処理がタイムアウトしないようにする
        await ctx.defer()

        # ギルド固有の再生状態クラスを取得する
        state = self._get_guild_state(ctx.guild.id)
        # 再生状態クラスが正しく取得できたか（上限に達していないか）判定する
        if not state:
            # 取得に失敗した場合は、エラーメッセージを送信して処理を終了する
            await self._send_ctx_message(ctx, content="サーバーの上限に達しています。", ephemeral=True)
            # コマンドの実行を終了する
            return

        # ボイスチャンネルへの接続を確認、または新規接続を行い、ボイスクライアントを取得する
        vc = await self._ensure_voice(ctx, connect_if_not_in=True)
        # ボイスチャンネルに接続できなかったか判定する
        if not vc:
            # 接続失敗時はこれ以上処理を進めず終了する
            return

        # キューのサイズが設定上の上限値に達しているか判定する
        if state.queue.qsize() >= self.max_queue_size:
            # キュー上限到達のエラーレスポンスを返信して終了する
            await self._send_response(ctx, "max_queue_size_reached",
                                      max_size=self.max_queue_size)
            # コマンドの実行を終了する
            return

        # コマンド開始時点での再生状態、または読み込み状態を取得してフラグに保持する
        was_playing = state.is_playing or state.is_loading
        # 読み込み状態フラグをTrueにする
        state.is_loading = True

        # 検索中のメッセージオブジェクトを初期化する
        searching_msg = None
        try:
            # 検索開始した事実を示す一時的メッセージを送信し、オブジェクトを保持する
            searching_msg = await self._send_ctx_message(
                ctx,
                content=self.exception_handler.get_message("searching_for_song", query=query),
            )

            # yt-dlp等を用いて検索クエリから音声情報を抽出する（プレイリスト上限は config を渡す）
            extracted_media = await extract_audio_data(
                query,
                shuffle_playlist=False,
                max_playlist_items=self.max_playlist_items,
            )

            # 音声情報の抽出に失敗した（結果が空だった）か判定する
            if not extracted_media:
                # 検索メッセージが取得できていれば、検索結果なしのテキストに書き換える
                if searching_msg:
                    # 検索メッセージの内容を更新する
                    await searching_msg.edit(content=self.exception_handler.get_message("search_no_results", query=query))
                else:
                    # メッセージが無い場合は、新規に検索結果なしメッセージを送信する
                    await self._send_ctx_message(
                        ctx,
                        content=self.exception_handler.get_message("search_no_results", query=query),
                    )

                # コマンドの実行を終了する
                return

            # 抽出されたデータがリスト（プレイリスト）であるか判定し、トラックリストに変換する
            tracks = extracted_media if isinstance(extracted_media, list) else [extracted_media]
            # 追加された曲数と、最初のトラックへの参照を初期化する
            added_count, first_track = 0, None

            # 抽出されたトラックのリストを走査する
            for track in tracks:
                # キューが上限数に達していないか走査のたびに判定する
                if state.queue.qsize() < self.max_queue_size:
                    # トラックのリクエストユーザーIDを設定する
                    track.requester_id = ctx.author.id
                    # ストリームURLを初期状態としてNoneにする
                    track.stream_url = None
                    # トラックオブジェクトを非同期の再生キューに追加する
                    await state.queue.put(track)
                    # 最初のトラックである（added_countが0）か判定する
                    if added_count == 0:
                        # 最初のトラックへの参照を保持する
                        first_track = track
                    # 追加曲数のカウントを1加算する
                    added_count += 1
                # キュー上限に達したため走査を終了する
                else:
                    # すでに曲追加中の場合、キュー上限エラーメッセージを送信する
                    await self._send_ctx_message(
                        ctx,
                        content=self.exception_handler.get_message("max_queue_size_reached",
                                                                  max_size=self.max_queue_size)
                    )

                    # ループ処理を終了する
                    break

            # すでに何らかの音楽が再生中であったか判定する
            if was_playing:
                # 複数曲（プレイリスト）がキューに追加されたか判定する
                if added_count > 1:
                    # 従来どおりプレイリスト追加は Embed で簡潔に出す
                    playlist_embed = discord.Embed(
                        description=self.exception_handler.get_message(
                            "added_playlist_to_queue",
                            count=added_count,
                        ),
                        color=discord.Color.from_rgb(79, 194, 255),
                    )
                    # 検索開始メッセージがあれば Embed に差し替える
                    if searching_msg:
                        # 本文を消して Embed のみにする
                        await searching_msg.edit(content=None, embed=playlist_embed, view=None)
                    else:
                        # 新規に Embed を送る（silent）
                        await self._send_ctx_message(ctx, embed=playlist_embed)

                # 1曲だけが追加され、かつそのトラックオブジェクトが有効か判定する
                elif added_count == 1 and first_track:
                    # 単曲追加は小さめの Components V2 パネルにする
                    added_view = QueueAddedLayoutView(first_track, ctx.author)
                    # 検索開始メッセージがあれば V2 に差し替える
                    if searching_msg:
                        # 本文・Embed を消して LayoutView のみにする
                        await searching_msg.edit(content=None, embed=None, view=added_view)
                    else:
                        # 新規に V2 パネルを送る
                        await self._send_ctx_message(ctx, view=added_view)
                # キュー追加後に Now Playing のキュー一覧を更新する
                await self._update_now_playing_message_ui(ctx.guild.id)

            # 再生中ではない（この play コマンドで新規再生を開始する）か判定する
            if not was_playing:
                # _play_next_songを実行し、searching_msgを再生メッセージとして流用・編集する
                await self._play_next_song(ctx.guild.id, play_msg=searching_msg)

        # 検索または追加処理中に例外が発生した場合のハンドリングを行う
        except Exception as e:
            # Video unavailable / DRM 等は Components V2 のエラーパネルで出す
            load_error_text = self._format_ui_load_error(
                url=query,
                error=e,
            )
            # エラー専用 LayoutView を組み立てる
            error_view = LoadErrorLayoutView(load_error_text)
            # 検索中メッセージが存在するか判定する
            if searching_msg:
                try:
                    # 検索メッセージをエラー専用 V2 に差し替える
                    await searching_msg.edit(content=None, embed=None, view=error_view)
                except Exception as edit_err:
                    # 編集失敗時はフォールバックでテキスト通知する
                    logger.warning(
                        "Guild %s: Failed to edit play reply into load-error panel: %s",
                        ctx.guild.id if ctx.guild else "?",
                        edit_err,
                    )
                    # 従来のラップ文言へフォールバックする
                    error_message = self.exception_handler.handle_error(e, ctx.guild)
                    wrapped_error_msg = self.exception_handler.get_message(
                        "error_message_wrapper",
                        error=error_message,
                    )
                    # テキストで編集する
                    await searching_msg.edit(content=wrapped_error_msg)
            else:
                # メッセージがない場合は、新規に V2 パネルを送信する
                await self._send_ctx_message(ctx, view=error_view)

        # 最終的に必ず実行するクリーンアップ処理
        finally:
            # 読み込み状態フラグをFalseに戻す
            state.is_loading = False

    @commands.hybrid_command(name="seek", description="Seek to a specified time in the track.")
    @app_commands.describe(time="Seek target (e.g. 1:30 or 90 seconds).")
    async def seek(self, ctx: commands.Context, *, time: str):
        state = self._get_guild_state(ctx.guild.id)
        if not state:
            await self._send_ctx_message(ctx, content="エラーが発生しました。", ephemeral=True)

            return

        await ctx.defer()

        if not await self._ensure_voice(ctx, connect_if_not_in=False):
            return

        if not state.current_track:
            await self._send_response(ctx, "nothing_to_skip", ephemeral=True)
            return

        seek_seconds = parse_time_to_seconds(time)
        if seek_seconds is None:
            await self._send_response(ctx, "invalid_time_format", ephemeral=True)
            return

        if seek_seconds >= state.current_track.duration:
            await self._send_response(ctx, "seek_beyond_duration", ephemeral=True,
                                      duration=format_duration(state.current_track.duration))
            return

        # シーク操作: is_seekingフラグでコールバックからの二重処理を防止
        state.is_seeking = True
        try:
            # _play_next_songがseek_seconds > 0で呼ばれると、同じトラックをシーク位置から再生
            # add_source('music', new_source) が旧ソースを自動的に置き換えるため、
            # 旧ソースの明示的な削除は不要
            await self._play_next_song(ctx.guild.id, seek_seconds=seek_seconds)
            await self._send_response(ctx, "seeked_to_position", position=format_duration(seek_seconds))
        finally:
            state.is_seeking = False

    @commands.hybrid_command(name="pause", description="Pause playback.")
    async def pause(self, ctx: commands.Context):
        state = self._get_guild_state(ctx.guild.id)
        if not state or not await self._ensure_voice(ctx, connect_if_not_in=False):
            return

        if not state.is_playing:
            await self._send_response(ctx, "error_playing", ephemeral=True, error="再生中ではありません。")
            return

        if state.is_paused:
            await self._send_response(ctx, "error_playing", ephemeral=True, error="既に一時停止中です。")
            return

        state.voice_client.pause()
        state.is_paused = True
        state.paused_at = time.time()
        await self._send_response(ctx, "playback_paused")
        await self._update_now_playing_message_ui(ctx.guild.id)

    @commands.hybrid_command(name="resume", description="Resume paused playback.")
    async def resume(self, ctx: commands.Context):
        state = self._get_guild_state(ctx.guild.id)
        if not state or not await self._ensure_voice(ctx, connect_if_not_in=False):
            return

        if not state.is_paused:
            await self._send_response(ctx, "error_playing", ephemeral=True, error="一時停止中ではありません。")
            return

        state.voice_client.resume()
        state.is_paused = False
        if state.paused_at and state.playback_start_time:
            pause_duration = time.time() - state.paused_at
            state.playback_start_time += pause_duration
        state.paused_at = None
        await self._send_response(ctx, "playback_resumed")
        await self._update_now_playing_message_ui(ctx.guild.id)

    @commands.hybrid_command(name="skip", description="Skip the current track.")
    async def skip(self, ctx: commands.Context):
        state = self._get_guild_state(ctx.guild.id)
        if not state:
            await self._send_ctx_message(ctx, content="エラーが発生しました。", ephemeral=True)
            return

        await ctx.defer()
        vc = await self._ensure_voice(ctx, connect_if_not_in=False)
        if not vc or not state.current_track:
            await self._send_response(ctx, "nothing_to_skip", ephemeral=True)
            return

        skipped_title = state.current_track.title
        await self._send_response(ctx, "skipped_song", title=skipped_title)

        # ミキサーから音楽ソースを削除
        # remove_sourceのコールバックで_on_music_source_removedが呼ばれ、次の曲が自動再生される
        if state.mixer:
            await state.mixer.remove_source('music')
        elif state.voice_client and state.voice_client.is_playing():
            # ミキサーなしで再生中の場合（フォールバック）
            state.voice_client.stop()

    @commands.hybrid_command(name="stop", description="Stop playback and clear the queue.")
    async def stop(self, ctx: commands.Context):
        state = self._get_guild_state(ctx.guild.id)
        if not state:
            await self._send_ctx_message(ctx, content="エラーが発生しました。", ephemeral=True)

            return

        await ctx.defer()
        if not await self._ensure_voice(ctx, connect_if_not_in=False):
            return

        state.loop_mode = LoopMode.OFF
        await state.clear_queue()
        # Stop 確認ダイアログが残っていれば解除する
        state.confirming_stop = False
        # キューページを先頭に戻す
        state.queue_page = 0
        if state.mixer:
            state.mixer.stop()
            state.mixer = None
        if state.voice_client and state.voice_client.is_playing():
            state.voice_client.stop()
        state.is_playing = False
        state.is_paused = False
        # 停止前に URL 再生履歴を残す（クリア後は current_track が無い）
        self._remember_play_history_url(state, state.current_track)
        state.current_track = None
        state.reset_playback_tracking()
        # stop 時はプログレスバー更新を停止する
        state.stop_progress_updater()
        await self._send_response(ctx, "stopped_playback")
        await self._update_now_playing_message_ui(ctx.guild.id)

    @commands.hybrid_command(name="leave", description="Disconnect the bot from the voice channel.")
    async def leave(self, ctx: commands.Context):
        state = self._get_guild_state(ctx.guild.id)
        if not state:
            await self._send_ctx_message(ctx, content="エラーが発生しました。", ephemeral=True)

            return

        await ctx.defer()
        async with state.connection_lock:
            if not state.voice_client or not state.voice_client.is_connected():
                await self._send_response(ctx, "bot_not_in_voice_channel", ephemeral=True)
                return
            await self._send_response(ctx, "leaving_voice_channel")
            await self._cleanup_guild_state(ctx.guild.id)

    @commands.hybrid_command(name="queue", description="Show the current playback queue.")
    async def queue(self, ctx: commands.Context):
        # ギルドの再生状態を取得する
        state = self._get_guild_state(ctx.guild.id)
        # 状態が無ければエラーを返す
        if not state:
            # エフェメラルでエラー通知する
            await self._send_ctx_message(ctx, content="エラーが発生しました。", ephemeral=True)
            # 処理を終了する
            return

        # 操作チャンネルを記録する
        state.update_last_text_channel(ctx.channel.id)
        # キューも再生中曲も無い場合は空メッセージを返す
        if state.queue.empty() and not state.current_track:
            # キュー空メッセージをエフェメラルで送る
            await self._send_ctx_message(
                ctx,
                content=self.exception_handler.get_message("queue_empty"),
                ephemeral=True,
            )
            # 処理を終了する
            return

        # キュー表示ページを先頭に戻す
        state.queue_page = 0
        # Now Playing パネルがある場合はそこにキュー＋ページングを統合表示する
        if state.last_now_playing_message and state.current_track:
            # パネルUIを最新キューで再描画する
            await self._update_now_playing_message_ui(ctx.guild.id)
            # パネル参照を促すエフェメラル案内を送る
            await self._send_ctx_message(
                ctx,
                content="📜 キューは Now Playing パネル下部に表示しています（ページ切替ボタンあり）。",
                ephemeral=True,
            )
            # 処理を終了する
            return

        # パネルが無い場合のフォールバック：簡易テキスト一覧を送る
        queue_text, _page, _total = self._build_queue_display_text(state)
        # フォールバック本文を送信する
        await self._send_ctx_message(ctx, content=queue_text)

    def _build_queue_display_text(self, state: GuildState) -> Tuple[str, int, int]:
        """キュー一覧テキストと現在ページ・総ページ数を返す（0始まりページ）。"""
        # 内部キューからリストを取り出す
        queue_list = list(state.queue._queue)
        # 総曲数を取得する
        total_items = len(queue_list)
        # 曲が無ければ空表示を返す
        if total_items == 0:
            # ページは 0/1 として扱う
            return "### Queue\n*(empty — no upcoming tracks)*", 0, 1

        # 総ページ数を計算する（最低1）
        total_pages = max(1, math.ceil(total_items / QUEUE_PAGE_SIZE))
        # 保存ページを有効範囲にクランプする
        page = max(0, min(state.queue_page, total_pages - 1))
        # クランプ結果を状態へ書き戻す
        state.queue_page = page
        # 表示範囲の開始インデックスを求める
        start = page * QUEUE_PAGE_SIZE
        # 表示範囲の終了インデックスを求める
        end = start + QUEUE_PAGE_SIZE
        # 行バッファを初期化する
        lines: List[str] = []
        # ページ内の曲を走査する
        for i, track in enumerate(queue_list[start:end], start=start + 1):
            # タイトルが None / 空でも落ちないよう文字列化する
            raw_title = track.title or "Unknown title"
            # 長すぎるタイトルは省略する
            title = raw_title if len(raw_title) <= 42 else raw_title[:39] + "..."
            # URL が無い場合はリンクにせずプレーン表示にする
            track_url = track.url or ""
            # 番号付き行を追加する
            if track_url:
                # リンク付きで追加する
                lines.append(f"`{i}.` [{title}]({track_url})")
            else:
                # URL 無しはタイトルのみ追加する
                lines.append(f"`{i}.` {title}")
        # 見出し付き本文を組み立てる
        body = (
            f"### Queue ({total_items}) — {page + 1}/{total_pages}\n"
            + "\n".join(lines)
        )
        # 本文とページ情報を返す
        return body, page, total_pages

    @commands.hybrid_command(name="nowplaying", description="Show info about the currently playing track.")
    async def nowplaying(self, ctx: commands.Context):
        # ギルドの再生状態オブジェクトを取得する
        state = self._get_guild_state(ctx.guild.id)
        # 再生状態オブジェクトが存在しない、または現在再生中のトラックが無いか判定する
        if not state or not state.current_track:
            # 再生中の曲がない旨を示すエラーレスポンスを送信する
            await self._send_ctx_message(
                ctx,
                content=self.exception_handler.get_message("now_playing_nothing"),
                ephemeral=True,
            )

            # コマンドの実行を終了する
            return

        # コマンドのレスポンス保留（defer）を開始する
        await ctx.defer()
        
        # 既に Now Playing コントロールメッセージが存在しているか判定する
        if state.last_now_playing_message:
            try:
                # チャット上の古いコントロールメッセージを削除する
                await state.last_now_playing_message.delete()
            # メッセージ削除中に発生した例外のハンドリング
            except Exception:
                # 削除失敗時はパスする
                pass
            # 古いメッセージへの参照を初期化する
            state.last_now_playing_message = None

        # 一体型UIが統合された LayoutView オブジェクトを構築する
        view = MusicControllerView(self, ctx.guild.id)
        
        # 新しい Now Playing リッチUIを送信する（embedは指定しない）
        msg = await self._send_ctx_message(ctx, view=view)
        # 送信が成功してメッセージオブジェクトが返ってきたか判定する
        if msg:
            # webhook期限切れ回避のため、チャンネル経由のMessageへ変換して保存する
            state.last_now_playing_message = await self._to_durable_message(msg)
            # nowplaying 再表示時もプログレスバー定期更新を開始する
            self._start_progress_updater(ctx.guild.id)

    def _create_now_playing_embed(self, state: GuildState, track: Track) -> discord.Embed:
        # 再生が一時停止中であるか判定して、表示用アイコンを設定する
        status_icon = "⏸️" if state.is_paused else "▶️"
        # 再生が一時停止中であるか判定して、表示用のステータス文字列を設定する
        status_text = "Paused" if state.is_paused else "Playing"
        
        # リクエストユーザーのメンション文字列を初期化する
        requester_mention = "Unknown"
        # トラック情報にリクエストユーザーIDが存在するか判定する
        if track.requester_id:
            # ユーザーIDをDiscordのメンション形式に変換して設定する
            requester_mention = f"<@{track.requester_id}>"
            
        # Embedオブジェクトを作成し、タイトルとブランドカラー（水色）を設定する
        embed = discord.Embed(
            title=f"{status_icon} Now {status_text}",
            color=discord.Color.from_rgb(79, 194, 255)
        )
        # Embedのメイン説明文として、再生中のトラックタイトルをリンク付きで設定する
        embed.description = f"**[{track.title}]({track.url})**"
        
        # 現在の再生位置（秒）を取得する
        current_pos = state.get_current_position()
        # 再生位置と曲の長さからプログレスバー文字列を生成する
        progress_bar = self._create_progress_bar(current_pos, track.duration)
        # 現在の再生時間と曲の総時間をフォーマットした文字列を構築する
        duration_str = f"`{format_duration(current_pos)}` / `{format_duration(track.duration)}`"
        # プログレスバーと再生時間を表示するフィールドをEmbedに追加する
        embed.add_field(name="Progress", value=f"{progress_bar}\n{duration_str}", inline=False)
        
        # アップローダー/チャンネル名が定義されているか判定し、無ければ「Unknown」にする
        uploader_val = track.uploader if track.uploader else "Unknown"
        # チャンネルURLがあればリンク付きにする
        if track.uploader_url and uploader_val != "Unknown":
            # Embed 用の Markdown リンクにする
            uploader_val = f"[{uploader_val}]({track.uploader_url})"
        # アップローダー名を記載するフィールドをEmbedに追加する
        embed.add_field(name="Channel / Uploader", value=uploader_val, inline=True)
        # リクエストユーザーを記載するフィールドをEmbedに追加する
        embed.add_field(name="Requested By", value=requester_mention, inline=True)
        # 現在有効になっているループモード名を表示するフィールドをEmbedに追加する
        embed.add_field(name="Loop Mode", value=f"`{state.loop_mode.name.lower()}`", inline=True)
        
        # 現在のキューに残っている曲数を取得する
        remaining = state.queue.qsize()
        # 残り曲数を表示するフィールドをEmbedに追加する
        embed.add_field(name="Queue Status", value=f"{remaining} songs in queue", inline=True)
        
        # サムネイル画像のURLが有効であり、かつ文字列「None」ではないか判定する
        if track.thumbnail and track.thumbnail.strip() and track.thumbnail != "None":
            # サムネイルURLをEmbedの右上サムネイル画像として登録する
            embed.set_thumbnail(url=track.thumbnail)
            
        # フッターにMOMOKAミュージックプレイヤーのクレジットを設定する
        embed.set_footer(text="MOMOKA Music Player")
        # 構築完了したEmbedオブジェクトを返す
        return embed

    def _format_ui_load_error(
        self,
        *,
        url: Optional[str] = None,
        title: Optional[str] = None,
        error: Optional[BaseException] = None,
        detail: Optional[str] = None,
    ) -> str:
        """Components V2 用のロード失敗文言（URL + エラー）を組み立てる。"""
        # 表示行を溜めるリスト
        lines: List[str] = []
        # URL が空ならプレースホルダを使う
        display_url = (url or "").strip() or "(unknown URL)"
        # 先頭行に URL を載せる
        lines.append(f"Could not load: {display_url}")
        # タイトルがあれば補助行として付ける
        if title and str(title).strip():
            # タイトル行を追加する
            lines.append(f'Title: "{title}"')
        # 詳細メッセージの候補を決める
        msg = (detail or "").strip() if detail else ""
        # 明示 detail が無く、例外がある場合は原因例外を優先する
        if not msg and error is not None:
            # yt-dlp 元例外（Video unavailable 等）を優先する
            cause = getattr(error, "__cause__", None)
            # 原因があればそれ、無ければ例外本体の文字列
            raw = str(cause) if cause else str(error)
            # 前後空白を落とす
            msg = raw.strip()
        # メッセージが取れた場合のみ整形して追加する
        if msg:
            # ログ由来の ANSI 色コードを除去する
            msg = re.sub(r"\x1b\[[0-9;]*m", "", msg)
            # yt-dlp の "ERROR: " プレフィックスを落とす
            if msg.upper().startswith("ERROR:"):
                # 先頭 6 文字を除く
                msg = msg[6:].strip()
            # 整形後のエラー行を追加する
            lines.append(msg)
        # 改行結合したバナー文言を返す
        return "\n".join(lines)

    async def _present_playback_load_error(
        self,
        guild_id: int,
        load_error_text: str,
        *,
        has_next_in_queue: bool,
        preferred_message: Optional[discord.Message] = None,
    ) -> bool:
        """
        ロード失敗を Components V2 で出す。
        次曲あり: Now Playing 最下部バナー用に state へ載せ True を返す。
        次曲なし: 専用パネルを出し False（次曲再生しない）を返す。
        """
        # ギルド状態を取得する
        state = self._get_guild_state(guild_id)
        # 状態が無ければ次曲再生もしない
        if not state:
            # 継続不可
            return False
        # 単発失敗（次曲なし）は専用パネルへ
        if not has_next_in_queue:
            # 再生中トラックをクリアする
            state.current_track = None
            # 再生位置をリセットする
            state.reset_playback_tracking()
            # バナー状態は専用パネルに任せるのでクリアする
            state.ui_load_error = None
            # 表示済みフラグも戻す
            state.ui_load_error_seen = False
            # プログレス更新を止める
            state.stop_progress_updater()
            # エラー専用 Components V2 を出す（/play 応答があればそれを優先編集）
            await self._show_standalone_load_error_panel(
                guild_id,
                load_error_text,
                preferred_message=preferred_message,
            )
            # アイドルミキサーを片付ける
            await self._cleanup_idle_mixer(state)
            # 次曲再生へ進まない
            return False
        # 次曲がある場合はバナーを載せたままスキップ再生する
        state.ui_load_error = load_error_text
        # 次曲の Now Playing で一度見せ、その次の曲で消す
        state.ui_load_error_seen = False
        # 呼び出し側は次曲再生へ進む
        return True

    async def _show_standalone_load_error_panel(
        self,
        guild_id: int,
        load_error_text: str,
        preferred_message: Optional[discord.Message] = None,
    ) -> None:
        """単発再生のロード失敗時、エラー専用 Components V2 パネルを出す。"""
        # ギルド状態を取得する
        state = self._get_guild_state(guild_id)
        # 状態が無ければ何もしない
        if not state:
            # 早期リターン
            return

        # エラー専用 LayoutView を組み立てる
        view = LoadErrorLayoutView(load_error_text)

        # 編集候補: /play の searching 応答 → 既存 Now Playing の順
        edit_candidates: List[discord.Message] = []
        # preferred があれば最優先候補へ入れる
        if preferred_message is not None:
            # /play 応答などを先頭に置く
            edit_candidates.append(preferred_message)
        # 既存 Now Playing があれば続ける
        if state.last_now_playing_message is not None:
            # 同一メッセージの二重編集を避ける
            if preferred_message is None or state.last_now_playing_message.id != getattr(
                preferred_message, "id", None
            ):
                # Now Playing を候補へ追加する
                edit_candidates.append(state.last_now_playing_message)

        # 候補を順に編集試行する
        for candidate in edit_candidates:
            try:
                # webhook 期限切れ回避のため通常 Message へ変換する
                target = await self._to_durable_message(candidate)
                # 変換できた場合のみ編集する
                if target is not None:
                    # エラー専用 UI に上書きする
                    await target.edit(content=None, embed=None, view=view)
                    # 参照をクリアして以降のプログレス更新対象外にする
                    state.last_now_playing_message = None
                    # 成功したので終了する
                    return
            except Exception as e:
                # 編集失敗はログに残し、次候補または新規送信へ進む
                logger.warning(
                    f"Guild {guild_id}: Failed to edit message into load-error panel: {e}"
                )
                # preferred 以外（Now Playing）が壊れていれば参照を捨てる
                if candidate is state.last_now_playing_message:
                    # 壊れた参照を捨てる
                    state.last_now_playing_message = None

        # 新規送信先チャンネルを解決する
        channel = None
        # last_text_channel_id があればそこへ送る
        if state.last_text_channel_id:
            # チャンネルオブジェクトを取得する
            channel = self.bot.get_channel(state.last_text_channel_id)
        # テキストチャンネルでなければ送れない
        if not isinstance(channel, discord.TextChannel):
            # 送信先なし
            return
        try:
            # @silent でエラー専用パネルを新規投稿する
            await channel.send(view=view, silent=True)
        except Exception as e:
            # 送信失敗をログする
            logger.error(f"Guild {guild_id}: Failed to send load-error panel: {e}")
    async def _update_now_playing_message_ui(
        self,
        guild_id: int,
        finished_message: Optional[str] = None,
    ):
        # ギルドの再生状態オブジェクトを取得する
        state = self._get_guild_state(guild_id)
        # 再生状態、または直前の再生中メッセージが存在しない場合は処理を中断する
        if not state or not state.last_now_playing_message:
            # 早期リターン
            return

        # 最新の再生状態を元に一体型UI（LayoutView）を新規構築する
        view = MusicControllerView(
            self,
            guild_id,
            finished_message=finished_message,
        )
        # 編集対象メッセージをローカル変数に保持する
        target_message = state.last_now_playing_message

        try:
            # InteractionMessageのままならチャンネル経由Messageへ変換する
            target_message = await self._to_durable_message(target_message)
            # 変換結果を状態へ反映する
            state.last_now_playing_message = target_message
            # 古いメッセージの Embed をクリアしつつ、新しい V2レイアウトでメッセージを上書き編集する
            await target_message.edit(embed=None, view=view)
        except discord.NotFound:
            # ユーザー削除などでメッセージが消えている場合は参照を捨てて更新ループを止める
            state.last_now_playing_message = None
            # プログレス更新も止めて 404 連打を防ぐ
            state.stop_progress_updater()
            # 想定内のため WARNING に留める
            logger.warning(
                f"Guild {guild_id}: Now Playing message missing (deleted); "
                "cleared reference to stop update errors."
            )
        except discord.HTTPException as e:
            # Invalid Webhook Token（50027）は期限切れInteraction応答の典型なので復旧を試みる
            if e.code == 50027:
                recovered = await self._recover_now_playing_message(state, view)
                # 復旧できなければ参照を捨ててエラー連打を防ぐ
                if not recovered:
                    # 期限切れ参照を破棄する
                    state.last_now_playing_message = None
                    # プログレス更新も止めて無駄なAPI呼び出しを防ぐ
                    state.stop_progress_updater()
                    # 復旧失敗を警告ログに残す
                    logger.warning(
                        f"Guild {guild_id}: Now Playing webhook token expired; "
                        "cleared message reference to stop update errors."
                    )
            elif e.code == 10008:
                # Unknown Message: NotFound 以外の経路でも参照を破棄する
                state.last_now_playing_message = None
                # プログレス更新を止める
                state.stop_progress_updater()
                # 連打防止の警告を残す
                logger.warning(
                    f"Guild {guild_id}: Now Playing message unknown (10008); "
                    "cleared reference to stop update errors."
                )
            else:
                # それ以外のHTTPエラーは通常どおり記録する
                logger.error(f"Failed to update now playing message UI: {e}")
        # 編集処理中に例外が発生した場合のハンドリング
        except Exception as e:
            # エラーログを出力する
            logger.error(f"Failed to update now playing message UI: {e}")

        # 再生中の曲がなくなっている（再生が終了または停止している）か判定する
        if not state.current_track:
            # メッセージの参照をクリアして、次の再生に備える
            state.last_now_playing_message = None

    async def _recover_now_playing_message(
        self,
        state: "GuildState",
        view: "MusicControllerView",
    ) -> bool:
        """期限切れwebhookのNow Playingメッセージをチャンネル再送で復旧する。"""
        # 送信先チャンネルIDが無ければ復旧不可
        if not state.last_text_channel_id:
            # 復旧失敗
            return False
        # チャンネルオブジェクトを取得する
        channel = self.bot.get_channel(state.last_text_channel_id)
        # テキストチャンネル以外は復旧対象外
        if not isinstance(channel, discord.TextChannel):
            # 復旧失敗
            return False
        try:
            # 古い期限切れメッセージは削除を試みる（失敗しても続行）
            if state.last_now_playing_message:
                try:
                    # 期限切れメッセージを削除する
                    await state.last_now_playing_message.delete()
                except Exception:
                    # 削除失敗は無視して再送へ進む
                    pass
            # チャンネル経由で新しい Now Playing を送信する（webhook非依存・silent）
            state.last_now_playing_message = await channel.send(view=view, silent=True)
            # 復旧成功
            return True
        except Exception as e:
            # 再送失敗をログに残す
            logger.error(f"Failed to recover now playing message: {e}")
            # 復旧失敗
            return False

    def _create_progress_bar(self, current: int, total: int, length: int = PROGRESS_BAR_LENGTH) -> str:
        # 総時間が無効な場合は空のバーを返す
        if total <= 0:
            # 未確定長の曲向けにプレースホルダを返す
            return "─" * length
        # 0.0〜1.0 に正規化した進捗率を計算する
        progress = min(max(current, 0) / total, 1.0)
        # バー末尾に ○ を残すため、塗りつぶしは最大 length-1 にする
        filled = min(int(length * progress), length - 1)
        # 塗りつぶし・現在位置・残りを結合してプログレスバー文字列を作る
        bar = "━" * filled + "○" + "─" * (length - filled - 1)
        # 生成したバー文字列を返す
        return bar

    @commands.hybrid_command(name="shuffle", description="Shuffle the playback queue.")
    async def shuffle(self, ctx: commands.Context):
        state = self._get_guild_state(ctx.guild.id)
        if not state or not await self._ensure_voice(ctx, connect_if_not_in=False):
            return

        if state.queue.qsize() < 2:
            await self._send_response(ctx, "error_playing", ephemeral=True,
                                      error="シャッフルするにはキューに2曲以上必要です。")
            return

        queue_list = list(state.queue._queue)
        random.shuffle(queue_list)
        state.queue = asyncio.Queue()
        for item in queue_list:
            await state.queue.put(item)
        await self._send_response(ctx, "queue_shuffled")
        # Now Playing のキュー表示を更新する
        await self._update_now_playing_message_ui(ctx.guild.id)

    @commands.hybrid_command(name="clear", description="Clear the queue (does not stop the current track).")
    async def clear(self, ctx: commands.Context):
        state = self._get_guild_state(ctx.guild.id)
        if not state or not await self._ensure_voice(ctx, connect_if_not_in=False):
            return

        await state.clear_queue()
        # キューページを先頭に戻す
        state.queue_page = 0
        await self._send_response(ctx, "queue_cleared")
        # Now Playing のキュー表示を更新する
        await self._update_now_playing_message_ui(ctx.guild.id)

    @commands.hybrid_command(name="remove", description="Remove a track from the queue by number.")
    @app_commands.describe(index="Queue position to remove.")
    async def remove(self, ctx: commands.Context, index: int):
        state = self._get_guild_state(ctx.guild.id)
        if not state:
            await self._send_ctx_message(ctx, content="エラーが発生しました。", ephemeral=True)

            return

        if index < 1:
            await self._send_response(ctx, "invalid_queue_number", ephemeral=True)
            return

        if state.queue.empty():
            await self._send_ctx_message(
                ctx,
                content=self.exception_handler.get_message("queue_empty"),
                ephemeral=True,
            )

            return

        actual_index = index - 1
        if not (0 <= actual_index < state.queue.qsize()):
            await self._send_response(ctx, "invalid_queue_number", ephemeral=True)
            return

        queue_list = list(state.queue._queue)
        removed_track = queue_list.pop(actual_index)
        state.queue = asyncio.Queue()
        for item in queue_list:
            await state.queue.put(item)
        await self._send_response(ctx, "song_removed", title=removed_track.title)
        # Now Playing のキュー表示を更新する
        await self._update_now_playing_message_ui(ctx.guild.id)

    @commands.hybrid_command(name="volume", description="Change the volume (0-200).")
    @app_commands.describe(level="Volume level to set (0-200).")
    async def volume(self, ctx: commands.Context, level: int):
        if not 0 <= level <= 200:
            await self._send_ctx_message(ctx, content="音量は0から200の間で指定してください。", ephemeral=True)

            return

        state = self._get_guild_state(ctx.guild.id)
        if not state:
            await self._send_ctx_message(ctx, content="エラーが発生しました。", ephemeral=True)

            return

        state.volume = level / 100.0
        state.update_activity()
        if state.mixer:
            await state.mixer.set_volume('music', state.volume)
        await self._send_response(ctx, "volume_set", volume=level)

    @commands.hybrid_command(name="loop", description="Set the loop playback mode.")
    @app_commands.describe(mode="Select the loop mode.")
    @app_commands.choices(mode=[
        app_commands.Choice(name="オフ (Loop Off)", value="off"),
        app_commands.Choice(name="現在の曲をループ (Loop One)", value="one"),
        app_commands.Choice(name="キュー全体をループ (Loop All)", value="all")
    ])
    async def loop(self, ctx: commands.Context, mode: str):
        state = self._get_guild_state(ctx.guild.id)
        if not state:
            await self._send_ctx_message(ctx, content="エラーが発生しました。", ephemeral=True)

            return

        await ctx.defer()
        mode_map = {"off": LoopMode.OFF, "one": LoopMode.ONE, "all": LoopMode.ALL}
        mode_val = mode.lower()
        if mode_val not in mode_map:
            await self._send_ctx_message(
                ctx,
                content="無効なモードです。`off`, `one`, `all`のいずれかを指定してください。",
                ephemeral=True,
            )

            return
        state.loop_mode = mode_map.get(mode_val, LoopMode.OFF)
        state.update_activity()
        await self._send_response(ctx, f"loop_{mode_val}")
        # Now Playing の Loop / QLoop 表示を更新する
        await self._update_now_playing_message_ui(ctx.guild.id)

    @commands.hybrid_command(name="join", description="Join your voice channel.")
    async def join(self, ctx: commands.Context):
        await ctx.defer(ephemeral=True)
        if await self._ensure_voice(ctx, connect_if_not_in=True):
            await self._send_ctx_message(
                ctx,
                content=self.exception_handler.get_message("already_connected"),
                ephemeral=True,
            )

class QueueAddedLayoutView(discord.ui.LayoutView):
    """再生中に単曲をキュー追加したときの小さめ Components V2 パネル。"""

    def __init__(self, track: Track, requester: discord.abc.User):
        # 静的表示のためタイムアウトなし
        super().__init__(timeout=None)
        # タイトルを安全に文字列化する
        safe_title = track.title or "Unknown title"
        # 曲URLがあればリンクにする
        if track.url:
            # Markdown リンクにする
            title_line = f"[{safe_title}]({track.url})"
        else:
            # URL 無しはプレーンにする
            title_line = safe_title
        # チャンネル名を取る
        uploader_val = track.uploader or "Unknown"
        # チャンネルURLがあればリンクにする
        if track.uploader_url and uploader_val != "Unknown":
            # リンク付きチャンネル名にする
            channel_line = f"[{uploader_val}]({track.uploader_url})"
        else:
            # プレーン名にする
            channel_line = uploader_val
        # 長さをフォーマットする
        duration_str = format_duration(track.duration)
        # 小さめの本文を組み立てる
        body = (
            f"### ➕ Added to queue\n"
            f"{title_line}\n"
            f"**Channel:** {channel_line}\n"
            f"**Duration:** `{duration_str}`\n"
            f"**Requested by:** {requester.mention}"
        )
        # 水色アクセントのコンテナを作る
        container = discord.ui.Container(accent_color=discord.Color.from_rgb(79, 194, 255))
        # サムネイルがあれば Section、無ければ TextDisplay
        thumb = track.thumbnail
        if thumb and str(thumb).strip() and str(thumb) != "None":
            # 右にサムネ付きで載せる
            container.add_item(
                discord.ui.Section(
                    discord.ui.TextDisplay(body),
                    accessory=discord.ui.Thumbnail(str(thumb)),
                )
            )
        else:
            # テキストのみ載せる
            container.add_item(discord.ui.TextDisplay(body))
        # ビューにコンテナを載せる
        self.add_item(container)


class LoadErrorLayoutView(discord.ui.LayoutView):
    """単発再生のストリーム取得失敗用 Components V2 パネル。"""

    def __init__(self, load_error_text: str):
        # タイムアウトなし（静的表示）
        super().__init__(timeout=None)
        # 警告色のコンテナを作る
        container = discord.ui.Container(accent_color=discord.Color.orange())
        # 見出しを載せる
        container.add_item(
            discord.ui.TextDisplay("### Could not load track")
        )
        # 英語メッセージをコードブロックで最下部相当に載せる
        container.add_item(
            discord.ui.TextDisplay(f"```\n{load_error_text}\n```")
        )
        # ビューにコンテナを載せる
        self.add_item(container)


class MusicControllerView(discord.ui.LayoutView):
    def __init__(
        self,
        cog: MusicCog,
        guild_id: int,
        finished_message: Optional[str] = None,
    ):
        # タイムアウトなしで初期化する
        super().__init__(timeout=None)
        # 親のMusicCogインスタンスを保持する
        self.cog = cog
        # 対象のギルドIDを保持する
        self.guild_id = guild_id
        # 停止/終了時に表示するカスタム文言（無ければデフォルト文）
        self.finished_message = finished_message
        # UI（V2コンポーネント）の構築処理を実行する
        self.rebuild_ui()

    def rebuild_ui(self):
        # 既存のビューアイテムをすべてクリアする
        self.clear_items()

        # ギルドの再生状態オブジェクトを取得する
        state = self.cog._get_guild_state(self.guild_id)
        # 再生状態、または再生中のトラックが存在しないか判定する
        if not state or not state.current_track:
            # 停止確認フラグをクリアする
            if state:
                # 確認ダイアログ状態を解除する
                state.confirming_stop = False
            # グレーのアクセントカラーでコンテナを生成する
            container = discord.ui.Container(accent_color=discord.Color.light_grey())
            # 呼び出し元指定の終了文言があれば使い、無ければ停止用のデフォルト文を使う
            stopped_text = self.finished_message or (
                "⏹️ **Playback Stopped**\n"
                "Playback was stopped or the queue has finished."
            )
            # /play が URL だった場合は見出しの直下に履歴 URL を差し込む
            if state and state.last_history_url:
                # サムネなし・テキストのみで履歴を載せる
                stopped_text = self.cog._inject_history_url(
                    stopped_text,
                    state.last_history_url,
                )
            # Section は accessory 必須のため、停止メッセージは TextDisplay のみ使う
            container.add_item(discord.ui.TextDisplay(stopped_text))

            # 無効化されたボタンを配置するアクション行を作成する
            action_row = discord.ui.ActionRow()
            # 一時停止ボタンを無効状態で追加する
            action_row.add_item(discord.ui.Button(label="⏸️ Pause", style=discord.ButtonStyle.secondary, disabled=True))
            # スキップボタンを無効状態で追加する
            action_row.add_item(discord.ui.Button(label="⏭️ Skip", style=discord.ButtonStyle.secondary, disabled=True))
            # 停止ボタンを無効状態で追加する
            action_row.add_item(discord.ui.Button(label="⏹️ Stop", style=discord.ButtonStyle.secondary, disabled=True))
            # 曲ループボタンを無効状態で追加する
            action_row.add_item(discord.ui.Button(label="🔂 Loop", style=discord.ButtonStyle.secondary, disabled=True))
            # キューループボタンを無効状態で追加する
            action_row.add_item(discord.ui.Button(label="🔁 QLoop", style=discord.ButtonStyle.secondary, disabled=True))
            # コンテナにアクション行を追加する
            container.add_item(action_row)

            # ビュー自体にコンテナを追加して完了する
            self.add_item(container)
            # 処理を終了する
            return

        # Stop 確認ダイアログ表示中なら確認専用UIを組み立てる
        if state.confirming_stop:
            # 確認UIを構築して終了する
            self._build_stop_confirm_ui(state)
            # 通常UIは組まない
            return

        # 再生中のトラック情報を取得する
        track = state.current_track
        # 一時停止状態であるか取得する
        is_paused = state.is_paused

        # 再起アイコンと再生ステータスの文言を一時停止状態に合わせて決定する
        status_icon = "⏸️" if is_paused else "▶️"
        # ステータス文字列を設定する
        status_text = "Paused" if is_paused else "Playing"

        # 水色のアクセント色でV2コンテナを初期化する
        container = discord.ui.Container(accent_color=discord.Color.from_rgb(79, 194, 255))

        # タイトル本文を構築する（title が None でも落ちないようにする）
        safe_title = track.title or "Unknown title"
        # 曲URLが無い場合はリンクにしない
        if track.url:
            # リンク付きタイトルにする（見出しで強調するため太字は付けない）
            title_line = f"[{safe_title}]({track.url})"
        else:
            # プレーンタイトルにする
            title_line = safe_title

        # チャンネル名（アップローダー）のデフォルトフォールバックを設定する
        uploader_val = track.uploader if track.uploader else "Unknown"
        # チャンネルURLがあれば Markdown リンクにする
        if track.uploader_url and uploader_val != "Unknown":
            # クリック可能なチャンネル名にする
            uploader_display = f"[{uploader_val}]({track.uploader_url})"
        else:
            # URL が無ければプレーンテキストのまま使う
            uploader_display = uploader_val

        # ステータスは小さめ、曲名は ##（# より一段小さく）、直後にチャンネル名
        title_text = (
            f"### {status_icon} Now {status_text}\n"
            f"## {title_line}\n"
            f"{uploader_display}"
        )
        # サムネイルがあるときだけ Section（accessory 必須）を使い、無いときは TextDisplay
        if track.thumbnail and track.thumbnail.strip() and track.thumbnail != "None":
            # 右上サムネイル付きセクションを追加する
            container.add_item(
                discord.ui.Section(
                    discord.ui.TextDisplay(title_text),
                    accessory=discord.ui.Thumbnail(track.thumbnail),
                )
            )
        else:
            # サムネイル無しはテキストのみ追加する
            container.add_item(discord.ui.TextDisplay(title_text))

        # リクエストユーザーのメンション文字列を設定する
        requester_mention = f"<@{track.requester_id}>" if track.requester_id else "Unknown"
        # 残りのキューの数を取得する
        remaining = state.queue.qsize()
        # ループモード表示用の短いラベルを決める
        loop_label = state.loop_mode.name.lower()

        # 現在の再生位置（秒）を取得する
        current_pos = state.get_current_position()
        # 進行状況バー（テキストアート）を生成する
        progress_bar = self.cog._create_progress_bar(current_pos, track.duration)
        # 現在時間 / 総時間を同じ行に載せ、インラインコードで表示する
        # 例: `━━━━━━━━━━○───────────────── 11:11 / 33:33`
        progress_line = (
            f"{progress_bar} "
            f"{format_duration(current_pos)} / {format_duration(track.duration)}"
        )
        # Progress を単一バッククォートの1行でまとめる
        container.add_item(
            discord.ui.TextDisplay(f"`{progress_line}`")
        )

        # Progress とメタ情報の間に区切り線を入れる
        container.add_item(discord.ui.Separator())

        # Requested By / Loop / Queue は Progress の下にまとめる
        info_text = (
            f"**Requested By:** {requester_mention}\n"
            f"**Loop:** `{loop_label}`  |  **Queue:** {remaining} songs"
        )
        # メタデータを TextDisplay で追加する
        container.add_item(discord.ui.TextDisplay(info_text))

        # 一時停止/再生ボタンを初期化する
        self.pause_resume_btn = discord.ui.Button(
            # 一時停止中なら緑色、再生中ならグレーでボタンのカラーを設定する
            style=discord.ButtonStyle.success if is_paused else discord.ButtonStyle.secondary,
            # 一時停止中なら「再開」、再生中なら「一時停止」でラベルを設定する
            label="▶️ Resume" if is_paused else "⏸️ Pause",
            # カスタムIDを設定する
            custom_id=f"music_pause_resume_{self.guild_id}"
        )
        # コールバックメソッドを紐付ける
        self.pause_resume_btn.callback = self.pause_resume_callback

        # スキップボタンをプライマリカラーで初期化する
        self.skip_btn = discord.ui.Button(
            style=discord.ButtonStyle.primary,
            label="⏭️ Skip",
            custom_id=f"music_skip_{self.guild_id}"
        )
        # コールバックメソッドを紐付ける
        self.skip_btn.callback = self.skip_callback

        # 停止ボタンをレッドカラーで初期化する
        self.stop_btn = discord.ui.Button(
            style=discord.ButtonStyle.danger,
            label="⏹️ Stop",
            custom_id=f"music_stop_{self.guild_id}"
        )
        # コールバックメソッドを紐付ける
        self.stop_btn.callback = self.stop_callback

        # 曲単体ループ（ONE）ボタンを初期化する
        loop_one_active = state.loop_mode == LoopMode.ONE
        # 有効時は緑、無効時はグレーにする
        self.loop_btn = discord.ui.Button(
            style=discord.ButtonStyle.success if loop_one_active else discord.ButtonStyle.secondary,
            label="🔂 Loop",
            custom_id=f"music_loop_one_{self.guild_id}"
        )
        # コールバックを紐付ける
        self.loop_btn.callback = self.loop_one_callback

        # キュー全体ループ（ALL）ボタンを初期化する
        loop_all_active = state.loop_mode == LoopMode.ALL
        # 有効時は緑、無効時はグレーにする
        self.queue_loop_btn = discord.ui.Button(
            style=discord.ButtonStyle.success if loop_all_active else discord.ButtonStyle.secondary,
            label="🔁 QLoop",
            custom_id=f"music_loop_all_{self.guild_id}"
        )
        # コールバックを紐付ける
        self.queue_loop_btn.callback = self.loop_all_callback

        # 再生コントロール用アクション行を作成する（最大5ボタン）
        action_row = discord.ui.ActionRow()
        # Pause/Resume を追加する
        action_row.add_item(self.pause_resume_btn)
        # Skip を追加する
        action_row.add_item(self.skip_btn)
        # Stop を追加する
        action_row.add_item(self.stop_btn)
        # Loop (ONE) を追加する
        action_row.add_item(self.loop_btn)
        # QueueLoop (ALL) を追加する
        action_row.add_item(self.queue_loop_btn)
        # コンテナにコントロール行を追加する
        container.add_item(action_row)

        # 控えめな寄付リンク（enabled 時のみ）を別行に載せる
        donation_btn = make_subtle_link_button(donation_from_bot(self.cog.bot))
        # 寄付ボタンが有効な場合のみ行を追加する
        if donation_btn is not None:
            # 寄付専用のアクション行を作る
            donation_row = discord.ui.ActionRow()
            # 寄付ボタンを追加する
            donation_row.add_item(donation_btn)
            # コンテナに寄付行を追加する
            container.add_item(donation_row)

        # キューに次曲があるときだけ Queue 一覧を出す（単発再生中は非表示）
        if not state.queue.empty():
            # コントロールとキュー一覧の間に区切り線を入れる
            container.add_item(discord.ui.Separator())

            # キュー一覧テキストとページ情報を取得する
            queue_text, page, total_pages = self.cog._build_queue_display_text(state)
            # キュー一覧を TextDisplay で追加する
            container.add_item(discord.ui.TextDisplay(queue_text))

            # 複数ページあるときだけページングボタンを付ける
            if total_pages > 1:
                # ページング用アクション行を作成する
                nav_row = discord.ui.ActionRow()
                # 先頭ページボタンを作る
                first_btn = discord.ui.Button(
                    style=discord.ButtonStyle.primary,
                    label="⏪",
                    custom_id=f"music_q_first_{self.guild_id}",
                    disabled=(page <= 0),
                )
                # コールバックを紐付ける
                first_btn.callback = self.queue_first_callback
                # 行に追加する
                nav_row.add_item(first_btn)

                # 前ページボタンを作る
                prev_btn = discord.ui.Button(
                    style=discord.ButtonStyle.primary,
                    label="◀️",
                    custom_id=f"music_q_prev_{self.guild_id}",
                    disabled=(page <= 0),
                )
                # コールバックを紐付ける
                prev_btn.callback = self.queue_prev_callback
                # 行に追加する
                nav_row.add_item(prev_btn)

                # 現在ページ表示（押下不可）を作る
                page_btn = discord.ui.Button(
                    style=discord.ButtonStyle.secondary,
                    label=f"{page + 1}/{total_pages}",
                    custom_id=f"music_q_page_{self.guild_id}",
                    disabled=True,
                )
                # 行に追加する
                nav_row.add_item(page_btn)

                # 次ページボタンを作る
                next_btn = discord.ui.Button(
                    style=discord.ButtonStyle.primary,
                    label="▶️",
                    custom_id=f"music_q_next_{self.guild_id}",
                    disabled=(page >= total_pages - 1),
                )
                # コールバックを紐付ける
                next_btn.callback = self.queue_next_callback
                # 行に追加する
                nav_row.add_item(next_btn)

                # 末尾ページボタンを作る
                last_btn = discord.ui.Button(
                    style=discord.ButtonStyle.primary,
                    label="⏩",
                    custom_id=f"music_q_last_{self.guild_id}",
                    disabled=(page >= total_pages - 1),
                )
                # コールバックを紐付ける
                last_btn.callback = self.queue_last_callback
                # 行に追加する
                nav_row.add_item(last_btn)

                # コンテナにページング行を追加する
                container.add_item(nav_row)

        # ロード失敗バナーがあればキューの下（最下部）にコードブロックで出す
        self._append_load_error_banner(container, state)

        # ビューに構築したコンテナをアタッチする
        self.add_item(container)

    def _append_load_error_banner(self, container: discord.ui.Container, state: Optional[GuildState]):
        """NO audio 等のロード失敗を Components V2 最下部に英語コードブロックで付ける。"""
        # 状態またはバナー文字列が無ければ何もしない
        if not state or not state.ui_load_error:
            # 早期リターン
            return
        # 区切り線を入れてバナーを目立たせる
        container.add_item(discord.ui.Separator())
        # Discord コードブロックとして英語メッセージを載せる
        container.add_item(
            discord.ui.TextDisplay(f"```\n{state.ui_load_error}\n```")
        )
        # 一度表示したことを記録し、次曲開始で消せるようにする
        state.ui_load_error_seen = True

    def _build_stop_confirm_ui(self, state: GuildState):
        """Stop 確認用の Confirm / Cancel UI を組み立てる。"""
        # 警告色のコンテナを初期化する
        container = discord.ui.Container(accent_color=discord.Color.orange())
        # 確認メッセージ本文を構築する
        confirm_text = (
            "### ⏹️ Stop Playback?\n"
            "再生を停止し、キューをすべてクリアします。\n"
            "Stop playback and clear the entire queue."
        )
        # TextDisplay で確認文を載せる
        container.add_item(discord.ui.TextDisplay(confirm_text))

        # Confirm / Cancel 用アクション行を作る
        row = discord.ui.ActionRow()
        # 確定ボタンを作る
        confirm_btn = discord.ui.Button(
            style=discord.ButtonStyle.danger,
            label="✅ Confirm",
            custom_id=f"music_stop_confirm_{self.guild_id}",
        )
        # コールバックを紐付ける
        confirm_btn.callback = self.stop_confirm_callback
        # 行に追加する
        row.add_item(confirm_btn)

        # キャンセルボタンを作る
        cancel_btn = discord.ui.Button(
            style=discord.ButtonStyle.secondary,
            label="❌ Cancel",
            custom_id=f"music_stop_cancel_{self.guild_id}",
        )
        # コールバックを紐付ける
        cancel_btn.callback = self.stop_cancel_callback
        # 行に追加する
        row.add_item(cancel_btn)

        # コンテナに確認行を追加する
        container.add_item(row)
        # ビューにコンテナをアタッチする
        self.add_item(container)

    async def _edit_after_interaction(self, interaction: discord.Interaction, state: GuildState):
        """defer 後に LayoutView を再編集する共通処理。"""
        try:
            # コンポーネント用トークンで元メッセージを編集する
            await interaction.edit_original_response(embed=None, view=self)
        except Exception:
            # フォールバック: チャンネル経由 Message へ変換して編集する
            durable = await self.cog._to_durable_message(interaction.message)
            # 変換できた場合のみ編集する
            if durable is not None:
                # 通常 Message として編集する
                await durable.edit(embed=None, view=self)
                # 最新の参照を状態へ保存する
                state.last_now_playing_message = durable

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        # ギルドの再生状態オブジェクトを取得する
        state = self.cog._get_guild_state(self.guild_id)
        # 再生状態、またはボイス接続が存在しないか判定する
        if not state or not state.voice_client:
            # エフェメラルエラーを送信する
            await interaction.response.send_message("The bot is not in a voice channel.", ephemeral=True)
            # チェック失敗
            return False

        # 操作を実行したユーザーのボイス接続状態を取得する
        user_voice = interaction.user.voice
        # ユーザーがボイスチャンネルに入っていない、またはボットと異なるチャンネルか判定する
        if not user_voice or not user_voice.channel or user_voice.channel != state.voice_client.channel:
            # エフェメラルエラーを送信する
            await interaction.response.send_message("You must be in the same voice channel as the bot to use the controls.", ephemeral=True)
            # チェック失敗
            return False
        # チェックパス
        return True

    async def pause_resume_callback(self, interaction: discord.Interaction):
        # インタラクションへの遅延応答を開始する
        await interaction.response.defer()
        # ギルドの再生状態オブジェクトを取得する
        state = self.cog._get_guild_state(self.guild_id)
        # オブジェクトが存在しない場合は終了する
        if not state:
            # 処理終了
            return

        # 現在一時停止中であるか判定する
        if state.is_paused:
            # ボイス接続が存在するか判定する
            if state.voice_client:
                # 音声再生を再開する
                state.voice_client.resume()
            # 一時停止フラグをFalseに設定する
            state.is_paused = False
            # 一時停止開始のタイムスタンプが存在するか判定する
            if state.paused_at and state.playback_start_time:
                # 一時停止されていた実経過時間を算出する
                pause_duration = time.time() - state.paused_at
                # 総再生開始時刻に一時停止時間を加算して進行位置を補正する
                state.playback_start_time += pause_duration
            # 一時停止開始時刻を初期化する
            state.paused_at = None
            # 再開ログを記録する
            logger.info(f"Guild {self.guild_id}: playback resumed via UI button")
        else:
            # ボイス接続が存在するか判定する
            if state.voice_client:
                # 音声再生を一時停止する
                state.voice_client.pause()
            # 一時停止フラグをTrueに設定する
            state.is_paused = True
            # 現在の時刻を一時停止開始時刻として記録する
            state.paused_at = time.time()
            # 一時停止ログを記録する
            logger.info(f"Guild {self.guild_id}: playback paused via UI button")

        # 変更された再生状態に基づいてUIを再構築する
        self.rebuild_ui()
        # メッセージを編集して反映する
        await self._edit_after_interaction(interaction, state)

    async def skip_callback(self, interaction: discord.Interaction):
        # インタラクションへの遅延応答を開始する
        await interaction.response.defer()
        # ギルドの再生状態オブジェクトを取得する
        state = self.cog._get_guild_state(self.guild_id)
        # 再生状態、または再生中のトラックが存在しない場合は終了する
        if not state or not state.current_track:
            # 処理終了
            return

        # スキップ開始をログに記録する
        logger.info(f"Guild {self.guild_id}: skipping song via UI button")
        # オーディオミキサーが存在するか判定する
        if state.mixer:
            # ミキサーから対象の音源を削除してスキップをトリガーする
            await state.mixer.remove_source('music')
        # ミキサーがなく、ボイスクライアントが直接再生中であるか判定する
        elif state.voice_client and state.voice_client.is_playing():
            # 再生を停止してスキップをトリガーする
            state.voice_client.stop()

    async def stop_callback(self, interaction: discord.Interaction):
        # インタラクションへの遅延応答を開始する
        await interaction.response.defer()
        # ギルドの再生状態オブジェクトを取得する
        state = self.cog._get_guild_state(self.guild_id)
        # オブジェクトが存在しない場合は終了する
        if not state:
            # 処理終了
            return

        # Stop 確認ダイアログを有効にする
        state.confirming_stop = True
        # 確認UIに切り替える
        self.rebuild_ui()
        # メッセージを編集して確認UIを出す
        await self._edit_after_interaction(interaction, state)

    async def stop_confirm_callback(self, interaction: discord.Interaction):
        # インタラクションへの遅延応答を開始する
        await interaction.response.defer()
        # ギルドの再生状態オブジェクトを取得する
        state = self.cog._get_guild_state(self.guild_id)
        # オブジェクトが存在しない場合は終了する
        if not state:
            # 処理終了
            return

        # 確認フラグを解除する
        state.confirming_stop = False
        # 停止処理の開始をログに記録する
        logger.info(f"Guild {self.guild_id}: stopping playback via UI confirm")
        # ループモードをOFFに設定する
        state.loop_mode = LoopMode.OFF
        # キューの内容をすべて消去する
        await state.clear_queue()
        # キューページをリセットする
        state.queue_page = 0
        # ロード失敗バナーも消す
        state.ui_load_error = None
        # 表示済みフラグも戻す
        state.ui_load_error_seen = False
        # オーディオミキサーが存在するか判定する
        if state.mixer:
            # ミキサーを完全に停止する
            state.mixer.stop()
            # ミキサーオブジェクトを破棄する
            state.mixer = None
        # ボイスクライアントが直接再生中であるか判定する
        if state.voice_client and state.voice_client.is_playing():
            # 再生を停止する
            state.voice_client.stop()
        # 再生中フラグを初期化する
        state.is_playing = False
        # 一時停止フラグを初期化する
        state.is_paused = False
        # 停止前に URL 再生履歴を残す
        self.cog._remember_play_history_url(state, state.current_track)
        # 再生中トラック情報を初期化する
        state.current_track = None
        # 再生時間計測情報を初期化する
        state.reset_playback_tracking()
        # UI 停止時はプログレスバー更新も止める
        state.stop_progress_updater()

        # 停止した状態に基づいてUIを再構築する
        self.rebuild_ui()
        # メッセージを編集して停止表示にする
        await self._edit_after_interaction(interaction, state)

        # 直前の Now Playing メッセージへの参照が存在するか判定する
        if state.last_now_playing_message:
            # 参照を初期化する
            state.last_now_playing_message = None

    async def stop_cancel_callback(self, interaction: discord.Interaction):
        # インタラクションへの遅延応答を開始する
        await interaction.response.defer()
        # ギルドの再生状態オブジェクトを取得する
        state = self.cog._get_guild_state(self.guild_id)
        # オブジェクトが存在しない場合は終了する
        if not state:
            # 処理終了
            return

        # 確認フラグを解除する
        state.confirming_stop = False
        # 通常の再生UIへ戻す
        self.rebuild_ui()
        # メッセージを編集して通常UIに戻す
        await self._edit_after_interaction(interaction, state)

    async def loop_one_callback(self, interaction: discord.Interaction):
        # インタラクションへの遅延応答を開始する
        await interaction.response.defer()
        # ギルドの再生状態オブジェクトを取得する
        state = self.cog._get_guild_state(self.guild_id)
        # オブジェクトが存在しない場合は終了する
        if not state:
            # 処理終了
            return

        # 既に ONE なら OFF、それ以外なら ONE に切り替える
        if state.loop_mode == LoopMode.ONE:
            # ループを解除する
            state.loop_mode = LoopMode.OFF
        else:
            # 曲単体ループを有効にする
            state.loop_mode = LoopMode.ONE
        # 最終操作時刻を更新する
        state.update_activity()
        # 変更ログを残す
        logger.info(f"Guild {self.guild_id}: loop mode set to {state.loop_mode.name} via Loop button")
        # UIを再構築する
        self.rebuild_ui()
        # メッセージを編集する
        await self._edit_after_interaction(interaction, state)

    async def loop_all_callback(self, interaction: discord.Interaction):
        # インタラクションへの遅延応答を開始する
        await interaction.response.defer()
        # ギルドの再生状態オブジェクトを取得する
        state = self.cog._get_guild_state(self.guild_id)
        # オブジェクトが存在しない場合は終了する
        if not state:
            # 処理終了
            return

        # 既に ALL なら OFF、それ以外なら ALL に切り替える
        if state.loop_mode == LoopMode.ALL:
            # キューループを解除する
            state.loop_mode = LoopMode.OFF
        else:
            # キュー全体ループを有効にする
            state.loop_mode = LoopMode.ALL
        # 最終操作時刻を更新する
        state.update_activity()
        # 変更ログを残す
        logger.info(f"Guild {self.guild_id}: loop mode set to {state.loop_mode.name} via QLoop button")
        # UIを再構築する
        self.rebuild_ui()
        # メッセージを編集する
        await self._edit_after_interaction(interaction, state)

    async def queue_first_callback(self, interaction: discord.Interaction):
        # 先頭ページへ移動する
        await self._change_queue_page(interaction, target_page=0)

    async def queue_prev_callback(self, interaction: discord.Interaction):
        # ギルド状態を取得する
        state = self.cog._get_guild_state(self.guild_id)
        # 状態が無ければ終了する
        if not state:
            # interaction を消費する
            await interaction.response.defer()
            # 処理終了
            return
        # 前ページ番号を計算する
        await self._change_queue_page(interaction, target_page=max(0, state.queue_page - 1))

    async def queue_next_callback(self, interaction: discord.Interaction):
        # ギルド状態を取得する
        state = self.cog._get_guild_state(self.guild_id)
        # 状態が無ければ終了する
        if not state:
            # interaction を消費する
            await interaction.response.defer()
            # 処理終了
            return
        # 総ページ数を把握するため表示ヘルパーを呼ぶ
        _text, page, total_pages = self.cog._build_queue_display_text(state)
        # 次ページ番号を計算する
        await self._change_queue_page(interaction, target_page=min(total_pages - 1, page + 1))

    async def queue_last_callback(self, interaction: discord.Interaction):
        # ギルド状態を取得する
        state = self.cog._get_guild_state(self.guild_id)
        # 状態が無ければ終了する
        if not state:
            # interaction を消費する
            await interaction.response.defer()
            # 処理終了
            return
        # 総ページ数を把握する
        _text, _page, total_pages = self.cog._build_queue_display_text(state)
        # 末尾ページへ移動する
        await self._change_queue_page(interaction, target_page=total_pages - 1)

    async def _change_queue_page(self, interaction: discord.Interaction, target_page: int):
        # インタラクションへの遅延応答を開始する
        await interaction.response.defer()
        # ギルドの再生状態オブジェクトを取得する
        state = self.cog._get_guild_state(self.guild_id)
        # オブジェクトが存在しない場合は終了する
        if not state:
            # 処理終了
            return
        # ページ番号を書き換える
        state.queue_page = max(0, target_page)
        # UIを再構築する
        self.rebuild_ui()
        # メッセージを編集する
        await self._edit_after_interaction(interaction, state)


async def setup(bot: commands.Bot):
    try:
        await bot.add_cog(MusicCog(bot))
        logger.info("MusicCog successfully loaded")
    except Exception as e:
        logger.error(f"MusicCogのセットアップ中にエラー: {e}", exc_info=True)
        raise
