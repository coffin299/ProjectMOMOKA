# ホスト専用 GUI API（127.0.0.1 + Bearer トークン）。ギルド管理者 API とは分離。

from __future__ import annotations

import asyncio
import queue
import secrets
import socket
from pathlib import Path
from typing import Any, Dict, Optional, Set

from fastapi import (
    APIRouter,
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Query,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from MOMOKA.GUI.bot_bridge import (
    build_status_payload,
    get_active_vc_snapshots,
    get_guild_list,
    get_llm_average_seconds,
    request_shutdown,
)
from MOMOKA.GUI.persistent_log import (
    DEFAULT_HISTORY_LINES,
    categorize_logger_name,
    load_log_history,
)
from MOMOKA.services.log_sanitize import sanitize_log_message
from MOMOKA.storage import NS_LOG_VIEWER_CONFIG, get_default_settings_db

# 既定ポート（空きならこれを使う）
DEFAULT_HOST_GUI_PORT = 18765
# バインドは loopback 固定（LAN 公開パスを持たない）
BIND_HOST = "127.0.0.1"
# ルート接頭辞（将来 /guild と混ぜない）
API_PREFIX = "/host-gui"


class HostGuiAuth:
    """起動時ワンタイムトークンを保持する。"""

    def __init__(self, token: str) -> None:
        # 暗号論的乱数トークン
        self.token = token


def generate_host_gui_token() -> str:
    """ホスト GUI 用 Bearer トークンを生成する。"""
    # URL セーフな十分長い乱数
    return secrets.token_urlsafe(32)


def find_free_port(preferred: int = DEFAULT_HOST_GUI_PORT) -> int:
    """preferred が空いていればそれを、ダメなら OS 割り当てポートを返す。"""
    # 希望ポートを試す
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        # すぐ再利用可能に
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            # loopback のみで bind 試行
            sock.bind((BIND_HOST, preferred))
            # 取れれば希望ポート
            return preferred
        except OSError:
            # 使用中なら OS に任せる
            pass
    # 空きポートを OS に選ばせる
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        # port 0
        sock.bind((BIND_HOST, 0))
        # 割り当て結果
        return int(sock.getsockname()[1])


def _extract_bearer(authorization: Optional[str]) -> Optional[str]:
    """Authorization: Bearer からトークンを取り出す。"""
    # ヘッダ無し
    if not authorization:
        return None
    # 前後空白
    value = authorization.strip()
    # Bearer スキーム
    if value.lower().startswith("bearer "):
        # 本体を返す
        return value[7:].strip()
    # スキーム不明は拒否扱い
    return None


def create_host_gui_app(
    log_queue: queue.Queue,
    auth: HostGuiAuth,
) -> FastAPI:
    """ホスト GUI 用 FastAPI アプリを構築する。"""
    # アプリ本体（docs はローカルでも余計な面を減らすため閉じる）
    app = FastAPI(
        title="MOMOKA Host GUI API",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    # CORS: ブラウザ横断を許可しない（allow_origins 空相当）
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[],
        allow_credentials=False,
        allow_methods=[],
        allow_headers=[],
    )
    # ルーター（/host-gui 配下）
    router = APIRouter(prefix=f"{API_PREFIX}/api")
    # 接続中 WebSocket 集合
    subscribers: Set[WebSocket] = set()
    # 設定 DB
    settings_db = get_default_settings_db()

    def require_token(
        authorization: Optional[str] = Header(default=None),
        x_momoka_host_token: Optional[str] = Header(default=None),
    ) -> None:
        """REST 用トークン検証。不一致は 401。"""
        # Bearer または専用ヘッダ
        token = _extract_bearer(authorization) or (
            x_momoka_host_token.strip() if x_momoka_host_token else None
        )
        # 不一致
        if not token or not secrets.compare_digest(token, auth.token):
            # 認証失敗
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="unauthorized",
            )

    def _default_config() -> Dict[str, Any]:
        """ログビューア既定設定。"""
        # Tk 時代と同型
        return {
            "font": ("Meiryo UI", 9),
            "max_lines": 1000,
            "auto_scroll": True,
            "log_levels": {
                "general": "INFO",
                "llm": "INFO",
                "tts": "INFO",
                "error": "WARNING",
            },
        }

    @router.get("/status")
    def get_status(_: None = Depends(require_token)) -> Dict[str, Any]:
        """稼働ステータス。"""
        # bot_bridge に委譲
        return build_status_payload()

    @router.get("/vc")
    def get_vc(_: None = Depends(require_token)) -> Dict[str, Any]:
        """Active VC 一覧。"""
        # スナップショット
        return {"items": get_active_vc_snapshots()}

    @router.get("/guilds")
    def get_guilds(_: None = Depends(require_token)) -> Dict[str, Any]:
        """参加サーバー id/name。"""
        # メンバー無し
        return {"items": get_guild_list()}

    @router.get("/llm/stats")
    def get_llm_stats(_: None = Depends(require_token)) -> Dict[str, Any]:
        """LLM 平均応答秒。"""
        # 平均値
        return {"average_seconds": get_llm_average_seconds()}

    @router.post("/shutdown")
    def post_shutdown(_: None = Depends(require_token)) -> Dict[str, Any]:
        """全 Bot シャットダウン要求。"""
        # スケジューリング結果
        ok = request_shutdown()
        # 失敗時は 503
        if not ok:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="shutdown_unavailable",
            )
        # 受け付け
        return {"ok": True}

    @router.get("/config")
    def get_config(_: None = Depends(require_token)) -> Dict[str, Any]:
        """ホスト専用 log_viewer_config。"""
        # 既定
        cfg = _default_config()
        try:
            # DB から読む
            saved = settings_db.load(NS_LOG_VIEWER_CONFIG)
            # dict ならマージ
            if isinstance(saved, dict):
                cfg.update(saved)
        except Exception:
            # 破損時は既定
            pass
        # 返す
        return cfg

    @router.put("/config")
    def put_config(
        body: Dict[str, Any],
        _: None = Depends(require_token),
    ) -> Dict[str, Any]:
        """ホスト専用設定を保存する（ギルド設定は扱わない）。"""
        # 既定ベース
        cfg = _default_config()
        try:
            # 既存を読む
            saved = settings_db.load(NS_LOG_VIEWER_CONFIG)
            # マージ
            if isinstance(saved, dict):
                cfg.update(saved)
        except Exception:
            # 無視
            pass
        # リクエストで上書き
        if isinstance(body, dict):
            cfg.update(body)
        # ホストネームスペースへ保存
        settings_db.save(NS_LOG_VIEWER_CONFIG, cfg)
        # 保存後を返す
        return cfg

    def _categorize(name: str, level: str) -> str:
        """ログカテゴリを決める。"""
        # persistent_log と同一ロジック
        return categorize_logger_name(name, level)

    @router.get("/logs/history")
    def get_logs_history(
        max_lines: int = Query(default=DEFAULT_HISTORY_LINES, ge=1, le=50_000),
        _: None = Depends(require_token),
    ) -> Dict[str, Any]:
        """起動復元用: data/momoka_gui.log 末尾を返す。"""
        # 末尾パース
        items = load_log_history(max_lines=max_lines)
        # クライアントへ
        return {"items": items, "source": "momoka_gui.log"}

    @router.websocket("/logs")
    async def ws_logs(
        websocket: WebSocket,
        token: Optional[str] = Query(default=None),
    ) -> None:
        """ログストリーム（クエリ token 必須）。"""
        # クエリトークン検証
        if not token or not secrets.compare_digest(token, auth.token):
            # 握手前に閉じる
            await websocket.close(code=4401)
            return
        # 接続受理
        await websocket.accept()
        # 購読者に追加
        subscribers.add(websocket)
        try:
            # クライアント切断待ち
            while True:
                # 受信（無視して生存確認）
                await websocket.receive_text()
        except WebSocketDisconnect:
            # 切断
            pass
        finally:
            # 購読解除
            subscribers.discard(websocket)

    async def _pump_logs() -> None:
        """キューから WebSocket 購読者へログを流す。"""
        # 永続ループ
        while True:
            try:
                # 短タイムアウトでキュー取得
                name, level, message = await asyncio.to_thread(
                    log_queue.get, True, 0.25
                )
            except queue.Empty:
                # 空なら継続
                await asyncio.sleep(0.05)
                continue
            except Exception:
                # 想定外は短休止
                await asyncio.sleep(0.1)
                continue
            # 伏せ字
            safe = sanitize_log_message(str(message), max_length=100_000)
            # ペイロード
            payload = {
                "name": name,
                "level": level,
                "message": safe,
                "category": _categorize(str(name), str(level)),
            }
            # 購読者がいなければ破棄
            if not subscribers:
                continue
            # 切断済みを集める
            dead: list[WebSocket] = []
            # 全員へ送る
            for ws in list(subscribers):
                try:
                    # JSON 送信
                    await ws.send_json(payload)
                except Exception:
                    # 死んだ接続
                    dead.append(ws)
            # 掃除
            for ws in dead:
                subscribers.discard(ws)

    @app.on_event("startup")
    async def _on_startup() -> None:
        """ログポンプを起動する。"""
        # バックグラウンドタスク
        asyncio.create_task(_pump_logs())

    # ルーター登録
    app.include_router(router)

    # ビルド済み UI を同一オリジンで配信（file:// CORS 回避）
    dist_dir = Path(__file__).resolve().parents[2] / "gui-electron" / "dist"
    if dist_dir.is_dir():
        assets = dist_dir / "assets"
        if assets.is_dir():
            app.mount(
                "/assets",
                StaticFiles(directory=str(assets)),
                name="assets",
            )

        @app.get("/")
        def _index() -> FileResponse:
            """Electron が開くインデックス。"""
            return FileResponse(dist_dir / "index.html")

    # 返す
    return app
