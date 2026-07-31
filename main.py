import asyncio
import json
import logging
import os
import sys

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks

from MOMOKA.GUI import attach_gui_logging, run_log_viewer_thread, set_bot_ref, set_dark_mode
from MOMOKA.storage import NS_LOGGING_CHANNELS, get_default_settings_db
from MOMOKA.utilities.command_i18n import CommandDescriptionTranslator
from MOMOKA.utilities.restart_notice import SHUTDOWN_USER_ID
from MOMOKA.version import status_version_string

# Python 3.11 未満では依存パッケージ（discord.py 2.7 / torch 等）の動作保証外のため起動を拒否する
if sys.version_info < (3, 11):
    # 現在のインタプリタバージョンをユーザー向けに表示する
    _current = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    # 標準エラーへ要件を明示して終了する
    sys.stderr.write(
        f"MOMOKA requires Python 3.11 or higher (detected {_current}).\n"
        "Please recreate the virtual environment with Python 3.11 "
        "(e.g. `py -3.11 -m venv .venv` or startMOMOKA.bat).\n"
    )
    # 非ゼロ終了コードでプロセスを終了する
    sys.exit(1)


def _preload_torch() -> None:
    """venv の torch を最優先で読み込み、C10 二重登録を防ぐ。

    PATH 上に別環境の ``torch\\lib``（例: グローバル Python 3.11 の torch 2.7）が
    残っていると、c10.dll が二重ロードされ
    ``Key already registered with the same priority: C10`` で即終了することがある。
    Discord ログイン直後の Cog（TTS / 画像）読込より前に確定させる。
    """
    # 現在の PATH を分割する
    path_parts = os.environ.get("PATH", "").split(os.pathsep)
    # 他環境の torch\\lib を除外した PATH を組み立てる
    filtered_parts = []
    for part in path_parts:
        # パス比較用に区切りを正規化する
        norm = part.replace("/", "\\").lower()
        # site-packages\\torch\\lib を含むエントリは衝突源なので捨てる
        if "\\torch\\lib" in norm:
            continue
        # 残すパスを追加する
        filtered_parts.append(part)
    # 浄化した PATH を書き戻す
    os.environ["PATH"] = os.pathsep.join(filtered_parts)
    try:
        # TTS / diffusers より先に torch 本体をロードする
        import torch  # noqa: F401
    except ImportError:
        # 画像・TTS 無効構成でも起動できるよう欠落は無視する
        return


# Cog 読込より前に torch を確定する（C10 衝突回避）
_preload_torch()

# Windows ダークモード（ログビューア用）を有効化する
set_dark_mode()

# --- ロギング設定の初期化 ---
# ルートロガーの設定
root_logger = logging.getLogger()
root_logger.setLevel(logging.INFO)

# 特定のロガーのログレベル設定
logging.getLogger("discord").setLevel(logging.WARNING)
logging.getLogger("openai").setLevel(logging.WARNING)
logging.getLogger("google.generativeai").setLevel(logging.WARNING)
logging.getLogger("google.ai").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("asyncio").setLevel(logging.WARNING)
logging.getLogger("PIL").setLevel(logging.WARNING)


class _VoiceWsTimeoutLogFilter(logging.Filter):
    """discord.voice_state の WS TimeoutError 再接続ログを1行 WARN に圧縮する。"""

    def filter(self, record: logging.LogRecord) -> bool:
        # 対象メッセージ以外はそのまま通す
        if "Disconnected from voice" not in record.msg:
            return True
        # 再接続待ちログ以外はそのまま通す
        if "Reconnecting" not in record.msg:
            return True
        # 例外種別を取り出す（無い場合は対象外）
        exc = record.exc_info[1] if record.exc_info else None
        # TimeoutError 以外（ConnectionClosed 等）は ERROR+traceback のまま残す
        if not isinstance(exc, TimeoutError):
            return True
        # ERROR を WARNING へ落とす
        record.levelno = logging.WARNING
        # レベル名も合わせて書き換え
        record.levelname = "WARNING"
        # traceback を出さない
        record.exc_info = None
        # 整形済み traceback キャッシュも消す
        record.exc_text = None
        # 1行メッセージに差し替える（args の retry 秒を再利用）
        record.msg = "Voice WS timeout; reconnecting in %.2fs."
        return True


# VC 制御 WS の一時切断 traceback を抑制する
logging.getLogger("discord.voice_state").addFilter(_VoiceWsTimeoutLogFilter())

# GUI ログキューへルートロガー / stdout を接続する
# 注意: DiscordLogHandler は setup_hook 内で追加されるため、GUI と Discord の両方にログが送信される
log_queue, queue_handler, stdout_capture = attach_gui_logging(root_logger)

from MOMOKA.services.discord_handler import DiscordLogHandler, DiscordLogFormatter

class Momoka(commands.Bot):
    """MOMOKA Botのメインクラス"""

    # /shutdown を実行できるユーザー ID（ハードコード定数）
    SHUTDOWN_USER_ID = SHUTDOWN_USER_ID

    def __init__(
        self,
        *args,
        config: dict,
        bot_id: str,
        bot_role: str,
        persona_key: str,
        display_name: str,
        cogs_to_load: list,
        enable_discord_logging: bool = True,
        **kwargs
    ):
        super().__init__(*args, **kwargs)
        # マージ済み設定辞書を保持する
        self.config = config
        # ボット識別子（例: "plana", "arona"）
        self.bot_id = bot_id
        # ボット役割（"primary" or "companion"）
        self.bot_role = bot_role
        # ペルソナキー（例: "plana", "arona"）
        self.persona_key = persona_key
        # 表示名（例: "[PLANA]", "[ARONA]"）
        self.display_name = display_name
        # Discord ログ出力を有効化するか（primary のみ True）
        self.enable_discord_logging = enable_discord_logging
        # ステータスローテーション用テンプレートリスト
        self.status_templates = []
        # ステータスローテーション用インデックス
        self.status_index = 0
        # ロードする Cog のリスト（primary / companion で異なる）
        self.cogs_to_load = cogs_to_load
        # ランタイム設定用 SQLite（プロセス共通インスタンス）
        self.settings_db = get_default_settings_db()

    def is_admin(self, user_id: int) -> bool:
        """ユーザーが管理者かどうかをチェック"""
        # 設定値が文字列でも Discord の整数 ID と比較できるよう正規化する
        admin_ids = {
            int(admin_id)
            for admin_id in self.config.get('admin_user_ids', [])
            if str(admin_id).isdigit()
        }
        return user_id in admin_ids

    async def notify_active_users_of_restart(self) -> None:
        """利用中ユーザー（音楽・LLM）へ再起動通知を送る。"""
        # 重い Cog モジュールはメソッド内で参照する（起動時の循環を避ける）
        from MOMOKA.llm.llm_cog import LLMCog
        from MOMOKA.music.music_cog import MusicCog

        # 並列通知用のコルーチン一覧
        tasks = []
        # 音楽 Cog を名前で取得する
        music_cog = self.get_cog(MusicCog.COG_NAME)
        # 通知メソッドがある場合のみキューに入れる
        if music_cog is not None and hasattr(music_cog, "notify_admin_restart"):
            # MusicCog の再起動通知をキューへ追加する
            tasks.append(music_cog.notify_admin_restart())
        # LLM Cog を名前で取得する
        llm_cog = self.get_cog(LLMCog.COG_NAME)
        # 通知メソッドがある場合のみキューに入れる
        if llm_cog is not None and hasattr(llm_cog, "notify_admin_restart"):
            # LLMCog の再起動通知をキューへ追加する
            tasks.append(llm_cog.notify_admin_restart())
        # 対象が無ければ何もしない
        if not tasks:
            # 早期リターン
            return
        # 各 Cog の通知を並行実行し、例外は個別に収集する
        results = await asyncio.gather(*tasks, return_exceptions=True)
        # 失敗した通知だけログに残す
        for result in results:
            # 例外オブジェクトかどうかを判定する
            if isinstance(result, Exception):
                # 通知失敗を警告ログへ出す
                logging.warning("Failed to notify active users of restart: %s", result)

    async def close(self) -> None:
        """終了前に VC 状態を保存し、利用中ユーザーへ通知してから接続を閉じる。"""
        try:
            # 音楽 Cog があれば再起動用に VC セッションを先に保存する
            from MOMOKA.music.music_cog import MusicCog
            # Cog を名前で取得する
            music_cog = self.get_cog(MusicCog.COG_NAME)
            # persist メソッドがある場合のみ保存する
            if music_cog is not None and hasattr(
                music_cog, "persist_vc_sessions_for_restart"
            ):
                # notify より前にスナップショットを取る（notify は current_track を消す）
                await music_cog.persist_vc_sessions_for_restart()
        except Exception as e:
            # 保存失敗でもシャットダウン自体は継続する
            logging.warning(
                "%s persist_vc_sessions_for_restart failed during close: %s",
                self.display_name,
                e,
            )
        try:
            # Ctrl+C /shutdown 共通で再起動通知を送る
            await self.notify_active_users_of_restart()
        except Exception as e:
            # 通知失敗でもシャットダウン自体は継続する
            logging.warning(
                "%s notify_active_users_of_restart failed during close: %s",
                self.display_name,
                e
            )
        # Discord ログ Handler を先に外して Session is closed を防ぐ
        try:
            # ルートロガーのハンドラ一覧を走査する
            root = logging.getLogger()
            # DiscordLogHandler だけ取り外す
            for handler in list(root.handlers):
                # クラス名で判定する（循環 import 回避）
                if handler.__class__.__name__ == "DiscordLogHandler":
                    # ハンドラを閉じる
                    try:
                        handler.close()
                    except Exception:
                        pass
                    # ルートから除去する
                    root.removeHandler(handler)
        except Exception as e:
            # ハンドラ除去失敗はシャットダウンを止めない
            logging.debug("%s DiscordLogHandler detach failed: %s", self.display_name, e)
        # discord.py 本来のクローズ処理へ進む
        await super().close()

    async def setup_hook(self):
        """Botの初期セットアップ（ログイン後、接続準備完了前）"""
        # 実行中の Python バージョンを起動ログへ残す（3.11 前提の切り分け用）
        logging.info(
            "%s Runtime Python %s.%s.%s (MOMOKA requires 3.11.x)",
            self.display_name,
            sys.version_info.major,
            sys.version_info.minor,
            sys.version_info.micro,
        )

        # self.config は __init__ で渡されているため、ファイル読み込みは不要
        logging.info("%s マージ済み設定を使用して起動します。", self.display_name)

        # ステータスローテーションの設定を取得する（日付は最終 git コミット日）
        self.status_templates = self.config.get('status_rotation', [
            "operating on {guild_count} servers",
            status_version_string(),
        ])
        # ステータスローテーションタスクを開始する
        self.rotate_status.start()

        # Discord ログ出力は primary のみ有効化する
        if self.enable_discord_logging:
            # 設定辞書からログチャンネル ID リストを取得する
            log_channel_ids_from_config = self.config.get('log_channel_ids', [])
            # リスト形式でなければ空リストに戻す
            if not isinstance(log_channel_ids_from_config, list):
                log_channel_ids_from_config = []
                logging.warning(
                    "%s configs/core_config.yaml の 'log_channel_ids' はリスト形式である必要があります。",
                    self.display_name
                )

            # SQLite から追加のチャンネル ID を読み込む
            log_channel_ids_from_db = []
            try:
                # settings DB から logging_channels namespace を取る
                data = self.settings_db.load(NS_LOGGING_CHANNELS)
                # リスト形式かつ全要素が int であれば採用する
                if isinstance(data, list) and all(isinstance(i, int) for i in data):
                    log_channel_ids_from_db = data
                elif data is None:
                    # 未移行時は空で起動する（移行スクリプト実行を前提）
                    logging.info(
                        "%s logging_channels が DB に無いため空リストで起動します。",
                        self.display_name,
                    )
            except Exception as e:
                logging.error(
                    "%s logging_channels の読込中にエラーが発生しました: %s",
                    self.display_name,
                    e,
                )

            # 設定ファイルと DB のチャンネル ID を統合する
            all_log_channel_ids = list(set(log_channel_ids_from_config + log_channel_ids_from_db))

            # チャンネル ID が設定されていれば Discord ログハンドラを追加する
            if all_log_channel_ids:
                try:
                    # Discord ログハンドラを作成する
                    discord_handler = DiscordLogHandler(
                        bot=self,
                        channel_ids=all_log_channel_ids,
                        interval=6.0,
                    )
                    # ログレベルを INFO に設定する
                    discord_handler.setLevel(logging.INFO)
                    # フォーマッタを設定する
                    discord_formatter = DiscordLogFormatter('%(asctime)s - %(levelname)s - [%(funcName)s] %(message)s')
                    discord_handler.setFormatter(discord_formatter)
                    # ルートロガーへハンドラを追加する
                    root_logger.addHandler(discord_handler)
                    logging.info(
                        "%s Discord へのロギングをチャンネル ID %s で有効化しました。",
                        self.display_name,
                        all_log_channel_ids
                    )
                except Exception as e:
                    logging.error("%s DiscordLogHandler の初期化中にエラーが発生しました: %s", self.display_name, e)
            else:
                logging.warning("%s ログ送信先の Discord チャンネルが設定されていません。", self.display_name)

        # Cog のロードを開始する
        logging.info("%s Cog のロードを開始します...", self.display_name)
        loaded_cogs_count = 0
        # Cog リストを順番にロードする
        for module_path in self.cogs_to_load:
            try:
                # Cog をロードする
                await self.load_extension(module_path)
                logging.info("%s   > Cog '%s' のロードに成功しました。", self.display_name, module_path)
                # ロード成功数を加算する
                loaded_cogs_count += 1
            except commands.ExtensionAlreadyLoaded:
                logging.debug("%s Cog '%s' は既にロードされています。", self.display_name, module_path)
            except commands.ExtensionNotFound:
                logging.error(
                    "%s   > Cog '%s' が見つかりません。ファイルパスを確認してください。",
                    self.display_name,
                    module_path
                )
            except commands.NoEntryPointError:
                logging.error(
                    "%s   > Cog '%s' に setup 関数が見つかりません。Cog として正しく実装されていますか？",
                    self.display_name,
                    module_path
                )
            except Exception as e:
                logging.error(
                    "%s   > Cog '%s' のロード中に予期しないエラーが発生しました: %s",
                    self.display_name,
                    module_path,
                    e,
                    exc_info=True
                )
        logging.info(
            "%s Cog のロードが完了しました。合計 %d 個の Cog をロードしました。",
            self.display_name,
            loaded_cogs_count
        )

        # companion ボットは同期タイミングをずらす（rate limit 回避）
        if self.bot_role == "companion":
            # 2秒待機して primary の同期を先に行わせる
            await asyncio.sleep(2)

        # description 多言語 Translator を sync 前に登録する（name は英語固定）
        try:
            # YAML カタログベースの Translator をセットする
            await self.tree.set_translator(CommandDescriptionTranslator(self.config))
            # 登録成功をログに残す
            logging.info("%s コマンド description Translator を登録しました。", self.display_name)
        except Exception as e:
            # Translator 失敗でも起動は継続する（英語デフォルトのみ）
            logging.error(
                "%s コマンド description Translator の登録に失敗しました: %s",
                self.display_name,
                e,
                exc_info=True,
            )

        # スラッシュコマンドの同期を行う
        if self.config.get('sync_slash_commands', True):
            try:
                # テストギルド ID が設定されているか確認する
                test_guild_id = self.config.get('test_guild_id')
                if test_guild_id:
                    # テストギルドへグローバルコマンドを複製して同期する
                    guild_obj = discord.Object(id=int(test_guild_id))
                    self.tree.copy_global_to(guild=guild_obj)
                    synced_commands = await self.tree.sync(guild=guild_obj)
                    logging.info(
                        "%s %d 個のスラッシュコマンドをテストギルド %s に同期しました。",
                        self.display_name,
                        len(synced_commands),
                        test_guild_id
                    )
                else:
                    # グローバルに同期する
                    synced_commands = await self.tree.sync()
                    logging.info(
                        "%s %d 個のグローバルスラッシュコマンドを同期しました。",
                        self.display_name,
                        len(synced_commands)
                    )
            except Exception as e:
                logging.error(
                    "%s スラッシュコマンドの同期中にエラーが発生しました: %s",
                    self.display_name,
                    e,
                    exc_info=True
                )
        else:
            logging.info("%s スラッシュコマンドの同期は設定で無効化されています。", self.display_name)

        # エラーハンドラを設定する
        self.tree.on_error = self.on_app_command_error

    @tasks.loop(seconds=15)
    async def rotate_status(self):
        """ボットのステータスを定期的に変更する"""
        if not self.status_templates:
            return

        # 次のステータスを選択
        status_template = self.status_templates[self.status_index]
        self.status_index = (self.status_index + 1) % len(self.status_templates)

        # プレースホルダーを置換
        try:
            status_text = status_template.format(guild_count=len(self.guilds))
        except KeyError:
            status_text = status_template  # プレースホルダーがない場合はそのまま使用

        # 配信 URL の設定を取得し、未設定時は公式の既定 URL を使う
        presence_config = self.config.get("presence", {})
        streaming_url = presence_config.get(
            "streaming_url",
            "https://www.twitch.tv/coffinnoob299",
        )

        # 配信中ステータスを更新
        try:
            await self.change_presence(
                activity=discord.Streaming(name=status_text, url=streaming_url)
            )
        except (aiohttp.client_exceptions.ClientConnectionResetError, ConnectionResetError) as e:
            logging.warning(f"Failed to rotate status due to connection reset: {e}")
        except Exception as e:
            logging.error(f"Failed to rotate status: {e}")

    @rotate_status.before_loop
    async def before_rotate_status(self):
        """ステータスローテーションタスクの開始を待機"""
        await self.wait_until_ready()

    async def on_command_error(self, ctx: commands.Context, error: commands.CommandError):
        """通常コマンド（プレフィックスコマンド）のエラーハンドリング"""
        # CommandNotFoundエラーは無視（コマンドを探すモードを無効化）
        if isinstance(error, commands.CommandNotFound):
            return  # エラーを無視して何もしない
        
        # その他のエラーはログに記録（必要に応じて処理）
        logging.debug(f"コマンドエラー: {error}")

    async def _send_app_command_error(
        self,
        interaction: discord.Interaction,
        message: str,
    ) -> None:
        """スラッシュコマンドのエラー応答を重複なく送信する。"""
        # 初回応答済みなら followup を使い、二重応答エラーを防ぐ
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
            return
        # 未応答なら interaction の初回応答として送信する
        await interaction.response.send_message(message, ephemeral=True)

    async def on_app_command_error(
        self,
        interaction: discord.Interaction,
        error: app_commands.AppCommandError,
    ):
        """スラッシュコマンドのエラーハンドリング"""
        # InvokeError は元例外を取り出して適切なエラー種別で判定する
        original_error = (
            error.original
            if isinstance(error, app_commands.CommandInvokeError)
            else error
        )

        if isinstance(original_error, app_commands.MissingPermissions):
            await self._send_app_command_error(
                interaction,
                "❌ このコマンドを実行する権限がありません。",
            )
        elif isinstance(
            original_error,
            (app_commands.BotMissingPermissions, discord.Forbidden),
        ):
            await self._send_app_command_error(
                interaction,
                "❌ ボットに必要な権限がありません。管理者に連絡してください。",
            )
        elif isinstance(original_error, app_commands.CommandOnCooldown):
            await self._send_app_command_error(
                interaction,
                "⏳ このコマンドは "
                f"{original_error.retry_after:.1f} 秒後に再試行できます。",
            )
        elif isinstance(original_error, app_commands.CheckFailure):
            return
        else:
            # 未分類の例外は原因を記録し、利用者には共通メッセージだけを返す
            logging.error("コマンドエラー: %s", original_error, exc_info=original_error)
            await self._send_app_command_error(
                interaction,
                "❌ コマンドの実行中にエラーが発生しました。",
            )


# CONFIG_FILE / DEFAULT_CONFIG_FILE は削除済み — configs/*.yaml を使用

# ===============================================================
# ===== Cog ロードリスト ========================================
# ===============================================================
# プライマリボット（PLANA）が読み込む全 Cog
PRIMARY_COGS = [
    'MOMOKA.count.count_cog',
    'MOMOKA.images.image_commands_cog',
    'MOMOKA.llm.llm_cog',
    'MOMOKA.link_fix.link_fix_cog',
    'MOMOKA.media_downloader.ytdlp_downloader_cog',
    'MOMOKA.music.music_cog',
    'MOMOKA.notifications.earthquake_notification_cog',
    'MOMOKA.notifications.twitch_notification_cog',
    'MOMOKA.tracker.r6s_tracker_cog',
    'MOMOKA.tracker.valorant_tracker_cog',
    'MOMOKA.tts.tts_cog',
    'MOMOKA.utilities.guild_events_cog',
    'MOMOKA.utilities.slash_command_cog',
]

# コンパニオンボット（ARONA）が読み込む軽量 Cog（LLM / 音楽 / スラッシュコマンド）
COMPANION_COGS = [
    'MOMOKA.llm.llm_cog',
    'MOMOKA.music.music_cog',
    'MOMOKA.utilities.guild_events_cog',
    'MOMOKA.utilities.slash_command_cog',
]

# ===============================================================

if __name__ == "__main__":
    momoka_art = r"""
███╗   ███╗ ██████╗ ███╗   ███╗ ██████╗ ██╗  ██╗ █████╗ 
████╗ ████║██╔═══██╗████╗ ████║██╔═══██╗██║ ██╔╝██╔══██╗
██╔████╔██║██║   ██║██╔████╔██║██║   ██║█████╔╝ ███████║
██║╚██╔╝██║██║   ██║██║╚██╔╝██║██║   ██║██╔═██╗ ██╔══██║
██║ ╚═╝ ██║╚██████╔╝██║ ╚═╝ ██║╚██████╔╝██║  ██╗██║  ██║
╚═╝     ╚═╝ ╚═════╝ ╚═╝     ╚═╝ ╚═════╝ ╚═╝  ╚═╝╚═╝  ╚═╝
    """
    print(momoka_art)
    
    # ログビューアをスレッドで起動する
    log_viewer_thread = run_log_viewer_thread(log_queue)
    print("ホスト GUI（Electron）を起動しました。")

    # --- 設定の読み込みとバリデーション ---
    from MOMOKA.config.loader import load_merged_config, validate_bot_tokens
    from MOMOKA.bots.registry import registry
    from MOMOKA.llm.concurrency import init_concurrency
    from MOMOKA.services.bgutil_provider import (
        BgutilProviderConfig,
        BgutilProviderManager,
    )

    # configs/*.yaml を統合した設定辞書を読み込む
    try:
        # マージ済み設定辞書を取得する
        merged_config = load_merged_config()
        print("INFO: configs/bots_config.yaml などの設定を統合しました。")
    except Exception as e_load:
        print(f"CRITICAL: 設定ファイルの読み込み中にエラーが発生しました: {e_load}")
        sys.exit(1)

    # ボットトークンの存在確認を行う
    try:
        # PLANA / ARONA のトークンをバリデーションする
        validate_bot_tokens(merged_config)
        print("INFO: ボットトークンのバリデーションに成功しました。")
    except ValueError as e_token:
        print(f"CRITICAL: {e_token}")
        sys.exit(1)

    # マージ済み設定から共有PO Token Provider設定を構築する。
    bgutil_config = BgutilProviderConfig.from_mapping(merged_config)
    # PLANA/ARONAで共有するProvider Managerを一度だけ生成する。
    bgutil_manager = BgutilProviderManager(bgutil_config)

    # 通常チャット / 討論の並列背圧を初期化する
    try:
        # Chat と Debate で枠を分離する
        init_concurrency(merged_config)
        print("INFO: LLM 並列背圧（concurrency）を初期化しました。")
    except Exception as e_conc:
        print(f"CRITICAL: concurrency 初期化中にエラーが発生しました: {e_conc}")
        sys.exit(1)

    # --- Discord Intents / AllowedMentions の設定 ---
    # 両ボット共通の Intents を作成する
    intents = discord.Intents.default()
    intents.guilds = True
    intents.guild_messages = True
    intents.dm_messages = True
    intents.voice_states = True
    # Message Content Intent（特権）を明示ON — Developer Portal側も両Botで有効必須
    intents.message_content = True
    intents.members = False
    intents.presences = False

    # メンション設定を作成する
    allowed_mentions = discord.AllowedMentions(everyone=False, users=True, roles=False, replied_user=True)

    # --- PLANA（プライマリボット）の作成 ---
    # PLANA の設定ブロックを取得する
    plana_bot_config = merged_config['bots']['plana']
    # PLANA のトークンを取得する
    plana_token = plana_bot_config['token']
    # PLANA のペルソナキーを取得する
    plana_persona = plana_bot_config.get('persona', 'plana')
    # PLANA の表示名を取得する
    plana_display = plana_bot_config.get('display_name', 'PLANA')

    # PLANA ボットインスタンスを作成する
    plana_bot = Momoka(
        command_prefix=commands.when_mentioned,
        intents=intents,
        help_command=None,
        allowed_mentions=allowed_mentions,
        config=merged_config,
        bot_id='plana',
        bot_role='primary',
        persona_key=plana_persona,
        display_name=plana_display,
        cogs_to_load=PRIMARY_COGS,
        enable_discord_logging=True,  # primary のみ Discord へログ出力
    )
    # レジストリへ PLANA を登録する
    registry.register('plana', plana_bot, plana_display)
    print(f"INFO: PLANA ボットを作成し、レジストリへ登録しました。")

    # --- ARONA（コンパニオンボット）の作成 ---
    # ARONA の設定ブロックを取得する
    arona_bot_config = merged_config['bots']['arona']
    # ARONA のトークンを取得する
    arona_token = arona_bot_config['token']
    # ARONA のペルソナキーを取得する
    arona_persona = arona_bot_config.get('persona', 'arona')
    # ARONA の表示名を取得する
    arona_display = arona_bot_config.get('display_name', 'ARONA')

    # ARONA ボットインスタンスを作成する
    arona_bot = Momoka(
        command_prefix=commands.when_mentioned,
        intents=intents,
        help_command=None,
        allowed_mentions=allowed_mentions,
        config=merged_config,
        bot_id='arona',
        bot_role='companion',
        persona_key=arona_persona,
        display_name=arona_display,
        cogs_to_load=COMPANION_COGS,
        enable_discord_logging=False,  # companion は Discord ログ出力しない
    )
    # レジストリへ ARONA を登録する
    registry.register('arona', arona_bot, arona_display)
    print(f"INFO: ARONA ボットを作成し、レジストリへ登録しました。")

    # GUI 稼働モニタ / シャットダウンボタンから参照できるように PLANA を共有する
    set_bot_ref(plana_bot)

    # ===============================================================
    # ===== シャットダウン / Cogリロードコマンド（PLANA のみ） =======
    # ===============================================================
    @plana_bot.tree.command(
        name="shutdown",
        description="Shut down both bots after notifying users (owner only).",
    )
    async def shutdown_command(interaction: discord.Interaction):
        # ハードコード UID 以外は拒否する
        if interaction.user.id != Momoka.SHUTDOWN_USER_ID:
            # 権限不足を ephemeral で返す
            await interaction.response.send_message(
                "❌ You are not allowed to use this command.",
                ephemeral=True,
            )
            # 処理終了
            return
        # シャットダウン開始を応答する
        await interaction.response.send_message(
            "Shutting down both bots... Active users will be notified.",
            ephemeral=False,
        )
        # 実行者をログに残す
        logging.info(
            "/shutdown was executed by user %s (%s)",
            interaction.user,
            interaction.user.id,
        )
        # レジストリの全ボットをクローズする（再起動通知を含む）
        await registry.close_all()

    @plana_bot.tree.command(name="reload_plana", description="Reload PLANA cogs (admin only).")
    async def reload_plana_cog(interaction: discord.Interaction, cog_name: str = None):
        # 管理者でなければ拒否する
        if not plana_bot.is_admin(interaction.user.id):
            await interaction.response.send_message("❌ このコマンドは管理者のみ実行できます。", ephemeral=False)
            return

        # リロード処理を開始する
        await interaction.response.defer(ephemeral=False)

        if cog_name:
            # 特定の Cog をリロードする
            if not cog_name.startswith('MOMOKA.'):
                # プレフィックスを補完する
                cog_name = f'MOMOKA.{cog_name}'

            try:
                # Cog をリロードする
                await plana_bot.reload_extension(cog_name)
                await interaction.followup.send(f"✅ Cog `{cog_name}` をリロードしました。", ephemeral=False)
                logging.info(f"Cog '{cog_name}' がユーザー {interaction.user} によってリロードされました。")
            except commands.ExtensionNotLoaded:
                # 未ロードの場合は新規ロードする
                try:
                    await plana_bot.load_extension(cog_name)
                    await interaction.followup.send(f"✅ Cog `{cog_name}` をロードしました（未ロードでした）。",
                                                    ephemeral=False)
                    logging.info(f"Cog '{cog_name}' がユーザー {interaction.user} によってロードされました。")
                except Exception as e:
                    await interaction.followup.send(f"❌ Cog `{cog_name}` のロードに失敗しました: {e}", ephemeral=False)
                    logging.error(f"Cog '{cog_name}' のロードに失敗しました: {e}")
            except Exception as e:
                await interaction.followup.send(f"❌ Cog `{cog_name}` のリロードに失敗しました: {e}", ephemeral=False)
                logging.error(f"Cog '{cog_name}' のリロードに失敗しました: {e}")
        else:
            # 全 Cog をリロードする
            reloaded = []
            failed = []

            # Cog リストをループする
            for module_path in plana_bot.cogs_to_load:
                try:
                    # Cog をリロードする
                    await plana_bot.reload_extension(module_path)
                    reloaded.append(module_path)
                except commands.ExtensionNotLoaded:
                    # 未ロードの場合は新規ロードする
                    try:
                        await plana_bot.load_extension(module_path)
                        reloaded.append(f"{module_path} (新規ロード)")
                    except Exception as e:
                        failed.append(f"{module_path}: {e}")
                except Exception as e:
                    failed.append(f"{module_path}: {e}")

            # 結果メッセージを作成する
            result_msg = f"✅ {len(reloaded)}個のCogをリロード/ロードしました。"
            if failed:
                result_msg += f"\n❌ {len(failed)}個のCogでエラーが発生しました。"

            await interaction.followup.send(result_msg, ephemeral=False)
            logging.info(
                f"全Cogリロードがユーザー {interaction.user} によって実行されました。成功: {len(reloaded)}, 失敗: {len(failed)}"
            )

    @plana_bot.tree.command(name="list_plana_cogs", description="List loaded PLANA cogs.")
    async def list_plana_cogs(interaction: discord.Interaction):
        # ロード済み拡張機能のリストを取得する
        loaded_extensions = list(plana_bot.extensions.keys())
        if not loaded_extensions:
            await interaction.response.send_message("現在ロードされているCogはありません。", ephemeral=False)
            return

        # Cog リストを整形する
        cog_list = "\n".join([f"• `{ext}`" for ext in sorted(loaded_extensions)])
        await interaction.response.send_message(
            f"**PLANA ロード済みCog一覧** ({len(loaded_extensions)}個):\n{cog_list}",
            ephemeral=False
        )

    # --- 両ボットの起動 ---
    async def run_bots():
        """PLANA と ARONA の両方を並行起動する。"""
        try:
            try:
                # Discord接続前にPO Token Providerを準備する。
                provider_ready = await bgutil_manager.start()
            except Exception as e_provider:
                # 必須設定では不完全な音楽機能で起動しない。
                if bgutil_config.required:
                    # 元の起動失敗を外側へ伝える。
                    raise
                # 任意設定ならProvider無しの劣化運転を選ぶ。
                provider_ready = False
                # 劣化運転を明確に警告する。
                logging.warning(
                    "BgUtils Provider is unavailable; continuing without it: %s",
                    e_provider,
                )
            # yt-dlpラッパーへProvider接続先を共有する。
            from MOMOKA.music.plugins.ytdlp_wrapper import (
                set_bgutil_provider_base_url,
            )
            # Provider利用可能時だけplugin URLを有効化する。
            set_bgutil_provider_base_url(
                bgutil_manager.base_url if provider_ready else None
            )
            # 両ボットを並行起動する
            await asyncio.gather(
                plana_bot.start(plana_token),
                arona_bot.start(arona_token),
            )
        except Exception as e_run:
            # 実行中のエラーをログに残す
            logging.critical(f"ボットの実行中に致命的なエラーが発生しました: {e_run}", exc_info=True)
            print(f"CRITICAL: ボットの実行中に致命的なエラーが発生しました: {e_run}")
            # 外側の終了コード処理へ失敗を伝える。
            raise
        finally:
            # キャンセル・Ctrl+Cを含む全経路で両Botを閉じる。
            await registry.close_all()
            # Bot停止後に自分が所有するProviderだけを停止する。
            await bgutil_manager.stop()
            # ホスト Electron GUI も落とす（コンソール pause 阻害防止）
            try:
                from MOMOKA.GUI.runner import stop_host_gui

                stop_host_gui()
            except Exception:
                pass

    # asyncio イベントループで両ボットを起動する
    try:
        asyncio.run(run_bots())
    except KeyboardInterrupt:
        # 既に run_bots 内で処理済み
        print("INFO: シャットダウン完了。")
    except Exception as e_main:
        logging.critical(f"メインループで致命的なエラーが発生しました: {e_main}", exc_info=True)
        print(f"CRITICAL: メインループで致命的なエラーが発生しました: {e_main}")
        sys.exit(1)
