# MOMOKA/notifications/error/earthquake_errors.py
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
    from ..earthquake_notification_cog import EarthquakeTsunamiCog

# 公開名の互換エイリアス
EarthquakeTsunamiError = NotificationDomainError


class EarthquakeTsunamiExceptionHandler(BaseNotificationExceptionHandler):
    """地震・津波情報 Cog 向けエラーハンドラ。"""

    # ユーザー向け文言
    user_api_prefix = "情報の取得または解析中にエラーが発生しました"

    def __init__(self, cog_instance: "EarthquakeTsunamiCog") -> None:
        # Cog 参照を保持する
        self.cog = cog_instance
        # 共通土台（統計カウンタ付き）
        super().__init__(
            error_stats=cog_instance.error_stats,
            log_prefix="",
        )

    def handle_api_error(self, error: Exception, url: str) -> APIError:
        """地震 API は第2引数名が url のためラップする。"""
        # 共通実装へ委譲する
        return super().handle_api_error(error, url)

    def handle_api_response_error(self, status: int, url: str) -> APIError:
        """地震 API は応答本文を渡さない。"""
        # text 無しで共通実装へ
        return super().handle_api_response_error(status, url, text="")
