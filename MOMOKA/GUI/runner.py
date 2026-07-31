# ホスト GUI: FastAPI スレッド + Electron 起動（失敗時は Bot 継続）

from __future__ import annotations

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
    # node / npm
    npm = shutil.which("npm")
    # electron ローカルバイナリ
    electron_bin = (
        _GUI_ELECTRON_DIR
        / "node_modules"
        / ".bin"
        / ("electron.cmd" if os.name == "nt" else "electron")
    )
    # dist の有無
    dist_index = _GUI_ELECTRON_DIR / "dist" / "index.html"
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
        # npm run electron:prod（Windows でも安定）
        if npm and (_GUI_ELECTRON_DIR / "package.json").is_file():
            return subprocess.Popen(
                [npm, "run", "electron:prod"],
                cwd=str(_GUI_ELECTRON_DIR),
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        # ローカル electron フォールバック
        if electron_bin.exists():
            return subprocess.Popen(
                [str(electron_bin), "."],
                cwd=str(_GUI_ELECTRON_DIR),
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                shell=(os.name == "nt"),
            )
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
