# ホスト GUI: FastAPI スレッド + Electron 起動（失敗時は Bot 継続）

from __future__ import annotations

import atexit
import os
import shutil
import subprocess
import threading
import time
import traceback
from pathlib import Path
from typing import Optional

# リポジトリルート（MOMOKA/GUI/runner.py → parents[2]）
_REPO_ROOT = Path(__file__).resolve().parents[2]
# Electron フロントディレクトリ
_GUI_ELECTRON_DIR = _REPO_ROOT / "gui-electron"
# 起動中の Electron（または npm）プロセス
_electron_proc: Optional[subprocess.Popen] = None
# stop の多重呼び出し防止
_stop_lock = threading.Lock()
# atexit 登録済みか
_atexit_registered = False

# Windows: コンソールから切り離し（pause のキー入力を奪わない）
_CREATE_NEW_PROCESS_GROUP = 0x00000200
_DETACHED_PROCESS = 0x00000008


def stop_host_gui() -> None:
    """Electron ホスト GUI プロセスツリーを終了する。"""
    # グローバル参照
    global _electron_proc
    # 多重実行を直列化
    with _stop_lock:
        # 現在のプロセスを取る
        proc = _electron_proc
        # 参照を先に外す
        _electron_proc = None
    # 無ければ何もしない
    if proc is None:
        return
    # 既に終了済み
    if proc.poll() is not None:
        return
    try:
        # Windows はツリーごと強制終了（npm 経由の子も含む）
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        else:
            # POSIX: まず terminate
            proc.terminate()
            try:
                # 短い猶予
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                # だめなら kill
                proc.kill()
    except Exception as e:
        # 終了失敗は警告のみ
        print(f"WARNING: Failed to stop host Electron GUI: {e}")


def _register_atexit() -> None:
    """プロセス終了時に Electron を落とす。"""
    # グローバル
    global _atexit_registered
    # 一度だけ
    if _atexit_registered:
        return
    # 登録
    atexit.register(stop_host_gui)
    _atexit_registered = True


def _popen_detached(args: list, *, cwd: str, env: dict) -> subprocess.Popen:
    """コンソール非継承で子プロセスを起動する。"""
    # 共通 kwargs
    kwargs: dict = {
        "cwd": cwd,
        "env": env,
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
    }
    # Windows はデタッチ
    if os.name == "nt":
        kwargs["creationflags"] = _DETACHED_PROCESS | _CREATE_NEW_PROCESS_GROUP
    else:
        # 新しいセッション
        kwargs["start_new_session"] = True
    # 起動
    return subprocess.Popen(args, **kwargs)


def _start_api_server(log_queue) -> tuple[int, str]:
    """uvicorn をデーモンスレッドで起動し (port, token) を返す。"""
    # 遅延 import
    import uvicorn

    from MOMOKA.GUI.api_server import (
        BIND_HOST,
        HostGuiAuth,
        create_host_gui_app,
        find_free_port,
        generate_host_gui_token,
    )

    # トークン生成
    token = generate_host_gui_token()
    # 認証オブジェクト
    auth = HostGuiAuth(token)
    # 空きポート
    port = find_free_port()
    # FastAPI アプリ
    app = create_host_gui_app(log_queue, auth)

    def _run() -> None:
        """uvicorn をこのスレッドで回す。"""
        try:
            # loopback のみ
            uvicorn.run(
                app,
                host=BIND_HOST,
                port=port,
                log_level="warning",
                access_log=False,
                use_colors=False,
            )
        except Exception as e:
            # API 失敗でも Bot は継続
            print(f"ホスト GUI API でエラーが発生しました: {e}")
            traceback.print_exc()

    # デーモンスレッド
    thread = threading.Thread(target=_run, name="momoka-host-gui-api", daemon=True)
    # 開始
    thread.start()
    # 起動待ち（短い）
    time.sleep(0.8)
    # ポートとトークン
    return port, token


def _launch_electron(port: int, token: str) -> Optional[subprocess.Popen]:
    """gui-electron を subprocess 起動。失敗時は None。"""
    # グローバルに保持
    global _electron_proc
    # ディレクトリ無ければ諦める
    if not _GUI_ELECTRON_DIR.is_dir():
        print(
            "WARNING: gui-electron/ が見つかりません。"
            "ホスト GUI ウィンドウは起動しません（Bot は継続）。"
        )
        return None
    # 環境変数（Electron が読む）
    env = os.environ.copy()
    env["MOMOKA_HOST_GUI_PORT"] = str(port)
    env["MOMOKA_HOST_GUI_TOKEN"] = token
    env["MOMOKA_HOST_GUI_HOST"] = "127.0.0.1"
    # electron ローカルバイナリ（npm ラッパより直接起動を優先）
    electron_bin = (
        _GUI_ELECTRON_DIR
        / "node_modules"
        / ".bin"
        / ("electron.cmd" if os.name == "nt" else "electron")
    )
    # Windows では .cmd より electron.exe を優先（DETACHED と相性）
    electron_exe = (
        _GUI_ELECTRON_DIR
        / "node_modules"
        / "electron"
        / "dist"
        / "electron.exe"
    )
    # dist の有無
    dist_index = _GUI_ELECTRON_DIR / "dist" / "index.html"
    # npm
    npm = shutil.which("npm")
    try:
        # node_modules が無ければ警告のみ
        if not (_GUI_ELECTRON_DIR / "node_modules").is_dir():
            print(
                "WARNING: gui-electron/node_modules がありません。"
                "初回は `cd gui-electron && npm install && npm run build` を実行してください。"
                "（Bot は継続します）"
            )
            return None
        # dist 未ビルド
        if not dist_index.is_file():
            print(
                "WARNING: gui-electron/dist がありません。"
                "`cd gui-electron && npm run build` を実行してください。（Bot は継続します）"
            )
            return None
        # 1) electron.exe 直接
        if os.name == "nt" and electron_exe.is_file():
            proc = _popen_detached(
                [str(electron_exe), "."],
                cwd=str(_GUI_ELECTRON_DIR),
                env=env,
            )
            _electron_proc = proc
            return proc
        # 2) node_modules/.bin/electron
        if electron_bin.exists():
            proc = _popen_detached(
                [str(electron_bin), "."],
                cwd=str(_GUI_ELECTRON_DIR),
                env=env,
            )
            _electron_proc = proc
            return proc
        # 3) npm run electron:prod フォールバック
        if npm and (_GUI_ELECTRON_DIR / "package.json").is_file():
            proc = _popen_detached(
                [npm, "run", "electron:prod"],
                cwd=str(_GUI_ELECTRON_DIR),
                env=env,
            )
            _electron_proc = proc
            return proc
    except Exception as e:
        # 起動失敗
        print(f"WARNING: Electron の起動に失敗しました: {e}")
        traceback.print_exc()
        return None
    # 手段なし
    print(
        "WARNING: Electron GUI を起動できませんでした。"
        "Node.js / gui-electron のビルドを確認してください（Bot は継続）。"
    )
    return None


def run_log_viewer_thread(log_queue) -> threading.Thread:
    """ホスト GUI API + Electron を別スレッドで起動し、Thread を返す。

    互換のため関数名は従来どおり。内部は Tk ではなく FastAPI + Electron。
    """
    # 終了時クリーンアップを登録
    _register_atexit()

    def run_gui() -> None:
        """API と Electron を起動する。"""
        try:
            # API 起動
            port, token = _start_api_server(log_queue)
            # 起動ログ（トークンは出さない）
            print(f"ホスト GUI API を 127.0.0.1:{port} で起動しました。")
            # 静的配信・uvicorn 準備待ち
            time.sleep(0.8)
            # Electron
            _launch_electron(port, token)
        except Exception as e:
            # GUI 失敗でも Bot 本体は止めない
            print(f"ホスト GUI でエラーが発生しました: {e}")
            traceback.print_exc()

    # デーモンスレッド
    thread = threading.Thread(target=run_gui, name="momoka-host-gui", daemon=True)
    # 開始
    thread.start()
    # 返す
    return thread
