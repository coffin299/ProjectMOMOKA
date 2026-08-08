import asyncio
import collections
import gc
import io
import itertools
import logging
import math
import re
import subprocess
import time
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import discord
from discord import app_commands
from discord.ext import commands, tasks

from MOMOKA.bots.registry import registry
from MOMOKA.utilities.donation import donation_from_bot, make_subtle_link_button
from MOMOKA.music.music_helpers import format_duration

# 依存モジュールの読み込み失敗も含めて音楽機能のログへ記録する。
logger = logging.getLogger(__name__)


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
    from MOMOKA.music.guild_state import GuildState, LoopMode
    from MOMOKA.music.music_views import (
        LoadErrorLayoutView,
        MusicControllerView,
        QueueAddedLayoutView,
    )
except ImportError as e:
    logger.error(
                "[CRITICAL] MusicCog: 必須コンポーネントのインポートに失敗しました。エラー: %s",
                e,
            )
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
    GuildState = None
    LoopMode = None
    LoadErrorLayoutView = None
    MusicControllerView = None
    QueueAddedLayoutView = None

# Now Playing プログレスバーの更新間隔（秒）。Discord rate limit を考慮して 10 秒にする
PROGRESS_UPDATE_INTERVAL = 10
# Now Playing パネル下部に表示するキューの1ページあたり曲数
QUEUE_PAGE_SIZE = 5
# Components V2 上で表示するプログレスバーの長さ（インラインコード1行向け）
PROGRESS_BAR_LENGTH = 28
# Discord VC ステータス文字数上限
VC_STATUS_MAX_LEN = 500


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


class MusicCog(commands.Cog, name="music_cog"):
    # get_cog / GUI 合算用の正式名（変更時は呼び出し側も合わせる）
    COG_NAME = "music_cog"

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        if not all((
            Track,
            extract_audio_data,
            ensure_stream,
            MusicCogExceptionHandler,
            AudioMixer,
            MusicAudioSource,
            GuildState,
            LoopMode,
            LoadErrorLayoutView,
            MusicControllerView,
            QueueAddedLayoutView,
        )):
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
        self.max_guilds = self.music_config.get('max_guilds', 50)
        self.inactive_timeout_minutes = self.music_config.get('inactive_timeout_minutes', 30)
        self.global_connection_lock = asyncio.Lock()
        self.cleanup_task = None
        # グレースフル再起動後の VC 復元を1プロセスにつき1回だけ実行するフラグ
        self._vc_sessions_restored = False
        # 復元中に pause へ戻すギルド（guild_id -> True）
        self._pending_restore_pause: Dict[int, bool] = {}
        # VC セッション永続化ストア
        from MOMOKA.storage.restart_state import get_vc_playback_session_store
        # 共有ストアを保持する
        self._vc_session_store = get_vc_playback_session_store()

    async def cog_load(self):
        # 起動時に yt-dlp / プロジェクト cache を掃除して古いストリーム情報を残さない
        if clear_ytdlp_cache is not None:
            try:
                # キャッシュ削除を実行する
                clear_ytdlp_cache()
            except Exception as e:
                # キャッシュ削除失敗でも Cog ロードは続行する
                logger.warning(f"yt-dlp cache cleanup failed (non-fatal): {e}")

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
        # bot.config（configs/ マージ結果）のみ使う。ルート config.yaml は非対応。
        if hasattr(self.bot, 'config') and self.bot.config:
            return self.bot.config
        # 注入が無い場合は空 dict（起動時 loader が前提）
        logger.warning(
            "bot.config is missing; music settings fall back to empty. "
            "Ensure configs/*_config.yaml are loaded at startup."
        )
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
        from MOMOKA.utilities.restart_notice import restart_notice_from_config
        # bot.config の support 節から文言を組み立てる
        restart_notice = restart_notice_from_config(
            self.bot.config if hasattr(self.bot, "config") else None
        )
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
            state = self.get_existing_guild_state(guild_id)
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
                    finished_message=restart_notice,
                )
            except Exception as e:
                # 1ギルドの失敗で他ギルド通知を止めない
                logger.warning(
                    "Guild %s: failed to notify admin restart on Now Playing: %s",
                    guild_id,
                    e,
                )

    def _bot_id_key(self) -> str:
        """永続化キー用の bot_id を返す。"""
        # Momoka.bot_id があればそれを使う
        return str(getattr(self.bot, "bot_id", None) or "unknown")

    def _is_companion_bot(self) -> bool:
        """companion（ARONA）ロールかどうか。"""
        # bot_role 未設定時は primary 扱い
        return getattr(self.bot, "bot_role", "primary") == "companion"

    def _partner_display_name(self) -> str:
        """相方 Bot の表示名を返す。"""
        # 自 bot_id から相方 id を解決する
        partner_bot_id = registry.partner_id(self._bot_id_key())
        # レジストリの表示名を返す
        return registry.display_name(partner_bot_id)

    def _partner_blocks_channel(self, guild_id: int, channel_id: int) -> bool:
        """相方が同一 VC に接続中なら True（新規入室拒否用）。"""
        # レジストリの実接続判定を使う
        return registry.partner_in_voice_channel(
            self._bot_id_key(),
            guild_id,
            channel_id,
        )

    async def _leave_if_partner_coexists(
        self,
        guild_id: int,
        channel: Optional[discord.abc.Connectable],
        *,
        notify: bool = True,
    ) -> bool:
        """Companion のみ: Primary と同 VC なら自己切断する。切断したら True。"""
        # Primary は同居理由では退出しない
        if not self._is_companion_bot():
            # 対象外
            return False
        # チャンネルが無ければ判定不能
        if channel is None or not hasattr(channel, "id"):
            # 切断しない
            return False
        # Primary が同 channel にいなければ何もしない
        if not self._partner_blocks_channel(guild_id, int(channel.id)):
            # 同居なし
            return False
        # 通知用に状態を先に読む
        state = self.get_existing_guild_state(guild_id)
        # テキスト案内が必要なら送る
        if notify and state and state.last_text_channel_id:
            # Companion 退出メッセージをバックグラウンド送信する
            await self._send_background_message(
                state.last_text_channel_id,
                "partner_coexist_left",
                partner_name=self._partner_display_name(),
            )
        # 切断理由をログする
        logger.info(
            "Guild %s: companion leaving VC %s due to primary coexistence",
            guild_id,
            getattr(channel, "name", channel.id),
        )
        # Music 状態ごと切断する
        await self._cleanup_guild_state(guild_id)
        # 切断した
        return True

    async def _evacuate_companion_coexistence_all(self) -> None:
        """Companion の全接続を点検し、Primary 同居分だけ退出する。"""
        # Primary では sweep 不要
        if not self._is_companion_bot():
            # 早期リターン
            return
        # 接続中ギルドをスナップショットする
        for guild_id, state in list(self.guild_states.items()):
            # VoiceClient が無ければスキップ
            if not state.voice_client or not state.voice_client.is_connected():
                # 次へ
                continue
            # 現在のチャンネルを取る
            channel = state.voice_client.channel
            # 同居なら自己切断する
            await self._leave_if_partner_coexists(guild_id, channel, notify=True)

    @staticmethod
    def _track_to_persist_dict(track: Track) -> dict:
        """Track から永続化可能なフィールドだけを抜き出す。"""
        # stream_url / http_headers は揮発なので保存しない
        return {
            "url": track.url,
            "title": track.title,
            "duration": int(track.duration or 0),
            "thumbnail": track.thumbnail,
            "requester_id": track.requester_id,
            "original_query": track.original_query,
            "uploader": track.uploader,
            "uploader_url": track.uploader_url,
        }

    @staticmethod
    def _track_from_persist_dict(data: dict) -> Optional[Track]:
        """永続化 dict から Track を再構築する。"""
        # url が無ければ復元不能
        if not data or not data.get("url"):
            # 失敗
            return None
        # Track を組み立てる
        return Track(
            url=str(data["url"]),
            title=str(data.get("title") or "Unknown"),
            duration=int(data.get("duration") or 0),
            thumbnail=data.get("thumbnail"),
            requester_id=data.get("requester_id"),
            original_query=data.get("original_query"),
            uploader=data.get("uploader"),
            uploader_url=data.get("uploader_url"),
        )

    def _snapshot_queue_tracks(self, state: GuildState) -> list:
        """キュー内容を永続化用リストへコピーする（キューは破壊しない）。"""
        # asyncio.Queue の内部 deque を読む（永続化直前のスナップショット用途）
        raw = getattr(state.queue, "_queue", None)
        # 内部構造が取れなければ空
        if raw is None:
            # 空リスト
            return []
        # 永続化 dict の列
        items = []
        # 各 Track を写す
        for track in list(raw):
            # Track 以外は無視
            if track is None:
                # 次へ
                continue
            # 永続化 dict を積む
            items.append(self._track_to_persist_dict(track))
        # スナップショットを返す
        return items

    async def persist_vc_sessions_for_restart(self) -> None:
        """グレースフル終了前に接続中 VC の再生状態を DB へ保存する。"""
        # bot 識別子
        bot_id = self._bot_id_key()
        # 保存件数
        saved = 0
        # 全ギルド状態を走査する
        for guild_id, state in list(self.guild_states.items()):
            # VoiceClient が接続中でなければスキップ
            if not (state.voice_client and state.voice_client.is_connected()):
                # 次へ
                continue
            # VC チャンネル ID を取る
            channel = state.voice_client.channel
            # チャンネルが無ければスキップ
            if channel is None:
                # 次へ
                continue
            # 再生中トラックもキューも無ければ単なる join 残留なので捨てる
            queue_items = self._snapshot_queue_tracks(state)
            # 現在曲の有無
            has_current = state.current_track is not None
            # どちらも無ければ保存しない
            if not has_current and not queue_items:
                # 次へ
                continue
            # 現在曲 dict
            current_dict = (
                self._track_to_persist_dict(state.current_track) if has_current else None
            )
            # ループモード名
            loop_name = (
                state.loop_mode.name
                if isinstance(state.loop_mode, LoopMode)
                else "OFF"
            )
            try:
                # DB へ UPSERT する
                self._vc_session_store.upsert(
                    {
                        "bot_id": bot_id,
                        "guild_id": guild_id,
                        "voice_channel_id": int(channel.id),
                        "text_channel_id": state.last_text_channel_id,
                        "volume": float(state.volume),
                        "loop_mode": loop_name,
                        "is_paused": bool(state.is_paused),
                        "position_sec": int(state.get_current_position() or 0),
                        "current_track_json": current_dict,
                        "queue_json": queue_items,
                    }
                )
                # 保存成功数を増やす
                saved += 1
            except Exception as e:
                # 1ギルド失敗でも他は続ける
                logger.warning(
                    "Guild %s: failed to persist VC session for restart: %s",
                    guild_id,
                    e,
                )
        # 結果をログする
        logger.info(
            "%s: persisted %s VC playback session(s) for restart",
            bot_id,
            saved,
        )

    def _delete_vc_restart_session(self, guild_id: int) -> None:
        """再生開始成功後に再起動セッション行を削除する。"""
        try:
            # bot × guild の行を消す
            self._vc_session_store.delete(self._bot_id_key(), guild_id)
        except Exception as e:
            # 削除失敗は再生自体を止めない
            logger.warning(
                "Guild %s: failed to delete VC restart session: %s",
                guild_id,
                e,
            )

    async def _connect_voice_channel(
        self,
        guild_id: int,
        channel: discord.abc.Connectable,
    ) -> Optional[discord.VoiceClient]:
        """コンテキスト無しで指定 VC へ接続する（再起動復元用）。"""
        # 状態を取得または作成する
        state = self._get_guild_state(guild_id)
        # 上限等で作成できなければ失敗
        if not state:
            # 接続不可
            return None
        # ギルドオブジェクト
        guild = self.bot.get_guild(guild_id)
        # 接続ロックで直列化する
        async with state.connection_lock:
            # 既に同じチャンネルへ接続済みならそれを返す
            if (
                state.voice_client
                and state.voice_client.is_connected()
                and state.voice_client.channel
                and state.voice_client.channel.id == channel.id
            ):
                # 既存接続
                return state.voice_client
            # 別チャンネルや切断済みなら掃除する
            if state.voice_client:
                # 切断してから入り直す
                await state.cleanup_voice_client()
                # 短い間隔を空ける
                await asyncio.sleep(0.3)
            # 相方が同一 VC にいる場合は復元接続しない
            if self._partner_blocks_channel(guild_id, int(channel.id)):
                # 拒否理由をログする
                logger.info(
                    "Guild %s: skip VC restore to %s — partner already connected",
                    guild_id,
                    getattr(channel, "name", channel.id),
                )
                # 復元失敗扱い
                return None
            try:
                # VC へ接続する
                state.voice_client = await asyncio.wait_for(
                    channel.connect(timeout=30.0, reconnect=True, self_deaf=False),
                    timeout=35.0,
                )
                # Companion は接続直後に Primary 同居を再点検する
                if await self._leave_if_partner_coexists(guild_id, channel, notify=True):
                    # 同居解消のため切断済み
                    return None
                # サーバー側スピーカーミュートを試みる
                if guild is not None:
                    # deafen を適用する
                    await self._apply_server_deafen(guild)
                # 接続成功をログする
                logger.info(
                    "Guild %s: Restored VC connection to %s",
                    guild_id,
                    getattr(channel, "name", channel.id),
                )
                # VoiceClient を返す
                return state.voice_client
            except Exception as e:
                # 接続失敗をログする
                logger.warning(
                    "Guild %s: failed to reconnect VC for restore: %s",
                    guild_id,
                    e,
                )
                # 参照をクリアする
                state.voice_client = None
                # 失敗
                return None

    async def restore_vc_sessions_after_restart(self) -> None:
        """起動後に DB 上の VC セッションを復元して再生を再開する。"""
        # 二重復元を防ぐ
        if self._vc_sessions_restored:
            # 既に実施済み
            return
        # bot 識別子
        bot_id = self._bot_id_key()
        try:
            # DB からセッション一覧を読む
            sessions = self._vc_session_store.load_for_bot(bot_id)
        except Exception as e:
            # 読込失敗ならフラグを立てず再試行可能にする
            logger.warning("%s: failed to load VC restart sessions: %s", bot_id, e)
            # 終了（_vc_sessions_restored は False のまま）
            return
        # 復元対象が無ければ完了扱い
        if not sessions:
            # 次回 on_ready での再走査を止める
            self._vc_sessions_restored = True
            # 早期リターン
            return
        # 件数をログする
        logger.info("%s: restoring %s VC playback session(s)", bot_id, len(sessions))
        # 一時失敗が残れば False のままにし再試行する
        all_resolved = True
        # セッションごとに復元する
        for session in sessions:
            # ギルド ID
            guild_id = int(session["guild_id"])
            try:
                # True=完了（成功または恒久破棄）、False=後で再試行
                resolved = await self._restore_one_vc_session(session)
                # 未解決なら全体完了にしない
                if not resolved:
                    # 再試行待ちを記録する
                    all_resolved = False
            except Exception as e:
                # 予期せぬ失敗でも行は消さず再試行可能にする
                logger.warning(
                    "Guild %s: VC session restore failed: %s",
                    guild_id,
                    e,
                )
                # 全体完了にしない
                all_resolved = False
                # テキストへ短い失敗通知を試みる
                text_id = session.get("text_channel_id")
                # チャンネルがあれば送る
                if text_id:
                    try:
                        # チャンネルを取得する
                        channel = self.bot.get_channel(int(text_id))
                        # テキストチャンネルなら通知する
                        if channel is not None and hasattr(channel, "send"):
                            # 失敗を短く伝える
                            await channel.send(
                                "⚠️ Failed to restore playback after restart.",
                                silent=True,
                            )
                    except Exception:
                        # 通知失敗は無視する
                        pass
        # すべて解決したときだけ完了フラグを立てる
        if all_resolved:
            # 再入を防ぐ
            self._vc_sessions_restored = True

    async def _restore_one_vc_session(self, session: dict) -> bool:
        """1 ギルド分の VC セッションを復元する。

        Returns:
            True: 成功または恒久的に破棄済み（再試行不要）
            False: 一時失敗のため DB 行を残して再試行する
        """
        # ギルド ID
        guild_id = int(session["guild_id"])
        # ギルドを取得する
        guild = self.bot.get_guild(guild_id)
        # キャッシュ未到達の一時欠落では行を消さない
        if guild is None:
            # 再試行待ちをログする
            logger.info(
                "Guild %s: VC restore deferred (guild not in cache yet)",
                guild_id,
            )
            # 未解決
            return False
        # VC チャンネルを取得する
        voice_channel = guild.get_channel(int(session["voice_channel_id"]))
        # チャンネル欠落も一時的なことがあるため削除しない
        if voice_channel is None or not isinstance(
            voice_channel, (discord.VoiceChannel, discord.StageChannel)
        ):
            # 再試行待ちをログする
            logger.info(
                "Guild %s: VC restore deferred (voice channel unavailable)",
                guild_id,
            )
            # 未解決
            return False
        # 状態を用意する
        state = self._get_guild_state(guild_id)
        # 上限で作れなければ後で再試行する
        if not state:
            # 再試行待ちをログする
            logger.info(
                "Guild %s: VC restore deferred (guild state unavailable)",
                guild_id,
            )
            # 未解決
            return False
        # テキストチャンネルを復元する
        if session.get("text_channel_id"):
            # last_text_channel を戻す
            state.last_text_channel_id = int(session["text_channel_id"])
        # 音量を戻す
        state.volume = float(session.get("volume") or state.volume)
        # ループモードを戻す
        loop_name = str(session.get("loop_mode") or "OFF").upper()
        try:
            # Enum へ変換する
            state.loop_mode = LoopMode[loop_name]
        except KeyError:
            # 不明値は OFF
            state.loop_mode = LoopMode.OFF
        # 接続前にキューを積まない（失敗時のメモリ汚染を防ぐ）
        vc = await self._connect_voice_channel(guild_id, voice_channel)
        # 接続失敗は一時扱い（相方ブロック・タイムアウト等）
        if vc is None:
            # 万一キューが残っていればメモリだけ掃除して DB は残す
            await state.clear_queue()
            # 現在曲参照も落とす
            state.current_track = None
            # 再生中フラグを下ろす
            state.is_playing = False
            # テキストへ通知する
            if state.last_text_channel_id:
                # 失敗通知
                await self._send_background_message(
                    state.last_text_channel_id,
                    "error_playing",
                    error="Failed to restore voice connection after restart.",
                )
            # DB 行は残して再試行する
            return False
        # 接続成功後にキューを復元する
        for item in session.get("queue") or []:
            # Track を再構築する
            track = self._track_from_persist_dict(item)
            # 成功したものだけ積む
            if track is not None:
                # キューへ入れる
                await state.put_queue_track(track)
        # 現在曲を復元する
        current = self._track_from_persist_dict(session.get("current_track") or {})
        # 現在曲もキューも無ければ恒久破棄して終了
        if current is None and state.queue.empty():
            # 壊れた／空の行を消す
            self._delete_vc_restart_session(guild_id)
            # 解決済み
            return True
        # シーク位置
        position = max(0, int(session.get("position_sec") or 0))
        # 一時停止だったかを覚える
        was_paused = bool(session.get("is_paused"))
        # 再生後に pause するフラグ
        if was_paused:
            # ギルド単位で保留する
            self._pending_restore_pause[guild_id] = True
        # 現在曲がある場合は current にセットしてから強制再生する
        if current is not None:
            # 現在曲をセットする
            state.current_track = current
            # 再生中フラグは下ろしたまま再生処理へ渡す
            state.is_playing = False
            # キューを消費せず現在曲を再開する（位置 0 でも force_current）
            await self._play_next_song(
                guild_id,
                seek_seconds=position,
                force_current=True,
            )
        else:
            # 現在曲が無くキューだけなら通常の次曲再生
            await self._play_next_song(guild_id)
        # 復元処理完了（行削除は再生開始成功側）
        return True

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
        """コマンド開始時に必要なギルド状態を取得または作成する。"""
        # 既存状態が無い場合だけ、新しい状態の確保可否を判定する
        if guild_id not in self.guild_states:
            # 保持上限に達している場合は、先に非再生状態の退避候補を探す
            if len(self.guild_states) >= self.max_guilds:
                # 最古の非再生状態を保持する変数を初期化する
                oldest_guild, oldest_time = None, datetime.now()
                # 全状態から安全に削除できる最古の状態を探す
                for gid, state in self.guild_states.items():
                    # 再生・読み込み中でなく、かつ現在の候補より古い状態だけを選ぶ
                    if (
                        not state.is_playing
                        and not state.is_loading
                        and state.last_activity < oldest_time
                    ):
                        # 削除候補のIDと最終操作時刻を更新する
                        oldest_guild, oldest_time = gid, state.last_activity
                # 削除候補がある場合は非同期クリーンアップを予約する
                if oldest_guild is not None:
                    # 状態が実際に削除されるまで新規状態を作らない
                    asyncio.create_task(self._cleanup_guild_state(oldest_guild))
                    # 削除予定のギルド名をログ用に解決する
                    guild = self.bot.get_guild(oldest_guild)
                    logger.info(
                        f"Evicting oldest inactive guild {oldest_guild} "
                        f"({guild.name if guild else ''}) before accepting a new state")
                # 削除を待たずに上限超過の状態を挿入しないため、呼び出し元へ拒否を返す
                return None
            # 上限内であるため、新しいギルド状態を登録する
            self.guild_states[guild_id] = GuildState(self.bot, guild_id, self.config)
        # コマンド操作を受けた状態の最終操作時刻を更新する
        self.guild_states[guild_id].update_activity()
        # 既存または新規状態を返す
        return self.guild_states[guild_id]

    def get_existing_guild_state(self, guild_id: int) -> Optional[GuildState]:
        """既存のギルド状態だけを返し、存在しなければ作成しない。"""
        # コールバック・クリーンアップから状態を復活させないため辞書を直接参照する
        return self.guild_states.get(guild_id)

    def get_active_vc_guild_count(self) -> int:
        """VC に接続中のギルド数を返す（GUI 稼働モニタ用）。"""
        # 接続済み voice_client を持つギルドだけを数える
        return sum(
            1
            for s in self.guild_states.values()
            if s.voice_client and s.voice_client.is_connected()
        )

    def get_active_vc_snapshots(self) -> list:
        """接続中 VC のギルド名・曲名・一時停止・キュー件数を返す。"""
        # 結果行
        rows = []
        # ギルド状態を走査する
        for guild_id, state in list(self.guild_states.items()):
            # 未接続は除外する
            if not (state.voice_client and state.voice_client.is_connected()):
                continue
            # Discord ギルドオブジェクト（名前用・メンバー一覧は触らない）
            guild = self.bot.get_guild(guild_id)
            # 曲名
            title = None
            # 再生中トラックがあればタイトルを取る
            if state.current_track is not None:
                title = getattr(state.current_track, "title", None)
            # 1 行分を積む
            rows.append(
                {
                    "guild_id": guild_id,
                    "guild_name": guild.name if guild else str(guild_id),
                    "title": title,
                    "paused": bool(state.is_paused),
                    "queue_size": state.queue.qsize(),
                }
            )
        # スナップショット一覧
        return rows

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
        # コマンド入口で確保済みの状態だけを利用し、補助処理では状態を作成しない
        state = self.get_existing_guild_state(ctx.guild.id)
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
                    # 別 VC にいる場合は切断せず、同一チャンネル参加を要求する
                    await self._send_response(
                        ctx,
                        "must_be_in_same_voice_channel",
                        ephemeral=True,
                    )
                    # 接続を維持したまま拒否する
                    return None

            for voice_client in list(self.bot.voice_clients):
                if voice_client.guild.id == ctx.guild.id and voice_client != state.voice_client:
                    try:
                        await asyncio.wait_for(voice_client.disconnect(force=True), timeout=3.0)
                    except asyncio.CancelledError:
                        # タスク停止要求は上位へ伝播して正常にキャンセルする
                        raise
                    except Exception:
                        # 残存 VoiceClient の切断失敗は新規接続を妨げない
                        pass

            if not vc and connect_if_not_in:
                # 相方が同一 VC にいる場合は新規接続を拒否する
                if self._partner_blocks_channel(ctx.guild.id, user_voice.channel.id):
                    # ユーザーへ拒否メッセージを返す
                    await self._send_response(
                        ctx,
                        "partner_already_in_voice",
                        ephemeral=True,
                        partner_name=self._partner_display_name(),
                    )
                    # 接続しない
                    return None
                try:
                    # 接続前の短い待機で前回切断との競合を避ける
                    await asyncio.sleep(0.3)
                    # 自己deafは使わず接続し、直後にサーバー側スピーカーミュートへ切り替え
                    state.voice_client = await asyncio.wait_for(
                        user_voice.channel.connect(
                            timeout=30.0, reconnect=True, self_deaf=False),
                        timeout=35.0
                    )
                    # Companion は接続直後に Primary 同居を再点検する
                    if await self._leave_if_partner_coexists(
                        ctx.guild.id,
                        user_voice.channel,
                        notify=False,
                    ):
                        # 同居解消で切断した旨をユーザーへ返す
                        await self._send_response(
                            ctx,
                            "partner_coexist_left",
                            ephemeral=True,
                            partner_name=self._partner_display_name(),
                        )
                        # 利用不可
                        return None
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
        # 終了コールバックでは削除済み状態を復活させない
        state = self.get_existing_guild_state(guild_id)
        # 意図的な停止中・次曲処理中・状態削除済みなら再生遷移を行わない
        if not state or state.stopping or state._playing_next:
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

        # 通常の music 終了は _on_music_source_removed が次曲/リトライ/NO-audio UI を担当する
        if state._music_source_removed:
            # mixer_finished 側では _playing_next を奪わず遷移しない
            logger.info(
                f"Guild {guild_id}: Mixer callback skipped next-song "
                "(owned by on_source_removed)"
            )
            # エラー通知だけは残す（ソース削除経路では error を持たないため）
            if error:
                # ギルドを解決する
                guild = self.bot.get_guild(guild_id)
                # ユーザー向けエラー文言を組み立てる
                error_message = self.exception_handler.handle_error(error, guild)
                # 通知先があれば送る
                if state.last_text_channel_id:
                    # バックグラウンドでエラーを投稿する
                    asyncio.run_coroutine_threadsafe(
                        self._send_background_message(
                            state.last_text_channel_id,
                            "error_message_wrapper",
                            error=error_message,
                        ),
                        self.bot.loop,
                    )
            # 次曲パスには進まない
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

        # 次の曲を再生（Loop ALL は put 完了を待ってから再生する）
        def play_next_and_reset_flag():
            async def _play():
                try:
                    # LoopMode.ALL なら終了曲を先にキューへ戻す
                    if finished_track and state.loop_mode == LoopMode.ALL:
                        # 次曲取得より前に再投入を完了させる
                        await state.put_queue_track(finished_track)
                    # 次曲（またはキュー終了処理）へ進む
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
        次曲・リトライ・NO-audio UI の唯一の所有者。
        """
        # 音源削除コールバックでは削除済み状態を復活させない
        state = self.get_existing_guild_state(guild_id)
        # シーク中や既に次曲処理中の場合はスキップ
        if not state or state.stopping or state.is_seeking or state._playing_next:
            # mixer_finished が待ち続けないようフラグを戻す
            if state is not None:
                # ソース削除所有フラグを解除する
                state._music_source_removed = False
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
                await state.put_queue_track(finished_track)

            # 次の曲を再生（キューが空の場合はミキサーの停止も行う）
            await self._play_next_song(guild_id)
        finally:
            # 次曲所有フラグを解除する
            state._playing_next = False
            # mixer_finished 向けのソース削除所有フラグも解除する
            state._music_source_removed = False

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

    async def _leave_vc_on_queue_empty(self, guild_id: int) -> None:
        """キュー終了後、TTS 等がミキサーに残っていなければ VC から退出する。"""
        # ギルド状態を取得する（存在しなければ何もしない）
        state = self.get_existing_guild_state(guild_id)
        # 状態が無ければ早期リターン
        if not state:
            return
        # VC 未接続なら退出不要
        if not state.voice_client or not state.voice_client.is_connected():
            return
        # 再生中またはキューに曲が残っていれば退出しない
        if state.is_playing or not state.queue.empty():
            return
        # TTS 等のソースがミキサーに残っていれば VC を維持する
        if state.mixer and state.mixer.has_sources():
            return
        # Bot が設定した VC ステータスをクリアする（ユーザーロック時は触らない）
        await self._clear_voice_channel_status(guild_id)
        # キュー終了に伴う VC 退出をログに残す
        logger.info("Guild %s: Queue empty, leaving voice channel", guild_id)
        # 音声接続のみ切断する（Queue Finished UI は維持する）
        await state.cleanup_voice_client()
        # VC 退出後はステータス追跡を初期化する
        self._reset_vc_status_tracking(state)

    async def _play_next_song(
        self,
        guild_id: int,
        seek_seconds: int = 0,
        play_msg: Optional[discord.Message] = None,
        *,
        retry_track: Optional[Track] = None,
        use_fallback_clients: bool = False,
        force_current: bool = False,
    ):
        # 内部再生処理では状態を新規作成しない
        state = self.get_existing_guild_state(guild_id)
        if not state:
            return

        if state.is_playing and not seek_seconds > 0 and retry_track is None and not force_current:
            return

        is_seek_operation = seek_seconds > 0
        track_to_play: Optional[Track] = None

        # 403 リトライ時は同一トラックを強制再生する
        if retry_track is not None and not is_seek_operation:
            # リトライ対象をそのまま使う
            track_to_play = retry_track
        elif (is_seek_operation or force_current) and state.current_track:
            # シーク／再起動復元では現在曲を再利用する（キューから取らない）
            track_to_play = state.current_track
        elif state.loop_mode == LoopMode.ONE and state.current_track and not is_seek_operation:
            track_to_play = state.current_track
        elif not state.is_playing and not state.queue.empty() and not is_seek_operation:
            try:
                # ロック付き非ブロッキング取出（shuffle 差し替えとの競合を防ぐ）
                track_to_play = await state.pop_queue_track()
            except asyncio.CancelledError:
                # タスク停止要求は上位へ伝播して正常にキャンセルする
                raise
            except Exception:
                # キュー取得時の予期しない失敗は再生対象なしとして処理する
                track_to_play = None

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
            # キュー終了に合わせて VC ステータスをクリアする
            await self._clear_voice_channel_status(guild_id)
            # キューが空になったら VC から退出する（TTS 再生中はミキサー残存でスキップ）
            await self._leave_vc_on_queue_empty(guild_id)
            # 次曲再生処理を終了する
            return

        # 失敗バナーを「次曲の再生中だけ」出すため、
        # バナー表示済みの状態でさらに次の曲へ進むときに消す
        if state.ui_load_error_seen:
            # バナー本文をクリアする
            state.ui_load_error = None
            # 表示済みフラグも戻す
            state.ui_load_error_seen = False

        if not is_seek_operation and not force_current:
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
                        # 削除済みギルド状態は復活させない
                        removed_state = self.get_existing_guild_state(guild_id)
                        # 状態があれば同期的に所有フラグを立てる（mixer_finished より先に必須）
                        if removed_state is not None:
                            # 次曲遷移の所有者を on_source_removed 側にする
                            removed_state._music_source_removed = True
                        # 次曲・リトライ・NO-audio UI を非同期で処理する
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
                # 次曲コールバックが走らないようソース削除通知を無効化する
                current_mixer.on_source_removed_callback = None
                try:
                    # add_source 済みの FFmpeg を強制解放する
                    await current_mixer.remove_source('music')
                except Exception as abort_err:
                    # 除去失敗時はミキサー全体を止めてリソースを捨てる
                    logger.warning(
                        "Guild %s: remove_source on abort failed: %s; tearing down mixer",
                        guild_id,
                        abort_err,
                    )
                    # 状態からミキサー参照を切る
                    if state.mixer is current_mixer:
                        # 参照をクリアする
                        state.mixer = None
                    # 全ソースをクリーンアップする
                    current_mixer.stop()
                else:
                    # music 除去後に他ソースが無ければミキサーを解体する
                    if not current_mixer.has_sources():
                        # 状態参照を先に切る
                        if state.mixer is current_mixer:
                            # アイドル終了として参照を消す
                            state.mixer = None
                        # プレイヤー終了とリソース解放
                        current_mixer.stop()
                    # 残ソースがあれば idle クリーンアップに任せる
                    else:
                        # TTS 等が残る場合の共通処理
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

            # 再起動復元セッションは再生開始成功で削除する
            self._delete_vc_restart_session(guild_id)
            # 復元時に一時停止だった場合は再生開始直後に pause する
            if self._pending_restore_pause.pop(guild_id, False):
                try:
                    # VoiceClient があれば pause する
                    if state.voice_client and state.voice_client.is_playing():
                        # 再生を一時停止する
                        state.voice_client.pause()
                        # 状態フラグを合わせる
                        state.is_paused = True
                        # 一時停止時刻を記録する
                        state.paused_at = time.time()
                except Exception as pause_err:
                    # pause 失敗は復元自体を失敗扱いにしない
                    logger.warning(
                        "Guild %s: failed to re-apply pause after restore: %s",
                        guild_id,
                        pause_err,
                    )

            # シーク操作以外、または再起動復元（force_current）では Now Playing UI を更新する
            # 次曲移行時も最初の /play 応答メッセージを編集して維持し、誰が再生開始したか分かるようにする
            if state.last_text_channel_id and (not is_seek_operation or force_current):
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
                        # 再生開始に合わせて VC ステータスを更新する
                        await self._sync_voice_channel_status(guild_id)
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
                            # 復旧後も VC ステータスを同期する
                            await self._sync_voice_channel_status(guild_id)
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
                await state.put_queue_track(track_to_play)
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
            # 権限不足は想定内のため debug のみ（WARNING は出さない）
            logger.debug(
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

    @staticmethod
    def _reset_vc_status_tracking(state: GuildState) -> None:
        """VC ステータス追跡フラグを初期化する。"""
        # ユーザーロックを解除する
        state.vc_status_locked = False
        # Bot が設定した最後の文字列を忘れる
        state.vc_status_last_bot = None
        # 反映待ちフラグを下ろす
        state.vc_status_pending_active = False
        # 反映待ち文字列をクリアする
        state.vc_status_pending = None
        # 権限不足フラグもセッション終了時に戻す
        state.vc_status_permission_denied = False
        # 管理対象 VC ID も忘れる
        state.vc_status_channel_id = None

    def _format_vc_status_text(self, state: GuildState) -> Optional[str]:
        """再生状態から VC ステータス文字列を組み立てる。"""
        # 再生中トラックが無ければクリア対象
        if not state.current_track or not state.is_playing:
            # None はステータス削除を意味する
            return None
        # 曲名が空のときのフォールバック
        title = (state.current_track.title or "Unknown").strip()
        # 一時停止中かどうかで接頭辞を切り替える
        prefix = "Paused" if state.is_paused else "NowPlaying"
        # ユーザー指定形式「NowPlaying - 曲名」で返す
        return f"{prefix} - {title}"[:VC_STATUS_MAX_LEN]

    def _get_vc_status_voice_channel(
        self, state: GuildState,
    ) -> Optional[discord.VoiceChannel]:
        """VC ステータス更新対象の VoiceChannel を返す。"""
        # 接続中 VoiceClient が無ければ対象外
        if not state.voice_client or not state.voice_client.is_connected():
            # 更新不可
            return None
        # 接続先チャンネルを取得する
        channel = state.voice_client.channel
        # VoiceChannel 以外（Stage 等）は未対応
        if not isinstance(channel, discord.VoiceChannel):
            # 更新不可
            return None
        # 対象チャンネルを返す
        return channel

    @staticmethod
    def _is_vc_status_permission_error(exc: BaseException) -> bool:
        """VC ステータス更新の権限不足系例外かどうかを判定する。"""
        # Forbidden は権限不足の典型
        if isinstance(exc, discord.Forbidden):
            return True
        # HTTPException でも 403 / Missing Permissions (50013) なら同様に扱う
        if isinstance(exc, discord.HTTPException):
            # HTTP ステータスが 403
            if getattr(exc, "status", None) == 403:
                return True
            # Discord エラーコード 50013 = Missing Permissions
            if getattr(exc, "code", None) == 50013:
                return True
            # 文言フォールバック（実装差・ラップ例外向け）
            text = str(exc).lower()
            if "missing permissions" in text or "missing access" in text:
                return True
        return False

    def _mark_vc_status_permission_denied(
        self,
        state: GuildState,
        guild_id: int,
        detail: object,
    ) -> None:
        """権限不足を記録し、初回のみ INFO（以降は完全サプレッション）。"""
        # 既に諦め済みなら何も出さない
        if state.vc_status_permission_denied:
            return
        # 以降の API 呼び出しを止める
        state.vc_status_permission_denied = True
        # 初回だけ INFO（ERROR/WARNING にはしない）
        logger.info(
            "Guild %s: VC status permission denied (%s); suppressing further updates",
            guild_id,
            detail,
        )

    async def _apply_voice_channel_status(
        self,
        guild_id: int,
        status_text: Optional[str],
    ) -> None:
        """VC ステータスを API 経由で設定する（権限不足は INFO 1回＋以降サプレッション）。"""
        # ギルド状態を取得する
        state = self.get_existing_guild_state(guild_id)
        # 状態が無い、ロック中、権限諦め済みなら何もしない
        if not state or state.vc_status_locked or state.vc_status_permission_denied:
            return
        # 前回 Bot が設定した値と同じなら API を叩かない
        if status_text == state.vc_status_last_bot:
            return
        # 更新対象 VC を解決する
        channel = self._get_vc_status_voice_channel(state)
        # VC が取れなければ終了
        if channel is None:
            return
        # ギルドと Bot メンバーを取得する
        guild = channel.guild
        me = guild.me
        # me が無ければ権限判定できない
        if me is None:
            return
        # Set Voice Channel Status 権限を確認する
        if not channel.permissions_for(me).set_voice_channel_status:
            # キャッシュ上の権限不足も INFO 1回でサプレッション
            self._mark_vc_status_permission_denied(
                state,
                guild_id,
                "missing set_voice_channel_status",
            )
            return
        # Bot 自身の更新 echo を待つため pending をセットする
        state.vc_status_pending_active = True
        # 設定予定文字列を保持する（クリア時は None）
        state.vc_status_pending = status_text
        # 管理対象 VC ID を記録する
        state.vc_status_channel_id = channel.id
        try:
            # Discord API で VC ステータスを書き換える
            await channel.edit(
                status=status_text,
                reason="Music bot: sync now playing to voice channel status",
            )
            # 成功した値を Bot 設定済みとして記録する
            state.vc_status_last_bot = status_text
        except Exception as exc:
            # 権限不足は INFO 1回＋以降スキップ（外側の ERROR に伝播させない）
            if self._is_vc_status_permission_error(exc):
                self._mark_vc_status_permission_denied(state, guild_id, exc)
            elif isinstance(exc, discord.HTTPException):
                # 一時的 HTTP 失敗は INFO のみ（再生自体は継続）
                logger.info(
                    "Guild %s: VC status update failed (%s)",
                    guild_id,
                    exc,
                )
            else:
                # 想定外も ERROR にせず INFO（ステータスはベストエフォート）
                logger.info(
                    "Guild %s: VC status update unexpected failure (%s)",
                    guild_id,
                    exc,
                )
        finally:
            # echo 待ちを終了する
            state.vc_status_pending_active = False

    async def _sync_voice_channel_status(self, guild_id: int) -> None:
        """現在の再生状態を VC ステータスへ反映する。"""
        try:
            # ギルド状態を取得する
            state = self.get_existing_guild_state(guild_id)
            # 状態が無ければ何もしない
            if not state:
                return
            # 表示文字列を組み立てる
            status_text = self._format_vc_status_text(state)
            # API で反映する
            await self._apply_voice_channel_status(guild_id, status_text)
        except Exception as exc:
            # 呼び出し元の Now Playing ERROR に混ぜない
            logger.info(
                "Guild %s: VC status sync suppressed (%s)",
                guild_id,
                exc,
            )

    async def _clear_voice_channel_status(self, guild_id: int) -> None:
        """Bot が設定した VC ステータスをクリアする。"""
        try:
            # ギルド状態を取得する
            state = self.get_existing_guild_state(guild_id)
            # 状態が無い、ロック中、Bot が一度も設定していなければ触らない
            if not state or state.vc_status_locked or state.vc_status_last_bot is None:
                return
            # 権限諦め済みならクリアも試行しない
            if state.vc_status_permission_denied:
                return
            # ステータスを None（削除）で反映する
            await self._apply_voice_channel_status(guild_id, None)
        except Exception as exc:
            # クリア失敗も再生フローを汚さない
            logger.info(
                "Guild %s: VC status clear suppressed (%s)",
                guild_id,
                exc,
            )

    def _schedule_auto_leave(self, guild_id: int):
        # 対象ギルドの再生状態を取得する
        state = self.get_existing_guild_state(guild_id)
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
        state = self.get_existing_guild_state(guild_id)
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
        state = self.get_existing_guild_state(guild_id)
        # 状態が存在するか判定する
        if state:
            # 退出前に Bot 設定の VC ステータスを消す
            await self._clear_voice_channel_status(guild_id)
            # 破棄中に到着した終了コールバックが次曲再生を始めないよう停止状態へ移行する
            state.stopping = True
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
            # VC ステータス追跡を初期化する
            self._reset_vc_status_tracking(state)
            # ギルド名解決用にギルドオブジェクトを取得する
            guild = self.bot.get_guild(guild_id)
            # クリーンアップ完了をログに残す
            logger.info(f"Guild {guild_id} ({guild.name if guild else ''}): State cleaned up")

    def _start_progress_updater(self, guild_id: int):
        # 対象ギルドの再生状態を取得する
        state = self.get_existing_guild_state(guild_id)
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
                state = self.get_existing_guild_state(guild_id)
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
            state = self.get_existing_guild_state(guild_id)
            # 状態が残っており、かつ自分自身のタスク参照ならクリアする
            if state and state.progress_update_task is asyncio.current_task():
                # 参照を None にして再利用可能にする
                state.progress_update_task = None

    @commands.Cog.listener()
    async def on_ready(self):
        logger.info(f"{self.bot.user.name} の MusicCog が正常にロードされました。")
        # グレースフル再起動で保存した VC セッションを復元する
        try:
            # 復元処理を起動する
            await self.restore_vc_sessions_after_restart()
        except Exception as e:
            # 復元失敗でも Cog は生かす
            logger.warning("VC session restore on_ready failed: %s", e)
        # Companion は復元後に Primary 同居分を掃除する
        try:
            # 既存同居を一括点検する
            await self._evacuate_companion_coexistence_all()
        except Exception as e:
            # sweep 失敗でも Cog は生かす
            logger.warning("companion coexistence sweep failed: %s", e)

    @commands.Cog.listener()
    async def on_socket_raw_receive(self, msg: dict):
        """VC ステータスの手動編集を検知して Bot 側の自動更新をロックする。"""
        # 関心のないイベントは無視する
        if msg.get("t") != "VOICE_CHANNEL_STATUS_UPDATE":
            return
        # イベント payload を取り出す
        data = msg.get("d")
        # payload が無ければ処理不能
        if not data:
            return
        # チャンネル ID を整数化する
        channel_id = int(data["id"])
        # ギルド ID を整数化する
        guild_id = int(data["guild_id"])
        # 新しいステータス文字列（クリア時は None）
        new_status = data.get("status")
        # 対象ギルドの再生状態を取得する
        state = self.get_existing_guild_state(guild_id)
        # 状態が無い、または Bot 管理対象 VC でなければ無視
        if not state or state.vc_status_channel_id != channel_id:
            # 接続中 VC と一致する場合は channel_id を補完する
            channel = self._get_vc_status_voice_channel(state) if state else None
            if not state or channel is None or channel.id != channel_id:
                return
            # 初回イベントで管理 ID を覚える
            state.vc_status_channel_id = channel_id
        # Bot 自身の更新 echo なら last_bot を同期して終了
        if state.vc_status_pending_active:
            # pending と一致すれば Bot 更新として確定
            if new_status == state.vc_status_pending:
                state.vc_status_last_bot = new_status
                return
        # Bot が設定した値と異なる変更はユーザー編集とみなしてロック
        if new_status != state.vc_status_last_bot:
            state.vc_status_locked = True
            logger.info(
                "Guild %s: VC status manually edited; bot will no longer update channel %s",
                guild_id,
                channel_id,
            )

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState,
                                    after: discord.VoiceState):
        # 自BotがVCから切断されたらギルド状態を破棄する
        if member.id == self.bot.user.id and before.channel and not after.channel:
            # 再生状態・VoiceClient・自動退出タスクをまとめて掃除
            await self._cleanup_guild_state(member.guild.id)
            # 以降の無人判定は不要
            return

        # Companion: Primary が自 VC に入ってきた／同居している場合は自己切断する
        if self._is_companion_bot() and after.channel is not None:
            # Primary の Discord user id を解決する
            primary_user_id = registry.user_id("plana")
            # Primary 本人の移動イベントだけを同居トリガにする
            if primary_user_id is not None and member.id == primary_user_id:
                # 自 Bot の状態を取得する
                own_state = self.get_existing_guild_state(member.guild.id)
                # 自 Bot が接続中で、Primary の到着先が同じ VC なら退出する
                if (
                    own_state
                    and own_state.voice_client
                    and own_state.voice_client.is_connected()
                    and own_state.voice_client.channel
                    and own_state.voice_client.channel.id == after.channel.id
                ):
                    # テキスト案内用のチャンネルを控える
                    text_channel_id = own_state.last_text_channel_id
                    # 退出理由をログする
                    logger.info(
                        "Guild %s: companion leaving VC %s — primary joined same channel",
                        member.guild.id,
                        after.channel.id,
                    )
                    # 切断前に案内を送る（cleanup で状態が消える）
                    if text_channel_id:
                        # partner_coexist_left をバックグラウンド送信する
                        await self._send_background_message(
                            text_channel_id,
                            "partner_coexist_left",
                            partner_name=self._partner_display_name(),
                        )
                    # Music 状態ごと切断する
                    await self._cleanup_guild_state(member.guild.id)
                    # 退出後は無人判定不要
                    return

        # 対象ギルドIDを取得する
        guild_id = member.guild.id
        # Music 状態が無いギルドは無視する
        if guild_id not in self.guild_states:
            # 早期リターン
            return

        # ギルド再生状態を取得する
        state = self.get_existing_guild_state(guild_id)
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

        # コマンド開始時点で実際に再生中かだけを保持し、並行する検索中は再生中と扱わない
        was_playing = state.is_playing
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
                    await state.put_queue_track(track)
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

            # 同時 /play でも初回再生を一度だけ開始できるよう、ギルド単位で判定する
            async with state.play_lock:
                # キュー追加後も再生が始まっていない場合だけ先頭曲を開始する
                if not state.is_playing:
                    # 以前の停止状態を解除して新しい再生を許可する
                    state.stopping = False
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

        # 再生時間が判明している曲だけ、終端以降へのシークを拒否する
        if state.current_track.duration > 0 and seek_seconds >= state.current_track.duration:
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
        await self._sync_voice_channel_status(ctx.guild.id)

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
        await self._sync_voice_channel_status(ctx.guild.id)

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
        # 停止起因の終了コールバックが次曲再生を始めないよう先に停止状態へ移行する
        state.stopping = True
        # Stop 確認ダイアログが残っていれば解除する
        state.confirming_stop = False
        # キューページを先頭に戻す
        state.queue_page = 0
        # コールバックより先にミキサー参照を切り離して古い終了通知を無効化する
        mixer = state.mixer
        state.mixer = None
        # 切り離したミキサーが存在する場合だけ停止する
        if mixer:
            # 音源と子プロセスを停止する
            mixer.stop()
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
        await self._clear_voice_channel_status(ctx.guild.id)

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
        state = self.get_existing_guild_state(guild_id)
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
        state = self.get_existing_guild_state(guild_id)
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
        state = self.get_existing_guild_state(guild_id)
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

        # Queue オブジェクトを差し替えず in-place で並べ替える
        await state.shuffle_queue()
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

        # Queue を差し替えず指定位置だけ外す
        removed_track = await state.remove_queue_index(actual_index)
        if removed_track is None:
            await self._send_response(ctx, "invalid_queue_number", ephemeral=True)
            return
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
        # コマンド入口でギルド状態を確保し、接続補助処理は既存状態だけを使えるようにする
        state = self._get_guild_state(ctx.guild.id)
        # 保持上限で状態を確保できない場合は接続処理を行わない
        if not state:
            # 上限到達をエフェメラルで通知する
            await self._send_ctx_message(ctx, content="サーバーの上限に達しています。", ephemeral=True)
            # コマンド処理を終了する
            return
        if await self._ensure_voice(ctx, connect_if_not_in=True):
            await self._send_ctx_message(
                ctx,
                content=self.exception_handler.get_message("already_connected"),
                ephemeral=True,
            )

async def setup(bot: commands.Bot):
    try:
        await bot.add_cog(MusicCog(bot))
        logger.info("MusicCog successfully loaded")
    except Exception as e:
        logger.error(f"MusicCogのセットアップ中にエラー: {e}", exc_info=True)
        raise
