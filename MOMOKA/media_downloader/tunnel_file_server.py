# MOMOKA/media_downloader/tunnel_file_server.py
# Cloudflare Tunnel 向け・localhost 限定の一時ファイル配信（DB 再起動耐性付き）。
from __future__ import annotations

import asyncio
import logging
import re
import secrets
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from urllib.parse import quote

from aiohttp import web

from MOMOKA.storage import NS_MEDIA_SHARE_ENTRIES, SettingsDB, resolve_settings_db

logger = logging.getLogger(__name__)

# ランダム部のバイト長（token_urlsafe）
_TOKEN_BYTES = 32
# URL / ストレージ上の固定プレフィックス
_TOKEN_PREFIX = "momoka-file-token-"
# トークン形式（path に載せる値・traversal 防止）
_TOKEN_RE = re.compile(
    rf"^{re.escape(_TOKEN_PREFIX)}[A-Za-z0-9_-]{{20,128}}$"
)
# 既定のローカル待受
_DEFAULT_BIND = "127.0.0.1"
_DEFAULT_PORT = 8765
_DEFAULT_EXPIRE = 600
# ストレージ相対パス
_DEFAULT_STORAGE = "temp_media_share"


@dataclass(frozen=True)
class MediaShareConfig:
    """media_share 設定の不変スナップショット。"""

    enabled: bool
    bind_host: str
    port: int
    public_base_url: str
    expire_seconds: int
    storage_dir: Path

    @classmethod
    def from_mapping(
        cls,
        config: Dict[str, Any],
        *,
        project_root: Optional[Path] = None,
    ) -> "MediaShareConfig":
        """マージ済み設定から media_share を読む。"""
        # リポジトリルート
        root = project_root or Path(__file__).resolve().parents[2]
        # external_services 配下
        services = config.get("external_services") or {}
        # media_share ブロック
        raw = services.get("media_share") or {}
        # 公開ベース URL（末尾スラッシュ除去）
        public = str(raw.get("public_base_url") or "").strip().rstrip("/")
        # ストレージパス
        storage = Path(str(raw.get("storage_dir") or _DEFAULT_STORAGE))
        # 相対ならリポジトリ基準
        if not storage.is_absolute():
            storage = root / storage
        # bind は loopback のみ許可（誤設定防止）
        bind_host = str(raw.get("bind_host") or _DEFAULT_BIND).strip() or _DEFAULT_BIND
        if bind_host not in ("127.0.0.1", "localhost", "::1"):
            # 強制的に loopback へ落とす
            logger.warning(
                "media_share.bind_host=%s is not loopback; forcing 127.0.0.1",
                bind_host,
            )
            bind_host = _DEFAULT_BIND
        # 有効期限秒
        expire = int(raw.get("expire_seconds") or _DEFAULT_EXPIRE)
        expire = max(60, min(expire, 172800))
        return cls(
            enabled=bool(raw.get("enabled", False)),
            bind_host=bind_host,
            port=int(raw.get("port") or _DEFAULT_PORT),
            public_base_url=public,
            expire_seconds=expire,
            storage_dir=storage,
        )


@dataclass
class _ShareEntry:
    """1 共有分のメタデータ。"""

    # ストレージ上の実ファイル
    path: Path
    # ダウンロード時の表示名
    filename: str
    # UNIX 失効時刻
    expires_at: float


class TunnelFileServer:
    """
    127.0.0.1 のみで GET /d/{token} を配信する。
    公開は Cloudflare Named Tunnel 経由。エントリは SettingsDB に永続化する。
    """

    def __init__(
        self,
        config: MediaShareConfig,
        *,
        settings_db: Optional[SettingsDB] = None,
    ) -> None:
        # 設定を保持
        self._config = config
        # 永続化ストア（未指定ならプロセス共通）
        self._settings_db = settings_db or resolve_settings_db()
        # token -> entry
        self._entries: Dict[str, _ShareEntry] = {}
        # エントリ操作の直列化
        self._lock = asyncio.Lock()
        # aiohttp アプリ / ランナー
        self._app: Optional[web.Application] = None
        self._runner: Optional[web.AppRunner] = None
        self._site: Optional[web.TCPSite] = None
        # 期限掃除タスク
        self._janitor_task: Optional[asyncio.Task[None]] = None

    @property
    def enabled(self) -> bool:
        """有効かつ公開 URL が揃っているか。"""
        return bool(self._config.enabled and self._config.public_base_url)

    @property
    def expire_seconds(self) -> int:
        """共有リンクの既定 TTL。"""
        return self._config.expire_seconds

    @property
    def public_base_url(self) -> str:
        """Cloudflare 側の公開ベース URL。"""
        return self._config.public_base_url

    def readiness_error(self) -> Optional[str]:
        """起動不可理由（問題なければ None）。"""
        if not self._config.enabled:
            return "media_share is disabled"
        if not self._config.public_base_url:
            return "media_share.public_base_url is not set"
        if not self._config.public_base_url.startswith("https://"):
            return "media_share.public_base_url must be https://"
        return None

    async def start(self) -> bool:
        """ローカル HTTP サーバを起動し、DB から有効エントリを復元する。"""
        # 設定不足なら起動しない
        err = self.readiness_error()
        if err:
            logger.warning("media_share not started: %s", err)
            return False
        # 二重起動防止
        if self._runner is not None:
            return True
        # ストレージを用意
        self._config.storage_dir.mkdir(parents=True, exist_ok=True)
        # DB + ディスクから有効分だけ復元（期限切れ・孤児は掃除）
        restored = self._restore_from_db()
        # ルートを組む
        app = web.Application()
        # 配信のみ（一覧・他パスは aiohttp 既定 404）
        app.router.add_get("/d/{token}", self._handle_download)
        self._app = app
        # ランナー起動
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        site = web.TCPSite(
            runner,
            host=self._config.bind_host,
            port=self._config.port,
        )
        await site.start()
        self._runner = runner
        self._site = site
        # 期限切れ掃除ループ
        self._janitor_task = asyncio.create_task(self._janitor_loop())
        logger.info(
            "media_share listening on %s:%s (public=%s restored=%s)",
            self._config.bind_host,
            self._config.port,
            self._config.public_base_url,
            restored,
        )
        return True

    async def stop(self) -> None:
        """
        HTTP だけ止める。
        有効期限内のファイルと DB は残し、再起動後もリンクを生かす。
        """
        # 掃除タスク停止
        if self._janitor_task is not None:
            self._janitor_task.cancel()
            try:
                await self._janitor_task
            except asyncio.CancelledError:
                pass
            self._janitor_task = None
        # 最新状態を DB へ書いてから止める
        async with self._lock:
            self._persist_entries_unlocked()
        # サイト停止
        if self._runner is not None:
            await self._runner.cleanup()
        self._runner = None
        self._site = None
        self._app = None
        # メモリは捨てる（ディスク/DB は残す）
        self._entries.clear()
        logger.info("media_share stopped (persisted entries kept on disk/db)")

    async def register(
        self,
        file_path: str,
        file_name: str,
        *,
        expire_seconds: Optional[int] = None,
    ) -> Tuple[Optional[str], Optional[str]]:
        """
        ファイルをストレージへ複製してトークン付き URL を返す。
        戻り値: (token, public_url)。失敗時 (None, None)。
        """
        # 未起動・未設定
        if not self.enabled or self._runner is None:
            logger.error("media_share register: server not ready")
            return None, None
        src = Path(file_path)
        # 存在確認
        if not src.is_file():
            logger.error("media_share register: missing file")
            return None, None
        # TTL
        ttl = int(expire_seconds or self._config.expire_seconds)
        ttl = max(60, min(ttl, 172800))
        # 推測困難なトークン（固定プレフィックス付き）
        token = f"{_TOKEN_PREFIX}{secrets.token_urlsafe(_TOKEN_BYTES)}"
        # 表示名（パス区切りを除去）
        safe_name = Path(file_name or src.name).name or "download.bin"
        # ストレージ先はトークン文字列そのもの
        dest = self._config.storage_dir / token
        try:
            # 配信用に複製（呼び出し側の一時削除と独立）
            shutil.copy2(src, dest)
        except OSError as exc:
            logger.error("media_share register: copy failed: %s", exc)
            return None, None
        entry = _ShareEntry(
            path=dest,
            filename=safe_name,
            expires_at=time.time() + ttl,
        )
        async with self._lock:
            self._entries[token] = entry
            # DB へ永続化
            self._persist_entries_unlocked()
        # 公開 URL
        url = f"{self._config.public_base_url}/d/{token}"
        # トークン全文はログに出さない
        logger.info(
            "media_share registered name=%s ttl=%ss",
            safe_name,
            ttl,
        )
        return token, url

    async def revoke(self, token: str) -> None:
        """トークンを無効化しストレージファイルと DB を更新する。"""
        if not token:
            return
        async with self._lock:
            await self._revoke_unlocked(token, delete_file=True)

    def _restore_from_db(self) -> int:
        """DB から有効エントリを復元し、孤児ファイルを掃除する。戻り値は復元件数。"""
        now = time.time()
        # DB 読込
        try:
            raw = self._settings_db.load(NS_MEDIA_SHARE_ENTRIES)
        except Exception:
            logger.exception("media_share db load failed")
            raw = None
        restored: Dict[str, _ShareEntry] = {}
        if isinstance(raw, dict):
            for token, meta in raw.items():
                # 形式チェック
                if not isinstance(token, str) or not _TOKEN_RE.match(token):
                    continue
                if not isinstance(meta, dict):
                    continue
                try:
                    expires_at = float(meta["expires_at"])
                    filename = str(meta.get("filename") or "download.bin")
                except (TypeError, ValueError, KeyError):
                    continue
                # 期限切れは捨てる
                if expires_at <= now:
                    path = self._config.storage_dir / token
                    try:
                        if path.is_file():
                            path.unlink()
                    except OSError:
                        pass
                    continue
                path = self._config.storage_dir / token
                # ファイルが無ければエントリも無効
                if not path.is_file():
                    continue
                restored[token] = _ShareEntry(
                    path=path,
                    filename=Path(filename).name or "download.bin",
                    expires_at=expires_at,
                )
        self._entries = restored
        # 正規化した内容を DB へ書き戻す
        self._persist_entries_unlocked()
        # DB に無い孤児ファイルを削除
        kept = set(restored.keys())
        try:
            for child in self._config.storage_dir.iterdir():
                if not child.is_file():
                    continue
                if child.name not in kept:
                    try:
                        child.unlink()
                    except OSError:
                        pass
        except OSError:
            logger.exception("media_share orphan purge failed")
        return len(restored)

    def _persist_entries_unlocked(self) -> None:
        """ロック保持中にメモリ上のエントリを DB へ全置換する。"""
        payload: Dict[str, Dict[str, Any]] = {}
        for token, entry in self._entries.items():
            payload[token] = {
                "filename": entry.filename,
                "expires_at": float(entry.expires_at),
            }
        try:
            self._settings_db.save(NS_MEDIA_SHARE_ENTRIES, payload)
        except Exception:
            logger.exception("media_share db save failed")

    async def _revoke_unlocked(self, token: str, *, delete_file: bool) -> None:
        """ロック保持中にエントリを外し、必要なら DB 更新する。"""
        entry = self._entries.pop(token, None)
        if entry is None:
            # メモリに無くても DB から落とすため永続化は行う
            self._persist_entries_unlocked()
            # ファイルだけ残っていれば消す
            if delete_file:
                orphan = self._config.storage_dir / token
                try:
                    if orphan.is_file():
                        orphan.unlink()
                except OSError:
                    pass
            return
        if delete_file:
            try:
                if entry.path.is_file():
                    entry.path.unlink()
            except OSError:
                logger.warning("media_share revoke: unlink failed")
        # DB 更新
        self._persist_entries_unlocked()

    async def _handle_download(self, request: web.Request) -> web.StreamResponse:
        """GET /d/{token} — 期限切れ・不明は 404。"""
        token = request.match_info.get("token", "")
        # 形式チェック（traversal 防止）
        if not _TOKEN_RE.match(token):
            return web.Response(status=404, text="Not Found")
        now = time.time()
        async with self._lock:
            entry = self._entries.get(token)
            # 無い / 期限切れ
            if entry is None or entry.expires_at <= now:
                if entry is not None:
                    await self._revoke_unlocked(token, delete_file=True)
                return web.Response(status=404, text="Not Found")
            path = entry.path
            filename = entry.filename
        # ファイル消失
        if not path.is_file():
            await self.revoke(token)
            return web.Response(status=404, text="Not Found")
        # 添付として返す
        resp = web.FileResponse(path)
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Cache-Control"] = "no-store"
        # RFC 5987 風のファイル名
        ascii_name = filename.encode("ascii", "ignore").decode("ascii") or "download.bin"
        utf8_name = quote(filename)
        resp.headers["Content-Disposition"] = (
            f'attachment; filename="{ascii_name}"; '
            f"filename*=UTF-8''{utf8_name}"
        )
        return resp

    async def _janitor_loop(self) -> None:
        """期限切れエントリを定期削除する。"""
        while True:
            await asyncio.sleep(30)
            now = time.time()
            async with self._lock:
                expired = [
                    t for t, e in self._entries.items() if e.expires_at <= now
                ]
                for token in expired:
                    await self._revoke_unlocked(token, delete_file=True)
