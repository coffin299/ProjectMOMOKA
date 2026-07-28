# MOMOKA/music/errors/errors.py
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Dict, Any

import discord
from discord import app_commands
from discord.ext import commands

if TYPE_CHECKING:
    from MOMOKA.music.plugins.ytdlp_wrapper import Track

logger = logging.getLogger(__name__)


class MusicCogExceptionHandler:
    """MusicCogに関連するエラーを処理し、ユーザー向けのメッセージを生成するクラス。"""

    def __init__(self, music_config: Dict[str, Any]):
        """
        Args:
            music_config (Dict[str, Any]): musicセクションのコンフィグ。
                                           'messages'キーからメッセージテンプレートを取得します。
        """
        self.messages = music_config.get('messages', {})

    def get_message(self, key: str, **kwargs) -> str:
        """
        コンフィグからメッセージテンプレートを取得し、フォーマットして返す。

        Args:
            key (str): メッセージのキー。
            **kwargs: テンプレートに渡す引数。

        Returns:
            str: フォーマット済みのメッセージ文字列。
        """
        template = self.messages.get(key, f"Message key '{key}' not found.")
        kwargs.setdefault('prefix', '/')
        try:
            return template.format(**kwargs)
        except KeyError as e:
            logger.warning(f"メッセージ '{key}' のフォーマット中にキーエラーが発生しました: {e}")
            return f"メッセージ '{key}' の表示エラー: 不足している引数があります。"

    def handle_error(self, error: Exception, guild: discord.Guild) -> str:
        """
        汎用的なエラーハンドラ。例外の種類に応じて適切なメッセージを返す。

        Args:
            error (Exception): 捕捉された例外オブジェクト。
            guild (discord.Guild): エラーが発生したギルド。

        Returns:
            str: ユーザーに表示するエラーメッセージ。
        """
        guild_log_info = f"Guild {guild.id} ({guild.name})"

        # DRM / 非対応 / 動画利用不可は想定内のため ERROR+traceback ではなく WARNING にする
        if type(error).__name__ == "UnsupportedMediaError":
            # 想定内の失敗として短い警告だけ残す
            logger.warning("%s: Unsupported media: %s", guild_log_info, error)
            # 例外メッセージをユーザー向けに使う
            err_text = str(error)
            # 動画利用不可・非対応 URL は例外メッセージを優先する
            if any(
                marker in err_text
                for marker in ("再生できません", "対応していません")
            ):
                # 具体的な失敗理由を返す
                return self.get_message("error_fetching_song", error=err_text)
            # コンフィグの DRM メッセージがあればそれを優先する
            drm_msg = self.messages.get("error_drm_protected")
            # 専用メッセージがある場合はそれを返す
            if drm_msg:
                # テンプレートをフォーマットする
                return self.get_message("error_drm_protected")
            # 例外メッセージそのものを返す
            return self.get_message("error_fetching_song", error=err_text)

        # --- Voice Channel Connection Errors（Discord 側の一時的失敗は想定内） ---
        if isinstance(error, asyncio.TimeoutError):
            # 接続タイムアウトは頻発するため traceback なし WARNING にする
            logger.warning("%s: Voice channel connection timed out", guild_log_info)
            return self.get_message("error_playing", error="ボイスチャンネルへの接続がタイムアウトしました。")
        if isinstance(error, discord.ClientException):
            # 既に接続中・権限不足なども想定内として WARNING にする
            logger.warning("%s: Voice channel connection failed: %s", guild_log_info, error)
            return self.get_message("error_playing", error="ボイスチャンネルへの接続に失敗しました。ボットが既に他の操作を行っている可能性があります。")

        # --- Song Fetching/Extraction Errors ---
        # ytdlp_wrapperからのRuntimeErrorを想定
        if isinstance(error, RuntimeError) and ("ストリーム" in str(error) or "DRM" in str(error) or "非対応" in str(error)):
            # 取得失敗は想定内として WARNING にする
            logger.warning("%s: Song fetch error: %s", guild_log_info, error)
            return self.get_message("error_fetching_song", error=str(error))

        # --- Playback Errors ---
        # FFmpegが見つからない場合など
        if "No such file or directory: 'ffmpeg'" in str(error):
            # 環境未設定は ERROR で残す
            logger.error("%s: FFmpeg not found", guild_log_info)
            return self.get_message("error_playing", error="再生に必要なコンポーネント(FFmpeg)が見つかりません。")

        # --- Generic Fallback ---
        # 想定外のみ ERROR + traceback を残す
        logger.error(f"{guild_log_info}: An error occurred: {error}", exc_info=True)
        return self.get_message("error_playing", error=f"予期せぬエラーが発生しました: {type(error).__name__}")

    async def handle_generic_command_error(self, ctx_or_interaction: discord.Interaction | commands.Context, error: Exception):
        """
        コマンドで発生した予期せぬエラーを処理し、ユーザーに応答する汎用ハンドラ。
        """
        # 引数がInteractionであるか判定し、Interactionオブジェクトを取得または構築する
        interaction = ctx_or_interaction if isinstance(ctx_or_interaction, discord.Interaction) else getattr(ctx_or_interaction, "interaction", None)
        
        # デフォルトのコマンド名を「Unknown Command」に初期化する
        command_name = "Unknown Command"
        # Interactionが存在し、かつコマンドオブジェクトが登録されているか判定する
        if interaction and interaction.command:
            # コマンドのフルネーム（クオリファイドネーム）を取得して格納する
            command_name = interaction.command.qualified_name
        # Interactionがなく、Contextオブジェクトであり、かつコマンドが存在するか判定する
        elif not interaction and isinstance(ctx_or_interaction, commands.Context) and ctx_or_interaction.command:
            # Contextからコマンドのフルネームを取得して格納する
            command_name = ctx_or_interaction.command.qualified_name

        # 発生したエラーログを詳細情報（スタックトレース）を含めて出力する
        logger.error(f"An unexpected error occurred in command '{command_name}': {error}", exc_info=True)

        # ユーザー向けの日本語および英語の汎用エラーメッセージを定義する
        message = "コマンドの実行中に予期せぬエラーが発生しました。\nAn unexpected error occurred while executing the command."

        try:
            # 有効なInteractionオブジェクトが存在するか判定する
            if interaction:
                # すでにインタラクションへの最初のレスポンスが完了しているか（deferなど）を判定する
                if not interaction.response.is_done():
                    # 最初のレスポンスとして、エフェメラル（他者に見えない）設定でエラーメッセージを送信する
                    await interaction.response.send_message(message, ephemeral=True)
                else:
                    # すでにレスポンスが完了している場合は、フォローアップメッセージとして送信する
                    await interaction.followup.send(message, ephemeral=True)
            else:
                # Interactionが存在しないContextの場合は、通常のメッセージとして送信する
                await ctx_or_interaction.send(message, ephemeral=True)
        # DiscordのHTTP通信エラーを検知した場合の例外ハンドリングを行う
        except discord.errors.HTTPException as e:
            # メッセージ送信自体に失敗した事実をエラーログとして記録する
            logger.error(f"Failed to send error message for command '{command_name}': {e}")