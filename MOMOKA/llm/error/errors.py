# MOMOKA/llm/error/errors.py

import logging
import re
import openai

try:
    import httpx
except ImportError:
    httpx = None

logger = logging.getLogger(__name__)


# --- MODIFIED: SearchAgent Exceptions ---
class SearchAgentError(Exception):
    """Base exception for SearchAgent errors."""
    def __init__(self, message: str, original_exception: Exception | None = None):
        super().__init__(message)
        self.original_exception = original_exception

class SearchAPIRateLimitError(SearchAgentError):
    """Raised when the Mistral Search API rate limit is exceeded."""
    pass

class SearchAPIServerError(SearchAgentError):
    """Raised for 5xx server errors from the Mistral Search API."""
    pass

class SearchAPIError(SearchAgentError):
    """Raised for other, unexpected API errors."""
    pass

class SearchExecutionError(SearchAgentError):
    """Raised for non-API errors during search execution."""
    pass
# --- END MODIFIED ---


class LLMExceptionHandler:
    def __init__(self, config: dict):
        self.config = config.get('error_msg', {})

    def _sanitize_error_message(self, error_text: str) -> str:
        """エラーメッセージからローカルディレクトリパスを除去してセキュアにする"""
        # Windowsパス（例: C:\Users\coffi\... や C:/Users/coffi/...）を除去
        sanitized = re.sub(r'[A-Za-z]:[\\\/](?:[^\s\\\/]+[\\\/])*', '', error_text)
        # Unixパス（例: /home/user/...）を除去
        sanitized = re.sub(r'\/(?:home|usr|var|tmp|opt)\/\S+\/', '', sanitized)
        # ファイルパスの "File \"...\"" 形式のトレースバック内パスを簡略化
        sanitized = re.sub(r'File\s+"[^"]*[\\\/]([^"\\\/]+)"', r'File "\1"', sanitized)
        return sanitized.strip()

    def handle_exception(self, exception: Exception) -> str:
        """
        LLM関連の例外を処理し、ユーザーフレンドリーなエラーメッセージを返す。
        """
        if isinstance(exception, openai.RateLimitError):
            logger.warning(f"LLM API rate limit error: {exception}")
            return self.config.get('rate_limit_error',
                                   "APIの利用制限に達しました。しばらくしてからもう一度お試しください。")

        if isinstance(exception, openai.AuthenticationError):
            logger.error(f"LLM API authentication error: {exception}")
            return self.config.get('auth_error', "APIキーが無効です。開発者に連絡してください。")

        if isinstance(exception, openai.APIConnectionError):
            logger.error(f"LLM API connection error: {exception}")
            return self.config.get('connection_error',
                                   "AIサービスへの接続に失敗しました。ネットワーク状態を確認するか、後でもう一度お試しください。")

        if isinstance(exception, openai.APITimeoutError):
            logger.error(f"LLM API timeout error: {exception}")
            return self.config.get('connection_error',
                                   "AIサービスへの接続がタイムアウトしました。しばらくしてからもう一度お試しください。")

        if httpx is not None and isinstance(
            exception,
            (
                httpx.RemoteProtocolError,
                httpx.ReadError,
                httpx.ConnectError,
                httpx.WriteError,
                httpx.PoolTimeout,
            ),
        ):
            logger.error(f"LLM API transient network error: {exception}")
            return self.config.get(
                'connection_error',
                "AIサービスとの通信が途中で切断されました。ネットワーク状態を確認するか、後でもう一度お試しください。",
            )

        if isinstance(exception, openai.APIStatusError):
            status_code = exception.status_code
            try:
                error_body = exception.response.json()

                if isinstance(error_body, list):
                    error_body = error_body[0] if error_body else {}

                if not isinstance(error_body, dict):
                    logger.error(f"LLM API status error: {status_code} - {error_body}")
                    return self.config.get('api_status_error', "APIから予期せぬエラーが返されました。").format(
                        status_code=status_code, detail=str(error_body))

                error_data = error_body.get('error')

                if isinstance(error_data, list) and error_data:
                    error_dict = error_data[0]
                    error_data = error_dict.get('error', error_dict)

                detail = "No details provided."
                if isinstance(error_data, dict):
                    detail = error_data.get('detail') or error_data.get('message') or error_data.get('title') or str(
                        error_data)

                logger.error(f"LLM API status error: {status_code} - {error_body}")

                if status_code == 503:
                    return self.config.get('model_overloaded',
                                           "現在、AIモデルが大変混み合っています。しばらくしてからもう一度お試しください。")

                return self.config.get('api_status_error', "APIから予期せぬエラーが返されました。").format(
                    status_code=status_code, detail=detail)

            except Exception as parse_error:
                logger.error(f"LLM API status error (could not parse response): {status_code} - {exception}",
                             exc_info=True)
                return self.config.get('api_parse_error',
                                       "APIからエラーが返されましたが、内容を解析できませんでした。").format(
                    status_code=status_code)

        # その他の予期せぬエラー
        logger.error(f"An unexpected error occurred during LLM interaction: {exception}", exc_info=True)
        # エラーメッセージからローカルディレクトリパスを除去して安全な表示を行う
        sanitized_error = self._sanitize_error_message(str(exception))
        return f"予期せぬエラーが発生しました。\nAn unexpected error occurred.\n\n**詳細 / Details:**\n```\n{sanitized_error}\n```\n\n下のボタンからフォームまたは GitHub で報告できます。 / Use the buttons below to report via form or GitHub."