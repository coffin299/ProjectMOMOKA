"""yt-dlp が起動する Deno / Node を、終了時にまとめて回収する。"""

from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# V8 の old space 上限（MB）。未指定だと物理メモリに比例して確保し、
# 曲を重ねるたび RSS が戻らない。
JS_RUNTIME_OLD_SPACE_MB = 1024

# Windows: 主スレッドを止めたままジョブへ入れる。
_CREATE_SUSPENDED = 0x00000004
# ジョブハンドルを閉じたとき、残った子孫も終了する。
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_TH32CS_SNAPTHREAD = 0x00000004
_THREAD_SUSPEND_RESUME = 0x0002

# ジョブ作成に失敗した理由は一度だけ出す。
_job_failure_logged = False


def _runtime_basename(executable: str) -> str:
    """実行ファイル名から拡張子を除いた小文字名を返す。"""
    # パス区切りを除いたファイル名だけを見る。
    base = os.path.basename(str(executable)).lower()
    # Windows の実体とシムを同じランタイムとして扱う。
    for suffix in (".exe", ".cmd", ".bat", ".ps1"):
        # 既知の拡張子なら取り除く。
        if base.endswith(suffix):
            # 拡張子の直前までを名前にする。
            base = base[: -len(suffix)]
            # 複数拡張子は想定しない。
            break
    # deno / node 判定用の名前を返す。
    return base


def _append_v8_old_space(cmd: list[str], insert_at: int) -> None:
    """既存の --v8-flags に old space 上限を足す。無ければ追加する。"""
    # V8 へ渡す old space 上限。
    flag = f"--max-old-space-size={JS_RUNTIME_OLD_SPACE_MB}"
    # 既存の v8-flags を探す。
    for index, arg in enumerate(cmd):
        # --v8-flags=... 形式ならカンマで追記する。
        if arg.startswith("--v8-flags="):
            # 同じ上限の二重指定は避ける。
            if flag not in arg:
                # 既存フラグ列の末尾へ足す。
                cmd[index] = f"{arg},{flag}"
            # 追加済みなので終了する。
            return
        # --v8-flags <value> 形式なら次要素へ追記する。
        if arg == "--v8-flags" and index + 1 < len(cmd):
            # 同じ上限が無ければ追記する。
            if flag not in cmd[index + 1]:
                # 次の引数にカンマ区切りで足す。
                cmd[index + 1] = f"{cmd[index + 1]},{flag}"
            # 追加済みなので終了する。
            return
    # 既存指定が無いので run の直後へ入れる。
    cmd.insert(insert_at, f"--v8-flags={flag}")


def limit_js_runtime_command(cmd: list[str]) -> list[str]:
    """Deno / Node の起動引数に V8 ヒープ上限を入れる。"""
    # 空コマンドはそのまま返す。
    if not cmd:
        # 変更しない。
        return cmd
    # ランタイム名を正規化する。
    name = _runtime_basename(cmd[0])
    # deno run のときだけ v8-flags を足す（--version は壊さない）。
    if name == "deno" and len(cmd) > 1 and cmd[1] == "run":
        # run サブコマンドの直後に上限を入れる。
        _append_v8_old_space(cmd, 2)
    # node は stdin スクリプト実行のときだけ専用フラグを置く（--version は壊さない）。
    elif name == "node" and "-" in cmd[1:]:
        # Node が解釈する old space 上限。
        flag = f"--max-old-space-size={JS_RUNTIME_OLD_SPACE_MB}"
        # 未指定のときだけ先頭引数の次へ入れる。
        if flag not in cmd:
            # 実行ファイルの直後に置く。
            cmd.insert(1, flag)
    # 変更後の引数リストを返す。
    return cmd


def child_process_env() -> dict[str, str]:
    """パイプ再生の Python に、起動時パッチ用ディレクトリを見せる環境を返す。"""
    # 親の環境を複製して PATH などを維持する。
    env = os.environ.copy()
    # sitecustomize.py があるディレクトリ。
    startup = Path(__file__).resolve().parent / "ytdlp_child_startup"
    # プロジェクトルート（MOMOKA パッケージの import 用）。
    root = Path(__file__).resolve().parents[3]
    # 子プロセスのモジュール探索順。sitecustomize を先に置く。
    parts = [str(startup), str(root)]
    # 既存 PYTHONPATH があれば後ろへ残す。
    existing = env.get("PYTHONPATH")
    # 空でない既存値だけ連結する。
    if existing:
        # ユーザー指定のパスを維持する。
        parts.append(existing)
    # 子の Python 起動時に sitecustomize が読まれるようにする。
    env["PYTHONPATH"] = os.pathsep.join(parts)
    # 子プロセスの起動ログを親の INFO と重複させない。
    env["MOMOKA_YTDLP_CHILD"] = "1"
    # 完成した環境を返す。
    return env


class KillJob:
    """Windows ジョブ。ハンドルを閉じると子孫プロセスも終了する。"""

    def __init__(self, handle: int) -> None:
        # 閉じるまで保持するジョブハンドル。
        self._handle = handle

    @classmethod
    def create(cls) -> Optional["KillJob"]:
        """ジョブを作る。Windows 以外や失敗時は None。"""
        # 非 Windows ではプロセスグループ終了に任せる。
        if os.name != "nt":
            # ジョブは使わない。
            return None
        # 失敗ログを一度だけにするためのフラグ。
        global _job_failure_logged
        try:
            # kernel32 のジョブ API を使う。
            kernel32 = _kernel32()
            # 無名ジョブを作る。
            handle = kernel32.CreateJobObjectW(None, None)
            # 作成失敗時はタスクキルへフォールバックする。
            if not handle:
                # 初回だけ理由を残す。
                if not _job_failure_logged:
                    # 以降は同じ警告を出さない。
                    _job_failure_logged = True
                    # ジョブが使えなかったことを記録する。
                    logger.warning(
                        "CreateJobObjectW failed: %s",
                        ctypes_get_last_error(),
                    )
                # 呼び出し側は taskkill に落とす。
                return None
            # 終了時に子孫を巻き込む制限を入れる。
            info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
            # KILL_ON_JOB_CLOSE だけを有効にする。
            info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            # ジョブへ制限を適用する。
            ok = kernel32.SetInformationJobObject(
                handle,
                _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
                ctypes_byref(info),
                ctypes_sizeof(info),
            )
            # 制限が設定できなければハンドルを捨てる。
            if not ok:
                # 使えないジョブを閉じる。
                kernel32.CloseHandle(handle)
                # 初回だけ理由を残す。
                if not _job_failure_logged:
                    # 以降は同じ警告を出さない。
                    _job_failure_logged = True
                    # 設定失敗を記録する。
                    logger.warning(
                        "SetInformationJobObject failed: %s",
                        ctypes_get_last_error(),
                    )
                # ジョブ無しで続行する。
                return None
            # 制限付きジョブを返す。
            return cls(int(handle))
        except Exception as exc:
            # 予期しない失敗も再生を止めない。
            if not _job_failure_logged:
                # 初回だけ残す。
                _job_failure_logged = True
                # 例外内容を記録する。
                logger.warning("KillJob.create failed: %s", exc)
            # ジョブ無しで続行する。
            return None

    def assign(self, process_handle: Optional[int]) -> bool:
        """プロセスをジョブへ入れる。子孫は以降このジョブに属する。"""
        # ハンドルが無ければ入れられない。
        if not self._handle or not process_handle:
            # 割り当て失敗として返す。
            return False
        try:
            # 指定プロセスをジョブのメンバーにする。
            ok = bool(
                _kernel32().AssignProcessToJobObject(
                    self._handle,
                    int(process_handle),
                )
            )
        except Exception as exc:
            # 割り当て例外は再生を止めない。
            logger.debug("AssignProcessToJobObject failed: %s", exc)
            # 失敗を返す。
            return False
        # API が拒否した場合も記録する。
        if not ok:
            # ネスト不可などは taskkill へ任せる。
            logger.debug(
                "AssignProcessToJobObject rejected: %s",
                ctypes_get_last_error(),
            )
        # 成否を返す。
        return ok

    def terminate(self) -> None:
        """ジョブ内の全プロセスを終了する。"""
        # 既に閉じていれば何もしない。
        if not self._handle:
            # 二重終了を無視する。
            return
        try:
            # 子孫をまとめて終了する。
            _kernel32().TerminateJobObject(self._handle, 1)
        except Exception as exc:
            # 終了失敗はハンドル閉じで回収を続ける。
            logger.debug("TerminateJobObject failed: %s", exc)

    def __del__(self) -> None:
        """参照が消えたジョブは閉じ、停止漏れの子孫を終了する。"""
        try:
            # ハンドルが残っていれば KILL_ON_JOB_CLOSE で回収する。
            self.close()
        except Exception:
            # 終了処理中の失敗は無視する。
            return

    def close(self) -> None:
        """ジョブハンドルを閉じる。生存中の子孫も終了する。"""
        # 閉じるハンドルを外へ出す。
        handle = self._handle
        # 二重 close を防ぐ。
        self._handle = 0
        # 既に閉じていれば戻る。
        if not handle:
            # 何もしない。
            return
        try:
            # KILL_ON_JOB_CLOSE により残りプロセスも終わる。
            _kernel32().CloseHandle(handle)
        except Exception as exc:
            # 閉じ損ねはログだけ残す。
            logger.debug("CloseHandle(job) failed: %s", exc)


def popen_in_job(
    job: Optional[KillJob],
    cmd: list[str],
    **kwargs,
) -> subprocess.Popen:
    """プロセスをジョブへ入れてから実行を再開する。"""
    # Windows でジョブがあるときだけ起動を一時停止する。
    suspended = False
    # ジョブへ入れる前に子を作らせない。
    if os.name == "nt" and job is not None:
        # 既存フラグを残したまま一時停止を足す。
        kwargs["creationflags"] = int(kwargs.get("creationflags", 0)) | _CREATE_SUSPENDED
        # 再開が必要な印を付ける。
        suspended = True
    # 子プロセスを起動する。
    proc = subprocess.Popen(cmd, **kwargs)
    try:
        # ジョブがあれば、子が動く前に所属させる。
        if job is not None:
            # プロセスハンドルをジョブへ入れる。
            job.assign(getattr(proc, "_handle", None))
    finally:
        # 停止したまま残さない。
        if suspended:
            # 主スレッドの停止カウントを戻す。
            _resume_process(proc.pid)
    # 実行中のプロセスを返す。
    return proc


def install_ytdlp_popen_guard() -> None:
    """yt-dlp の Popen にヒープ上限とジョブ回収を足す。"""
    # パッチ対象の Popen があるモジュール。
    import yt_dlp.utils as ytdlp_utils

    # 既にパッチ済みなら何もしない。
    if getattr(ytdlp_utils.Popen, "_momoka_tree_guard", False):
        # 二重ラップを避ける。
        return
    # 元の初期化と終了処理を保持する。
    original_init = ytdlp_utils.Popen.__init__
    original_kill = ytdlp_utils.Popen.kill
    original_exit = getattr(ytdlp_utils.Popen, "__exit__", None)
    original_del = getattr(ytdlp_utils.Popen, "__del__", None)

    def _reap(self) -> None:
        # この Popen に結び付いたジョブを外す。
        job = getattr(self, "_momoka_job", None)
        # 参照を先に消して再入を防ぐ。
        self._momoka_job = None
        # ジョブが無ければ終了する。
        if job is None:
            # 回収対象なし。
            return
        # 親が死んだあとの Deno などをまとめて止める。
        job.terminate()
        # ハンドルを閉じて KILL_ON_JOB_CLOSE も効かせる。
        job.close()

    def _init(self, args, *remaining, **kwargs):
        # リスト引数ならヒープ上限を差し込む。
        if isinstance(args, (list, tuple)):
            # 元のタプルを壊さないようリスト化する。
            args = limit_js_runtime_command(list(args))
        # Windows ではジョブ所属前に子を起動させない。
        suspended = False
        # 一時停止フラグを足す。
        if os.name == "nt":
            # yt-dlp 側の creationflags を保持する。
            kwargs["creationflags"] = int(kwargs.get("creationflags", 0)) | _CREATE_SUSPENDED
            # 初期化後に再開する。
            suspended = True
        # 元の Popen でプロセスを作る。
        original_init(self, args, *remaining, **kwargs)
        # ジョブ未設定で開始する。
        self._momoka_job = None
        try:
            # このプロセス専用のジョブを作る。
            job = KillJob.create()
            # 作成できたら所属させる。
            if job is not None and job.assign(getattr(self, "_handle", None)):
                # 終了時に回収できるよう保持する。
                self._momoka_job = job
            elif job is not None:
                # 所属できなかったジョブは閉じる。
                job.close()
        finally:
            # 失敗しても一時停止のままにしない。
            if suspended:
                # 主スレッドを再開する。
                _resume_process(getattr(self, "pid", None))

    def _kill(self, *args, **kwargs):
        # kill は子の Deno を残すので、先にジョブ全体を止める。
        _reap(self)
        # 元の kill で親プロセスも止める。
        return original_kill(self, *args, **kwargs)

    def _exit(self, exc_type, exc, tb):
        try:
            # 元の終了処理で wait する。
            if original_exit is not None:
                # yt-dlp 側の __exit__ を使う。
                return original_exit(self, exc_type, exc, tb)
            # 基底の wait に任せる。
            return subprocess.Popen.__exit__(self, exc_type, exc, tb)
        finally:
            # 親終了後に残った子孫を回収する。
            _reap(self)

    def _del(self):
        try:
            # 実行中のダウンローダはここでは殺さない。
            finished = True
            try:
                # poll が None ならまだ生きている。
                finished = self.poll() is not None
            except Exception:
                # 状態が取れなければ回収を試みる。
                finished = True
            # 終了済みのときだけ孤児を回収する。
            if finished:
                # ジョブを閉じて残骸を止める。
                _reap(self)
        finally:
            # 元のデストラクタがあれば続ける。
            if original_del is not None:
                # Popen 本体の後始末を呼ぶ。
                original_del(self)

    # 初期化時に上限とジョブを適用する。
    ytdlp_utils.Popen.__init__ = _init
    # kill で子孫も止める。
    ytdlp_utils.Popen.kill = _kill
    # with 抜けで孤児を回収する。
    ytdlp_utils.Popen.__exit__ = _exit
    # 参照が消えたあとも終了済みなら回収する。
    ytdlp_utils.Popen.__del__ = _del
    # 再パッチ防止の印を付ける。
    ytdlp_utils.Popen._momoka_tree_guard = True
    # 曲ごとの子プロセスでは出さない。親の起動時だけ INFO にする。
    if not os.environ.get("MOMOKA_YTDLP_CHILD"):
        # ヒープ上限とジョブ回収を有効にしたことを残す。
        logger.info(
            "yt-dlp JS runtime heap cap: %sMB (job kill on close)",
            JS_RUNTIME_OLD_SPACE_MB,
        )


def _resume_process(pid: Optional[int]) -> None:
    """CREATE_SUSPENDED で止めたプロセスのスレッドを再開する。"""
    # PID が無ければ再開できない。
    if not pid or os.name != "nt":
        # 対象外なので戻る。
        return
    try:
        # スレッド列挙に使う API。
        kernel32 = _kernel32()
        # 全スレッドのスナップショットを取る。
        snapshot = kernel32.CreateToolhelp32Snapshot(_TH32CS_SNAPTHREAD, 0)
        # スナップショット失敗時は再開できない。
        if not snapshot or int(snapshot) == -1:
            # 停止したままになるので警告する。
            logger.warning("Thread snapshot failed for pid %s", pid)
            # 再開処理を打ち切る。
            return
        # 列挙エントリを用意する。
        entry = _THREADENTRY32()
        # 先頭呼び出しの前に構造体サイズを入れる。
        entry.dwSize = ctypes_sizeof(entry)
        try:
            # 最初のスレッドを読む。
            has_thread = bool(kernel32.Thread32First(snapshot, ctypes_byref(entry)))
            # 該当 PID のスレッドを順に再開する。
            while has_thread:
                # 対象プロセスのスレッドだけ再開する。
                if int(entry.th32OwnerProcessID) == int(pid):
                    # 一時停止を解除できる権限で開く。
                    thread = kernel32.OpenThread(
                        _THREAD_SUSPEND_RESUME,
                        False,
                        int(entry.th32ThreadID),
                    )
                    # 開けたスレッドだけ再開する。
                    if thread:
                        try:
                            # 停止カウントを 1 つ戻す。
                            kernel32.ResumeThread(thread)
                        finally:
                            # スレッドハンドルを閉じる。
                            kernel32.CloseHandle(thread)
                # 次のスレッドへ進む。
                has_thread = bool(kernel32.Thread32Next(snapshot, ctypes_byref(entry)))
        finally:
            # スナップショットハンドルを閉じる。
            kernel32.CloseHandle(snapshot)
    except Exception as exc:
        # 再開失敗はプロセスが止まったままになる。
        logger.warning("Failed to resume suspended pid %s: %s", pid, exc)


def _kernel32():
    """引数型を固定した kernel32 を返す。"""
    # 初回だけ DLL とシグネチャを用意する。
    global _KERNEL32
    # 準備済みならそれを返す。
    if _KERNEL32 is not None:
        # 再利用する。
        return _KERNEL32
    # ctypes は Windows のときだけ読み込む。
    import ctypes
    from ctypes import wintypes

    # kernel32 をロードする。
    dll = ctypes.WinDLL("kernel32", use_last_error=True)
    # ジョブ作成。
    dll.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
    dll.CreateJobObjectW.restype = wintypes.HANDLE
    # ジョブ制限の設定。
    dll.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    ]
    dll.SetInformationJobObject.restype = wintypes.BOOL
    # プロセスのジョブ所属。
    dll.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    dll.AssignProcessToJobObject.restype = wintypes.BOOL
    # ジョブ全体の終了。
    dll.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    dll.TerminateJobObject.restype = wintypes.BOOL
    # ハンドルを閉じる。
    dll.CloseHandle.argtypes = [wintypes.HANDLE]
    dll.CloseHandle.restype = wintypes.BOOL
    # スレッド一覧。
    dll.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    dll.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    dll.Thread32First.argtypes = [wintypes.HANDLE, ctypes.POINTER(_THREADENTRY32)]
    dll.Thread32First.restype = wintypes.BOOL
    dll.Thread32Next.argtypes = [wintypes.HANDLE, ctypes.POINTER(_THREADENTRY32)]
    dll.Thread32Next.restype = wintypes.BOOL
    # スレッド再開。
    dll.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    dll.OpenThread.restype = wintypes.HANDLE
    dll.ResumeThread.argtypes = [wintypes.HANDLE]
    dll.ResumeThread.restype = wintypes.DWORD
    # 以降の呼び出しで使う。
    _KERNEL32 = dll
    # 準備した DLL を返す。
    return dll


def ctypes_byref(value):
    """ctypes.byref を遅延 import で返す。"""
    # 構造体のポインタを API へ渡す。
    import ctypes

    # byref で渡す。
    return ctypes.byref(value)


def ctypes_sizeof(value) -> int:
    """ctypes.sizeof を遅延 import で返す。"""
    # 構造体サイズを API へ渡す。
    import ctypes

    # バイト数を返す。
    return ctypes.sizeof(value)


def ctypes_get_last_error() -> int:
    """kernel32 の直前の Win32 エラーコードを返す。"""
    # use_last_error 付き DLL のエラーを読む。
    import ctypes

    # スレッドローカルの最終エラーを返す。
    return ctypes.get_last_error()


# kernel32 は最初のジョブ作成まで読み込まない。
_KERNEL32 = None


class _IO_COUNTERS:
    """プレースホルダ。実体は _bind_structures で差し替える。"""


class _JOBOBJECT_BASIC_LIMIT_INFORMATION:
    """プレースホルダ。実体は _bind_structures で差し替える。"""


class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION:
    """プレースホルダ。実体は _bind_structures で差し替える。"""


class _THREADENTRY32:
    """プレースホルダ。実体は _bind_structures で差し替える。"""


def _bind_structures() -> None:
    """Windows 構造体を ctypes で定義してモジュールグローバルへ置く。"""
    # 非 Windows では構造体を使わない。
    if os.name != "nt":
        # 定義を省略する。
        return
    # 構造体定義に使う。
    import ctypes
    from ctypes import wintypes

    # グローバルのクラスを実体へ差し替える。
    global _IO_COUNTERS
    global _JOBOBJECT_BASIC_LIMIT_INFORMATION
    global _JOBOBJECT_EXTENDED_LIMIT_INFORMATION
    global _THREADENTRY32

    class IOCounters(ctypes.Structure):
        # ジョブ拡張情報の I/O カウンタ領域。
        _fields_ = [
            ("ReadOperationCount", ctypes.c_ulonglong),
            ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong),
            ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong),
            ("OtherTransferCount", ctypes.c_ulonglong),
        ]

    class BasicLimit(ctypes.Structure):
        # KILL_ON_JOB_CLOSE を置く基本制限。
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class ExtendedLimit(ctypes.Structure):
        # SetInformationJobObject が要求する拡張制限。
        _fields_ = [
            ("BasicLimitInformation", BasicLimit),
            ("IoInfo", IOCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    class ThreadEntry(ctypes.Structure):
        # Toolhelp のスレッド一行。
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ThreadID", wintypes.DWORD),
            ("th32OwnerProcessID", wintypes.DWORD),
            ("tpBasePri", ctypes.c_long),
            ("tpDeltaPri", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
        ]

    # モジュールグローバルを実装クラスへ差し替える。
    _IO_COUNTERS = IOCounters
    _JOBOBJECT_BASIC_LIMIT_INFORMATION = BasicLimit
    _JOBOBJECT_EXTENDED_LIMIT_INFORMATION = ExtendedLimit
    _THREADENTRY32 = ThreadEntry


# import 時に構造体だけ用意する。DLL 呼び出しは後段。
_bind_structures()
