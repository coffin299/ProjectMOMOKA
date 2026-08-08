# ホスト専用 GUI API（127.0.0.1 + Bearer トークン）。ギルド管理者 API とは分離。

from __future__ import annotations

import asyncio
import hmac
import json
import queue
import secrets
import socket
from pathlib import Path
from typing import Any, Dict, Optional, Set

from fastapi import (
    APIRouter,
    Body,
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
from MOMOKA.GUI.user_data_purge import (
    delete_db_user_data,
    mask_log_lines,
    search_user_data,
)
from MOMOKA.services.log_sanitize import sanitize_log_message
from MOMOKA.storage import NS_LOG_VIEWER_CONFIG, get_default_settings_db

# 既定ポート（空きならこれを使う）
DEFAULT_HOST_GUI_PORT = 18765
# バインドは loopback 固定（LAN 公開パスを持たない）
BIND_HOST = "127.0.0.1"
# ルート接頭辞（将来 /guild と混ぜない）
API_PREFIX = "/host-gui"
# PUT /config で受理するキー
_CONFIG_ALLOWED_KEYS = frozenset({"font", "max_lines", "auto_scroll", "log_levels"})
# log_levels 内の許容カテゴリ
_LOG_LEVEL_KEYS = frozenset({"general", "llm", "tts", "error"})
# 許容ログレベル名
_ALLOWED_LOG_LEVELS = frozenset(
    {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"},
)
# max_lines 上限
_MAX_LINES_LIMIT = 50_000


class HostGuiAuth:
    """起動時ワンタイムトークンを保持する。"""

    def __init__(self, token: str) -> None:
        # 暗号論的乱数トークン
        self.token = token


def generate_host_gui_token() -> str:
    """ホスト GUI 用 Bearer トークンを生成する。"""
    # URL セーフな十分長い乱数
    return secrets.token_urlsafe(32)


def _tokens_match(provided: Optional[str], expected: str) -> bool:
    """トークンを安全比較する。型不正や比較例外は常に拒否。"""
    # 文字列以外は拒否する
    if not isinstance(provided, str) or not isinstance(expected, str):
        return False
    # 空トークンは拒否する
    if not provided or not expected:
        return False
    try:
        # タイミング攻撃耐性のある比較を使う
        return hmac.compare_digest(provided, expected)
    except (TypeError, ValueError):
        # 比較不能も拒否扱い
        return False


def _validate_config_body(body: Any, base: Dict[str, Any]) -> Dict[str, Any]:
    """PUT /config の body を検証し、マージ後の設定を返す。"""
    # body は dict 必須
    if not isinstance(body, dict):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="invalid_body",
        )
    # 未知キーは拒否する
    unknown = set(body.keys()) - _CONFIG_ALLOWED_KEYS
    if unknown:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="unknown_keys",
        )
    # 既定＋既存をベースにコピーする
    cfg = dict(base)
    # font: (name, size) タプル相当
    if "font" in body:
        font = body["font"]
        if (
            not isinstance(font, (list, tuple))
            or len(font) != 2
            or not isinstance(font[0], str)
            or not isinstance(font[1], int)
            or not (6 <= int(font[1]) <= 72)
        ):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="invalid_font",
            )
        cfg["font"] = [str(font[0]), int(font[1])]
    # max_lines: 正の int
    if "max_lines" in body:
        max_lines = body["max_lines"]
        if not isinstance(max_lines, int) or not (1 <= max_lines <= _MAX_LINES_LIMIT):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="invalid_max_lines",
            )
        cfg["max_lines"] = max_lines
    # auto_scroll: bool
    if "auto_scroll" in body:
        if not isinstance(body["auto_scroll"], bool):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="invalid_auto_scroll",
            )
        cfg["auto_scroll"] = body["auto_scroll"]
    # log_levels: カテゴリ→レベル
    if "log_levels" in body:
        levels = body["log_levels"]
        if not isinstance(levels, dict):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="invalid_log_levels",
            )
        if set(levels.keys()) - _LOG_LEVEL_KEYS:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="invalid_log_level_keys",
            )
        merged_levels = dict(cfg.get("log_levels") or {})
        for key, value in levels.items():
            if not isinstance(value, str) or value.upper() not in _ALLOWED_LOG_LEVELS:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="invalid_log_level_value",
                )
            merged_levels[str(key)] = value.upper()
        cfg["log_levels"] = merged_levels
    return cfg


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
        # 不一致（比較例外含む）は常に 401
        if not _tokens_match(token, auth.token):
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
        # スキーマ検証してから上書きする
        cfg = _validate_config_body(body, cfg)
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

    @router.get("/privacy/search")
    def privacy_search(
        user_id: str = Query(..., min_length=1),
        _: None = Depends(require_token),
    ) -> Dict[str, Any]:
        """ユーザー ID でログ・DB を横断検索する。"""
        # 数字以外は拒否
        if not str(user_id).isdigit():
            # 不正入力
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="invalid_user_id",
            )
        # 検索実行
        return search_user_data(int(user_id))

    @router.post("/privacy/logs/mask")
    def privacy_mask_logs(
        body: Dict[str, Any] = Body(default_factory=dict),
        _: None = Depends(require_token),
    ) -> Dict[str, Any]:
        """指定ログ行を日時のみ残してマスクする。"""
        # items 配列を取る
        items = body.get("items") if isinstance(body, dict) else None
        # 不正なら空扱い
        if not isinstance(items, list):
            # 空リスト
            items = []
        # マスク実行
        return mask_log_lines(items)

    @router.post("/privacy/db/delete")
    def privacy_delete_db(
        body: Dict[str, Any] = Body(default_factory=dict),
        _: None = Depends(require_token),
    ) -> Dict[str, Any]:
        """ユーザー紐付け DB 行を削除する。"""
        # 本体が dict でなければ拒否
        if not isinstance(body, dict):
            # 不正ボディ
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="invalid_body",
            )
        # user_id 必須
        raw_uid = body.get("user_id")
        # 数字チェック
        if raw_uid is None or not str(raw_uid).isdigit():
            # 不正
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="invalid_user_id",
            )
        # 削除実行
        return delete_db_user_data(
            int(raw_uid),
            delete_all=bool(body.get("all")),
            auto_join=body.get("auto_join") if isinstance(body.get("auto_join"), list) else None,
            vc_sessions=(
                body.get("vc_sessions")
                if isinstance(body.get("vc_sessions"), list)
                else None
            ),
        )

    @router.websocket("/logs")
    async def ws_logs(websocket: WebSocket) -> None:
        """ログストリーム（クエリ token 不可。subprotocol Bearer または初回メッセージ）。"""
        # Sec-WebSocket-Protocol から Bearer 系を探す
        raw_protocols = websocket.headers.get("sec-websocket-protocol") or ""
        # カンマ区切りプロトコル一覧
        offered = [part.strip() for part in raw_protocols.split(",") if part.strip()]
        # 採用する subprotocol（エコー返却用）
        chosen_subprotocol: Optional[str] = None
        # 抽出したトークン
        token: Optional[str] = None
        # 各プロトコル候補を検査する
        for idx, proto in enumerate(offered):
            # bearer.<token> 形式
            if proto.lower().startswith("bearer."):
                # プレフィックス以降が本体
                token = proto[7:]
                # ネゴシエート用にそのまま採用
                chosen_subprotocol = proto
                break
            # 一部クライアント向け: 先頭が Bearer で次要素がトークン
            if proto.lower() == "bearer" and idx + 1 < len(offered):
                # 次要素をトークン候補にする
                token = offered[idx + 1]
                # Bearer を選択プロトコルにする
                chosen_subprotocol = proto
                break
        # subprotocol 提示ありならそれで完結（不一致は即切断）
        if offered:
            # トークン不一致または抽出失敗
            if not _tokens_match(token, auth.token):
                # 握手前に閉じる
                await websocket.close(code=4401)
                return
            # 選択プロトコル付きで accept
            if chosen_subprotocol:
                await websocket.accept(subprotocol=chosen_subprotocol)
            else:
                await websocket.accept()
        else:
            # クエリではなく初回メッセージ認証へ進む
            await websocket.accept()
            try:
                # 認証タイムアウト（秒）
                raw = await asyncio.wait_for(websocket.receive_text(), timeout=5.0)
            except Exception:
                # 受信失敗は切断
                await websocket.close(code=4401)
                return
            # JSON または生トークンを解釈する
            parsed_token: Optional[str] = None
            try:
                # JSON なら type/auth + token
                payload = json.loads(raw)
            except Exception:
                # 生文字列をトークン候補にする
                parsed_token = raw.strip() or None
            else:
                # dict 以外は拒否
                if isinstance(payload, dict):
                    # Bearer 風フィールドも許容
                    parsed_token = (
                        payload.get("token")
                        or payload.get("authorization")
                        or payload.get("auth")
                    )
                    # Authorization: Bearer x 形式なら抽出
                    if isinstance(parsed_token, str) and parsed_token.lower().startswith(
                        "bearer "
                    ):
                        parsed_token = parsed_token[7:].strip()
                else:
                    parsed_token = None
            # 不一致なら切断
            if not _tokens_match(
                parsed_token if isinstance(parsed_token, str) else None,
                auth.token,
            ):
                await websocket.close(code=4401)
                return
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
