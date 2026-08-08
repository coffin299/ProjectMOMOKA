"""音楽再生のギルド単位状態を管理する。"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
from enum import Enum, auto
from typing import Optional

import discord
from discord.ext import commands

from MOMOKA.music.plugins.audio_mixer import AudioMixer
from MOMOKA.music.plugins.ytdlp_wrapper import Track

# 音楽状態のクリーンアップ失敗を記録する。
logger = logging.getLogger(__name__)


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
        # /play の同時到着時に初回再生を一度だけ起動するためのギルド単位ロック
        self.play_lock = asyncio.Lock()
        # 意図的な停止中に古いミキサーの終了コールバックを無視するフラグ
        self.stopping: bool = False
        self.mixer: Optional[AudioMixer] = None
        self._playing_next: bool = False  # 次の曲を再生中かどうかのフラグ
        # music ソース削除が on_source_removed 経由で処理中か（mixer_finished との二重遷移防止）
        self._music_source_removed: bool = False
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
        # ユーザーが VC ステータスを手動編集したら以降 Bot は書き換えない
        self.vc_status_locked: bool = False
        # Bot が最後に設定した VC ステータス文字列（未設定時は None）
        self.vc_status_last_bot: Optional[str] = None
        # Bot 自身の更新反映待ち（ゲートウェイ echo 照合用）
        self.vc_status_pending_active: bool = False
        # 反映待ち中の Bot 設定値（クリア時も None を保持）
        self.vc_status_pending: Optional[str] = None
        # 権限不足等で VC ステータス更新を諦めたギルド
        self.vc_status_permission_denied: bool = False
        # Bot が管理対象としている VC チャンネル ID
        self.vc_status_channel_id: Optional[int] = None

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


