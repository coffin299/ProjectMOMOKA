# GUI 向け永続ログ（data/ 配下 .txt / .log へ追記のみ・GUI 非読込）

from __future__ import annotations

import logging
from pathlib import Path

from MOMOKA.services.log_sanitize import sanitize_log_message

# リポジトリルートからの data ディレクトリ
_DATA_DIR = Path(__file__).resolve().parents[2] / "data"
# 同階層の追記先（ペア）
_LOG_TXT = _DATA_DIR / "momoka_gui.txt"
_LOG_LOG = _DATA_DIR / "momoka_gui.log"
# フォーマットは QueueHandler と揃える
_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"


class SanitizingFormatter(logging.Formatter):
    """整形後にシークレットを伏せる Formatter。"""

    def format(self, record: logging.LogRecord) -> str:
        # 通常フォーマットする
        text = super().format(record)
        # トークン等を伏せて返す
        return sanitize_log_message(text, max_length=100_000)


def attach_persistent_file_handlers(
    root_logger: logging.Logger | None = None,
) -> list[logging.Handler]:
    """momoka_gui.txt / .log へ同一内容を追記する Handler をルートへ追加する。"""
    # 未指定ならルートロガー
    if root_logger is None:
        root_logger = logging.getLogger()
    # data ディレクトリを用意する
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    # 追加したハンドラ一覧
    handlers: list[logging.Handler] = []
    # 同一フォーマッタを共有する
    formatter = SanitizingFormatter(_FORMAT)
    # txt / log の両方へ append
    for path in (_LOG_TXT, _LOG_LOG):
        # 追記モード・UTF-8
        handler = logging.FileHandler(path, mode="a", encoding="utf-8")
        # 伏せ字付きフォーマット
        handler.setFormatter(formatter)
        # ルートへ接続する
        root_logger.addHandler(handler)
        # 戻り値用に保持する
        handlers.append(handler)
    # 呼び出し側参照用
    return handlers
