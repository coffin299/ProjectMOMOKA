# MOMOKA GUI パッケージ（ホスト Electron GUI / API / ログ橋渡し）

from MOMOKA.GUI.bot_bridge import get_bot_ref, set_bot_ref
from MOMOKA.GUI.logging_bridge import (
    QueueHandler,
    StdoutCapture,
    attach_gui_logging,
    create_log_queue,
)
from MOMOKA.GUI.runner import run_log_viewer_thread, stop_host_gui
from MOMOKA.GUI.version import APP_NAME, COPYRIGHT, LOG_VIEWER_NAME, VERSION


def set_dark_mode() -> None:
    """互換スタブ（Tk テーマ廃止後は no-op）。"""
    # Electron 側で Discord ダーク固定のため何もしない
    return


def is_dark_mode() -> bool:
    """互換スタブ。常にダーク。"""
    # ホスト GUI はダーク固定
    return True


def get_theme_colors() -> dict:
    """互換スタブ（旧 Tk パレット相当の最小辞書）。"""
    # Discord ダークに寄せた色
    return {
        "bg": "#1E1F22",
        "fg": "#DBDEE1",
    }


__all__ = [
    "APP_NAME",
    "COPYRIGHT",
    "LOG_VIEWER_NAME",
    "VERSION",
    "QueueHandler",
    "StdoutCapture",
    "attach_gui_logging",
    "create_log_queue",
    "get_bot_ref",
    "get_theme_colors",
    "is_dark_mode",
    "run_log_viewer_thread",
    "set_bot_ref",
    "set_dark_mode",
    "stop_host_gui",
]
