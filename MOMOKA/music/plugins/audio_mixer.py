# MOMOKA/music/audio_mixer.py
import discord
import asyncio
import io
import shlex
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Dict, Optional, List, Tuple
import logging

import numpy as np

from MOMOKA.music.plugins.process_log_bridge import (
    ChildProcessLogPump,
    redact_media_url,
)

logger = logging.getLogger(__name__)

# Discord PCM: 20ms / 48kHz / 16bit LE / stereo → 960*2*2 = 3840 bytes
_FRAME_BYTES = 3840
# 1フレームあたりのサンプル数（L/R を含む）
_FRAME_SAMPLES = _FRAME_BYTES // 2
# PCM int16 リトルエンディアン（Discord 仕様に合わせる）
_PCM_DTYPE = np.dtype('<i2')


class AudioMixer(discord.AudioSource):
    def __init__(self, on_source_removed_callback=None):
        # ソース辞書: ソース名→AudioSourceオブジェクト
        self.sources: Dict[str, discord.AudioSource] = {}
        # ボリューム辞書: ソース名→音量(float)
        self.volumes: Dict[str, float] = {}
        # asyncio用ロック（非同期メソッド間の排他制御）
        self.lock = asyncio.Lock()
        # スレッド間の排他制御用ロック（read()はオーディオスレッドから呼ばれる）
        self._thread_lock = threading.Lock()
        # ミキサーの終了フラグ
        self._is_done = False
        # ミキサーのアクティブ状態
        self.active = True
        # ソース削除時に呼ばれるコールバック（各ソース削除ごとに発火）
        self.on_source_removed_callback = on_source_removed_callback

    def is_done(self) -> bool:
        """ミキサーが終了したかどうかを返す"""
        return self._is_done

    def stop(self):
        """ミキサーを停止し、全リソースをクリーンアップ"""
        self.active = False
        self._is_done = True
        self.cleanup()

    def read(self) -> bytes:
        """
        オーディオフレームを読み取り、全ソースをミキシングして返す。
        注意: このメソッドはDiscordのオーディオスレッドから呼ばれるため、
        asyncio APIは使用不可。threading.Lockで排他制御する。
        """
        # 停止済みなら空バイトを返してプレイヤーを終了させる
        if not self.active or self._is_done:
            return b''

        # 終了したソースを (名前, ソース参照) のタプルで記録
        finished_sources: List[Tuple[str, discord.AudioSource]] = []

        # スレッド安全にソース一覧のスナップショットを取得
        with self._thread_lock:
            sources_to_process = list(self.sources.items())

        # 音楽・TTSを含めて再生可能な音源が無い場合はプレイヤーを終了する
        if not sources_to_process:
            return b''

        # int32 累積バッファ（加算オーバーフロー回避、最後に一度だけ clip）
        mixed = np.zeros(_FRAME_SAMPLES, dtype=np.int32)

        # 各ソースからフレームを読み取り、NumPy でベクタ加算
        for name, source in sources_to_process:
            try:
                # ソースからPCMフレームを読み取る
                frame = source.read()
                # 空フレーム＝ソース終了
                if not frame:
                    finished_sources.append((name, source))
                    continue

                # フレーム長を Discord 20ms に揃える（不足はゼロ埋め、超過は切り捨て）
                frame_len = len(frame)
                if frame_len < _FRAME_BYTES:
                    frame = frame + b'\x00' * (_FRAME_BYTES - frame_len)
                elif frame_len > _FRAME_BYTES:
                    frame = frame[:_FRAME_BYTES]

                # 16bit LE PCM をゼロコピーで int16 ビュー化
                src = np.frombuffer(frame, dtype=_PCM_DTYPE)
                # このソースの音量を取得
                volume = self.volumes.get(name, 1.0)

                # volume=1.0 は乗算を省略して int32 加算のみ
                if volume == 1.0:
                    mixed += src.astype(np.int32, copy=False)
                else:
                    # int(s * volume) 相当（ゼロ方向へ切り捨て）で加算
                    mixed += (src.astype(np.float32) * np.float32(volume)).astype(np.int32)

            except Exception as read_err:
                # 読み取りエラーが発生したソースは終了扱い
                logger.warning(f"AudioMixer: Source '{name}' raised exception during read(): {read_err}")
                finished_sources.append((name, source))

        # 終了したソースを削除
        actually_removed: List[Tuple[str, discord.AudioSource]] = []
        if finished_sources:
            with self._thread_lock:
                for name, finished_source in finished_sources:
                    # ソースが置き換えられていないか確認（seek等で別ソースに差し替わっている場合はスキップ）
                    current_source = self.sources.get(name)
                    if current_source is not finished_source:
                        # 別のソースに差し替わっている→削除しない（新しいソースを保持）
                        continue
                    # ソース辞書から削除
                    self.sources.pop(name, None)
                    # ボリューム辞書からも削除
                    self.volumes.pop(name, None)
                    # FFmpegプロセス等のリソースをクリーンアップ
                    if finished_source and hasattr(finished_source, 'cleanup'):
                        try:
                            finished_source.cleanup()
                        except Exception as e:
                            logger.error(f"Error cleaning up finished source '{name}': {e}")
                    # 実際に削除されたソースを記録（失敗フラグ参照用にソースも残す）
                    actually_removed.append((name, finished_source))

            # コールバックはロック外で実行（デッドロック防止）
            # 各ソースの削除ごとにコールバックを発火
            if self.on_source_removed_callback and actually_removed:
                for name, removed_source in actually_removed:
                    try:
                        # ソース参照も渡し、NO audio / 403 判定を可能にする
                        self.on_source_removed_callback(name, removed_source)
                    except TypeError:
                        # 旧シグネチャ（name のみ）互換
                        try:
                            self.on_source_removed_callback(name)
                        except Exception as e:
                            logger.error(f"Error in on_source_removed_callback for '{name}': {e}")
                    except Exception as e:
                        logger.error(f"Error in on_source_removed_callback for '{name}': {e}")

        # 音源削除後に音楽・TTSとも残っていない場合だけプレイヤーを終了する
        if not self.has_sources():
            return b''

        # 16bit 範囲へ一括クリップして PCM bytes に戻す
        np.clip(mixed, -32768, 32767, out=mixed)
        return mixed.astype(_PCM_DTYPE).tobytes()

    async def add_source(self, name: str, source: discord.AudioSource, volume: float = 1.0):
        """
        ソースをミキサーに追加する。同名のソースが存在する場合は置き換え。
        """
        async with self.lock:
            with self._thread_lock:
                # 同名ソースが存在する場合は先にクリーンアップ
                if name in self.sources:
                    old_source = self.sources.get(name)
                    if old_source and hasattr(old_source, 'cleanup'):
                        old_source.cleanup()

                # 新しいソースを登録
                self.sources[name] = source
                # 音量を設定（0.0以上に制限）
                self.volumes[name] = max(0.0, volume)

    async def remove_source(self, name: str) -> Optional[discord.AudioSource]:
        """
        指定名のソースをミキサーから削除する。
        """
        removed_source = None
        async with self.lock:
            with self._thread_lock:
                # ソース辞書から削除
                removed_source = self.sources.pop(name, None)
                # ボリューム辞書からも削除
                self.volumes.pop(name, None)
            # FFmpegプロセス等のリソースをクリーンアップ（thread_lock外で実行）
            if removed_source and hasattr(removed_source, 'cleanup'):
                removed_source.cleanup()
        # ソース削除コールバックを発火（asyncio lock外で実行）
        if removed_source and self.on_source_removed_callback:
            try:
                # ソース参照も渡す（スキップ時は失敗フラグ無し）
                self.on_source_removed_callback(name, removed_source)
            except TypeError:
                # 旧シグネチャ互換
                try:
                    self.on_source_removed_callback(name)
                except Exception as e:
                    logger.error(f"Error in on_source_removed_callback for '{name}': {e}")
            except Exception as e:
                logger.error(f"Error in on_source_removed_callback for '{name}': {e}")
        return removed_source

    async def set_volume(self, name: str, volume: float):
        """指定ソースの音量を変更する"""
        async with self.lock:
            with self._thread_lock:
                if name in self.volumes:
                    self.volumes[name] = max(0.0, volume)

    def get_source(self, name: str) -> Optional[discord.AudioSource]:
        """指定ソースを取得する"""
        with self._thread_lock:
            return self.sources.get(name)

    def has_sources(self) -> bool:
        """ミキサーにソースが存在するかどうかを返す"""
        with self._thread_lock:
            return bool(self.sources)

    def get_source_names(self) -> list:
        """現在登録されているソース名のリストを返す"""
        with self._thread_lock:
            return list(self.sources.keys())

    def cleanup(self):
        """全ソースのリソースをクリーンアップ"""
        # スレッド安全にコピーを作成してからイテレート
        with self._thread_lock:
            sources_copy = list(self.sources.values())
            self.sources.clear()
            self.volumes.clear()
        # クリーンアップはロック外で実行
        for source in sources_copy:
            if hasattr(source, 'cleanup'):
                try:
                    source.cleanup()
                except Exception as e:
                    logger.error(f"Error cleaning up source: {e}")


class MusicAudioSource(discord.FFmpegPCMAudio):
    """
    音楽再生用のFFmpegオーディオソース。
    YouTube は googlevideo 直読みだと FFmpeg が 403 になるため、
    yt-dlp の stdout を FFmpeg stdin へパイプして再生する。
    ローカルファイル等はそのまま -i URL/PATH で再生する。
    """

    # 20ms × 500 = 10秒間の起動猶予（yt-dlp EJS + パイプ開始待ち）
    STARTUP_GRACE_FRAMES = 500
    # 3840バイト = 960サンプル × 2バイト(16bit) × 2チャンネル(ステレオ) = 20ms分の無音PCMフレーム
    SILENCE_FRAME = b'\x00' * 3840

    def __init__(
        self,
        source,
        *,
        title: str = "Unknown Track",
        guild_id: int,
        webpage_url: Optional[str] = None,
        http_headers: Optional[dict] = None,
        player_clients: Optional[list] = None,
        pipe_format: Optional[str] = None,
        pipe_use_cookies: Optional[bool] = None,
        executable: str = "ffmpeg",
        before_options: Optional[str] = None,
        options: Optional[str] = None,
        **kwargs,
    ):
        # stderrを一時ファイルにリダイレクト（PIPEより確実にエラーを捕捉できる）
        self._stderr_file = tempfile.TemporaryFile(mode="w+b")
        # ストリームURLを保持（エラーログ用）
        self._stream_url = source if isinstance(source, str) else "(pipe)"
        # 署名・IP等を含むクエリを除去したログ専用URLを保持する。
        self._safe_stream_url = redact_media_url(self._stream_url)
        # トラックのタイトル（ログ用）
        self.title = title
        # ギルドID（ログ用）
        self.guild_id = guild_id
        # read()呼び出し回数（FFmpeg起動猶予の判定に使用）
        self._read_count = 0
        # 実 PCM（無音パディング以外）を出したフレーム数（再生位置算出用）
        self._produced_audio_frames = 0
        # 1フレームでもオーディオを出力したかどうか
        self._has_produced_audio = False
        # prime_until_audio で先読みした先頭フレーム（次の read で返す）
        self._primed_frame: Optional[bytes] = None
        # yt-dlp パイプ用プロセス（未使用時は None）
        self._ytdlp_proc: Optional[subprocess.Popen] = None
        # yt-dlp stderrを常時排出するログポンプ（未使用時はNone）
        self._ytdlp_stderr_pump: Optional[ChildProcessLogPump] = None
        # 一度も PCM を出せずに終了したか
        self.no_audio_failure: bool = False
        # yt-dlp / FFmpeg 側が HTTP 403 だったか
        self.http_forbidden_failure: bool = False
        # 403 以外でも代替 client 再試行すべきストリーム失敗か（途中切断等）
        self.stream_retryable_failure: bool = False
        # パイプ再生時に使う player_client 列（未指定なら一次セット）
        self._player_clients = player_clients

        # YouTube 判定用に遅延インポートする（循環 import 回避）
        from MOMOKA.music.plugins.ytdlp_wrapper import (
            build_ytdlp_pipe_command,
            is_youtube_media_url,
        )

        # ページ URL または CDN URL が YouTube 系ならパイプ再生する
        use_ytdlp_pipe = bool(
            webpage_url
            and is_youtube_media_url(webpage_url)
            and not (isinstance(source, str) and Path(source).is_file())
        ) or (
            isinstance(source, str)
            and is_youtube_media_url(source)
            and webpage_url
            and not Path(source).is_file()
        )

        # FFmpeg 共通の出力引数を組み立てる
        out_args: list = [
            "-f",
            "s16le",
            "-ar",
            "48000",
            "-ac",
            "2",
            "-loglevel",
            "warning",
            "-blocksize",
            str(getattr(discord.FFmpegPCMAudio, "BLOCKSIZE", 8192)),
        ]
        # options（例: -vn）があれば分割して追加する
        if isinstance(options, str) and options.strip():
            # オプション文字列を分割する
            out_args.extend(shlex.split(options))
        # stdout へ PCM を出す
        out_args.append("pipe:1")

        # YouTube: yt-dlp → FFmpeg(pipe:0)
        if use_ytdlp_pipe and webpage_url:
            # yt-dlp CLI コマンドを組み立てる（抽出成功時の format / client / cookie を引き継ぐ）
            ytdlp_cmd = build_ytdlp_pipe_command(
                webpage_url,
                player_clients=player_clients,
                format_spec=pipe_format,
                use_cookies=pipe_use_cookies,
            )
            # yt-dlp を起動し stdout をパイプする
            self._ytdlp_proc = subprocess.Popen(
                ytdlp_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL,
            )
            try:
                # stderrを常時排出してパイプ満杯とread()ブロックを防ぐ。
                self._ytdlp_stderr_pump = ChildProcessLogPump(
                    self._ytdlp_proc.stderr,
                    logger=logger,
                    level=logging.WARNING,
                    label="yt-dlp",
                    # FFmpegが先に閉じるskip/停止時の二次エラーはDEBUGへ下げる。
                    debug_markers=("broken pipe",),
                )
                # 子プロセスが出力を始める前に読み取りを開始する。
                self._ytdlp_stderr_pump.start()
                # パイプ再生であることをログする
                logger.info(
                    "Guild %s: Using yt-dlp pipe for '%s' (avoids googlevideo 403)",
                    guild_id,
                    title,
                )
                # シーク等の before_options はパイプ入力では入力前に効きにくい。
                # -ss だけは出力側（-i の後）へ回して途中再開を可能にする。
                seek_out_args: list = []
                # 残りの before_options（reconnect 等）はパイプ経路では使わない。
                if isinstance(before_options, str) and before_options.strip():
                    # シェル風に分割する。
                    before_tokens = shlex.split(before_options)
                    # トークンを走査して -ss とその値だけ抜き出す。
                    i = 0
                    # 配列長まで進む。
                    while i < len(before_tokens):
                        # -ss なら次の秒数を出力側へ移す。
                        if before_tokens[i] == "-ss" and i + 1 < len(before_tokens):
                            # 出力側シーク引数を積む。
                            seek_out_args.extend(["-ss", before_tokens[i + 1]])
                            # -ss と値を消費する。
                            i += 2
                            # 次トークンへ。
                            continue
                        # その他の before オプションはパイプ経路では破棄する。
                        i += 1
                # -i pipe:0 のあとに -ss と通常 out_args を並べる。
                ffmpeg_args: list = ["-i", "pipe:0", *seek_out_args, *out_args]
                # FFmpegAudio を pipe:0 / stdin=ytdlp.stdout で初期化する
                discord.FFmpegAudio.__init__(
                    self,
                    "pipe:0",
                    executable=executable,
                    args=ffmpeg_args,
                    stdin=self._ytdlp_proc.stdout,
                    stderr=self._stderr_file,
                )
                # 親プロセス側の stdout ハンドルを閉じ、FFmpeg 側だけが読めるようにする
                if self._ytdlp_proc.stdout:
                    # 親の複製 FD を閉じる
                    self._ytdlp_proc.stdout.close()
            except Exception:
                # 初期化途中の子プロセス・ログポンプ・stderrを確実に解放する
                self._cleanup_failed_pipe_initialization()
                # 呼び出し元が再生開始失敗を処理できるよう元の例外を再送出する
                raise
        else:
            # ローカルファイルや非 YouTube: 従来どおり直接 -i source
            ffmpeg_args = []
            # before_options（reconnect / -ss）を先頭へ追加する
            if isinstance(before_options, str) and before_options.strip():
                # シェル風に分割する
                ffmpeg_args.extend(shlex.split(before_options))
            # 非 YouTube でヘッダーがあれば付与する（一部 CDN 向け）
            headers = {
                str(k): str(v)
                for k, v in (http_headers or {}).items()
                if v is not None and v != ""
            }
            if headers:
                # CRLF 区切りのヘッダーブロックを作る
                header_block = "".join(f"{k}: {v}\r\n" for k, v in headers.items())
                # argv に直接渡す
                ffmpeg_args.extend(["-headers", header_block])
            # 入力を指定する
            ffmpeg_args.extend(["-i", source, *out_args])
            # FFmpegAudio を初期化する
            discord.FFmpegAudio.__init__(
                self,
                source,
                executable=executable,
                args=ffmpeg_args,
                stdin=subprocess.DEVNULL,
                stderr=self._stderr_file,
            )

        # FFmpegのPIDをログ出力
        pid = "N/A"
        try:
            if hasattr(self, "_process") and self._process:
                pid = self._process.pid
        except Exception:
            pass
        ytdlp_pid = self._ytdlp_proc.pid if self._ytdlp_proc else None
        logger.info(
            f"Guild {guild_id}: FFmpeg PID={pid} yt-dlp PID={ytdlp_pid} started for '{title}' "
            f"url={self._safe_stream_url[:150]}..."
        )

    def _cleanup_failed_pipe_initialization(self):
        """yt-dlp パイプ初期化の途中失敗で確保済みリソースを解放する。"""
        # 作成済みのyt-dlpプロセスをローカル変数で保持する
        ytdlp_proc = self._ytdlp_proc
        # 再利用されないよう先に状態参照をクリアする
        self._ytdlp_proc = None
        # プロセスが存在する場合だけ終了処理を行う
        if ytdlp_proc is not None:
            try:
                # 実行中のyt-dlpを強制終了してパイプ待機を防ぐ
                if ytdlp_proc.poll() is None:
                    ytdlp_proc.kill()
                # 子プロセス終了を短時間待機してゾンビ化を防ぐ
                ytdlp_proc.wait(timeout=2)
            except Exception:
                # 終了待機の失敗は後続リソース解放を妨げない
                pass
        # 起動済みのstderrログポンプを停止する
        if self._ytdlp_stderr_pump is not None:
            try:
                # EOF待機スレッドを回収する
                self._ytdlp_stderr_pump.stop()
            except Exception:
                # ログポンプ停止の失敗はパイプハンドル解放を妨げない
                pass
            finally:
                # 再利用されないようログポンプ参照をクリアする
                self._ytdlp_stderr_pump = None
        # 子プロセスの停止後に親側パイプハンドルを解放する
        if ytdlp_proc is not None:
            try:
                # 親側に残ったstdoutハンドルを閉じる
                if ytdlp_proc.stdout is not None:
                    ytdlp_proc.stdout.close()
            except Exception:
                # stdoutが既に閉じられていてもstderr解放を続行する
                pass
            try:
                # 親側に残ったstderrハンドルを閉じる
                if ytdlp_proc.stderr is not None:
                    ytdlp_proc.stderr.close()
            except Exception:
                # stderrが既に閉じられていても一時ファイル解放を続行する
                pass
        # 初期化途中に起動したFFmpegプロセスも残さない
        ffmpeg_proc = getattr(self, "_process", None)
        # FFmpegプロセスが存在する場合だけ終了処理を行う
        if ffmpeg_proc is not None:
            try:
                # 実行中のFFmpegを強制終了して子プロセスを残さない
                if ffmpeg_proc.poll() is None:
                    ffmpeg_proc.kill()
                # 子プロセス終了を短時間待機してゾンビ化を防ぐ
                ffmpeg_proc.wait(timeout=2)
            except Exception:
                # 終了待機の失敗はstderr解放を妨げない
                pass
        # 初期化時に作成したFFmpeg stderr一時ファイルを閉じる
        if self._stderr_file is not None:
            try:
                # TemporaryFileを閉じてハンドルを解放する
                self._stderr_file.close()
            except Exception:
                # 既に閉じられていても初期化失敗を隠さない
                pass
            finally:
                # 以後の二重クローズを避けるため参照を消す
                self._stderr_file = None

    def get_produced_audio_seconds(self) -> float:
        """実 PCM を出した累積秒数を返す（起動時の無音パディングは含めない）。"""
        # 1 フレーム = 20ms
        return self._produced_audio_frames * 0.02

    def _process_still_starting(self) -> bool:
        """FFmpeg / yt-dlp がまだ生存しているか（起動待ち判定用）。"""
        # FFmpeg 生存
        process_alive = False
        try:
            if hasattr(self, "_process") and self._process:
                process_alive = self._process.poll() is None
        except Exception:
            process_alive = False
        # yt-dlp 生存
        ytdlp_alive = False
        try:
            if self._ytdlp_proc is not None:
                ytdlp_alive = self._ytdlp_proc.poll() is None
        except Exception:
            ytdlp_alive = False
        return process_alive or ytdlp_alive

    def _normalize_pcm_frame(self, data: bytes) -> bytes:
        """Discord 20ms フレーム長へ揃える。"""
        # 不足はゼロ埋め
        if len(data) < _FRAME_BYTES:
            return data + b"\x00" * (_FRAME_BYTES - len(data))
        # 超過は切り捨て
        if len(data) > _FRAME_BYTES:
            return data[:_FRAME_BYTES]
        return data

    def prime_until_audio(self, timeout_sec: float = 20.0) -> bool:
        """
        Discord play 前に実 PCM 先頭フレームを待つ。

        起動直後の無音パディングを VC に流すと、Discord 側では数秒経過後に
        曲の 0 秒地点が始まるように聞こえるため、先に実音を確保してから play する。
        """
        # 既にプライム済み／出力済みなら成功扱い
        if self._primed_frame is not None or self._has_produced_audio:
            return True
        # 期限（単調時計）
        deadline = time.monotonic() + max(0.5, float(timeout_sec))
        # 期限まで FFmpeg を読む
        while time.monotonic() < deadline:
            # 生の FFmpeg 読み（自前の無音パディングは使わない）
            try:
                data = super().read()
            except Exception as read_err:
                logger.warning(
                    "Guild %s: prime read failed for '%s': %s",
                    self.guild_id,
                    self.title,
                    read_err,
                )
                break
            # 試行回数を進める
            self._read_count += 1
            # 実データが来たら先頭フレームとして保持する
            if data:
                # フレーム長を揃える
                frame = self._normalize_pcm_frame(data)
                # 次の read() で返す（この時点では Discord 未出力）
                self._primed_frame = frame
                # _has_produced_audio は実際に Discord へ渡した read() 側で立てる
                # （プライム待ち中に seek bar が PCM 0 秒扱いへ跳ねないようにする）
                logger.info(
                    "Guild %s: Primed first audio for '%s' after %s reads (~%sms)",
                    self.guild_id,
                    self.title,
                    self._read_count,
                    self._read_count * 20,
                )
                return True
            # プロセスが死んでいてデータも無いなら失敗
            if not self._process_still_starting():
                break
        # プライム失敗を NO audio 扱いにする
        self.no_audio_failure = True
        # stderr を拾ってリトライ判定材料にする
        stderr_output = self._read_stderr_file()
        ytdlp_err = self._read_ytdlp_stderr()
        combined_err = f"{stderr_output}\n{ytdlp_err}".lower()
        if "403" in combined_err or "forbidden" in combined_err:
            self.http_forbidden_failure = True
            self.stream_retryable_failure = True
        elif any(
            marker in combined_err
            for marker in (
                "0 bytes read",
                "partial file",
                "giving up after",
                "invalid data found",
                "nothing was encoded",
                "truncated",
                "requested format is not available",
            )
        ):
            self.stream_retryable_failure = True
        logger.warning(
            "Guild %s: Failed to prime audio for '%s' within %.1fs "
            "(reads=%s)\n  stderr=%s\n  ytdlp_stderr=%s",
            self.guild_id,
            self.title,
            timeout_sec,
            self._read_count,
            stderr_output,
            ytdlp_err,
        )
        return False

    def read(self) -> bytes:
        """
        PCMフレームを読み取る。
        FFmpegがストリームURL接続中でまだstdoutに書き込みを開始していない場合、
        discord.pyのFFmpegPCMAudio.read()は「3840バイト未満 → ソース終了」と判定してb''を返す。
        FFmpegプロセスがまだ生きている間は無音フレームを返して即終了を防止する。
        """
        # プライム済み先頭フレームがあればそれを返す
        if self._primed_frame is not None:
            # 一度だけ返す
            frame = self._primed_frame
            self._primed_frame = None
            # Discord へ実際に渡したので出力開始とみなす
            self._has_produced_audio = True
            # 位置カウント
            self._produced_audio_frames += 1
            return frame

        data = super().read()
        self._read_count += 1

        if data:
            if not self._has_produced_audio:
                logger.info(
                    f"Guild {self.guild_id}: FFmpeg for '{self.title}' started producing audio "
                    f"after {self._read_count} reads ({self._read_count * 20}ms)"
                )
            self._has_produced_audio = True
            # 実データフレームだけ位置に加算する
            self._produced_audio_frames += 1
            return data

        # --- 空データが返された ---

        # FFmpeg / yt-dlp 生存確認
        still_starting = self._process_still_starting()

        # 未プライム経路のフォールバック: 生きていて猶予内なら無音
        if (
            still_starting
            and not self._has_produced_audio
            and self._read_count <= self.STARTUP_GRACE_FRAMES
        ):
            if self._read_count % 50 == 0:  # 1秒ごとにログ出力
                logger.info(
                    f"Guild {self.guild_id}: FFmpeg/yt-dlp for '{self.title}' still starting up "
                    f"(read #{self._read_count}, {self._read_count * 20}ms elapsed)"
                )
            return self.SILENCE_FRAME

        # --- 本当にソース終了 ---
        stderr_output = self._read_stderr_file()
        ytdlp_err = self._read_ytdlp_stderr()

        if not self._has_produced_audio:
            returncode = None
            try:
                if hasattr(self, "_process") and self._process:
                    returncode = self._process.poll()
            except Exception:
                pass

            # NO audio 失敗フラグを立てる（コールバック側でリトライ判定に使う）
            self.no_audio_failure = True
            # stderr 結合テキストで失敗種別を判定する
            combined_err = f"{stderr_output}\n{ytdlp_err}".lower()
            # Forbidden / HTTP 403 なら明示フラグを立てる
            if "403" in combined_err or "forbidden" in combined_err:
                # HTTP 403 失敗フラグを立てる
                self.http_forbidden_failure = True
                # 代替 client リトライ候補にも含める
                self.stream_retryable_failure = True
            # 途中切断・不完全ヘッダ・再試行尽きもリトライ候補とする
            elif any(
                marker in combined_err
                for marker in (
                    "0 bytes read",
                    "partial file",
                    "giving up after",
                    "invalid data found",
                    "nothing was encoded",
                    "truncated",
                    # yt-dlp CLI 側の format 全滅も代替 client で再試行する
                    "requested format is not available",
                )
            ):
                # ストリーム再試行フラグを立てる
                self.stream_retryable_failure = True

            # フォーマット欠落等の再試行対象なので ERROR ではなく WARN に落とす
            logger.warning(
                f"Guild {self.guild_id}: FFmpeg for '{self.title}' produced NO audio!\n"
                f"  read_count={self._read_count}, returncode={returncode}, "
                f"still_starting={still_starting}\n"
                f"  url={self._safe_stream_url[:200]}\n"
                f"  stderr={stderr_output}\n"
                f"  ytdlp_stderr={ytdlp_err}"
            )
        else:
            logger.info(
                f"Guild {self.guild_id}: FFmpeg for '{self.title}' finished normally "
                f"after {self._read_count} reads."
            )

        return b""

    def _read_stderr_file(self) -> str:
        """一時ファイルからFFmpegのstderrを読み取る。"""
        try:
            if not self._stderr_file:
                return "(no stderr file)"
            self._stderr_file.seek(0)
            raw = self._stderr_file.read(4096)
            if raw:
                return raw.decode("utf-8", errors="replace").strip()
            return "(empty)"
        except Exception as e:
            return f"(stderr file read error: {e})"

    def _read_ytdlp_stderr(self) -> str:
        """ログポンプが保持するyt-dlp stderr末尾を返す。"""
        try:
            # パイプ未使用時は診断情報なしとする。
            if self._ytdlp_stderr_pump is None:
                # 非YouTube音源向けの既定値を返す。
                return "(no ytdlp stderr)"
            # ロック保護された末尾履歴を取得する。
            snapshot = self._ytdlp_stderr_pump.snapshot()
            # 空履歴は明示的な表示へ変換する。
            return snapshot or "(empty)"
        except Exception as e:
            return f"(ytdlp stderr read error: {e})"

    def cleanup(self):
        """FFmpeg / yt-dlp プロセスと一時ファイルのクリーンアップ"""
        logger.info(f"Guild {self.guild_id}: Music FFmpeg process for '{self.title}' is being cleaned up.")
        # 先に FFmpeg を止める
        try:
            super().cleanup()
        finally:
            # yt-dlp パイププロセスを停止する
            if self._ytdlp_proc is not None:
                # 終了処理用にローカルへ退避する
                ytdlp_proc = self._ytdlp_proc
                try:
                    # まだ生きていれば強制終了する
                    if ytdlp_proc.poll() is None:
                        ytdlp_proc.kill()
                    # 終了を短時間待つ
                    ytdlp_proc.wait(timeout=2)
                except Exception:
                    # 終了待ち失敗は無視する
                    pass
                finally:
                    # プロセス終了後にstderr読取スレッドを回収する。
                    if self._ytdlp_stderr_pump is not None:
                        # EOF処理を短時間だけ待つ。
                        self._ytdlp_stderr_pump.stop()
                        # 再利用されないよう参照を消す。
                        self._ytdlp_stderr_pump = None
                    try:
                        # 失敗経路と同様に親側 stdout FD を閉じる
                        if ytdlp_proc.stdout is not None:
                            # ハンドルを解放する
                            ytdlp_proc.stdout.close()
                    except Exception:
                        # 既に閉じていても stderr 解放を続行する
                        pass
                    try:
                        # 失敗経路と同様に親側 stderr FD を閉じる
                        if ytdlp_proc.stderr is not None:
                            # ハンドルを解放する
                            ytdlp_proc.stderr.close()
                    except Exception:
                        # 既に閉じていても一時ファイル解放を続行する
                        pass
                    # 参照をクリアする
                    self._ytdlp_proc = None
            # 一時ファイルをクローズ（自動削除される）
            try:
                if self._stderr_file:
                    self._stderr_file.close()
                    self._stderr_file = None
            except Exception:
                pass


class TTSAudioSource(discord.FFmpegPCMAudio):
    """TTS読み上げ用のFFmpegオーディオソース"""
    def __init__(self, source, *, text: str, guild_id: int, **kwargs):
        # BytesIOの場合はpipe=Trueを強制
        if isinstance(source, io.BytesIO):
            kwargs['pipe'] = True

        # BytesIOの参照を保持してクリーンアップ時にクローズ
        self._source_buffer = source if isinstance(source, io.BytesIO) else None
        # テキスト（ログ用、30文字以上は切り詰め）
        self.text = text if len(text) < 30 else text[:27] + "..."
        # ギルドID（ログ用）
        self.guild_id = guild_id

        try:
            super().__init__(source, **kwargs)
        except Exception as e:
            logger.error(f"Guild {guild_id}: Failed to initialize TTSAudioSource: {e}")
            # 初期化失敗時にもバッファをクローズ
            if self._source_buffer:
                try:
                    self._source_buffer.close()
                except Exception:
                    pass
            raise

    def cleanup(self):
        """FFmpegプロセスとバッファのクリーンアップ"""
        logger.info(f"Guild {self.guild_id}: TTS FFmpeg process for '{self.text}' is being cleaned up.")
        try:
            super().cleanup()
        finally:
            # BytesIOバッファを明示的にクローズしてメモリを解放
            if self._source_buffer:
                try:
                    self._source_buffer.close()
                except Exception as e:
                    logger.warning(f"Guild {self.guild_id}: Failed to close TTS buffer: {e}")
                finally:
                    self._source_buffer = None
