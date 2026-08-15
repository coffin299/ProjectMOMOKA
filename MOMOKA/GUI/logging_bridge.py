# ルートロガー / stdout と GUI ログキューを橋渡しする

import logging
import queue
import sys
from io import StringIO
from typing import Tuple


def create_log_queue(maxsize: int = 5000) -> queue.Queue:
    """GUI ログビューアと共有するキューを生成する。"""
    # 上限付き FIFO（OOM 防止）
    return queue.Queue(maxsize=maxsize)


class QueueHandler(logging.Handler):
    """ログをキューに送信するハンドラ。"""

    def __init__(self, log_queue: queue.Queue):
        # 親 Handler を初期化する
        super().__init__()
        # GUI 側が読むキューを保持する
        self.log_queue = log_queue
        # 表示用フォーマットを設定する
        self.setFormatter(
            logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
        )

    def emit(self, record: logging.LogRecord) -> None:
        """1 レコードをキューへ載せる。"""
        try:
            # (ロガー名, レベル名, 整形済み文言) のタプルで送る
            self.log_queue.put_nowait(
                (record.name, record.levelname, self.format(record))
            )
        except queue.Full:
            # 満杯時は古いものを捨てて最新を優先する
            try:
                # 古い 1 件を捨てる
                self.log_queue.get_nowait()
            except queue.Empty:
                # 競合で空なら何もしない
                pass
            try:
                # 最新を再度入れる
                self.log_queue.put_nowait(
                    (record.name, record.levelname, self.format(record))
                )
            except queue.Full:
                # それでもダメなら破棄
                pass
        except Exception:
            # logging 標準のエラー処理に委ねる
            self.handleError(record)


class StdoutCapture:
    """標準出力をキャプチャしてログキューにも送るクラス。"""

    def __init__(self, log_queue: queue.Queue, original_stdout):
        # GUI 用キュー
        self.log_queue = log_queue
        # コンソールへも出すための元 stdout
        self.original_stdout = original_stdout
        # 互換用バッファ（flush で利用）
        self.buffer = StringIO()

    def write(self, text: str) -> int:
        """標準出力への書き込みをキャプチャする。"""
        # 元の標準出力にも書き込む（コンソールにも表示）
        written = self.original_stdout.write(text)
        # 即時反映する
        self.original_stdout.flush()
        # 空行や改行のみの場合はスキップする
        if not text.strip():
            return written if isinstance(written, int) else 0
        try:
            # 各行を個別に処理する
            for line in text.rstrip().split("\n"):
                # 空白のみの行は捨てる
                if line.strip():
                    # 標準出力のログとして扱う（満杯時は古いものを捨てる）
                    try:
                        # 非ブロッキング投入
                        self.log_queue.put_nowait(("stdout", "INFO", line))
                    except queue.Full:
                        try:
                            # 古い 1 件を捨てる
                            self.log_queue.get_nowait()
                            # 最新を入れる
                            self.log_queue.put_nowait(("stdout", "INFO", line))
                        except Exception:
                            # 破棄
                            pass
        except Exception:
            # エラーが発生しても元の標準出力は動作させる
            pass
        # write の戻り値（バイト/文字数）を返す
        return written if isinstance(written, int) else len(text)

    def flush(self) -> None:
        """フラッシュ処理。"""
        # 元 stdout を flush する
        self.original_stdout.flush()
        # 内部バッファがあれば flush する
        if hasattr(self.buffer, "flush"):
            self.buffer.flush()

    def isatty(self) -> bool:
        """TTY 判定を元 stdout に委譲（uvicorn 等が参照する）。"""
        # 元ストリームに isatty があればそれを使う
        orig = self.original_stdout
        if hasattr(orig, "isatty"):
            try:
                return bool(orig.isatty())
            except Exception:
                return False
        # 無ければ非 TTY
        return False

    def fileno(self) -> int:
        """ファイル番号を元 stdout に委譲する。"""
        # fileno が無ければ OSError（標準的な挙動）
        orig = self.original_stdout
        if hasattr(orig, "fileno"):
            return int(orig.fileno())
        raise OSError("underlying stdout has no fileno")

    @property
    def encoding(self):
        """文字エンコーディングを元 stdout から取る。"""
        return getattr(self.original_stdout, "encoding", "utf-8")

    @property
    def errors(self):
        """エラー処理モードを元 stdout から取る。"""
        return getattr(self.original_stdout, "errors", "strict")

    def __getattr__(self, name: str):
        """未定義属性は元 stdout へフォールバックする。"""
        # 元ストリームの属性を返す
        return getattr(self.original_stdout, name)


# コンソール / QueueHandler / 永続ファイルと揃えるフォーマット
_CONSOLE_LOG_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"


def flush_all_logging() -> None:
    """ルートロガーの全ハンドラと stdout を flush する（終了直前用）。"""
    # ルートロガーを取得する
    root = logging.getLogger()
    # 接続中のハンドラを順に flush する
    for handler in list(root.handlers):
        try:
            # バッファ済みレコードをディスク / コンソールへ押し出す
            handler.flush()
        except Exception:
            # 終了処理を止めない
            pass
    try:
        # print / StdoutCapture 側も押し出す
        sys.stdout.flush()
    except Exception:
        # 終了処理を止めない
        pass


def attach_gui_logging(
    root_logger: logging.Logger | None = None,
) -> Tuple[queue.Queue, QueueHandler, StdoutCapture]:
    """ルートロガーと stdout を GUI 用キューへ接続する。

    Returns:
        (log_queue, queue_handler, stdout_capture)
    """
    # 共有キューを作る
    log_queue = create_log_queue()
    # ルートロガーが未指定なら標準のルートを使う
    if root_logger is None:
        root_logger = logging.getLogger()
    # キューへ流すハンドラを作る
    queue_handler = QueueHandler(log_queue)
    # ルートへハンドラを追加する
    root_logger.addHandler(queue_handler)
    # data/momoka_gui.txt と .log へ追記（GUI 非読込）
    try:
        # 遅延 import（循環回避）
        from MOMOKA.GUI.persistent_log import attach_persistent_file_handlers

        # 永続ファイル Handler を付ける
        attach_persistent_file_handlers(root_logger)
    except Exception as e:
        # ファイル失敗でも GUI キューは維持する
        print(f"永続ログファイルの初期化に失敗しました: {e}")
    # 元の stdout を退避する（バッチコンソールの実体）
    original_stdout = sys.stdout
    # logging.info 等がバッチ画面に出るよう StreamHandler を付ける
    # StdoutCapture 経由にしない（QueueHandler との二重投入を避ける）
    console_handler = logging.StreamHandler(original_stdout)
    # ファイル側と同様にシークレットを伏せる Formatter を優先する
    try:
        # 遅延 import（循環回避）
        from MOMOKA.GUI.persistent_log import SanitizingFormatter

        # 伏せ字付きでコンソールへ出す
        console_handler.setFormatter(SanitizingFormatter(_CONSOLE_LOG_FORMAT))
    except Exception:
        # フォールバックは通常フォーマット
        console_handler.setFormatter(logging.Formatter(_CONSOLE_LOG_FORMAT))
    # INFO 以上をコンソールへ出す
    console_handler.setLevel(logging.INFO)
    # ルートへ接続する
    root_logger.addHandler(console_handler)
    # キャプチャで差し替える
    stdout_capture = StdoutCapture(log_queue, original_stdout)
    # 以降の print も GUI へ届くようにする
    sys.stdout = stdout_capture
    # 呼び出し側がキュー参照できるよう返す
    return log_queue, queue_handler, stdout_capture
