# ホスト GUI: FastAPI スレッド + Electron 起動（失敗時は Bot 継続）

from __future__ import annotations

import atexit
import os
import shutil
import subprocess
import threading
import time
import traceback
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

# リポジトリルート（MOMOKA/GUI/runner.py → parents[2]）
_REPO_ROOT = Path(__file__).resolve().parents[2]
# Electron フロントディレクトリ
_GUI_ELECTRON_DIR = _REPO_ROOT / "gui-electron"
# 起動中の Electron（または npm）プロセス
_electron_proc: Optional[subprocess.Popen] = None
# Popen ラッパーが先に死んでも追跡できるよう PID を別保持
_electron_pid: Optional[int] = None
# start / stop を直列化する（Popen 代入レース防止）
_lifecycle_lock = threading.Lock()
# atexit 登録済みか
_atexit_registered = False
# 起動世代（stop 中の古い start を破棄）
_start_generation = 0

# Windows: コンソールから切り離し（pause のキー入力を奪わない）
_CREATE_NEW_PROCESS_GROUP = 0x00000200
_DETACHED_PROCESS = 0x00000008


def _electron_dist_exe() -> Path:
    """gui-electron 同梱の electron.exe / electron バイナリパス。"""
    # Windows は dist/electron.exe
    if os.name == "nt":
        # パスを組み立てる
        return (
            _GUI_ELECTRON_DIR
            / "node_modules"
            / "electron"
            / "dist"
            / "electron.exe"
        )
    # POSIX は electron 実行ファイル
    return _GUI_ELECTRON_DIR / "node_modules" / "electron" / "dist" / "electron"


def _kill_pid_tree(pid: int) -> None:
    """指定 PID とその子孫を終了する。"""
    try:
        # Windows は taskkill でツリーごと落とす
        if os.name == "nt":
            # /T で子プロセスも含める
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            # ここで終了
            return
        # POSIX: プロセスグループごと送る（失敗時は単体）
        try:
            # 負の PID でプロセスグループへ
            os.killpg(pid, 15)
        except Exception:
            # グループ不可なら単体へ SIGTERM
            os.kill(pid, 15)
    except Exception as e:
        # 個別 PID の失敗は警告のみ（孤児掃討へ続く）
        print(f"WARNING: Failed to stop host Electron PID {pid}: {e}")


def _kill_gui_electron_orphans() -> None:
    """gui-electron 配下の残留 electron を落とす（ラッパー先行終了対策）。"""
    # 正規化したターゲット実行ファイル
    target = _electron_dist_exe()
    # ファイルが無ければ掃討不能
    if not target.is_file():
        # 何もしない
        return
    # 比較用の解決済みパス
    try:
        # 実パスへ
        target_resolved = str(target.resolve()).lower()
    except OSError:
        # 解決失敗時は文字列化のみ
        target_resolved = str(target).lower()
    # Windows: Win32_Process から ExecutablePath 一致を探す
    if os.name == "nt":
        try:
            # PID とパスをタブ区切りで列挙
            completed = subprocess.run(
                [
                    "powershell",
                    "-NoProfile",
                    "-Command",
                    (
                        "Get-CimInstance Win32_Process "
                        "-Filter \"Name='electron.exe'\" "
                        "| ForEach-Object { "
                        "'{0}`t{1}' -f $_.ProcessId, $_.ExecutablePath "
                        "}"
                    ),
                ],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
        except Exception:
            # 列挙失敗は諦める
            return
        # 1 行ずつ処理
        for line in completed.stdout.splitlines():
            # 空行スキップ
            raw = line.strip()
            # 空なら次へ
            if not raw:
                continue
            # PID とパスを分割
            parts = raw.split("\t", 1)
            # 形式不正はスキップ
            if len(parts) != 2:
                continue
            # 文字列を取り出す
            pid_s, exe_path = parts[0].strip(), parts[1].strip()
            # パスが無ければスキップ（権限不足等）
            if not exe_path:
                continue
            # 大文字小文字を無視して比較
            if exe_path.lower().replace("/", "\\") != target_resolved.replace(
                "/", "\\"
            ):
                # 不一致は別アプリの Electron
                continue
            try:
                # PID を整数化
                orphan_pid = int(pid_s)
            except ValueError:
                # 壊れた行は無視
                continue
            # ツリーごと終了
            _kill_pid_tree(orphan_pid)
        # Windows 掃討完了
        return
    # POSIX: pgrep で同パスを探す
    try:
        # 実行中の electron を列挙
        completed = subprocess.run(
            ["pgrep", "-f", str(target)],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except Exception:
        # 失敗時は何もしない
        return
    # 見つかった PID を落とす
    for line in completed.stdout.splitlines():
        # 空白除去
        pid_s = line.strip()
        # 空なら次
        if not pid_s:
            continue
        try:
            # 整数化
            orphan_pid = int(pid_s)
        except ValueError:
            # 壊れた行は無視
            continue
        # 終了
        _kill_pid_tree(orphan_pid)


def stop_host_gui() -> None:
    """Electron ホスト GUI プロセスツリーを終了する。"""
    # グローバル参照
    global _electron_proc, _electron_pid, _start_generation
    # 多重実行を直列化
    with _lifecycle_lock:
        # 進行中の start を無効化
        _start_generation += 1
        # 現在のプロセスを取る
        proc = _electron_proc
        # 別保持 PID を取る
        pid = _electron_pid
        # 参照を先に外す
        _electron_proc = None
        # PID もクリア
        _electron_pid = None
    # 終了対象 PID を集める
    pids: list[int] = []
    # 明示 PID があれば優先
    if pid is not None:
        # 追加
        pids.append(pid)
    # Popen の PID も念のため（ラッパー含む）
    if proc is not None and proc.pid not in pids:
        # 追加
        pids.append(proc.pid)
    # 既知 PID を順に落とす（poll 済みでも taskkill する）
    for target_pid in pids:
        # ツリー終了
        _kill_pid_tree(target_pid)
    # npm/.cmd ラッパー先行終了で孤児化した electron も掃討
    _kill_gui_electron_orphans()


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


def _wait_api_ready(port: int, token: str, timeout_sec: float = 15.0) -> bool:
    """Host GUI /status が 200 を返すまで待つ。"""
    # ヘルス確認 URL
    url = f"http://127.0.0.1:{port}/host-gui/api/status"
    # 期限
    deadline = time.monotonic() + timeout_sec
    # 認証付き GET
    req = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {token}"},
        method="GET",
    )
    # ポーリング
    while time.monotonic() < deadline:
        try:
            # 短いタイムアウトで叩く
            with urllib.request.urlopen(req, timeout=1.0) as resp:
                # 200 なら ready
                if getattr(resp, "status", 200) == 200:
                    return True
        except (urllib.error.URLError, TimeoutError, OSError):
            # 未起動は再試行
            time.sleep(0.2)
            continue
        except Exception:
            # 想定外も再試行
            time.sleep(0.2)
            continue
        # 非 200 も少し待つ
        time.sleep(0.2)
    # タイムアウト
    return False


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
    # 起動完了シグナル
    ready = threading.Event()

    def _run() -> None:
        """uvicorn をこのスレッドで回す。"""
        try:
            # リッスン開始直前に合図（実際の ready は HTTP で再確認）
            ready.set()
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
    # スレッド開始待ち
    ready.wait(timeout=5.0)
    # HTTP ready 待ち（固定 sleep をやめる）
    if not _wait_api_ready(port, token):
        print(
            "WARNING: ホスト GUI API の ready 確認がタイムアウトしました。"
            "Electron 起動を継続します。"
        )
    # ポートとトークン
    return port, token


def _launch_electron(port: int, token: str, generation: int) -> Optional[subprocess.Popen]:
    """gui-electron を subprocess 起動。失敗時は None。"""
    # グローバルに保持
    global _electron_proc, _electron_pid
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
    electron_exe = _electron_dist_exe()
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

        def _adopt(proc: subprocess.Popen) -> Optional[subprocess.Popen]:
            """世代が一致するときだけグローバルへ登録する。"""
            with _lifecycle_lock:
                # 世代一致なら採用して返す
                if generation == _start_generation:
                    # Popen 参照を保持
                    _electron_proc = proc
                    # ラッパー死対策で PID も保持
                    _electron_pid = proc.pid
                    # 採用成功
                    return proc
            # stop 済み世代: ツリー終了＋孤児掃討
            _kill_pid_tree(proc.pid)
            # 残留も落とす
            _kill_gui_electron_orphans()
            # 不採用
            return None

        # 1) electron.exe 直接
        if os.name == "nt" and electron_exe.is_file():
            return _adopt(
                _popen_detached(
                    [str(electron_exe), "."],
                    cwd=str(_GUI_ELECTRON_DIR),
                    env=env,
                )
            )
        # 2) POSIX 同梱バイナリ
        if os.name != "nt" and electron_exe.is_file():
            return _adopt(
                _popen_detached(
                    [str(electron_exe), "."],
                    cwd=str(_GUI_ELECTRON_DIR),
                    env=env,
                )
            )
        # 3) node_modules/.bin/electron
        if electron_bin.exists():
            return _adopt(
                _popen_detached(
                    [str(electron_bin), "."],
                    cwd=str(_GUI_ELECTRON_DIR),
                    env=env,
                )
            )
        # 4) npm run electron:prod フォールバック
        if npm and (_GUI_ELECTRON_DIR / "package.json").is_file():
            return _adopt(
                _popen_detached(
                    [npm, "run", "electron:prod"],
                    cwd=str(_GUI_ELECTRON_DIR),
                    env=env,
                )
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
    # 終了時クリーンアップを登録
    _register_atexit()

    def run_gui() -> None:
        """API と Electron を起動する。"""
        global _start_generation
        try:
            # この起動の世代番号
            with _lifecycle_lock:
                _start_generation += 1
                generation = _start_generation
            # API 起動（ready 待ち込み）
            port, token = _start_api_server(log_queue)
            # stop 済みなら Electron を出さない
            with _lifecycle_lock:
                if generation != _start_generation:
                    return
            # 起動ログ（トークンは出さない）
            print(f"ホスト GUI API を 127.0.0.1:{port} で起動しました。")
            # Electron
            _launch_electron(port, token, generation)
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
