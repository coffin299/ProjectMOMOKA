# MOMOKA/notifications/error/common.py
# 地震・Twitch 通知で共有する例外階層とハンドラ土台。
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Dict, Optional, Type

import aiohttp
import discord

logger = logging.getLogger(__name__)


class NotificationDomainError(Exception):
    """通知系 Cog 共通の基底例外。"""


class APIError(NotificationDomainError):
    """外部 API 関連エラー。"""


class DataParsingError(NotificationDomainError):
    """応答パース失敗。"""


class ConfigError(NotificationDomainError):
    """設定処理エラー。"""


class NotificationError(NotificationDomainError):
    """通知送信エラー。"""


class BaseNotificationExceptionHandler:
    """通知系エラーハンドラの共通実装。

    サブクラスは api_error_cls / メッセージ文言を差し替える。
    """

    # 生成する API 例外クラス（サブクラスで上書き可）
    api_error_cls: Type[APIError] = APIError
    # ユーザー向けメッセージのドメイン接頭（空なら汎用）
    user_api_prefix: str = "情報の取得または解析中にエラーが発生しました"
    # Forbidden 時の追加案内
    forbidden_hint: str = ""

    def __init__(
        self,
        *,
        error_stats: Optional[Dict[str, int]] = None,
        log_prefix: str = "",
    ) -> None:
        # 任意の統計カウンタ（地震向け）
        self.error_stats = error_stats
        # ログ識別用接頭辞
        self.log_prefix = log_prefix

    def _bump_stat(self, key: str) -> None:
        """error_stats があれば指定キーを +1 する。"""
        # 統計が無ければ何もしない
        if self.error_stats is None:
            return
        # キーが無ければ 0 起点
        self.error_stats[key] = self.error_stats.get(key, 0) + 1

    def handle_api_error(self, error: Exception, context: str) -> APIError:
        """リクエスト例外を APIError に変換する。"""
        # タイムアウト
        if isinstance(error, asyncio.TimeoutError):
            logger.error("%sタイムアウト: %s", self.log_prefix, context)
            self._bump_stat("network_errors")
            return self.api_error_cls("リクエストがタイムアウトしました。")
        # ネットワーク層
        if isinstance(error, aiohttp.ClientError):
            logger.error("%sネットワークエラー: %s - %s", self.log_prefix, context, error)
            self._bump_stat("network_errors")
            return self.api_error_cls(f"ネットワークエラーが発生しました: {error}")
        # その他
        logger.error(
            "%s予期しないAPIリクエストエラー: %s - %s",
            self.log_prefix,
            context,
            error,
            exc_info=True,
        )
        return self.api_error_cls(f"予期しないエラーが発生しました: {error}")

    def handle_api_response_error(
        self,
        status: int,
        url: str,
        text: str = "",
    ) -> APIError:
        """HTTP ステータスから APIError を返す。"""
        # 付加情報（Twitch 等で本文を残す）
        detail = f", 応答: {text}" if text else ""
        # 400
        if status == 400:
            logger.error(
                "%sAPIリクエストエラー (Bad Request): %s - ステータス: %s%s",
                self.log_prefix,
                url,
                status,
                detail,
            )
            self._bump_stat("api_errors")
            return self.api_error_cls(f"APIへのリクエストが不正です (Code: {status})。")
        # 401（認証系）
        if status == 401:
            logger.error(
                "%sAPI認証エラー (Unauthorized): %s - ステータス: %s",
                self.log_prefix,
                url,
                status,
            )
            self._bump_stat("api_errors")
            return self.api_error_cls(
                f"APIの認証に失敗しました (Code: {status})。"
                "アクセストークンが無効か期限切れの可能性があります。"
            )
        # 429
        if status == 429:
            logger.warning("%sAPI レート制限: %s", self.log_prefix, url)
            self._bump_stat("api_errors")
            return self.api_error_cls(f"APIの利用制限に達しました (Code: {status})。")
        # その他
        logger.error(
            "%sAPI エラー: %s - ステータス: %s%s",
            self.log_prefix,
            url,
            status,
            detail,
        )
        self._bump_stat("api_errors")
        return self.api_error_cls(f"APIサーバーがエラーを返しました (Code: {status})。")

    def handle_json_decode_error(
        self,
        error: json.JSONDecodeError,
        context: str,
    ) -> DataParsingError:
        """JSON 解析失敗を DataParsingError にする。"""
        logger.error("%sJSON解析エラー: %s - %s", self.log_prefix, context, error)
        self._bump_stat("parsing_errors")
        return DataParsingError("APIからの応答データの解析に失敗しました。")

    def log_generic_error(self, error: Exception, context: str) -> None:
        """汎用エラーをログする。"""
        logger.error(
            "%s%sで予期しないエラーが発生しました: %s",
            self.log_prefix,
            context,
            error,
            exc_info=True,
        )

    def get_user_friendly_message(self, error: Exception) -> str:
        """ユーザー向けメッセージを返す。"""
        # API / パース
        if isinstance(error, (APIError, DataParsingError)):
            return f"❌ {self.user_api_prefix}: {error}"
        # 設定
        if isinstance(error, ConfigError):
            return f"❌ 設定処理中にエラーが発生しました: {error}"
        # 通知送信
        if isinstance(error, NotificationError):
            return f"❌ 通知の送信に失敗しました: {error}"
        # Discord 権限
        if isinstance(error, discord.Forbidden):
            hint = self.forbidden_hint
            if hint:
                return f"❌ 権限が不足しているため、操作を完了できませんでした。{hint}"
            return "❌ 権限が不足しているため、操作を完了できませんでした。"
        # 想定外
        return "❌ 予期しないエラーが発生しました。詳細はボットのログを確認してください。"
