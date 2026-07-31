# GUI 向け永続ログ（data/ 配下 .txt / .log へ追記 + 起動時 .log 末尾復元）

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Dict, List

from MOMOKA.services.log_sanitize import sanitize_log_message

# リポジトリルートからの data ディレクトリ
_DATA_DIR = Path(__file__).resolve().parents[2] / "data"
# 同階層の追記先（ペア）
_LOG_TXT = _DATA_DIR / "momoka_gui.txt"
_LOG_LOG = _DATA_DIR / "momoka_gui.log"
# フォーマットは QueueHandler と揃える
_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
# 起動復元の既定行数
DEFAULT_HISTORY_LINES = 2000
# 末尾読込のチャンクサイズ
_TAIL_CHUNK = 64 * 1024
# asctime - name - LEVEL - message
_LINE_RE = re.compile(
    r"^(?P<asctime>.+?) - (?P<name>.+?) - (?P<level>[A-Z]+) - (?P<message>.*)$"
)


class SanitizingFormatter(logging.Formatter):
    """整形後にシークレットを伏せる Formatter。"""

    def format(self, record: logging.LogRecord) -> str:
        # 通常フォーマットする
        text = super().format(record)
        # トークン等を伏せて返す
        return sanitize_log_message(text, max_length=100_000)


def get_log_file_path() -> Path:
    """復元・追記の正本 .log パスを返す。"""
    # momoka_gui.log
    return _LOG_LOG


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


def categorize_logger_name(name: str, level: str) -> str:
    """ロガー名とレベルから GUI カテゴリを決める。"""
    # エラー優先
    if level in ("ERROR", "CRITICAL"):
        return "error"
    # stdout は一般
    if name == "stdout":
        return "general"
    # LLM
    if "MOMOKA.llm" in name:
        return "llm"
    # TTS / Music
    if "MOMOKA.tts" in name or "MOMOKA.music" in name:
        return "tts"
    # それ以外一般（GUILD_EVENT もここ）
    return "general"


def parse_log_line(line: str) -> Dict[str, Any] | None:
    """永続ログ1行を GUI 用 dict にパースする。失敗時 None。"""
    # 空行は無視
    text = line.rstrip("\r\n")
    if not text.strip():
        return None
    # 標準フォーマットを試す
    match = _LINE_RE.match(text)
    if match:
        # 名前・レベル・本文
        name = match.group("name")
        level = match.group("level")
        message = text
        # 伏せ字
        safe = sanitize_log_message(message, max_length=100_000)
        # 返す
        return {
            "name": name,
            "level": level,
            "message": safe,
            "category": categorize_logger_name(name, level),
        }
    # パース不能でも一般 INFO として残す（復元優先）
    safe = sanitize_log_message(text, max_length=100_000)
    return {
        "name": "restored",
        "level": "INFO",
        "message": safe,
        "category": "general",
    }


def read_log_tail_lines(
    max_lines: int = DEFAULT_HISTORY_LINES,
    path: Path | None = None,
) -> List[str]:
    """ファイル末尾から最大 max_lines 行を返す（古い→新しい順）。"""
    # 正本は .log
    target = path or _LOG_LOG
    # 無ければ空
    if not target.is_file():
        return []
    # 行数上限
    limit = max(1, int(max_lines))
    try:
        # バイナリで末尾から読む
        with target.open("rb") as fh:
            # ファイルサイズ
            fh.seek(0, 2)
            size = fh.tell()
            # 空
            if size <= 0:
                return []
            # 読込バッファ
            data = b""
            # 位置
            pos = size
            # 十分な改行が集まるまで戻る
            while pos > 0 and data.count(b"\n") <= limit:
                # チャンク幅
                read_size = min(_TAIL_CHUNK, pos)
                # 位置を戻す
                pos -= read_size
                fh.seek(pos)
                # 先頭に連結
                data = fh.read(read_size) + data
            # 途中行を捨てる（ファイル先頭以外）
            if pos > 0:
                nl = data.find(b"\n")
                if nl >= 0:
                    data = data[nl + 1 :]
        # デコード（壊れた文字は置換）
        text = data.decode("utf-8", errors="replace")
        # 行分割
        lines = [ln for ln in text.splitlines() if ln.strip()]
        # 末尾 limit 行
        return lines[-limit:]
    except Exception:
        # 読込失敗は空
        return []


def load_log_history(
    max_lines: int = DEFAULT_HISTORY_LINES,
) -> List[Dict[str, Any]]:
    """起動復元用: .log 末尾をパースしたエントリ一覧。"""
    # 末尾行
    lines = read_log_tail_lines(max_lines=max_lines)
    # 結果
    items: List[Dict[str, Any]] = []
    # 1行ずつ
    for line in lines:
        # パース
        parsed = parse_log_line(line)
        # 成功分だけ
        if parsed is not None:
            items.append(parsed)
    # 返す
    return items
