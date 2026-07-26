"""子プロセスのログを安全に Python logging へ橋渡しする。"""

from __future__ import annotations

import logging
import re
import threading
from collections import deque
from typing import IO, Any, Optional


# 外部プロセスが出力し得る認証情報を GUI へ渡す前に除去する。
_SECRET_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(
            r"(?i)\b(authorization)\s*[:=]\s*"
            r"(?:bearer\s+)?[^\s,;]+"
        ),
        r"\1=[REDACTED]",
    ),
    (
        re.compile(
            r"(?i)\b(cookie|set-cookie)\s*[:=]\s*[^\s,;]+"
        ),
        r"\1=[REDACTED]",
    ),
    (
        re.compile(
            r"(?i)\b(po[_ -]?token|potoken|visitor_data)\s*[:=+]\s*"
            r"[A-Za-z0-9._~+/=-]+"
        ),
        r"\1=[REDACTED]",
    ),
    (
        re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._~+/=-]+"),
        r"\1 [REDACTED]",
    ),
    (
        re.compile(
            r"(?i)((?:web|mweb)\.(?:gvs|player|subs)\+)"
            r"[A-Za-z0-9._~+/=-]+"
        ),
        r"\1[REDACTED]",
    ),
)


def redact_process_log(value: str) -> str:
    """外部プロセスログから認証情報を除去する。"""
    # 呼び出し元の文字列を順番に置換できる作業変数へ入れる。
    redacted = value
    # 定義済みの全パターンを適用して漏洩経路を閉じる。
    for pattern, replacement in _SECRET_PATTERNS:
        # 一致した秘密値だけを固定文言へ置換する。
        redacted = pattern.sub(replacement, redacted)
    # GUI の過剰描画を防ぐため1行の最大長を制限する。
    if len(redacted) > 2000:
        # 切り詰めた事実が分かる接尾辞を付ける。
        redacted = f"{redacted[:2000]} ...[truncated]"
    # 安全化したログ行を返す。
    return redacted


class ChildProcessLogPump:
    """PIPEを継続排出し、末尾ログを保持するデーモンスレッド。"""

    def __init__(
        self,
        stream: Optional[IO[Any]],
        *,
        logger: logging.Logger,
        level: int,
        label: str,
        history_size: int = 100,
    ) -> None:
        # 読み取り対象のテキストストリームを保持する。
        self._stream = stream
        # GUIへ伝播するロガーを保持する。
        self._logger = logger
        # stdout/stderrごとのログレベルを保持する。
        self._level = level
        # 表示時に出力元を識別する短い名前を保持する。
        self._label = label
        # 診断用途の末尾ログだけを上限付きで保持する。
        self._history: deque[str] = deque(maxlen=max(1, history_size))
        # 複数スレッドから履歴を読むためのロックを用意する。
        self._history_lock = threading.Lock()
        # 停止要求を通知するイベントを用意する。
        self._stop_event = threading.Event()
        # 実際の読み取りスレッドは start() まで作成しない。
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        """読み取りスレッドを一度だけ開始する。"""
        # ストリームが無い場合は開始できないため終了する。
        if self._stream is None:
            # PIPE未設定の呼び出しも安全に扱う。
            return
        # 既に開始済みなら二重起動しない。
        if self._thread is not None:
            # 冪等な開始操作として扱う。
            return
        # 親終了を妨げないデーモンスレッドを作成する。
        self._thread = threading.Thread(
            target=self._run,
            name=f"process-log-{self._label}",
            daemon=True,
        )
        # 子プロセスがPIPEを埋める前に読み取りを開始する。
        self._thread.start()

    def _run(self) -> None:
        """改行単位でPIPEを排出する。"""
        # 型検査向けにローカル参照へ固定する。
        stream = self._stream
        # ストリームが無ければ何もせず終了する。
        if stream is None:
            # start()外から呼ばれても安全にする。
            return
        try:
            # EOFまたは停止要求まで1行ずつ読み取る。
            while not self._stop_event.is_set():
                # テキスト・バイナリ共通で1行を取得する。
                raw_line = stream.readline()
                # 空文字または空bytesはEOFなので終了する。
                if not raw_line:
                    # 読み取りループを抜ける。
                    break
                # 停止要求後は新しいログを公開しない。
                if self._stop_event.is_set():
                    # 読み取りループを終了する。
                    break
                # バイナリPIPEの場合はUTF-8置換付きで文字列化する。
                if isinstance(raw_line, bytes):
                    # 不正バイトでログ収集スレッドを落とさない。
                    line = raw_line.decode("utf-8", errors="replace")
                else:
                    # テキストPIPEはそのまま扱う。
                    line = str(raw_line)
                # 改行だけを除去してログ本文を保つ。
                line = line.rstrip("\r\n")
                # 空行はGUIへ流さない。
                if not line:
                    # 次の行を待つ。
                    continue
                # 認証情報を除去してから保存・公開する。
                safe_line = redact_process_log(line)
                # 履歴更新をロックで保護する。
                with self._history_lock:
                    # 障害時に参照する末尾履歴へ追加する。
                    self._history.append(safe_line)
                # 指定ロガーへ出力してGUIキューへ伝播させる。
                self._logger.log(self._level, "[%s] %s", self._label, safe_line)
        except (OSError, ValueError) as error:
            # シャットダウン中のclose由来はDEBUGに留める。
            if not self._stop_event.is_set():
                # 予期しないPIPE切断を診断可能にする。
                self._logger.debug("[%s] log pipe closed: %s", self._label, error)

    def snapshot(self) -> str:
        """保持中の末尾ログを改行連結して返す。"""
        # 読み取り中のdeque複製をロックで保護する。
        with self._history_lock:
            # 呼び出し側が扱いやすい文字列へ変換する。
            return "\n".join(self._history)

    def stop(self, timeout: float = 1.0) -> None:
        """停止を通知し、短時間だけスレッド終了を待つ。"""
        # 読み取りループへ停止要求を通知する。
        self._stop_event.set()
        # 未開始なら待機は不要とする。
        if self._thread is None:
            # 何もせず終了する。
            return
        # 現在スレッド自身からjoinしてデッドロックしないよう判定する。
        if self._thread is not threading.current_thread():
            # 子プロセス終了後のEOF処理を短時間だけ待つ。
            self._thread.join(timeout=max(0.0, timeout))
