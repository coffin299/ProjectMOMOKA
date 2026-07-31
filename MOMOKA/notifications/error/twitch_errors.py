# MOMOKA/notifications/error/twitch_errors.py
from __future__ import annotations

from typing import TYPE_CHECKING

from MOMOKA.notifications.error.common import (
    APIError,
    BaseNotificationExceptionHandler,
    ConfigError,
    DataParsingError,
    NotificationDomainError,
    NotificationError,
)

if TYPE_CHECKING:
    from ..twitch_notification_cog import TwitchNotification

# 公開名の互換エイリアス
TwitchNotificationError = NotificationDomainError
TwitchAPIError = APIError


class TwitchExceptionHandler(BaseNotificationExceptionHandler):
    """Twitch 通知 Cog 向けエラーハンドラ。"""

    # Twitch 向け API 例外クラス（互換のため TwitchAPIError と同型）
    api_error_cls = TwitchAPIError
    # ユーザー向け文言
    user_api_prefix = "Twitchとの連携中にエラーが発生しました"
    # Forbidden 時の追加案内
    forbidden_hint = (
        "メッセージの送信や埋め込みリンクの権限を確認してください。"
    )

    def __init__(self, cog_instance: "TwitchNotification") -> None:
        # Cog 参照を保持する
        self.cog = cog_instance
        # 共通土台（Twitch は error_stats 無し）
        super().__init__(error_stats=None, log_prefix="Twitch ")

    def handle_api_error(self, error: Exception, context: str) -> TwitchAPIError:
        """Twitch 向けメッセージを少し具体化した API エラー変換。"""
        # 共通変換を呼ぶ
        result = super().handle_api_error(error, context)
        # タイムアウト文言を Twitch 向けに寄せる
        if "タイムアウト" in str(result):
            return TwitchAPIError("Twitch APIへのリクエストがタイムアウトしました。")
        # ネットワーク文言を Twitch 向けに寄せる
        if "ネットワークエラー" in str(result) and "Twitch" not in str(result):
            return TwitchAPIError(
                f"Twitch APIへの接続中にネットワークエラーが発生しました: {error}"
            )
        return result
