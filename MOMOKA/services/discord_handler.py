# MOMOKA/services/discord_handler.py

import asyncio
import json
import logging
import os
import re
from asyncio import Queue
from typing import List

import discord
import discord.errors
from discord import Client, TextChannel

# channel.send() の最大待機時間（秒）
# discord.py内部の5xxリトライが長時間ブロックするのを防止
SEND_TIMEOUT_SECONDS = 20.0


class DiscordLogFormatter(logging.Formatter):
    """
    ログレベルに応じてANSIエスケープコードを使い、文字色を変更するフォーマッター。
    """
    # ANSIカラーコード
    RESET = "\u001b[0m"
    RED = "\u001b[31m"
    YELLOW = "\u001b[33m"
    BLUE = "\u001b[34m"
    WHITE = "\u001b[37m"

    COLOR_MAP = {
        logging.DEBUG: WHITE,
        logging.INFO: BLUE,
        logging.WARNING: YELLOW,
        logging.ERROR: RED,
        logging.CRITICAL: RED,
    }

    def format(self, record: logging.LogRecord) -> str:
        """
        元のログメッセージをフォーマットし、全体をANSIカラーコードで囲む。
        """
        log_message = super().format(record)
        color = self.COLOR_MAP.get(record.levelno, self.WHITE)
        return f"{color}{log_message}{self.RESET}"


class DiscordLogHandler(logging.Handler):
    """
    Pythonのログを複数のDiscordチャンネルにバッチ送信するためのカスタムロギングハンドラ。
    レートリミットを回避するため、ログをキューに溜め、定期的にまとめて送信します。
    """

    def __init__(self, bot: Client, channel_ids: List[int], interval: float = 5.0,
                 config_path: str = "data/logging_channels.json"):
        super().__init__()
        self.bot = bot
        self.channel_ids = channel_ids
        self.interval = interval
        self.config_path = config_path

        self.queue: Queue[str] = Queue()
        self.channels: List[TextChannel] = []
        self._closed = False

        # 無効なチャンネルIDを追跡（連続で失敗した回数）
        self.invalid_channel_attempts: dict[int, int] = {}
        self.max_attempts = 3  # 3回連続で失敗したら削除

        self._task = self.bot.loop.create_task(self._log_sender_loop())

    def add_channel(self, channel_id: int):
        if channel_id not in self.channel_ids:
            self.channel_ids.append(channel_id)
            # 失敗カウントをリセット
            self.invalid_channel_attempts.pop(channel_id, None)

            if self.bot.is_ready():
                channel = self.bot.get_channel(channel_id)
                if isinstance(channel, TextChannel):
                    self.channels.append(channel)
                    print(f"DiscordLogHandler: Immediately added and activated channel {channel_id}.")
                    # 設定を保存
                    asyncio.create_task(self._save_config())
                else:
                    print(
                        f"DiscordLogHandler: Added channel ID {channel_id}, but it's not a valid text channel or not found yet.")
            else:
                self.channels = []
                print(f"DiscordLogHandler: Added channel ID {channel_id}. Will be activated once bot is ready.")
                # 設定を保存
                asyncio.create_task(self._save_config())

    def remove_channel(self, channel_id: int):
        if channel_id in self.channel_ids:
            self.channel_ids.remove(channel_id)
            self.channels = [ch for ch in self.channels if ch.id != channel_id]
            self.invalid_channel_attempts.pop(channel_id, None)
            print(f"DiscordLogHandler: Immediately removed and deactivated channel {channel_id}.")
            # 設定を保存
            asyncio.create_task(self._save_config())

    async def _save_config(self):
        """チャンネルIDリストをJSONファイルに保存"""
        try:
            os.makedirs(os.path.dirname(self.config_path), exist_ok=True)

            # main.py と同じ JSON 配列形式でチャンネル ID を保存する
            data = self.channel_ids

            try:
                import aiofiles
                async with aiofiles.open(self.config_path, 'w', encoding='utf-8') as f:
                    await f.write(json.dumps(data, indent=4, ensure_ascii=False))
            except ImportError:
                with open(self.config_path, 'w', encoding='utf-8') as f:
                    json.dump(data, f, indent=4, ensure_ascii=False)

            print(f"DiscordLogHandler: Saved log channel configuration to {self.config_path}")
        except Exception as e:
            print(f"DiscordLogHandler: Failed to save config: {e}")

    async def _remove_invalid_channel(self, channel_id: int, reason: str):
        """無効なチャンネルをリストから削除"""
        if channel_id in self.channel_ids:
            self.channel_ids.remove(channel_id)
            self.channels = [ch for ch in self.channels if ch.id != channel_id]
            self.invalid_channel_attempts.pop(channel_id, None)

            print(f"DiscordLogHandler: ⚠️ Removed invalid channel {channel_id} from config. Reason: {reason}")

            # 設定を保存
            await self._save_config()

    def emit(self, record: logging.LogRecord):
        # 外部ProviderログはGUI専用とし、認証情報の外部送信経路を閉じる。
        if record.name.startswith("MOMOKA.music.external."):
            # Discordキューへ投入せず終了する。
            return
        if self._closed:
            return
        msg = self.format(record)
        msg = self._sanitize_log_message(msg)
        try:
            self.queue.put_nowait(msg)
        except asyncio.QueueFull:
            print("DiscordLogHandler: Log queue is full, dropping message.")

    def _get_display_chars(self, text: str, count: int = 1) -> str:
        """
        テキストから先頭の記号・絵文字・空白を除去し、指定文字数を返す。
        絵文字も1文字としてカウントする。
        """
        # 先頭の引用符、括弧、空白を除去
        cleaned = re.sub(r'^[「『"\'『»«‹›〈〉《》【】〔〕［］｛｝（）()［］\s]+', '', text)

        # 絵文字パターン（Unicode絵文字の範囲）
        emoji_pattern = re.compile(
            "["
            "\U0001F1E0-\U0001F1FF"  # flags (iOS)
            "\U0001F300-\U0001F5FF"  # symbols & pictographs
            "\U0001F600-\U0001F64F"  # emoticons
            "\U0001F680-\U0001F6FF"  # transport & map symbols
            "\U0001F700-\U0001F77F"  # alchemical symbols
            "\U0001F780-\U0001F7FF"  # Geometric Shapes Extended
            "\U0001F800-\U0001F8FF"  # Supplemental Arrows-C
            "\U0001F900-\U0001F9FF"  # Supplemental Symbols and Pictographs
            "\U0001FA00-\U0001FA6F"  # Chess Symbols
            "\U0001FA70-\U0001FAFF"  # Symbols and Pictographs Extended-A
            "\U00002702-\U000027B0"  # Dingbats
            "\U000024C2-\U0001F251"
            "]+", flags=re.UNICODE
        )

        # 絵文字を除去してから文字数を取得
        chars_without_emoji = emoji_pattern.sub('', cleaned)

        if len(chars_without_emoji) >= count:
            return chars_without_emoji[:count]
        elif len(cleaned) >= count:
            return cleaned[:count]
        else:
            return text[:count] if text else ''

    # 1件のログメッセージの最大文字数（Discordの2000文字制限を考慮し余裕を持たせる）
    MAX_SINGLE_LOG_LENGTH = 1800

    def _sanitize_log_message(self, message: str) -> str:
        # --- バッククォートのエスケープ（```ansi コードブロック破壊を防止） ---
        # 連続する3つ以上のバッククォートをゼロ幅スペースで分断
        message = re.sub(r'`{3,}', lambda m: '`\u200b' * (len(m.group()) - 1) + '`', message)

        # Windowsユーザーパス
        message = re.sub(
            r'[A-Za-z]:\\Users\\[^\\]+\\[^\\]+',
            '********',
            message,
            flags=re.IGNORECASE
        )
        # Session ID
        message = re.sub(
            r'((?:Session ID:?|session)\s+)[a-f09]{32}',
            r'\1****',
            message,
            flags=re.IGNORECASE
        )
        # Session ID (上で取り切れなかった場合)
        message = re.sub(
            r'((?:Session ID:?|session)\s+)([a-f0-9])([a-f0-9]{31})',
            r'\1\2****',
            message,
            flags=re.IGNORECASE
        )
        # Discord Bot トークン形式を常に伏せる
        message = re.sub(
            r'\b(?:mfa\.)?[A-Za-z0-9_-]{24}\.[A-Za-z0-9_-]{6}\.[A-Za-z0-9_-]{20,}\b',
            '********',
            message,
        )
        # キー名と値が対になった一般的な API キー設定を伏せる
        message = re.sub(
            r'(?i)\b(api[_-]?key|access[_-]?token|client[_-]?secret|'
            r'authorization|bearer)\b(\s*[:=]\s*|\s+)(["\']?)'
            r'(?:bearer\s+)?[A-Za-z0-9_./+=-]{16,}\3',
            r'\1\2********',
            message,
        )
        # GitHub・OpenAI・Google などで使われる既知のキー接頭辞を伏せる
        message = re.sub(
            r'\b(?:gh[pousr]_[A-Za-z0-9_]{20,}|sk-[A-Za-z0-9_-]{20,}|'
            r'AIza[A-Za-z0-9_-]{20,})\b',
            '********',
            message,
        )

        # --- 長大なログメッセージを切り詰め（APIエラーレスポンス全文等の対策） ---
        if len(message) > self.MAX_SINGLE_LOG_LENGTH:
            message = message[:self.MAX_SINGLE_LOG_LENGTH] + " ...[truncated]"

        return message

    async def _process_queue(self):
        """
        キューに溜まったログを全て取り出し、個々のログが途切れないようにチャンク分けして送信する。
        """
        # シャットダウン後は送信しない
        if self._closed:
            return
        if self.queue.empty():
            return

        # チャンネルの存在確認と更新
        if not self.channels or len(self.channels) != len(self.channel_ids):
            found_channels = []
            channels_to_remove = []

            for cid in self.channel_ids:
                channel = self.bot.get_channel(cid)
                if channel and isinstance(channel, TextChannel):
                    found_channels.append(channel)
                    # 成功したら失敗カウントをリセット
                    self.invalid_channel_attempts.pop(cid, None)
                else:
                    # チャンネルが見つからない場合、失敗カウントを増やす
                    self.invalid_channel_attempts[cid] = self.invalid_channel_attempts.get(cid, 0) + 1

                    if self.invalid_channel_attempts[cid] >= self.max_attempts:
                        # 規定回数失敗したら削除対象に
                        channels_to_remove.append(cid)
                        print(f"DiscordLogHandler: ⚠️ Channel {cid} not found {self.max_attempts} times consecutively.")
                    else:
                        print(
                            f"DiscordLogHandler: Warning - Channel with ID {cid} not found or is not a text channel. (Attempt {self.invalid_channel_attempts[cid]}/{self.max_attempts})")

            self.channels = found_channels

            # 無効なチャンネルを削除
            for cid in channels_to_remove:
                await self._remove_invalid_channel(cid, f"Channel not found after {self.max_attempts} attempts")

        if not self.channels:
            if self.channel_ids:
                print(f"DiscordLogHandler: No valid channels found for IDs {self.channel_ids}. Clearing log queue.")
            while not self.queue.empty():
                self.queue.get_nowait()
            return

        # キューから全てのログを取得
        records = []
        while not self.queue.empty():
            records.append(self.queue.get_nowait())
        if not records:
            return

        # ログを1つのコードブロック内にまとめてチャンク分けする
        chunks = []
        current_logs = []
        # コードブロックのオーバーヘッド: ```ansi\n と \n``` で13文字
        CODE_BLOCK_OVERHEAD = 13
        # Discordの制限2000文字ギリギリを狙う（安全のため少し余裕を持たせる）
        CHUNK_LIMIT = 1990

        for record in records:
            # 改行付きでログを追加した場合のサイズを計算
            log_with_newline = record if not current_logs else f"\n{record}"

            # 現在のログ群 + 新しいログ + コードブロックのオーバーヘッド
            potential_size = sum(len(log) for log in current_logs) + len(log_with_newline) + CODE_BLOCK_OVERHEAD
            if current_logs:
                potential_size += len(current_logs) - 1  # 既存ログ間の改行分

            # 1つのログ自体が制限を超える場合
            if len(record) + CODE_BLOCK_OVERHEAD > CHUNK_LIMIT:
                # 現在のチャンクを確定
                if current_logs:
                    chunks.append("```ansi\n" + "\n".join(current_logs) + "\n```")
                    current_logs = []

                # 長いログを分割して送信（コードブロックなしで）
                for i in range(0, len(record), CHUNK_LIMIT):
                    chunk_part = record[i:i + CHUNK_LIMIT]
                    # 最初の部分にはコードブロック開始、最後の部分には終了を付ける
                    if i == 0:
                        chunk_part = "```ansi\n" + chunk_part
                    if i + CHUNK_LIMIT >= len(record):
                        chunk_part = chunk_part + "\n```"
                    chunks.append(chunk_part)
                continue

            # 追加するとチャンクサイズを超える場合
            if potential_size > CHUNK_LIMIT:
                # 現在のチャンクを確定して新しいチャンクを開始
                chunks.append("```ansi\n" + "\n".join(current_logs) + "\n```")
                current_logs = [record]
            else:
                # 現在のチャンクに追加
                current_logs.append(record)

        # 最後のチャンクを追加
        if current_logs:
            chunks.append("```ansi\n" + "\n".join(current_logs) + "\n```")

        # 全てのチャンネルに、作成したチャンクを送信
        channels_to_remove = []
        for channel in self.channels:
            send_success = False
            for chunk in chunks:
                if not chunk.strip():
                    continue
                try:
                    # タイムアウト付きで送信（discord.py内部の5xxリトライによる長時間ブロックを防止）
                    await asyncio.wait_for(
                        channel.send(chunk, silent=True),
                        timeout=SEND_TIMEOUT_SECONDS
                    )
                    send_success = True
                    # チャンク間の送信にわずかな遅延を入れ、レートリミットを回避
                    await asyncio.sleep(0.2)
                except discord.errors.Forbidden:
                    # 権限エラー: チャンネルが削除されたか、アクセス権がない（永続的エラー → 即削除）
                    print(f"DiscordLogHandler: ⚠️ No permission to send to channel {channel.id}. Marking for removal.")
                    channels_to_remove.append((channel.id, "Forbidden: No permission to send messages"))
                    break
                except discord.errors.NotFound:
                    # チャンネルが見つからない（削除された）（永続的エラー → 即削除）
                    print(f"DiscordLogHandler: ⚠️ Channel {channel.id} not found (deleted?). Marking for removal.")
                    channels_to_remove.append((channel.id, "NotFound: Channel has been deleted"))
                    break
                except discord.errors.HTTPException as e:
                    # Discord API エラーをステータスコードで分類
                    if 500 <= e.status < 600:
                        # 5xx系: Discordサーバー側の一時的エラー → チャンネル削除カウント増やさない
                        print(
                            f"DiscordLogHandler: ⚠️ Temporary Discord API error on channel {channel.id}: "
                            f"{e.status} {e.text}. Skipping this cycle."
                        )
                    elif e.status == 400:
                        # 400 Bad Request: 送信内容の問題（コードブロック破壊等）
                        # チャンネル自体は正常なので削除カウントは増やさない
                        print(
                            f"DiscordLogHandler: ⚠️ Bad Request on channel {channel.id}: "
                            f"message content may be malformed. Dropping this batch."
                        )
                    else:
                        # その他の4xx系（Forbidden/NotFound以外）: 失敗カウント加算
                        print(f"DiscordLogHandler: Discord API error on channel {channel.id}: {e.status} {e.text}")
                        if not send_success:
                            self.invalid_channel_attempts[channel.id] = (
                                self.invalid_channel_attempts.get(channel.id, 0) + 1
                            )
                            if self.invalid_channel_attempts[channel.id] >= self.max_attempts:
                                channels_to_remove.append(
                                    (channel.id, f"HTTPException {e.status} after {self.max_attempts} attempts")
                                )
                    break
                except asyncio.TimeoutError:
                    # channel.send() がタイムアウト（discord.py内部リトライで長時間ブロック等）
                    # 一時的な問題のためチャンネル削除カウントは増やさない
                    print(
                        f"DiscordLogHandler: ⚠️ Timed out sending to channel {channel.id} "
                        f"(>{SEND_TIMEOUT_SECONDS}s). Skipping this cycle."
                    )
                    break
                except (OSError, ConnectionError) as e:
                    # ネットワーク接続エラー: 一時的な問題なのでチャンネル削除カウントは増やさない
                    print(
                        f"DiscordLogHandler: ⚠️ Network error sending to channel {channel.id}: "
                        f"{type(e).__name__}: {e}. Skipping this cycle."
                    )
                    break
                except RuntimeError as e:
                    # Bot シャットダウン後の aiohttp Session is closed 等は黙って打ち切る
                    if "Session is closed" in str(e) or self._closed:
                        break
                    print(f"Failed to send log to Discord channel {channel.id}: RuntimeError: {e}")
                    break
                except Exception as e:
                    # 予期しないエラー: 失敗カウント加算
                    print(f"Failed to send log to Discord channel {channel.id}: {type(e).__name__}: {e}")
                    if not send_success:
                        # 1つも送信できなかった場合のみ失敗カウントを増やす
                        self.invalid_channel_attempts[channel.id] = self.invalid_channel_attempts.get(channel.id, 0) + 1
                        if self.invalid_channel_attempts[channel.id] >= self.max_attempts:
                            channels_to_remove.append(
                                (channel.id, f"Failed to send after {self.max_attempts} attempts: {str(e)}"))
                    break

            # 成功したらカウントリセット
            if send_success:
                self.invalid_channel_attempts.pop(channel.id, None)

        # 無効なチャンネルを削除
        for channel_id, reason in channels_to_remove:
            await self._remove_invalid_channel(channel_id, reason)

    async def _log_sender_loop(self):
        """
        バックグラウンドで定期的にキュー処理を呼び出すループ。
        個別の処理エラーではループを終了せず、ログ出力して継続する。
        """
        try:
            await self.bot.wait_until_ready()
            while not self._closed:
                try:
                    await self._process_queue()
                except asyncio.CancelledError:
                    # キャンセルはそのまま伝播させてループを終了
                    raise
                except Exception as e:
                    # 個別のキュー処理エラーではループを終了しない
                    # （NameError、接続エラー等もここで安全にキャッチ）
                    print(f"DiscordLogHandler: Error in _process_queue (continuing): {e}")
                await asyncio.sleep(self.interval)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            # ループ全体の致命的エラー（通常到達しない）
            print(f"DiscordLogHandler: Fatal error in log sender loop: {e}")
        finally:
            # ループ終了時に残っているログを送信（既に閉じていればスキップ）
            if not self._closed:
                try:
                    await self._process_queue()
                except Exception:
                    pass

    def close(self):
        """ハンドラを閉じる。"""
        # 二重 close を無視する
        if self._closed:
            return
        # 以降の emit / 送信ループを止める
        self._closed = True
        # バックグラウンドタスクがあればキャンセルする
        if self._task:
            try:
                self._task.cancel()
            except Exception:
                pass
            self._task = None
        # bot クローズ後は loop が _MissingSentinel になるため安全に確認する
        loop = getattr(self.bot, "loop", None)
        # is_running を持つ本物のループだけ残ログ送信を試みる
        if loop is not None and hasattr(loop, "is_running"):
            try:
                # ループが生きていれば残キューを送る
                if loop.is_running():
                    future = asyncio.run_coroutine_threadsafe(self._process_queue(), loop)
                    # タイムアウト付きで待つ
                    future.result(timeout=self.interval)
            except (asyncio.CancelledError, asyncio.TimeoutError, RuntimeError, Exception):
                # シャットダウン中の失敗は無視する
                pass
        # 親 Handler の close を呼ぶ
        try:
            super().close()
        except Exception:
            # atexit 経路でも落とさない
            pass
