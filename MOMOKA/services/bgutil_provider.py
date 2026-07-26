"""同梱BgUtils PO Token Providerの起動・終了を管理する。"""

from __future__ import annotations

import asyncio
import atexit
import json
import logging
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from MOMOKA.music.plugins.process_log_bridge import ChildProcessLogPump


# GUIのTTS+Music区画へ振り分けられる名前で外部サービスログを公開する。
logger = logging.getLogger("MOMOKA.music.external.bgutil")

# 同梱serverとPython pluginで一致させる固定バージョンを定義する。
BGUTIL_PROVIDER_VERSION = "1.3.1"


@dataclass(frozen=True)
class BgutilProviderConfig:
    """Provider起動に必要な不変設定。"""

    # Providerを利用するかを保持する。
    enabled: bool
    # Provider起動失敗時にBot起動を拒否するかを保持する。
    required: bool
    # Node.js実行ファイル名または絶対パスを保持する。
    runtime: str
    # 同梱serverディレクトリを保持する。
    server_dir: Path
    # yt-dlp pluginへ渡すHTTP base URLを保持する。
    base_url: str
    # ProviderがlistenするTCPポートを保持する。
    port: int
    # readinessを待つ最大秒数を保持する。
    startup_timeout: float

    @classmethod
    def from_mapping(
        cls,
        config: dict[str, Any],
        *,
        project_root: Optional[Path] = None,
    ) -> "BgutilProviderConfig":
        """マージ済みMOMOKA設定からProvider設定を構築する。"""
        # 呼び出し元未指定時はリポジトリルートを求める。
        root = project_root or Path(__file__).resolve().parents[2]
        # 外部サービス設定を辞書として安全に取り出す。
        services = config.get("external_services") or {}
        # BgUtils設定を辞書として安全に取り出す。
        provider = services.get("bgutil") or {}
        # 相対パスの既定値を同梱serverへ向ける。
        raw_server_dir = provider.get(
            "server_dir",
            "third_party/bgutil-ytdlp-pot-provider/server",
        )
        # 設定値をPathへ変換する。
        server_dir = Path(str(raw_server_dir))
        # 相対パスなら実行cwdではなくリポジトリルート基準へ固定する。
        if not server_dir.is_absolute():
            # OS差を吸収した絶対パスへ解決する。
            server_dir = root / server_dir
        # ポート設定を整数へ正規化する。
        port = int(provider.get("port", 4416))
        # base URL未指定時はloopbackと設定ポートから生成する。
        base_url = str(
            provider.get("base_url") or f"http://127.0.0.1:{port}"
        ).rstrip("/")
        # 検証済み設定オブジェクトを返す。
        return cls(
            enabled=bool(provider.get("enabled", True)),
            required=bool(provider.get("required", True)),
            runtime=str(provider.get("runtime", "node")),
            server_dir=server_dir,
            base_url=base_url,
            port=port,
            startup_timeout=max(
                1.0,
                float(provider.get("startup_timeout_seconds", 20.0)),
            ),
        )


class BgutilProviderManager:
    """Providerをプロセス全体で一度だけ管理する。"""

    def __init__(self, config: BgutilProviderConfig) -> None:
        # 起動設定を保持する。
        self.config = config
        # 自分が起動した子プロセスだけを保持する。
        self._process: Optional[subprocess.Popen[str]] = None
        # 既存Provider借用時に終了しないため所有状態を保持する。
        self._owned = False
        # stdoutの継続排出担当を保持する。
        self._stdout_pump: Optional[ChildProcessLogPump] = None
        # stderrの継続排出担当を保持する。
        self._stderr_pump: Optional[ChildProcessLogPump] = None
        # 同時startによる二重起動を防ぐ。
        self._start_lock = asyncio.Lock()
        # 通常経路外のPython終了でも所有プロセスを止める。
        atexit.register(self._stop_sync)

    @property
    def base_url(self) -> Optional[str]:
        """有効時だけyt-dlpへ渡すProvider URLを返す。"""
        # 無効設定ならProvider引数を注入しない。
        if not self.config.enabled:
            # 明示的に未設定を返す。
            return None
        # 正規化済みURLを返す。
        return self.config.base_url

    def _probe(self) -> Optional[dict[str, Any]]:
        """Providerの/pingを同期HTTPで確認する。"""
        # readiness専用URLを組み立てる。
        ping_url = f"{self.config.base_url}/ping"
        try:
            # localhost確認なので短いtimeoutでHTTP応答を取得する。
            with urllib.request.urlopen(ping_url, timeout=1.0) as response:
                # HTTP本文をUTF-8 JSONとして読み取る。
                payload = json.loads(response.read().decode("utf-8"))
            # 辞書レスポンスだけを有効な候補として返す。
            return payload if isinstance(payload, dict) else {}
        except (
            OSError,
            ValueError,
            urllib.error.URLError,
            urllib.error.HTTPError,
        ):
            # 未起動・起動途中は応答なしとして扱う。
            return None

    @staticmethod
    def _is_expected_provider(payload: dict[str, Any]) -> bool:
        """ping応答が固定バージョンのProviderか判定する。"""
        # version一致とuptimeキーの両方を確認して別サービスを排除する。
        return (
            str(payload.get("version")) == BGUTIL_PROVIDER_VERSION
            and "server_uptime" in payload
        )

    async def start(self) -> bool:
        """Providerを開始し、利用可能ならTrueを返す。"""
        # 無効設定なら起動不要としてFalseを返す。
        if not self.config.enabled:
            # 無効化を運用ログへ残す。
            logger.info("BgUtils PO Token Provider is disabled.")
            # Provider未使用を呼び出し元へ伝える。
            return False
        # 複数Botから同時に呼ばれても一度だけ開始する。
        async with self._start_lock:
            # 所有プロセスが生存中なら起動済みとする。
            if self._process is not None and self._process.poll() is None:
                # 冪等な成功として返す。
                return True
            # イベントループを取得してブロッキングHTTPを退避する。
            loop = asyncio.get_running_loop()
            # 同じポートの既存サービスを先に確認する。
            existing = await loop.run_in_executor(None, self._probe)
            # 応答がある場合は正規Providerか検証する。
            if existing is not None:
                # 別サービスまたは版違いなら安全のため起動を拒否する。
                if not self._is_expected_provider(existing):
                    # ポート競合の詳細を含めて例外化する。
                    raise RuntimeError(
                        f"TCP {self.config.port} is occupied by an "
                        "unexpected service or "
                        f"BgUtils version: {existing!r}"
                    )
                # 借用Providerを自分の終了時に停止しないよう所有を外す。
                self._owned = False
                # 借用成功をGUIへ表示する。
                logger.info(
                    "Using existing BgUtils PO Token Provider v%s at %s",
                    BGUTIL_PROVIDER_VERSION,
                    self.config.base_url,
                )
                # 利用可能状態を返す。
                return True
            # ビルド済みエントリポイントを組み立てる。
            entrypoint = self.config.server_dir / "build" / "main.js"
            # 初回セットアップ未完了なら明確な案内で停止する。
            if not entrypoint.is_file():
                # 起動ツールでのnpmビルドを促す。
                raise FileNotFoundError(
                    f"BgUtils Provider build not found: {entrypoint}. "
                    "Run startMOMOKA.bat to install/build the vendored provider."
                )
            # Windowsでは独立プロセスグループを付けて所有範囲を明確にする。
            creation_flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            # shellを介さずNode本体を直接起動する。
            self._process = subprocess.Popen(
                [
                    self.config.runtime,
                    str(entrypoint),
                    "--port",
                    str(self.config.port),
                ],
                cwd=str(self.config.server_dir),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                shell=False,
                creationflags=creation_flags,
            )
            # このManagerが起動したプロセスとして記録する。
            self._owned = True
            # stdoutをINFOとしてGUIへ継続転送する。
            self._stdout_pump = ChildProcessLogPump(
                self._process.stdout,
                logger=logger,
                level=logging.INFO,
                label="provider:stdout",
            )
            # stderrをWARNINGとしてGUIへ継続転送する。
            self._stderr_pump = ChildProcessLogPump(
                self._process.stderr,
                logger=logger,
                level=logging.WARNING,
                label="provider:stderr",
            )
            # PIPE詰まりを防ぐため両ポンプを直ちに開始する。
            self._stdout_pump.start()
            # stderr側も同時に開始する。
            self._stderr_pump.start()
            try:
                # 設定timeoutまでreadinessを繰り返し確認する。
                await self._wait_until_ready()
            except Exception:
                # 起動失敗時は残存Nodeを確実に停止する。
                await self.stop()
                # 元の失敗理由を呼び出し元へ返す。
                raise
            # 起動完了をGUIへ表示する。
            logger.info(
                "BgUtils PO Token Provider v%s is ready at %s",
                BGUTIL_PROVIDER_VERSION,
                self.config.base_url,
            )
            # 利用可能状態を返す。
            return True

    async def _wait_until_ready(self) -> None:
        """子プロセス終了またはtimeoutまで/pingを待つ。"""
        # loop時計でdeadlineを計算する。
        loop = asyncio.get_running_loop()
        # 設定された最大待機時刻を固定する。
        deadline = loop.time() + self.config.startup_timeout
        # deadline到達まで短い間隔で確認する。
        while loop.time() < deadline:
            # 子プロセスが先に終了した場合は即時失敗にする。
            if self._process is None or self._process.poll() is not None:
                # stderr末尾を診断情報として取得する。
                detail = self._stderr_pump.snapshot() if self._stderr_pump else ""
                # 早期終了を明示する。
                raise RuntimeError(
                    "BgUtils Provider exited before readiness. "
                    f"stderr={detail or '(empty)'}"
                )
            # ブロッキングHTTP確認をexecutorで行う。
            payload = await loop.run_in_executor(None, self._probe)
            # 期待Providerが応答したら準備完了とする。
            if payload is not None and self._is_expected_provider(payload):
                # 正常終了する。
                return
            # 起動処理へCPUを返しつつ短時間待つ。
            await asyncio.sleep(0.25)
        # timeoutを設定値付きで通知する。
        raise TimeoutError(
            "BgUtils Provider did not become ready within "
            f"{self.config.startup_timeout:.1f}s"
        )

    async def stop(self) -> None:
        """自分が所有するProviderだけを停止する。"""
        # 借用Providerまたは未起動なら停止しない。
        if not self._owned or self._process is None:
            # ローカル参照だけを初期化する。
            self._process = None
            # 所有状態も初期化する。
            self._owned = False
            # 終了処理を完了する。
            return
        # blockingなprocess waitをイベントループ外へ移す。
        loop = asyncio.get_running_loop()
        # 同期停止処理の完了を待つ。
        await loop.run_in_executor(None, self._stop_sync)

    def _stop_sync(self) -> None:
        """atexitからも呼べる同期停止処理。"""
        # 現在の所有プロセスをローカル参照へ固定する。
        process = self._process
        # 借用または未起動なら何もしない。
        if not self._owned or process is None:
            # 終了対象なしとして返す。
            return
        try:
            # 生存中なら通常のterminateを要求する。
            if process.poll() is None:
                # 直接起動したNodeプロセスを終了する。
                process.terminate()
            try:
                # 正常終了を最大5秒待つ。
                process.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                # 応答しない場合だけ強制終了する。
                process.kill()
                # プロセス回収を完了する。
                process.wait(timeout=2.0)
        finally:
            # EOF後にログポンプを停止する。
            if self._stdout_pump is not None:
                # stdout読取スレッドを短時間待つ。
                self._stdout_pump.stop()
            # stderr側も同様に停止する。
            if self._stderr_pump is not None:
                # stderr読取スレッドを短時間待つ。
                self._stderr_pump.stop()
            # 終了済みプロセス参照を破棄する。
            self._process = None
            # 所有状態を解除する。
            self._owned = False
            # ポンプ参照を破棄する。
            self._stdout_pump = None
            # stderrポンプ参照も破棄する。
            self._stderr_pump = None
            # 終了完了をGUIへ表示する。
            logger.info("BgUtils PO Token Provider stopped.")
