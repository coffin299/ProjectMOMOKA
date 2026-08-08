"""音楽再生のギルド単位状態を管理する。"""

from __future__ import annotations

import asyncio
import logging
import random
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
        # キュー並び替え・取出・投入の競合を防ぐ（shuffle 差し替え事故防止）
        self.queue_lock = asyncio.Lock()
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

    def _get_music_audio_source(self):
        """ミキサー上の music ソース（MusicAudioSource）を返す。無ければ None。"""
        # ミキサーが無ければ参照できない
        if self.mixer is None:
            return None
        try:
            # スレッド安全にソース辞書を読む
            with self.mixer._thread_lock:
                source = self.mixer.sources.get("music")
        except Exception:
            return None
        # 実 PCM 秒数 API を持つソースだけ採用する
        if source is not None and hasattr(source, "get_produced_audio_seconds"):
            return source
        return None

    def get_current_position(self) -> int:
        """再生位置（秒）。実 PCM が出るまでは進めず、以降はフレーム基準を優先する。"""
        # 実音開始前（ensure_stream / prime 中）はシーク位置で止める
        if self.is_playing and self.playback_start_time is None:
            return self.seek_position

        # 音楽ソースがあればフレーム基準を使う
        music_source = self._get_music_audio_source()
        if music_source is not None:
            # まだ実オーディオ未出力ならシーク位置のまま（壁時計でバーを進めない）
            if not getattr(music_source, "_has_produced_audio", False):
                return self.seek_position
            # 実 PCM 累積秒 + シーク開始位置
            try:
                produced = float(music_source.get_produced_audio_seconds())
            except Exception:
                produced = 0.0
            return self.seek_position + int(produced)

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

        # フォールバック: ソースが取れないときだけ壁時計
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

    def _queue_deque(self):
        """asyncio.Queue 内部 deque を返す（無ければ None）。"""
        # 内部構造を取る
        return getattr(self.queue, "_queue", None)

    async def shuffle_queue(self) -> int:
        """待ちキューを in-place で並べ替え、件数を返す（Queue オブジェクトは差し替えない）。"""
        # 取出・投入と直列化する
        async with self.queue_lock:
            # 内部 deque を取得する
            raw = self._queue_deque()
            # 取れなければ何もしない
            if raw is None:
                return 0
            # 現時点の待ち曲をコピーする
            items = list(raw)
            # 0〜1 件なら並べ替え不要
            if len(items) <= 1:
                return len(items)
            # 並べ替える
            random.shuffle(items)
            # 同一 Queue の中身だけ差し替える（waiters / unfinished を壊さない）
            raw.clear()
            # 並べ替え結果を戻す
            raw.extend(items)
            # ページを先頭に戻す
            self.queue_page = 0
            # 件数を返す
            return len(items)

    async def pop_queue_track(self) -> Optional[Track]:
        """キュー先頭を 1 曲取り出す（空なら None）。"""
        # 並び替えと競合しないようロックする
        async with self.queue_lock:
            try:
                # 非ブロッキングで取る（差し替え待ちでハングしない）
                track = self.queue.get_nowait()
            except asyncio.QueueEmpty:
                return None
            try:
                # unfinished_tasks を整合させる
                self.queue.task_done()
            except ValueError:
                # 過剰 task_done は無視する
                pass
            return track

    async def put_queue_track(self, track: Track) -> None:
        """キュー末尾へ 1 曲入れる。"""
        # 並び替えと競合しないようロックする
        async with self.queue_lock:
            # 末尾へ投入する
            await self.queue.put(track)

    async def remove_queue_index(self, index: int) -> Optional[Track]:
        """0 始まり index の曲をキューから外して返す。"""
        # 並び替えと競合しないようロックする
        async with self.queue_lock:
            # 内部 deque を取得する
            raw = self._queue_deque()
            # 取れなければ失敗
            if raw is None:
                return None
            # 範囲外は失敗
            if not (0 <= index < len(raw)):
                return None
            # 指定位置を取り除く
            removed = raw[index]
            del raw[index]
            try:
                # get 相当として unfinished を 1 減らす
                self.queue.task_done()
            except ValueError:
                # 整合不能時は無視する
                pass
            # ページが溢れたら戻す
            max_page = max(0, (len(raw) - 1) // 5) if raw else 0
            if self.queue_page > max_page:
                self.queue_page = max_page
            return removed

    async def clear_queue(self):
        # 並び替え・取出と直列化する
        async with self.queue_lock:
            # 残件をすべて捨てる
            while True:
                try:
                    # 非ブロッキングで取る
                    self.queue.get_nowait()
                    # unfinished を減らす
                    self.queue.task_done()
                except asyncio.QueueEmpty:
                    # 空になったら終了
                    break
                except ValueError:
                    # task_done 過剰は握りつぶして継続する
                    continue
            # 空の新 Queue に戻す（ここは待機者が居ない前提の掃除）
            self.queue = asyncio.Queue()
            # ページも先頭へ
            self.queue_page = 0

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


