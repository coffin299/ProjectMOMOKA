# MOMOKA/services/log_sanitize.py
# ログ文字列からシークレットらしき断片を伏せる共有ヘルパ。
# Discord 転送ログと将来の Web アクセスログの両方で再利用する。
from __future__ import annotations

import re

# 1 件のログの目安上限（Discord 2000 文字制限に余裕を持たせる）
DEFAULT_MAX_LOG_LENGTH = 1800


def sanitize_log_message(
    message: str,
    *,
    max_length: int = DEFAULT_MAX_LOG_LENGTH,
) -> str:
    """トークン・API キー・パスなどを伏せたログ文字列を返す。"""
    # コードブロック破壊を防ぐため連続バッククォートを分断する
    message = re.sub(
        r"`{3,}",
        lambda m: "`\u200b" * (len(m.group()) - 1) + "`",
        message,
    )
    # Windows ユーザーパスを伏せる
    message = re.sub(
        r"[A-Za-z]:\\Users\\[^\\]+\\[^\\]+",
        "********",
        message,
        flags=re.IGNORECASE,
    )
    # Session ID を伏せる
    message = re.sub(
        r"((?:Session ID:?|session)\s+)[a-f09]{32}",
        r"\1****",
        message,
        flags=re.IGNORECASE,
    )
    message = re.sub(
        r"((?:Session ID:?|session)\s+)([a-f0-9])([a-f0-9]{31})",
        r"\1\2****",
        message,
        flags=re.IGNORECASE,
    )
    # Discord Bot トークン形式を伏せる
    message = re.sub(
        r"\b(?:mfa\.)?[A-Za-z0-9_-]{24}\.[A-Za-z0-9_-]{6}\.[A-Za-z0-9_-]{20,}\b",
        "********",
        message,
    )
    # キー名付きの一般的なシークレットを伏せる
    message = re.sub(
        r"(?i)\b(api[_-]?key|access[_-]?token|client[_-]?secret|"
        r"authorization|bearer)\b(\s*[:=]\s*|\s+)([\"']?)"
        r"(?:bearer\s+)?[A-Za-z0-9_./+=-]{16,}\3",
        r"\1\2********",
        message,
    )
    # 既知のキー接頭辞を伏せる
    message = re.sub(
        r"\b(?:gh[pousr]_[A-Za-z0-9_]{20,}|sk-[A-Za-z0-9_-]{20,}|"
        r"AIza[A-Za-z0-9_-]{20,})\b",
        "********",
        message,
    )
    # キー名無しの長いトークン断片（英数字混在・40文字以上）を伏せる
    message = re.sub(
        r"\b(?=[A-Za-z0-9_-]*[A-Za-z])(?=[A-Za-z0-9_-]*\d)[A-Za-z0-9_-]{40,}\b",
        "********",
        message,
    )
    # 長すぎる場合は切り詰める
    if len(message) > max_length:
        message = message[:max_length] + " ...[truncated]"
    return message
