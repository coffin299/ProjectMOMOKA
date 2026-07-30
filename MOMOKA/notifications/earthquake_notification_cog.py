# MOMOKA/notifications/earthquake_notification_cog.py

import asyncio
import io
import json
import logging
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Dict, Literal, Optional

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks

# 最初にロガーを定義
logger = logging.getLogger('EarthquakeTsunamiCog')

# Matplotlibのインポート
MATPLOTLIB_AVAILABLE = False
CARTOPY_AVAILABLE = False
plt = None

try:
    import matplotlib

    matplotlib.use('Agg')
    import matplotlib.pyplot as plt

    MATPLOTLIB_AVAILABLE = True
    logger.info("✅ Matplotlibが正常にインポートされました。")

    # 日本語フォント設定（改善版）
    try:
        import japanize_matplotlib

        logger.info("✅ japanize_matplotlibが正常にインポートされました。")
    except ImportError:
        logger.info("ℹ️ japanize_matplotlibなし。代替フォントを設定します。")
        try:
            import matplotlib.font_manager as fm

            japanese_fonts = ['MS Gothic', 'Yu Gothic', 'Meiryo', 'MS UI Gothic', 'DejaVu Sans']
            available_fonts = [f.name for f in fm.fontManager.ttflist]

            for font in japanese_fonts:
                if font in available_fonts:
                    plt.rcParams['font.family'] = font
                    logger.info(f"✅ 日本語フォント設定: {font}")
                    break
            else:
                plt.rcParams['font.sans-serif'] = ['DejaVu Sans']
                logger.warning("⚠️ 日本語フォントが見つかりません。")
        except Exception as e:
            logger.debug(f"フォント設定エラー（続行）: {e}")

    # Cartopyのインポート
    try:
        import cartopy.crs as ccrs
        import cartopy.feature as cfeature

        CARTOPY_AVAILABLE = True
        logger.info("✅ Cartopyが正常にインポートされました。地図機能が有効です。")
    except ImportError as e:
        CARTOPY_AVAILABLE = False
        logger.warning(f"⚠️ Cartopyが見つかりません。地図機能は無効になります。")
        logger.error(f"   詳細エラー: {e}", exc_info=True)

except ImportError as e:
    MATPLOTLIB_AVAILABLE = False
    CARTOPY_AVAILABLE = False
    plt = None
    logger.error(f"❌ Matplotlibのインポートに失敗しました: {e}")
except Exception as e:
    MATPLOTLIB_AVAILABLE = False
    CARTOPY_AVAILABLE = False
    plt = None
    logger.error(f"❌ 予期しないエラーが発生しました: {e}", exc_info=True)

from MOMOKA.notifications.error.earthquake_errors import (
    EarthquakeTsunamiExceptionHandler,
    APIError,
    DataParsingError,
    ConfigError,
    NotificationError
)
from MOMOKA.storage import NS_EARTHQUAKE_CONFIG, resolve_settings_db

# 通知対象になり得る震度コード（P2P API v2。-1 は不明、99 は震度7程度以上）
ALL_NOTIFY_SCALES = [-1, 0, 10, 20, 30, 40, 45, 50, 55, 60, 70, 99]

# 設定 UI 用の震度ラベル（日本語固定）
NOTIFY_SCALE_LABELS = {
    -1: "震度不明",
    0: "震度0",
    10: "震度1",
    20: "震度2",
    30: "震度3",
    40: "震度4",
    45: "震度5弱",
    50: "震度5強",
    55: "震度6弱",
    60: "震度6強",
    70: "震度7",
    99: "震度7程度以上",
}

# 削除済みチャンネルと断定するまでに許容する NotFound 連続回数
NOT_FOUND_DELETE_THRESHOLD = 3


class InfoType(Enum):
    """情報タイプの定義"""
    EEW = "eew"
    QUAKE = "quake"
    TSUNAMI = "tsunami"
    UNKNOWN = "unknown"


def notification_embed_footer(*, test: bool = False) -> str:
    """配信 embed 用フッター文言を返す。"""
    # 1行目はデータ出典
    line1 = "Powered by P2P地震情報 WebSocket API | 気象庁"
    # 2行目は設定コマンド案内
    line2 = "設定: /earthquake_settings"
    # テスト時は先頭に明示する
    if test:
        # テストであることを先に出す
        return f"これはテスト通知です | {line1}\n{line2}"
    # 本番は2行
    return f"{line1}\n{line2}"


class EarthquakeTsunamiCog(commands.Cog, name="EarthquakeNotifications"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        logger.info("🔄 EarthquakeTsunamiCog 初期化開始...")

        # SettingsDB
        self.settings_db = resolve_settings_db(bot)
        self.config = self.load_config()

        self.last_ids: Dict[str, Optional[str]] = {
            InfoType.EEW.value: None, InfoType.QUAKE.value: None, InfoType.TSUNAMI.value: None
        }
        self.processed_ids: Dict[str, OrderedDict[str, None]] = {
            InfoType.EEW.value: OrderedDict(),
            InfoType.QUAKE.value: OrderedDict(),
            InfoType.TSUNAMI.value: OrderedDict(),
        }
        self.max_processed_ids = 1000
        # 設定ファイルの同時書き込みを直列化する
        self.config_save_lock = asyncio.Lock()
        # Discord API が返す NotFound の連続回数を通知先ごとに保持する
        self.not_found_counts: Dict[tuple[str, str], int] = {}

        self.ws_session = None
        self.ws_connection = None
        self.ws_reconnect_delay = 5
        self.ws_max_reconnect_delay = 300
        self.ws_running = False

        self.http_session = None
        self.jst = timezone(timedelta(hours=+9), 'JST')
        self.api_base_url = "https://api.p2pquake.net/v2"
        self.ws_url = "wss://api.p2pquake.net/v2/ws"
        self.request_headers = {'User-Agent': 'Discord-Bot-EarthquakeTsunami/3.0', 'Accept': 'application/json'}

        self.error_stats = {'api_errors': 0, 'parsing_errors': 0, 'network_errors': 0, 'ws_disconnects': 0,
                            'last_error_time': None}
        self.processing_stats = {'eew_processed': 0, 'quake_processed': 0, 'tsunami_processed': 0, 'unknown_skipped': 0,
                                 'last_stats_output': datetime.now(self.jst)}
        self.stats_interval = 3600

        self.exception_handler = EarthquakeTsunamiExceptionHandler(self)
        logger.info("✅ EarthquakeTsunamiCog 初期化完了")

    async def cog_load(self):
        logger.info("🔄 EarthquakeTsunamiCog セットアップ開始...")
        try:
            await self.recreate_http_session()
            logger.info("🔄 最新情報のIDを初期化中...")
            await self.initialize_processed_ids()

            self.ws_running = True
            asyncio.create_task(self.websocket_listener())

            self.output_stats_task.start()

            logger.info("✅ EarthquakeTsunamiCog セットアップ完了")
        except Exception as e:
            self.exception_handler.log_generic_error(e, "Cogのセットアップ")
            logger.critical(f"❌ セットアップに失敗しました: {e}")

    async def cog_unload(self):
        logger.info("🔄 EarthquakeTsunamiCog アンロード中...")

        self.ws_running = False
        if self.ws_connection and not self.ws_connection.closed:
            await self.ws_connection.close()
        if self.ws_session and not self.ws_session.closed:
            await self.ws_session.close()

        if self.http_session and not self.http_session.closed:
            await self.http_session.close()

        if hasattr(self, 'output_stats_task'):
            self.output_stats_task.cancel()

        logger.info("✅ EarthquakeTsunamiCog アンロード完了")

    async def websocket_listener(self):
        """WebSocketで地震情報をリアルタイム受信"""
        # 再接続待機秒数を初期値から始める
        reconnect_delay = self.ws_reconnect_delay

        # 停止フラグが立つまで再接続ループを回す
        while self.ws_running:
            try:
                # 接続開始を INFO ログへ残す
                logger.info(f"🔌 WebSocket接続開始: {self.ws_url}")

                # セッション未作成／クローズ済みなら作り直す
                if not self.ws_session or self.ws_session.closed:
                    # 新しい ClientSession を作る
                    self.ws_session = aiohttp.ClientSession(headers=self.request_headers)

                # WebSocket 接続を確立する
                async with self.ws_session.ws_connect(self.ws_url) as ws:
                    # 接続オブジェクトを状態へ保持する
                    self.ws_connection = ws
                    # 接続成功をログする
                    logger.info("✅ WebSocket接続成功")
                    # 成功したらバックオフを初期値へ戻す
                    reconnect_delay = self.ws_reconnect_delay

                    # サーバーからのメッセージを順に処理する
                    async for msg in ws:
                        # テキストフレームなら JSON として処理する
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            try:
                                # JSON を辞書へ変換する
                                data = json.loads(msg.data)
                                # 受信内容をデバッグログへ残す
                                logger.debug(
                                    f"WebSocket受信: code={data.get('code')}, id={data.get('_id') or data.get('id')}")
                                # 地震／津波メッセージを処理する
                                await self.process_websocket_message(data)
                            except json.JSONDecodeError as e:
                                # JSON 破損を ERROR で残す
                                logger.error(f"WebSocketメッセージのJSON解析エラー: {e}")
                                # パースエラー統計を加算する
                                self.error_stats['parsing_errors'] += 1
                            except Exception as e:
                                # 個別メッセージ処理失敗をハンドラへ渡す
                                self.exception_handler.log_generic_error(e, "WebSocketメッセージ処理")

                        # プロトコルエラーならループを抜ける
                        elif msg.type == aiohttp.WSMsgType.ERROR:
                            # 例外内容を ERROR で残す
                            logger.error(f"WebSocketエラー: {ws.exception()}")
                            # 受信ループを終了して再接続へ進む
                            break

                        # サーバー／ローカルのクローズは想定内として抜ける
                        elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSE):
                            # クローズを INFO で残す
                            logger.info("WebSocketがクローズされました。再接続します。")
                            # 受信ループを終了する
                            break

            except (aiohttp.ClientConnectionError, ConnectionResetError, BrokenPipeError) as e:
                # 切断中書き込みは想定内なので WARNING に落とす
                err_text = str(e).lower()
                # closing transport 系は再接続で回復する
                if "closing transport" in err_text or "cannot write" in err_text:
                    # ノイズを抑えるため WARNING にする
                    logger.warning(f"WebSocket切断中の書き込みを無視: {e}")
                else:
                    # P2P WS は頻繁に途切れるため INFO で残す
                    logger.info(f"WebSocket接続エラー: {e}")
                # ネットワーク／切断統計を加算する
                self.error_stats['network_errors'] += 1
                self.error_stats['ws_disconnects'] += 1
                # 壊れたセッションは次回作り直すためクローズする
                await self._reset_ws_session()
            except aiohttp.ClientError as e:
                # P2P WS は頻繁に途切れるため INFO で残す
                logger.info(f"WebSocket接続エラー: {e}")
                # 統計を加算する
                self.error_stats['network_errors'] += 1
                self.error_stats['ws_disconnects'] += 1
                # セッションをリセットする
                await self._reset_ws_session()
            except Exception as e:
                # 想定外例外をハンドラへ渡す
                self.exception_handler.log_generic_error(e, "WebSocket接続")
                # セッションをリセットする
                await self._reset_ws_session()
            finally:
                # 接続参照を必ずクリアする
                self.ws_connection = None

            # 停止要求が無ければバックオフして再接続する
            if self.ws_running:
                # 再接続待ちを WARNING で残す
                logger.warning(f"⚠️ WebSocket切断。{reconnect_delay}秒後に再接続...")
                # 指定秒数待機する
                await asyncio.sleep(reconnect_delay)
                # 指数バックオフで上限まで延ばす
                reconnect_delay = min(reconnect_delay * 2, self.ws_max_reconnect_delay)

    async def _reset_ws_session(self) -> None:
        """壊れた WebSocket 用 ClientSession を安全に破棄する。"""
        # 現在のセッション参照を取る
        session = self.ws_session
        # 参照を先にクリアする
        self.ws_session = None
        # 接続参照もクリアする
        self.ws_connection = None
        # セッションが残っていればクローズを試みる
        if session is not None and not session.closed:
            try:
                # セッションをクローズする
                await session.close()
            except Exception as e:
                # クローズ失敗は WARNING に留める
                logger.warning(f"WebSocketセッションクローズ失敗: {e}")

    async def process_websocket_message(self, data: Dict[str, Any]):
        """WebSocketから受信したメッセージを処理"""
        try:
            if not isinstance(data, dict):
                logger.debug("受信データが辞書型ではありません")
                return

            code = data.get('code', 0)

            if code not in [551, 552, 556]:
                logger.debug(f"処理対象外のcode: {code}")
                return

            info_id = self.extract_id_safe(data)
            if not info_id:
                logger.warning(f"IDを抽出できませんでした: {data}")
                return

            info_type = self.classify_info_type(data)

            if info_type == InfoType.UNKNOWN:
                self.processing_stats['unknown_skipped'] += 1
                logger.debug(f"UNKNOWN情報をスキップ: ID {info_id}, code={code}")
                return

            if info_id in self.processed_ids[info_type.value]:
                logger.debug(f"既に処理済みのID: {info_id} ({info_type.value})")
                return

            logger.info(f"🆕 WebSocketで新しい{info_type.value}情報を受信: ID {info_id}, code={code}")

            if info_type == InfoType.EEW:
                await self.send_eew_notification(data)
                self.processing_stats['eew_processed'] += 1
            elif info_type == InfoType.QUAKE:
                await self.send_quake_notification(data)
                self.processing_stats['quake_processed'] += 1
            elif info_type == InfoType.TSUNAMI:
                tsunami_info = self.get_tsunami_info(data)
                if tsunami_info.get('has_tsunami', False):
                    await self.send_tsunami_notification(data, tsunami_info)
                    self.processing_stats['tsunami_processed'] += 1
                else:
                    logger.debug(f"津波データなし: ID {info_id}")
                    return

            self.add_processed_id(info_type.value, info_id)
            self.last_ids[info_type.value] = info_id

        except NotificationError as e:
            logger.error(f"通知エラー: {e}", exc_info=True)
        except Exception as e:
            self.exception_handler.log_generic_error(e, "WebSocketメッセージ処理")

    async def recreate_http_session(self):
        if self.http_session and not self.http_session.closed:
            await self.http_session.close()
        self.http_session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=15),
            headers=self.request_headers,
            connector=aiohttp.TCPConnector(limit=10)
        )
        logger.info("HTTPセッションを再作成しました")

    async def safe_api_request(self, url: str, timeout: int = 15) -> Optional[Dict[str, Any]]:
        try:
            if not self.http_session or self.http_session.closed:
                await self.recreate_http_session()
            async with self.http_session.get(url, timeout=aiohttp.ClientTimeout(total=timeout)) as response:
                if response.status == 200:
                    try:
                        return await response.json()
                    except json.JSONDecodeError as e:
                        self.error_stats['last_error_time'] = datetime.now(self.jst)
                        raise self.exception_handler.handle_json_decode_error(e, url)
                else:
                    self.error_stats['last_error_time'] = datetime.now(self.jst)
                    raise self.exception_handler.handle_api_response_error(response.status, url)
        except Exception as e:
            if isinstance(e, (APIError, DataParsingError)):
                raise e
            self.error_stats['last_error_time'] = datetime.now(self.jst)
            raise self.exception_handler.handle_api_error(e, url)

    def manage_processed_ids(self, info_type: str):
        """処理済み ID を受信順のまま上限件数へ切り詰める。"""
        # 対象種別の順序付き ID 集合を取得する
        processed_ids = self.processed_ids[info_type]
        # 最も古い ID から上限超過分だけ削除する
        while len(processed_ids) > self.max_processed_ids:
            # 先頭は最も古い受信済み ID
            processed_ids.popitem(last=False)
            logger.info(f"{info_type}: 処理済みID数を{self.max_processed_ids}に制限")

    def add_processed_id(self, info_type: str, info_id: str) -> None:
        """処理済み ID を受信順で記録する。"""
        # 対象種別の順序付き ID 集合を取得する
        processed_ids = self.processed_ids[info_type]
        # 重複 ID は末尾へ移動できるよう、既存値を取り除く
        processed_ids.pop(info_id, None)
        # 最新の ID を末尾へ記録する
        processed_ids[info_id] = None
        # 古い ID を上限まで削除する
        self.manage_processed_ids(info_type)

    async def initialize_processed_ids(self):
        logger.info("🔍 最新情報のIDを初期化中...")

        # API コードと内部種別を対応付けて、各履歴を別々に初期化する
        history_sources = (
            (551, InfoType.QUAKE, "地震情報"),
            (552, InfoType.TSUNAMI, "津波情報"),
            (556, InfoType.EEW, "緊急地震速報"),
        )
        # 各 API コードの履歴を順に取得する
        for code, info_type, label in history_sources:
            # 当該コードだけを指定して最新 100 件を取得する
            url = f"{self.api_base_url}/history?codes={code}&limit=100"
            try:
                # 取得先をログへ残す
                logger.info(f"📡 {label}取得: {url}")
                # API 応答を取得する
                data = await self.safe_api_request(url)
                # 配列以外または空の応答は記録対象にしない
                if not data or not isinstance(data, list):
                    logger.warning(f"⚠️ {label}(code {code})の取得結果が空です")
                    continue
                # 取得件数をログへ残す
                logger.info(f"✅ {label}を{len(data)}件取得")
                # 履歴は API コードと一致する項目だけを処理する
                for item in reversed(data):
                    # 不正な項目は無視する
                    if not isinstance(item, dict) or item.get("code") != code:
                        continue
                    # ID を安全に取得する
                    item_id = self.extract_id_safe(item)
                    # ID がなければ重複排除できないため無視する
                    if item_id is None:
                        continue
                    # 古い履歴から順に記録して受信順を維持する
                    self.add_processed_id(info_type.value, item_id)
                # API は新しい順なので、最初の有効 ID を最後の ID として記録する
                latest_id = None
                # 新しい履歴から順に有効な ID を探す
                for item in data:
                    # 対象コード以外と不正な項目は無視する
                    if not isinstance(item, dict) or item.get("code") != code:
                        continue
                    # ID を安全に取得する
                    latest_id = self.extract_id_safe(item)
                    # 有効な ID が取れた時点で探索を終える
                    if latest_id is not None:
                        break
                # 有効な最新 ID が取得できたときだけ状態を更新する
                if latest_id is not None:
                    self.last_ids[info_type.value] = latest_id
                    logger.info(f"  {info_type.value.upper()}最新ID: {latest_id[:12]}...")
            except (APIError, DataParsingError) as error:
                # 既知の API エラーは種別とコードを付けて記録する
                logger.error(f"❌ {label}(code {code})のID初期化に失敗: {error}")
            except Exception as error:
                # 想定外の初期化失敗は共通ハンドラへ渡す
                self.exception_handler.log_generic_error(
                    error,
                    f"{label}(code {code})のID初期化",
                )

        logger.info("🔍 ID初期化結果:")
        for it, lid in self.last_ids.items():
            count = len(self.processed_ids.get(it, set()))
            logger.info(f"  {it.upper()}: {lid[:8] if lid else '未取得'} (処理済み: {count}件)")

    def extract_id_safe(self, item: Dict[str, Any]) -> Optional[str]:
        """IDを安全に抽出"""
        try:
            item_id = item.get('_id') or item.get('id')
            if item_id is None:
                return None
            return str(item_id)
        except Exception as e:
            logger.warning(f"ID抽出エラー: {e}")
            return None

    @tasks.loop(seconds=3600)
    async def output_stats_task(self):
        """統計情報を定期的に出力"""
        error_total = sum(v for k, v in self.error_stats.items() if k.endswith('_errors') or k == 'ws_disconnects')
        stats_msg = (
            f"[統計] EEW:{self.processing_stats['eew_processed']} "
            f"QUAKE:{self.processing_stats['quake_processed']} "
            f"TSUNAMI:{self.processing_stats['tsunami_processed']} "
            f"UNKNOWN:{self.processing_stats['unknown_skipped']} "
            f"エラー:{error_total} WS切断:{self.error_stats['ws_disconnects']}"
        )
        logger.info(stats_msg)

    def classify_info_type(self, item: Dict[str, Any]) -> InfoType:
        """情報タイプを判定"""
        try:
            code = item.get('code', 0)
            issue_type = item.get('issue', {}).get('type', '').lower()

            if code == 556:
                return InfoType.EEW

            if code == 552:
                return InfoType.TSUNAMI

            if code == 551:
                return InfoType.QUAKE

            logger.debug(f"UNKNOWN情報: code={code}, issue.type={issue_type}")
            return InfoType.UNKNOWN

        except Exception as e:
            logger.warning(f"情報分類エラー: {e}", exc_info=True)
            return InfoType.UNKNOWN

    def load_config(self) -> Dict[str, Any]:
        try:
            # SettingsDB から設定全体を読む
            config = self.settings_db.load(NS_EARTHQUAKE_CONFIG)
            # 無ければ空
            if config is None:
                return {}
            # dict でなければ空
            if not isinstance(config, dict):
                return {}
            for guild_id, value in list(config.items()):
                if isinstance(value, int):
                    config[guild_id] = {it.value: value for it in InfoType if it != InfoType.UNKNOWN}
                # ギルド設定を正規化する
                if isinstance(config.get(guild_id), dict):
                    # フィルタ系キーの欠落を埋める
                    self._normalize_guild_config(config[guild_id])
            return config
        except Exception as e:  # noqa: BLE001
            logger.warning(f"設定読み込みエラー: {e}")
        return {}

    async def save_config(self) -> None:
        """設定を排他制御付きで SettingsDB へ保存する。"""
        # 同時実行される設定変更を直列化する
        async with self.config_save_lock:
            try:
                # 設定全体を namespace に書く
                await self.settings_db.save_async(NS_EARTHQUAKE_CONFIG, self.config)
            except Exception as error:
                # 呼び出し元が扱える設定エラーとして送出する
                raise ConfigError(f"設定の保存に失敗しました: {error}") from error

    def _normalize_guild_config(self, guild_config: Dict[str, Any]) -> None:
        """ギルド設定にフィルタ用キーのデフォルトを補完する。"""
        # EEW 震度リストが無ければ全レベル＋不明
        if "notify_scales_eew" not in guild_config:
            # デフォルトは全通知
            guild_config["notify_scales_eew"] = list(ALL_NOTIFY_SCALES)
        # 通常地震も同様
        if "notify_scales_quake" not in guild_config:
            # デフォルトは全通知
            guild_config["notify_scales_quake"] = list(ALL_NOTIFY_SCALES)
        # 津波通知フラグが無ければオン
        if "notify_tsunami" not in guild_config:
            # 既存互換で true
            guild_config["notify_tsunami"] = True

    def ensure_guild_config(self, guild_id: str) -> Dict[str, Any]:
        """ギルド設定辞書を取得し、無ければ作成して正規化する。"""
        # 未作成なら空 dict
        if guild_id not in self.config or not isinstance(self.config.get(guild_id), dict):
            # 新規ギルド枠
            self.config[guild_id] = {}
        # 正規化する
        self._normalize_guild_config(self.config[guild_id])
        # 参照を返す
        return self.config[guild_id]

    def get_notify_scales(self, guild_id: str, info_type: str) -> list:
        """通知する震度コード一覧を返す（欠落時は全レベル）。"""
        # キーを決める
        key = f"notify_scales_{info_type}"
        # ギルド設定
        guild_config = self.config.get(guild_id) or {}
        # 生の値
        raw = guild_config.get(key)
        # 無ければデフォルト
        if raw is None:
            return list(ALL_NOTIFY_SCALES)
        # int 化して返す
        result = []
        for item in raw:
            try:
                result.append(int(item))
            except (TypeError, ValueError):
                continue
        return result

    async def set_notify_scales(
        self,
        guild_id: str,
        info_type: str,
        scales: list,
    ) -> None:
        """通知する震度コード一覧を保存する。"""
        # ギルド枠を確保
        guild_config = self.ensure_guild_config(guild_id)
        # 許可コードだけ残す
        allowed = set(ALL_NOTIFY_SCALES)
        cleaned = []
        for item in scales:
            try:
                code = int(item)
            except (TypeError, ValueError):
                continue
            if code in allowed and code not in cleaned:
                cleaned.append(code)
        # 安定した並びにする
        cleaned.sort(key=lambda c: ALL_NOTIFY_SCALES.index(c) if c in ALL_NOTIFY_SCALES else c)
        # 書き込む
        guild_config[f"notify_scales_{info_type}"] = cleaned
        # 永続化
        await self.save_config()

    def get_notify_tsunami(self, guild_id: str) -> bool:
        """津波通知が有効かどうかを返す。"""
        # ギルド設定
        guild_config = self.config.get(guild_id) or {}
        # デフォルト true
        return bool(guild_config.get("notify_tsunami", True))

    async def set_notify_tsunami(self, guild_id: str, enabled: bool) -> None:
        """津波通知の有効/無効を保存する。"""
        # ギルド枠を確保
        guild_config = self.ensure_guild_config(guild_id)
        # フラグを書く
        guild_config["notify_tsunami"] = bool(enabled)
        # 永続化
        await self.save_config()

    async def set_channels_unified(self, guild_id: str, channel_id: int) -> None:
        """EEW / 地震 / 津波の通知先を同一チャンネルに揃える。"""
        # ギルド枠を確保
        guild_config = self.ensure_guild_config(guild_id)
        # 3種に同じ ID
        for key in (InfoType.EEW.value, InfoType.QUAKE.value, InfoType.TSUNAMI.value):
            guild_config[key] = int(channel_id)
        # 永続化
        await self.save_config()

    async def set_channel_for_type(
        self,
        guild_id: str,
        info_type: str,
        channel_id: int,
    ) -> None:
        """指定種別の通知チャンネルを保存する。"""
        # ギルド枠を確保
        guild_config = self.ensure_guild_config(guild_id)
        # チャンネル ID を書く
        guild_config[info_type] = int(channel_id)
        # 永続化
        await self.save_config()

    def normalize_max_scale(self, max_scale: Any) -> int:
        """maxScale を比較用 int に正規化する（欠落は -1）。"""
        # None は不明
        if max_scale is None:
            return -1
        try:
            return int(max_scale)
        except (TypeError, ValueError):
            return -1

    def should_notify_by_scale(self, guild_id: str, info_type: str, max_scale: Any) -> bool:
        """震度フィルタに基づき通知すべきか判定する。"""
        # 正規化
        code = self.normalize_max_scale(max_scale)
        # 許可リストに含まれるか
        return code in self.get_notify_scales(guild_id, info_type)

    def format_scales_summary(self, guild_id: str, info_type: str) -> str:
        """設定中の震度一覧を短い日本語にする。"""
        # 現在値
        scales = self.get_notify_scales(guild_id, info_type)
        # 空ならミュート
        if not scales:
            return "なし（すべて非通知）"
        # 全選択なら略記
        if set(scales) == set(ALL_NOTIFY_SCALES):
            return "すべて"
        # ラベル連結
        return "、".join(NOTIFY_SCALE_LABELS.get(s, str(s)) for s in scales)

    def scale_to_japanese(self, scale_code):
        if scale_code is None or scale_code == -1:
            return "震度情報なし"
        scale_map = {
            0: "震度0", 10: "震度1", 20: "震度2", 30: "震度3",
            40: "震度4", 45: "震度5弱", 50: "震度5強",
            55: "震度6弱", 60: "震度6強", 70: "震度7",
            99: "震度7程度以上",
        }
        return scale_map.get(scale_code, f"不明({scale_code})")

    def get_embed_color(self, scale_code, info_type="quake"):
        if info_type == "tsunami":
            return discord.Color.purple()
        if scale_code is None or scale_code == -1:
            return discord.Color.light_grey()
        if scale_code >= 55:
            return discord.Color.dark_red()
        if scale_code >= 50:
            return discord.Color.red()
        if scale_code >= 40:
            return discord.Color.orange()
        if scale_code >= 30:
            return discord.Color.gold()
        return discord.Color.blue()

    def parse_earthquake_time(self, time_str, announced_time=None):
        try:
            if isinstance(time_str, str) and time_str.strip():
                try:
                    return datetime.strptime(time_str, "%Y/%m/%d %H:%M:%S").replace(tzinfo=self.jst)
                except ValueError:
                    pass
            if announced_time and isinstance(announced_time, str):
                try:
                    return datetime.strptime(announced_time, "%Y/%m/%d %H:%M:%S").replace(tzinfo=self.jst)
                except ValueError:
                    pass
            return datetime.now(self.jst)
        except Exception:
            return datetime.now(self.jst)

    def format_magnitude(self, magnitude):
        try:
            if magnitude is None or magnitude == -1 or magnitude == "-1":
                return "不明"
            mag_value = float(magnitude)
            if mag_value == -1:
                return "不明"
            return f"M{mag_value:.1f}"
        except (ValueError, TypeError):
            return "不明"

    def format_depth(self, depth):
        try:
            if depth is None or depth == -1 or depth == "-1":
                return "不明"
            if isinstance(depth, str):
                if not depth.replace('km', '').replace('m', '').strip().isdigit():
                    return depth
                depth_value = int(depth.replace('km', '').strip())
            else:
                depth_value = int(depth)

            if depth_value == -1:
                return "不明"
            return "ごく浅い" if depth_value == 0 else f"{depth_value}km"
        except (ValueError, TypeError):
            return "不明"

    def get_tsunami_info(self, data):
        """津波情報を抽出"""
        info = {'has_tsunami': False, 'warning_level': None, 'areas': [], 'description': ""}
        try:
            if data.get('code') == 552:
                tsunami_data = data.get('tsunami')
                if not tsunami_data:
                    return info

                info['has_tsunami'] = True
                grades = {'MajorWarning': '大津波警報', 'Warning': '津波警報', 'Watch': '津波注意報'}
                highest_level = 0
                level_text = '津波予報'

                areas_data = tsunami_data.get('areas', [])
                for area in areas_data if isinstance(areas_data, list) else []:
                    if not isinstance(area, dict):
                        continue
                    grade = area.get('grade')
                    if grade == 'MajorWarning' and highest_level < 3:
                        highest_level, level_text = 3, grades[grade]
                    elif grade == 'Warning' and highest_level < 2:
                        highest_level, level_text = 2, grades[grade]
                    elif grade == 'Watch' and highest_level < 1:
                        highest_level, level_text = 1, grades[grade]
                    if area.get('name'):
                        info['areas'].append({'name': area['name'], 'grade': grades.get(grade, '情報')})

                info['warning_level'] = level_text
                return info

            earthquake_data = data.get('earthquake', {})
            domestic_tsunami = earthquake_data.get('domesticTsunami', 'None')

            if domestic_tsunami and domestic_tsunami not in ['None', '', None]:
                info['has_tsunami'] = True
                tsunami_map = {
                    'Checking': '津波の有無調査中',
                    'NonEffective': '津波の心配なし',
                    'Watch': '津波注意報',
                    'Warning': '津波警報',
                    'Unknown': '不明'
                }
                info['warning_level'] = tsunami_map.get(domestic_tsunami, domestic_tsunami)

        except Exception as e:
            logger.warning(f"津波情報取得エラー: {e}", exc_info=True)

        return info

    def get_eew_max_scale(self, data: Dict[str, Any]) -> int:
        """EEW の地域予測震度から最大値を取得する。"""
        # 比較可能な地域別予測震度を格納する
        scales = []
        # EEW 専用の areas 配列を取得する
        areas = data.get("areas", [])
        # 配列でない値は空の地域一覧として扱う
        if not isinstance(areas, list):
            return -1
        # 各地域の予測震度を確認する
        for area in areas:
            # 辞書以外の地域データは無視する
            if not isinstance(area, dict):
                continue
            # 上限値を優先し、未設定の場合だけ下限値へフォールバックする
            scale = area.get("scaleTo", area.get("scaleFrom"))
            # 数値として解釈できる予測震度だけを追加する
            if scale is not None:
                scales.append(self.normalize_max_scale(scale))
        # 地域が無い場合は震度不明、それ以外は最大予測震度を返す
        return max(scales, default=-1)

    def format_eew_areas(self, data: Dict[str, Any]) -> str:
        """EEW の予測地域を Discord embed 用の文字列へ整形する。"""
        # EEW 専用の areas 配列を取得する
        areas = data.get("areas", [])
        # 表示可能な地域情報を格納する
        formatted_areas = []
        # 配列以外は表示できないため空文字を返す
        if not isinstance(areas, list):
            return ""
        # 各地域を embed の1行へ変換する
        for area in areas:
            # 辞書以外の地域データは表示しない
            if not isinstance(area, dict):
                continue
            # 上限値を優先し、未設定の場合だけ下限値へフォールバックする
            scale_to = self.normalize_max_scale(
                area.get("scaleTo", area.get("scaleFrom"))
            )
            # 下限値は上限値が無い場合に上限値と同じ値として扱う
            scale_from = self.normalize_max_scale(
                area.get("scaleFrom", area.get("scaleTo"))
            )
            # 高い震度ほど目立つ絵文字を選ぶ
            emoji = (
                "🔴" if scale_to >= 55 else "🟠" if scale_to >= 50
                else "🟡" if scale_to >= 40 else "🟢" if scale_to >= 30
                else "🔵"
            )
            # 予測範囲がある場合は下限から上限までを表示する
            scale_text = (
                f"{self.scale_to_japanese(scale_from)}〜"
                f"{self.scale_to_japanese(scale_to)}"
                if scale_from != scale_to
                else self.scale_to_japanese(scale_to)
            )
            # 地域名を優先し、欠落時は府県予報区名を使う
            area_name = area.get("name") or area.get("pref") or "地域不明"
            # 地域別の到達予測時刻を取得する
            arrival_time = area.get("arrivalTime")
            # 到達予測時刻があれば表示へ付加する
            arrival_suffix = f"（到達予測: {arrival_time}）" if arrival_time else ""
            # 並び替え用の震度と表示行を記録する
            formatted_areas.append(
                (scale_to, f"{emoji} **{scale_text}** - {area_name}{arrival_suffix}")
            )
        # 高い予測震度順に最大 8 地域を表示する
        return "\n".join(
            text
            for _, text in sorted(
                formatted_areas,
                key=lambda item: item[0],
                reverse=True,
            )[:8]
        )

    async def send_eew_notification(self, data: Dict[str, Any]) -> None:
        """code 556 の EEW スキーマを専用 embed として配信する。"""
        # API のテスト情報は本番通知チャンネルへ送信しない
        if data.get("test") is True:
            logger.info("EEW テスト情報を本番チャンネルへ送信せずスキップしました")
            return
        # 取消フラグを取得する
        cancelled = data.get("cancelled") is True
        # 取消情報では earthquake が欠落し得るため、安全な辞書だけを使う
        earthquake = data.get("earthquake")
        earthquake = earthquake if isinstance(earthquake, dict) else {}
        # issue は API 情報番号と発表時刻の取得に使う
        issue = data.get("issue")
        issue = issue if isinstance(issue, dict) else {}
        # 地域予測から EEW 専用の最大予測震度を計算する
        max_scale = self.get_eew_max_scale(data)
        # 発生時刻を優先し、取消時などは発表時刻を使う
        event_time = earthquake.get("originTime") or issue.get("time")
        # Discord embed の時刻を JST で生成する
        timestamp = self.parse_earthquake_time(event_time, issue.get("time"))
        # 取消と通常通知でタイトルを切り替える
        title = "🚨 緊急地震速報（取消）" if cancelled else "🚨 緊急地震速報（警報）"
        # 取消では誤った震度を示さない専用の本文を使う
        description = (
            "発表されていた緊急地震速報は**取り消されました**。"
            if cancelled
            else (
                "強い揺れに警戒してください。"
                if max_scale == -1
                else (
                    f"**最大予測震度 {self.scale_to_japanese(max_scale)}** "
                    "程度の揺れが予想されます。"
                )
            )
        )
        # 取消は灰色、通常情報は予測震度に対応する色で embed を作る
        embed = discord.Embed(
            title=title,
            description=description,
            color=(
                discord.Color.light_grey()
                if cancelled
                else self.get_embed_color(max_scale, InfoType.EEW.value)
            ),
            timestamp=timestamp,
        )
        # 取消でない場合だけ earthquake の詳細を表示する
        if not cancelled:
            # 震源情報は EEW スキーマの earthquake 配下にある
            hypocenter = earthquake.get("hypocenter")
            hypocenter = hypocenter if isinstance(hypocenter, dict) else {}
            # 震源地名を欠落時も安全に表示する
            hypocenter_name = hypocenter.get("name") or "調査中"
            # 震源地を embed へ追加する
            embed.add_field(
                name="🌏 震源地",
                value=f"```{hypocenter_name}```",
                inline=True,
            )
            # 推定マグニチュードを embed へ追加する
            embed.add_field(
                name="📊 マグニチュード",
                value=f"```推定 {self.format_magnitude(hypocenter.get('magnitude'))}```",
                inline=True,
            )
            # 推定深さを embed へ追加する
            embed.add_field(
                name="📏 深さ",
                value=f"```{self.format_depth(hypocenter.get('depth'))}```",
                inline=True,
            )
            # EEW の地震発生時刻を表示する
            if earthquake.get("originTime"):
                embed.add_field(
                    name="🕐 発生時刻",
                    value=f"```{earthquake['originTime']}```",
                    inline=True,
                )
            # EEW の主要動到達予測時刻を表示する
            if earthquake.get("arrivalTime"):
                embed.add_field(
                    name="⏱️ 主要動到達予測",
                    value=f"```{earthquake['arrivalTime']}```",
                    inline=True,
                )
            # EEW 専用地域配列を表示用文字列へ変換する
            areas_text = self.format_eew_areas(data)
            # 地域情報があるときだけ予測地域として表示する
            if areas_text:
                embed.add_field(
                    name="📍 予測地域・予測震度",
                    value=areas_text[:1024],
                    inline=False,
                )
            # 地域情報が無い場合は不足を明示する
            else:
                embed.add_field(
                    name="📍 予測地域・予測震度",
                    value="予測地域の詳細は現在取得できていません。",
                    inline=False,
                )
            # 速報の安全行動を案内する
            embed.add_field(
                name="⚠️ 注意",
                value="この情報は速報です。揺れが予想される地域では身の安全を確保してください。",
                inline=False,
            )
        # 情報番号があれば取消を含む通知の識別に使う
        if issue.get("eventId") or issue.get("serial"):
            # 空値を除外して情報番号を連結する
            identifier = " / ".join(
                str(value)
                for value in (issue.get("eventId"), issue.get("serial"))
                if value
            )
            # 情報番号を embed へ追加する
            embed.add_field(name="ℹ️ 情報番号", value=f"`{identifier}`", inline=False)
        # 共通の情報元フッターを設定する
        embed.set_footer(text=notification_embed_footer())
        # P2Pquake のロゴを表示する
        embed.set_thumbnail(url="https://www.p2pquake.net/images/QuakeLogo_100x100.png")
        # 取消は震度フィルタに関わらず、設定済み EEW チャンネルへ通知する
        await self.send_embed_to_channels(
            embed,
            InfoType.EEW.value,
            max_scale=max_scale,
            apply_scale_filter=not cancelled,
        )

    async def send_quake_notification(self, data):
        await self.send_notification(data, InfoType.QUAKE.value, "📊 地震情報")

    async def send_notification(self, data, info_type, title_prefix):
        try:
            earthquake = data.get('earthquake', {})
            if not earthquake:
                logger.warning(f"{info_type}: earthquake データが存在しません")
                return

            hypocenter = earthquake.get('hypocenter', {})
            issue_data = data.get('issue', {})
            report_type = issue_data.get('type', '情報')
            max_scale = earthquake.get('maxScale', -1)
            quake_time = self.parse_earthquake_time(earthquake.get('time', ''), issue_data.get('time', ''))

            magnitude = hypocenter.get('magnitude', -1)
            depth = hypocenter.get('depth', -1)

            if info_type == InfoType.EEW.value:
                description = f"強い揺れに警戒してください。" if max_scale == -1 else f"**最大震度 {self.scale_to_japanese(max_scale)}** 程度の揺れが予想されます。"
                description += "\n⚠️ **これは速報です。情報が更新される可能性があります。**"
            else:
                description = f"**最大震度 {self.scale_to_japanese(max_scale)}** の地震が発生しました。"

            embed = discord.Embed(
                title=f"{title_prefix} ({report_type})",
                description=description,
                color=self.get_embed_color(max_scale, info_type),
                timestamp=quake_time
            )
            hypocenter_name = hypocenter.get('name', '不明')
            embed.add_field(name="🌏 震源地", value=f"```{hypocenter_name or '調査中'}```", inline=True)
            mag_prefix = "推定 " if info_type == InfoType.EEW.value else ""
            embed.add_field(name="📊 マグニチュード", value=f"```{mag_prefix}{self.format_magnitude(magnitude)}```",
                            inline=True)
            embed.add_field(name="📏 深さ", value=f"```{self.format_depth(depth)}```", inline=True)

            points = data.get('points', [])
            if points and isinstance(points, list):
                areas_text = ""
                field_name = "📍 予測震度" if info_type == InfoType.EEW.value else "📍 各地の震度"
                for point in sorted(points, key=lambda p: p.get('scale', 0), reverse=True)[:8]:
                    scale, addr = point.get('scale', -1), point.get('addr', '不明')
                    emoji = "🔴" if scale >= 55 else "🟠" if scale >= 50 else "🟡" if scale >= 40 else "🟢" if scale >= 30 else "🔵"
                    scale_suffix = " 程度" if info_type == InfoType.EEW.value else ""
                    areas_text += f"{emoji} **{self.scale_to_japanese(scale)}{scale_suffix}** - {addr}\n"
                if areas_text:
                    embed.add_field(name=field_name, value=areas_text[:1024], inline=False)
            elif info_type == InfoType.EEW.value:
                embed.add_field(name="📍 震度情報", value="詳細な震度情報は確定情報をお待ちください", inline=False)

            tsunami_info = self.get_tsunami_info(data)
            if tsunami_info['has_tsunami'] and info_type == InfoType.QUAKE.value:
                embed.add_field(name="🌊 津波情報",
                                value=f"🌊 **{tsunami_info.get('warning_level', '津波予報')}** が発表されています",
                                inline=False)
            if info_type == InfoType.EEW.value:
                embed.add_field(name="⚠️ 注意",
                                value="この情報は速報です。揺れが予想される地域の方は、身の安全を確保してください。",
                                inline=False)

            embed.set_footer(text=notification_embed_footer())
            embed.set_thumbnail(url="https://www.p2pquake.net/images/QuakeLogo_100x100.png")

            map_file = None
            if CARTOPY_AVAILABLE:
                lat = hypocenter.get('latitude')
                lon = hypocenter.get('longitude')

                if lat is not None and lon is not None:
                    try:
                        quake_data = {
                            'lat': lat,
                            'lon': lon,
                            'magnitude': magnitude,
                            'depth': depth,
                            'max_scale': max_scale,
                            'name': hypocenter_name,
                            'time': quake_time
                        }

                        map_buffer = await self.generate_single_earthquake_map(quake_data, info_type)
                        map_file = discord.File(fp=map_buffer, filename="earthquake_location.png")
                        embed.set_image(url="attachment://earthquake_location.png")
                    except Exception as e:
                        logger.warning(f"地図生成に失敗: {e}")

            await self.send_embed_to_channels(embed, info_type, map_file, max_scale=max_scale)

        except Exception as e:
            raise NotificationError(f"{info_type}通知処理エラー: {e}")

    async def send_tsunami_notification(self, data, tsunami_info):
        try:
            warning_level = tsunami_info.get('warning_level', '津波予報')
            emoji_map = {"大津波警報": "🔴", "津波警報": "🟠", "津波注意報": "🟡"}
            embed = discord.Embed(
                title=f"{emoji_map.get(warning_level, '🌊')} {warning_level}",
                description=f"**{warning_level}** が発表されました。",
                color=discord.Color.purple(),
                timestamp=datetime.now(self.jst)
            )
            earthquake = data.get('earthquake', {})
            if earthquake and isinstance(earthquake, dict):
                hypocenter = earthquake.get('hypocenter', {})
                magnitude = hypocenter.get('magnitude', -1)
                depth = hypocenter.get('depth', -1)
                embed.add_field(name="🌏 震源地", value=f"```{hypocenter.get('name', '不明')}```", inline=True)
                embed.add_field(name="📊 マグニチュード", value=f"```{self.format_magnitude(magnitude)}```", inline=True)
                embed.add_field(name="📏 深さ", value=f"```{self.format_depth(depth)}```", inline=True)

            areas = tsunami_info.get('areas', [])
            if areas and isinstance(areas, list):
                area_text = "".join(
                    f"🌊 **{area.get('grade', warning_level)}** - {area.get('name', '不明')}\n"
                    for area in areas[:5] if isinstance(area, dict)
                )
                if area_text:
                    embed.add_field(name="🏖️ 予報区域", value=area_text, inline=False)

            warning_text = (
                "⚠️ **直ちに避難してください** ⚠️\n高台や避難ビルなど安全な場所へ" if warning_level == "大津波警報"
                else "⚠️ **直ちに避難してください**\n海岸や川から離れ、高いところへ" if warning_level == "津波警報"
                else "⚠️ 海の中や海岸付近は危険です\n海から上がって、海岸から離れてください"
            )
            embed.add_field(name="⚠️ 避難指示", value=warning_text, inline=False)
            if tsunami_info.get('description'):
                embed.add_field(name="ℹ️ 詳細情報", value=tsunami_info['description'][:500], inline=False)

            embed.set_footer(text=notification_embed_footer())
            embed.set_thumbnail(url="https://www.p2pquake.net/images/QuakeLogo_100x100.png")
            await self.send_embed_to_channels(embed, InfoType.TSUNAMI.value)
        except Exception as e:
            raise NotificationError(f"津波通知処理エラー: {e}")

    async def generate_single_earthquake_map(self, quake: dict, info_type: str) -> io.BytesIO:
        """単一の地震の位置を地図に表示"""
        # 実行中のイベントループを取得する（3.11 では get_event_loop は非推奨）
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._generate_single_map_sync, quake, info_type)

    def _calculate_smart_map_extent(self, lat: float, lon: float, max_scale: int) -> tuple:
        """
        震源地の位置と震度に基づいて、最適な地図表示範囲を計算
        フィリピンなど遠方の地震にも対応
        """
        # 拡大した日本周辺の境界（フィリピンを含む）
        REGION_LON_MIN, REGION_LON_MAX = 118, 150
        REGION_LAT_MIN, REGION_LAT_MAX = 10, 46

        # 震源地が範囲外（フィリピンなど）の場合の判定
        is_far_south = lat < 24
        is_far_west = lon < 122

        # 震度に応じた基本ズーム範囲
        if max_scale >= 50:
            base_zoom = 5.0
        elif max_scale >= 40:
            base_zoom = 4.0
        else:
            base_zoom = 3.0

        # フィリピン付近の場合はズームを調整
        if is_far_south or is_far_west:
            base_zoom = max(base_zoom, 8.0)

        lon_span = base_zoom * 2
        lat_span = base_zoom * 1.6

        # 震源地からの距離を計算
        dist_to_west = lon - REGION_LON_MIN
        dist_to_east = REGION_LON_MAX - lon
        dist_to_south = lat - REGION_LAT_MIN
        dist_to_north = REGION_LAT_MAX - lat

        edge_threshold = base_zoom

        center_lon = lon
        center_lat = lat

        # 西端・東端の調整
        if dist_to_west < edge_threshold:
            center_lon = lon + (edge_threshold - dist_to_west) * 0.5
        elif dist_to_east < edge_threshold:
            center_lon = lon - (edge_threshold - dist_to_east) * 0.5

        # 南端・北端の調整（フィリピンなど南方向を特に考慮）
        if dist_to_south < edge_threshold:
            center_lat = lat + (edge_threshold - dist_to_south) * 0.5
        elif dist_to_north < edge_threshold:
            center_lat = lat - (edge_threshold - dist_to_north) * 0.5

        # 表示範囲を計算
        lon_min = center_lon - lon_span / 2
        lon_max = center_lon + lon_span / 2
        lat_min = center_lat - lat_span / 2
        lat_max = center_lat + lat_span / 2

        # 境界調整
        if lon_min < REGION_LON_MIN:
            shift = REGION_LON_MIN - lon_min
            lon_min = REGION_LON_MIN
            lon_max = min(lon_max + shift, REGION_LON_MAX)

        if lon_max > REGION_LON_MAX:
            shift = lon_max - REGION_LON_MAX
            lon_max = REGION_LON_MAX
            lon_min = max(lon_min - shift, REGION_LON_MIN)

        if lat_min < REGION_LAT_MIN:
            shift = REGION_LAT_MIN - lat_min
            lat_min = REGION_LAT_MIN
            lat_max = min(lat_max + shift, REGION_LAT_MAX)

        if lat_max > REGION_LAT_MAX:
            shift = lat_max - REGION_LAT_MAX
            lat_max = REGION_LAT_MAX
            lat_min = max(lat_min - shift, REGION_LAT_MIN)

        return (lon_min, lon_max, lat_min, lat_max)

    def _generate_single_map_sync(self, quake: dict, info_type: str) -> io.BytesIO:
        """単一の地震マップ画像を生成（台風風デザイン）"""
        lat, lon = quake['lat'], quake['lon']
        max_scale = quake['max_scale']

        fig = plt.figure(figsize=(16, 16), dpi=150, facecolor='#2c3e50')
        ax = fig.add_axes([0, 0, 1, 1], projection=ccrs.PlateCarree(), facecolor='#2c3e50')

        # スマートな地図範囲計算
        lon_min, lon_max, lat_min, lat_max = self._calculate_smart_map_extent(lat, lon, max_scale)
        ax.set_extent([lon_min, lon_max, lat_min, lat_max], crs=ccrs.PlateCarree())

        # 台風風のデザイン：海と陸の色分け
        ax.add_feature(cfeature.OCEAN, facecolor='#2c3e50', zorder=0)
        ax.add_feature(cfeature.LAND, facecolor='#95a5a6', edgecolor='none', zorder=1)
        ax.add_feature(cfeature.COASTLINE, edgecolor='white', linewidth=1.5, zorder=3)

        # 都道府県境界
        try:
            states = cfeature.NaturalEarthFeature(
                category='cultural',
                name='admin_1_states_provinces_lines',
                scale='10m',
                facecolor='none'
            )
            ax.add_feature(states, edgecolor='white', linewidth=0.6, alpha=0.5, zorder=2)
        except:
            logger.debug("都道府県境界の追加をスキップ")

        # グリッド線（白色）
        ax.gridlines(crs=ccrs.PlateCarree(), draw_labels=False,
                     linewidth=0.5, color='white', alpha=0.3, linestyle='--')

        # タイトル
        title_prefix = "緊急地震速報" if info_type == "eew" else "地震情報"
        title = f'{title_prefix} - 震源位置\n{quake["name"]}'
        ax.text(0.5, 0.98, title, transform=ax.transAxes,
                fontsize=18, fontweight='normal', ha='center', va='top', color='white',
                bbox=dict(boxstyle='round,pad=0.8', facecolor='black',
                          edgecolor='white', alpha=0.8, linewidth=2))

        # 主要都市のマーカー
        cities = {
            '札幌': (141.35, 43.06), '仙台': (140.87, 38.27), '東京': (139.69, 35.69),
            '名古屋': (136.91, 35.18), '大阪': (135.50, 34.69), '福岡': (130.42, 33.59),
            '那覇': (127.68, 26.21), 'マニラ': (120.98, 14.60)
        }

        displayed_cities = 0
        for city, (city_lon, city_lat) in cities.items():
            if lon_min <= city_lon <= lon_max and lat_min <= city_lat <= lat_max:
                ax.plot(city_lon, city_lat, marker='^', color='yellow',
                        markersize=8, zorder=8, transform=ccrs.Geodetic(),
                        markeredgecolor='black', markeredgewidth=1.5)
                ax.text(city_lon, city_lat + 0.15, city, fontsize=9, ha='center', color='white',
                        bbox=dict(boxstyle='round,pad=0.3', facecolor='black',
                                  edgecolor='yellow', alpha=0.85, linewidth=1),
                        transform=ccrs.Geodetic(), zorder=9, fontweight='normal')
                displayed_cities += 1

        # 震源地の色とサイズ
        def get_color_and_size(scale):
            if scale >= 70:
                return '#8B0000', 550
            elif scale >= 60:
                return '#DC143C', 500
            elif scale >= 55:
                return '#FF0000', 450
            elif scale >= 50:
                return '#FF4500', 400
            elif scale >= 45:
                return '#FF8C00', 350
            elif scale >= 40:
                return '#FFA500', 300
            elif scale >= 30:
                return '#FFD700', 250
            else:
                return '#87CEEB', 200

        color, size = get_color_and_size(max_scale)

        # 震源地をマーク
        ax.scatter(lon, lat, marker='x', c='red', s=size * 2,
                   linewidths=6, zorder=11, transform=ccrs.Geodetic())
        ax.scatter(lon, lat, c='red', s=size, alpha=0.8,
                   edgecolors='white', linewidths=3, zorder=10,
                   transform=ccrs.Geodetic(), label='震源')

        # 震源地情報
        info_text = f'震度: {self.scale_to_japanese(max_scale)}\n'
        if quake['magnitude'] != -1:
            info_text += f'M{quake["magnitude"]:.1f}\n'
        if quake['depth'] != -1:
            info_text += f'深さ: {quake["depth"]}km'

        zoom_range = (lon_max - lon_min) / 2
        text_offset = zoom_range * 0.6
        text_y = lat - text_offset

        if text_y < lat_min + 0.5:
            text_y = lat + text_offset

        text_x = lon
        if lon < lon_min + 1:
            text_x = lon_min + 1.5
        elif lon > lon_max - 1:
            text_x = lon_max - 1.5

        ax.text(text_x, text_y, info_text,
                fontsize=13, ha='center', va='top', color='white',
                bbox=dict(boxstyle='round,pad=0.7', facecolor='black',
                          edgecolor='red', linewidth=2.5, alpha=0.9),
                transform=ccrs.Geodetic(), zorder=12, fontweight='normal')

        # 凡例
        ax.legend(loc='upper left', frameon=True, fontsize=12,
                  fancybox=True, shadow=True, framealpha=0.9,
                  bbox_to_anchor=(0.02, 0.92), facecolor='black',
                  edgecolor='white', labelcolor='white')

        # 画像として保存
        buffer = io.BytesIO()
        plt.savefig(buffer, format='png', dpi=150, bbox_inches='tight',
                    pad_inches=0, facecolor='#2c3e50', edgecolor='none')
        buffer.seek(0)
        plt.close(fig)

        return buffer

    def _generate_map_sync(self, quakes: list, min_scale: Optional[str], hours: Optional[int]) -> io.BytesIO:
        """複数の地震マップ画像を生成（台風風デザイン）"""
        fig = plt.figure(figsize=(16, 16), dpi=150, facecolor='#2c3e50')
        ax = fig.add_axes([0, 0, 1, 1], projection=ccrs.PlateCarree(), facecolor='#2c3e50')

        # 日本周辺に範囲を限定
        ax.set_extent([128, 146, 30, 46], crs=ccrs.PlateCarree())

        ax.add_feature(cfeature.OCEAN, facecolor='#2c3e50', zorder=0)
        ax.add_feature(cfeature.LAND, facecolor='#95a5a6', edgecolor='none', zorder=1)
        ax.add_feature(cfeature.COASTLINE, edgecolor='white', linewidth=1.5, zorder=3)

        # 都道府県境界
        try:
            states = cfeature.NaturalEarthFeature(
                category='cultural',
                name='admin_1_states_provinces_lines',
                scale='10m',
                facecolor='none'
            )
            ax.add_feature(states, edgecolor='white', linewidth=0.6, alpha=0.5, zorder=2)
        except:
            logger.debug("都道府県境界の追加をスキップ")

        # グリッド線
        ax.gridlines(crs=ccrs.PlateCarree(), draw_labels=False,
                     linewidth=0.5, color='white', alpha=0.3, linestyle='--')

        # タイトル
        if hours is not None:
            title = f'地震発生地点マップ（過去{hours}時間、{len(quakes)}件）'
        else:
            title = f'地震発生地点マップ（{len(quakes)}件）'
        if min_scale:
            title += f'\n最小震度: {min_scale}'
        ax.text(0.5, 0.98, title, transform=ax.transAxes,
                fontsize=18, fontweight='normal', ha='center', va='top', color='white',
                bbox=dict(boxstyle='round,pad=0.8', facecolor='black',
                          edgecolor='white', alpha=0.9, linewidth=2))

        # 震度に応じた色とサイズ
        def get_color_and_size(max_scale):
            if max_scale >= 70:
                return '#8B0000', 350, '震度7'
            elif max_scale >= 60:
                return '#DC143C', 300, '震度6強'
            elif max_scale >= 55:
                return '#FF0000', 250, '震度6弱'
            elif max_scale >= 50:
                return '#FF4500', 200, '震度5強'
            elif max_scale >= 45:
                return '#FF8C00', 150, '震度5弱'
            elif max_scale >= 40:
                return '#FFA500', 120, '震度4'
            elif max_scale >= 30:
                return '#FFD700', 100, '震度3'
            elif max_scale >= 20:
                return '#90EE90', 80, '震度2'
            else:
                return '#87CEEB', 60, '震度1'

        legend_elements = {}

        # 各地震をプロット
        for quake in quakes:
            color, size, label = get_color_and_size(quake['max_scale'])
            ax.scatter(quake['lon'], quake['lat'], c=color, s=size, alpha=0.7,
                       edgecolors='white', linewidths=1.5, zorder=5,
                       transform=ccrs.Geodetic())
            if label not in legend_elements:
                legend_elements[label] = plt.scatter([], [], c=color, s=120,
                                                     edgecolors='white', linewidths=1.5, alpha=0.7)

        # 凡例
        scale_order = ['震度7', '震度6強', '震度6弱', '震度5強', '震度5弱', '震度4', '震度3', '震度2', '震度1']
        legend_items = [legend_elements[s] for s in scale_order if s in legend_elements]
        legend_labels = [s for s in scale_order if s in legend_elements]

        if legend_items:
            legend = ax.legend(legend_items, legend_labels, loc='upper right', frameon=True,
                               fontsize=11, title='震度', title_fontsize=12,
                               fancybox=True, shadow=True, framealpha=0.9,
                               bbox_to_anchor=(0.98, 0.92), facecolor='black',
                               edgecolor='white')
            plt.setp(legend.get_texts(), color='white')
            plt.setp(legend.get_title(), color='white')

        # 主要都市
        cities = {
            '札幌': (141.35, 43.06), '東京': (139.69, 35.69),
            '名古屋': (136.91, 35.18), '大阪': (135.50, 34.69),
            '福岡': (130.42, 33.59),
        }

        for city, (lon, lat) in cities.items():
            ax.plot(lon, lat, marker='^', color='yellow', markersize=7,
                    zorder=4, transform=ccrs.Geodetic(),
                    markeredgecolor='black', markeredgewidth=1.2)
            ax.text(lon, lat + 0.35, city, fontsize=9, ha='center', color='white',
                    bbox=dict(boxstyle='round,pad=0.3', facecolor='black',
                              edgecolor='yellow', alpha=0.85, linewidth=0.8),
                    transform=ccrs.Geodetic(), zorder=4, fontweight='normal')

        # 画像として保存
        buffer = io.BytesIO()
        plt.savefig(buffer, format='png', dpi=150, bbox_inches='tight',
                    pad_inches=0, facecolor='#2c3e50', edgecolor='none')
        buffer.seek(0)
        plt.close(fig)

        return buffer

    def reset_not_found_count(self, guild_id: str, info_type: str) -> None:
        """NotFound 以外の結果を受けた通知先の連続回数をリセットする。"""
        # 通知先を表すキーを作る
        count_key = (guild_id, info_type)
        # 成功または別種別の失敗で連続 NotFound を途切れさせる
        self.not_found_counts.pop(count_key, None)

    async def send_embed_to_channels(
        self,
        embed: discord.Embed,
        info_type: str,
        map_file: Optional[discord.File] = None,
        max_scale: Optional[int] = None,
        apply_scale_filter: bool = True,
    ) -> None:
        """設定済みチャンネルへ embed を配信する。"""
        # 設定が無い場合は配信先が無いため終了する
        if not self.config:
            logger.warning(f"通知送信スキップ ({info_type}): config が空です")
            return

        # 配信開始をログへ残す
        logger.info(f"📤 {info_type}通知送信開始 - 設定ギルド数: {len(self.config)}")
        # 配信結果の集計値を初期化する
        sent_count, failed_count, skipped_count = 0, 0, 0
        # 保存が必要な設定変更があったかを記録する
        config_modified = False

        # 設定の走査中に辞書が変わっても安全なようにコピーを使う
        for guild_id, guild_config in self.config.copy().items():
            try:
                # 辞書でない壊れた設定は通知対象にしない
                if not isinstance(guild_config, dict):
                    logger.warning(
                        f"送信スキップ ({info_type}): ギルド {guild_id} の設定が辞書型ではありません"
                    )
                    skipped_count += 1
                    continue
                # 種別別の通知チャンネル ID を取得する
                channel_id = guild_config.get(info_type)
                # 通知先が未設定なら配信しない
                if not channel_id:
                    skipped_count += 1
                    continue
                # 津波通知オフ時は設定を保持したまま配信しない
                if (
                    info_type == InfoType.TSUNAMI.value
                    and not self.get_notify_tsunami(guild_id)
                ):
                    logger.info(f"津波通知オフでスキップ: ギルド {guild_id}")
                    skipped_count += 1
                    continue
                # 通常の EEW と地震情報には震度フィルタを適用する
                if (
                    apply_scale_filter
                    and info_type in (InfoType.EEW.value, InfoType.QUAKE.value)
                    and not self.should_notify_by_scale(guild_id, info_type, max_scale)
                ):
                    logger.info(
                        f"震度フィルタでスキップ ({info_type}): ギルド {guild_id}, "
                        f"maxScale={self.normalize_max_scale(max_scale)}"
                    )
                    skipped_count += 1
                    continue
                # キャッシュ上のギルドを取得する
                guild = self.bot.get_guild(int(guild_id))
                # キャッシュに無いだけでは退出を断定できないため設定は残す
                if guild is None:
                    logger.warning(
                        f"送信スキップ ({info_type}): ギルド {guild_id} を確認できません。"
                        "設定は削除しません。"
                    )
                    # NotFound ではないため連続回数をリセットする
                    self.reset_not_found_count(guild_id, info_type)
                    failed_count += 1
                    continue
                # キャッシュ上の通知チャンネルを取得する
                channel = guild.get_channel(int(channel_id))
                # キャッシュに無い場合は Discord API へ照会して削除済みか確認する
                if channel is None:
                    # API が返す NotFound だけを削除判定の対象にする
                    channel = await self.bot.fetch_channel(int(channel_id))
                # 別ギルドのチャンネルは安全のため配信しない
                if getattr(channel, "guild", None) != guild:
                    logger.warning(
                        f"送信スキップ ({info_type}): チャンネル {channel_id} は"
                        f"ギルド {guild_id} に属していません。設定は削除しません。"
                    )
                    # NotFound ではないため連続回数をリセットする
                    self.reset_not_found_count(guild_id, info_type)
                    failed_count += 1
                    continue
                # Bot 自身の当該チャンネル権限を確認する
                permissions = channel.permissions_for(guild.me)
                # 権限不足は一時的に解消し得るため設定を残して警告する
                if not permissions.send_messages or not permissions.embed_links:
                    logger.warning(
                        f"送信失敗 ({info_type}): チャンネル '{channel.name}' への権限が不足しています。"
                        "設定は削除しません。"
                    )
                    # NotFound ではないため連続回数をリセットする
                    self.reset_not_found_count(guild_id, info_type)
                    failed_count += 1
                    continue
                # 添付地図がある場合はチャンネルごとに独立したファイルを作る
                if map_file:
                    # 元ファイルの先頭から読み込む
                    map_file.fp.seek(0)
                    # 同じストリームを複数回送る競合を防ぐ
                    file_copy = discord.File(
                        fp=io.BytesIO(map_file.fp.read()),
                        filename=map_file.filename,
                    )
                    # embed と地図を送信する
                    await channel.send(embed=embed, file=file_copy)
                else:
                    # embed のみを送信する
                    await channel.send(embed=embed)
                # 送信成功時は連続 NotFound 回数をリセットする
                self.reset_not_found_count(guild_id, info_type)
                # 成功件数を加算する
                sent_count += 1
                # 成功先をログへ残す
                logger.info(f"✅ 送信成功: '{guild.name}' の '{channel.name}'")

            except discord.NotFound:
                # 実際に Discord API が NotFound を返した回数だけを記録する
                count_key = (guild_id, info_type)
                # 直前の連続失敗回数へ 1 を加える
                not_found_count = self.not_found_counts.get(count_key, 0) + 1
                # 更新後の連続失敗回数を保存する
                self.not_found_counts[count_key] = not_found_count
                # 閾値未満では設定を削除せず警告だけを残す
                if not_found_count < NOT_FOUND_DELETE_THRESHOLD:
                    logger.warning(
                        f"送信失敗 ({info_type}): NotFound {not_found_count}/"
                        f"{NOT_FOUND_DELETE_THRESHOLD} - ギルド {guild_id}。設定は保持します。"
                    )
                else:
                    # 連続 NotFound が閾値に達した場合だけ対象チャンネル設定を削除する
                    guild_settings = self.config.get(guild_id)
                    # 削除対象の設定が現在も存在するか確認する
                    if isinstance(guild_settings, dict) and info_type in guild_settings:
                        # 当該通知種別のチャンネル設定だけを削除する
                        del guild_settings[info_type]
                        # 設定変更を保存対象として記録する
                        config_modified = True
                        # 次の設定先では新たに連続回数を数える
                        self.not_found_counts.pop(count_key, None)
                        logger.warning(
                            f"🗑️ 連続 {NOT_FOUND_DELETE_THRESHOLD} 回の NotFound により、"
                            f"ギルド {guild_id} の {info_type} 通知先設定を削除しました。"
                        )
                # 送信失敗を集計する
                failed_count += 1
            except discord.Forbidden:
                # 権限エラーは回復し得るため設定を削除しない
                logger.warning(
                    f"送信失敗 ({info_type}): 権限不足 - ギルド {guild_id}。"
                    "設定は削除しません。"
                )
                # NotFound ではないため連続回数をリセットする
                self.reset_not_found_count(guild_id, info_type)
                # 送信失敗を集計する
                failed_count += 1
            except discord.HTTPException as error:
                # その他の Discord API エラーは設定を削除せず記録する
                logger.error(
                    f"送信失敗 ({info_type}): Discord APIエラー - {error.status}"
                )
                # NotFound ではないため連続回数をリセットする
                self.reset_not_found_count(guild_id, info_type)
                # 送信失敗を集計する
                failed_count += 1
            except Exception as error:
                # 想定外の失敗は設定を削除せず詳細をログへ残す
                logger.error(
                    f"予期せぬ送信失敗 ({info_type}): ギルド {guild_id}",
                    exc_info=True,
                )
                # NotFound ではないため連続回数をリセットする
                self.reset_not_found_count(guild_id, info_type)
                # 送信失敗を集計する
                failed_count += 1

        # 連続 NotFound により設定を削除した場合だけ原子的に保存する
        if config_modified:
            try:
                # 非同期ロック付きの保存処理を完了まで待つ
                await self.save_config()
                logger.info("💾 NotFound が連続した通知先設定を削除して保存しました")
            except Exception as error:
                # 保存失敗は配信ループを妨げずログへ残す
                logger.error(f"設定ファイルの保存に失敗: {error}")

        logger.info(
            f"📊 {info_type}通知送信完了: 成功 {sent_count}件, 失敗 {failed_count}件, スキップ {skipped_count}件")

        if sent_count == 0 and (failed_count > 0 or skipped_count > 0):
            logger.warning(f"⚠️ {info_type}の通知が1件も送信されませんでした")

    @app_commands.command(name="earthquake_channel", description="Set the notification channel for earthquake/tsunami alerts.")
    @app_commands.describe(channel="Channel to send notifications to.", info_type="Type of alert to notify.")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def set_channel(self, interaction: discord.Interaction, channel: discord.TextChannel,
                          info_type: Literal["緊急地震速報", "地震情報", "津波予報", "すべて"]):
        try:
            guild_id = str(interaction.guild.id)
            # 全種別指定は共通の設定保存処理へ委譲する
            if info_type == "すべて":
                await self.set_channels_unified(guild_id, channel.id)
            else:
                # 日本語の選択値を内部種別へ変換する
                type_map = {
                    "緊急地震速報": InfoType.EEW.value,
                    "地震情報": InfoType.QUAKE.value,
                    "津波予報": InfoType.TSUNAMI.value,
                }
                # 選択された1種別だけを保存する
                await self.set_channel_for_type(
                    guild_id,
                    type_map[info_type],
                    channel.id,
                )
            await interaction.response.send_message(
                f"✅ **{info_type}** の通知チャンネルを {channel.mention} に設定しました。\n"
                f"ℹ️ 震度フィルタ等は `/earthquake_settings` でも変更できます。"
            )
        except Exception as e:
            self.exception_handler.log_generic_error(e, "チャンネル設定コマンド")
            await interaction.response.send_message(self.exception_handler.get_user_friendly_message(e),
                                                    ephemeral=False)

    @app_commands.command(
        name="earthquake_settings",
        description="Configure earthquake/tsunami notification filters and channels.",
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    async def earthquake_settings(self, interaction: discord.Interaction):
        """Components V2 の地震通知設定画面を開く。"""
        # ギルド外は拒否
        if interaction.guild is None:
            await interaction.response.send_message("このコマンドはサーバー内でのみ使用できます。", ephemeral=True)
            return
        # 遅延インポート（循環参照回避）
        from MOMOKA.notifications.earthquake_settings_view import EarthquakeSettingsView
        # ギルド設定を正規化しておく
        self.ensure_guild_config(str(interaction.guild.id))
        # View 生成
        view = EarthquakeSettingsView(self.bot, self, interaction.guild)
        # ephemeral で送信（LayoutView は content なし）
        await interaction.response.send_message(view=view, ephemeral=True)

    @app_commands.command(name="earthquake_status", description="Check earthquake/tsunami system status.")
    async def status_system(self, interaction: discord.Interaction):
        try:
            await interaction.response.defer(ephemeral=False)
            embed = discord.Embed(
                title="🔧 地震・津波情報システム状態",
                color=discord.Color.blue(),
                timestamp=datetime.now(self.jst)
            )

            ws_status = "✅ 接続中" if self.ws_connection and not self.ws_connection.closed else "❌ 切断中"
            embed.add_field(name="🔌 WebSocket状態", value=ws_status, inline=True)

            embed.add_field(
                name="🌐 HTTPセッション",
                value="✅ 正常" if self.http_session and not self.http_session.closed else "❌ 無効",
                inline=True
            )

            id_status = ""
            for it, lid in self.last_ids.items():
                count = len(self.processed_ids.get(it, set()))
                id_status += f"**{it.upper()}**: `{lid[:8] if lid else '未取得'}` ({count}件)\n"
            embed.add_field(name="🆔 最後のID", value=id_status, inline=False)

            guild_id = str(interaction.guild.id)
            if guild_id in self.config:
                channel_status = ""
                type_map = {
                    InfoType.EEW.value: '緊急地震速報',
                    InfoType.QUAKE.value: '地震情報',
                    InfoType.TSUNAMI.value: '津波予報'
                }
                for it, name in type_map.items():
                    if it in self.config[guild_id]:
                        channel = interaction.guild.get_channel(self.config[guild_id][it])
                        status = f"✅ {channel.mention}" if channel else "❌ 削除済み"
                    else:
                        status = "⚠️ 未設定"
                    channel_status += f"**{name}**: {status}\n"
            else:
                channel_status = "⚠️ すべて未設定"

            embed.add_field(name="📢 通知チャンネル", value=channel_status, inline=False)

            # フィルタ状態
            filter_status = (
                f"**EEW 震度:** {self.format_scales_summary(guild_id, InfoType.EEW.value)}\n"
                f"**地震情報 震度:** {self.format_scales_summary(guild_id, InfoType.QUAKE.value)}\n"
                f"**津波通知:** {'オン' if self.get_notify_tsunami(guild_id) else 'オフ'}\n"
                f"ℹ️ 変更は `/earthquake_settings`"
            )
            embed.add_field(name="🎚️ 通知フィルタ", value=filter_status, inline=False)

            if self.error_stats['last_error_time']:
                embed.add_field(
                    name="🕐 最後のエラー",
                    value=self.error_stats['last_error_time'].strftime('%m/%d %H:%M:%S'),
                    inline=True
                )

            error_summary = (
                f"API: {self.error_stats['api_errors']} | "
                f"解析: {self.error_stats['parsing_errors']} | "
                f"WS切断: {self.error_stats['ws_disconnects']}"
            )
            embed.add_field(name="📊 エラー統計", value=error_summary, inline=False)

            embed.set_footer(text="システム診断完了 | P2P地震情報 WebSocket API")
            await interaction.followup.send(embed=embed)
        except Exception as e:
            self.exception_handler.log_generic_error(e, "ステータスコマンド")
            msg = self.exception_handler.get_user_friendly_message(e)
            if not interaction.response.is_done():
                await interaction.response.send_message(msg, ephemeral=False)
            else:
                await interaction.followup.send(msg)

    @app_commands.command(name="earthquake_test", description="Send a test earthquake/tsunami notification.")
    @app_commands.describe(
        info_type="Alert type to test.",
        max_scale="Maximum intensity for the test.",
        tsunami_level="Tsunami warning level for the test."
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    async def test_notification(
            self,
            interaction: discord.Interaction,
            info_type: Literal["緊急地震速報", "地震情報", "津波予報"],
            max_scale: Optional[Literal["震度3", "震度5強", "震度7"]] = "震度5強",
            tsunami_level: Optional[Literal["津波注意報", "津波警報", "大津波警報"]] = "津波警報"
    ):
        try:
            await interaction.response.defer(ephemeral=False)
            target_channel, is_configured = interaction.channel, False
            guild_id = str(interaction.guild.id)

            if guild_id in self.config:
                type_map = {
                    "緊急地震速報": InfoType.EEW.value,
                    "地震情報": InfoType.QUAKE.value,
                    "津波予報": InfoType.TSUNAMI.value
                }
                config_key = type_map.get(info_type)
                if config_key and config_key in self.config[guild_id]:
                    channel = interaction.guild.get_channel(self.config[guild_id][config_key])
                    if channel:
                        target_channel, is_configured = channel, True

            map_file = None
            embed = None

            if info_type == "津波予報":
                embed = await self.create_tsunami_test_embed(tsunami_level)
            else:
                scale_code = {"震度3": 30, "震度5強": 50, "震度7": 70}.get(max_scale, 50)
                embed = await self.create_earthquake_test_embed(info_type, max_scale, scale_code)

                if CARTOPY_AVAILABLE:
                    try:
                        test_quake_data = {
                            'lat': 36.0, 'lon': 140.5, 'magnitude': 7.0, 'depth': 30,
                            'max_scale': scale_code, 'name': 'テスト震源地 (関東沖)',
                            'time': datetime.now(self.jst)
                        }
                        info_type_value = "eew" if info_type == "緊急地震速報" else "quake"
                        map_buffer = await self.generate_single_earthquake_map(test_quake_data, info_type_value)
                        map_file = discord.File(fp=map_buffer, filename="earthquake_test_map.png")
                        embed.set_image(url="attachment://earthquake_test_map.png")
                    except Exception as e:
                        logger.warning(f"テスト通知の地図生成に失敗: {e}")

            await target_channel.send(embed=embed, file=map_file)

            msg = (
                f"✅ 設定されたチャンネル {target_channel.mention} に **{info_type}** のテスト通知を送信しました。"
                if is_configured
                else f"✅ このチャンネルに **{info_type}** のテスト通知を送信しました。\nℹ️ 本番の通知は `/earthquake_settings` または `/earthquake_channel` で設定したチャンネルに送信されます。"
            )
            await interaction.followup.send(msg)
        except discord.Forbidden:
            await interaction.followup.send(f"❌ {target_channel.mention} にメッセージを送信する権限がありません。")
        except Exception as e:
            self.exception_handler.log_generic_error(e, "テスト通知コマンド")
            await interaction.followup.send(self.exception_handler.get_user_friendly_message(e))

    async def create_earthquake_test_embed(self, info_type, max_scale, scale_code):
        title = (
            f"🚨【テスト】緊急地震速報 (予報)"
            if info_type == "緊急地震速報"
            else f"📊【テスト】地震情報"
        )
        description = f"**最大震度 {max_scale}** の地震が{'検知されました' if info_type == '緊急地震速報' else '発生しました'}。"

        embed = discord.Embed(
            title=title,
            description=description,
            color=self.get_embed_color(scale_code),
            timestamp=datetime.now(self.jst)
        )
        embed.add_field(name="🌏 震源地", value="```テスト震源地```", inline=True)
        embed.add_field(name="📊 マグニチュード", value="```M7.0```", inline=True)
        embed.add_field(name="📏 深さ", value="```30km```", inline=True)
        embed.add_field(
            name="📍 各地の震度",
            value=f"🔴 **{max_scale}** - テスト県A市\n🟠 **震度4** - テスト県B市\n🟡 **震度3** - テスト県C市",
            inline=False
        )
        embed.set_footer(text=notification_embed_footer(test=True))
        embed.set_thumbnail(url="https://www.p2pquake.net/images/QuakeLogo_100x100.png")
        return embed

    async def create_tsunami_test_embed(self, tsunami_level):
        emoji_map = {"津波注意報": "🟡", "津波警報": "🟠", "大津波警報": "🔴"}
        embed = discord.Embed(
            title=f"{emoji_map.get(tsunami_level, '🌊')}【テスト】{tsunami_level}",
            description=f"**{tsunami_level}** が発表されました。",
            color=discord.Color.purple(),
            timestamp=datetime.now(self.jst)
        )
        embed.add_field(name="🌏 震源地", value="```テスト海域```", inline=True)
        embed.add_field(name="📊 マグニチュード", value="```M7.5```", inline=True)
        embed.add_field(name="📏 深さ", value="```10km```", inline=True)
        embed.add_field(
            name="🏖️ 予報区域",
            value=f"🌊 **{tsunami_level}**\n・テスト県沿岸\n・テスト湾\n・テスト海岸",
            inline=False
        )
        warning_text = (
            "⚠️ **直ちに避難してください** ⚠️"
            if tsunami_level == "大津波警報"
            else "⚠️ 直ちに海岸や川から離れ、高いところに避難してください。"
            if tsunami_level == "津波警報"
            else "⚠️ 海の中や海岸付近は危険です。海から上がって、海岸から離れてください。"
        )
        embed.add_field(name="⚠️ 注意事項", value=warning_text, inline=False)
        embed.set_footer(text=notification_embed_footer(test=True))
        embed.set_thumbnail(url="https://www.p2pquake.net/images/QuakeLogo_100x100.png")
        return embed

    @app_commands.command(name="earthquake_remove", description="Remove earthquake/tsunami notification settings.")
    @app_commands.describe(info_type="Notification setting to remove.")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def remove_channel(
            self,
            interaction: discord.Interaction,
            info_type: Literal["緊急地震速報", "地震情報", "津波予報", "すべて"]
    ):
        try:
            guild_id = str(interaction.guild.id)

            if guild_id not in self.config:
                await interaction.response.send_message("❌ このサーバーには通知設定がありません。", ephemeral=False)
                return

            type_map = {
                "緊急地震速報": InfoType.EEW.value,
                "地震情報": InfoType.QUAKE.value,
                "津波予報": InfoType.TSUNAMI.value
            }

            removed_types = []

            if info_type == "すべて":
                # すべての設定を削除
                if guild_id in self.config:
                    del self.config[guild_id]
                    removed_types = ["緊急地震速報", "地震情報", "津波予報"]
                    await self.save_config()
                    await interaction.response.send_message(
                        "✅ **すべての通知設定** を削除しました。",
                        ephemeral=False
                    )
                else:
                    await interaction.response.send_message(
                        "❌ このサーバーには通知設定がありません。",
                        ephemeral=False
                    )
                return
            else:
                # 個別の設定を削除
                config_key = type_map[info_type]
                if config_key in self.config[guild_id]:
                    del self.config[guild_id][config_key]
                    removed_types.append(info_type)

                    # 設定が空になった場合はギルド設定自体も削除
                    if not self.config[guild_id]:
                        del self.config[guild_id]
                        logger.info(f"ギルド '{interaction.guild.name}' の設定が空になったため削除しました")

                    await self.save_config()
                    await interaction.response.send_message(
                        f"✅ **{info_type}** の通知設定を削除しました。",
                        ephemeral=False
                    )
                else:
                    await interaction.response.send_message(
                        f"❌ **{info_type}** の通知設定は存在しません。",
                        ephemeral=False
                    )

        except Exception as e:
            self.exception_handler.log_generic_error(e, "通知削除コマンド")
            await interaction.response.send_message(
                self.exception_handler.get_user_friendly_message(e),
                ephemeral=False
            )

    @app_commands.command(name="earthquake_help", description="Show help for this system.")
    async def help_system(self, interaction: discord.Interaction):
        embed = discord.Embed(
            title="📚 地震・津波情報システム ヘルプ",
            description=(
                "このボットは気象庁の地震・津波情報をリアルタイムで通知します（WebSocket接続）。\n"
                "通知フィルタ・チャンネルは `/earthquake_settings` で設定できます。"
            ),
            color=discord.Color.green(),
            timestamp=datetime.now(self.jst)
        )
        embed.add_field(
            name="🛠️ 利用可能なコマンド",
            value=(
                "**🔧 設定コマンド**\n"
                "`/earthquake_settings` - 通知先・震度・津波の設定（推奨）\n"
                "`/earthquake_channel` - 通知チャンネルを設定\n"
                "`/earthquake_remove` - 通知設定を削除\n"
                "`/earthquake_test` - テスト通知を送信\n\n"
                "**📊 情報表示コマンド**\n"
                "`/earthquake_status` - システム状態を確認\n"
                "`/earthquake_history` - 最近の地震履歴を表示\n"
                "`/earthquake_map` - 地震を地図上に表示\n"
                "`/earthquake_debug` - 詳細診断情報を表示\n\n"
                "**❓ その他**\n"
                "`/earthquake_help` - このヘルプを表示"
            ),
            inline=False
        )
        embed.set_footer(text=notification_embed_footer())
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="earthquake_map", description="Display recent earthquakes on a map of Japan.")
    @app_commands.describe(
        limit="Number of earthquakes to show (1-50).",
        min_scale="Minimum intensity to display.",
        hours="Show earthquakes within the past N hours (1-168 = 7 days)."
    )
    async def show_earthquake_map(
            self,
            interaction: discord.Interaction,
            limit: Optional[int] = 20,
            min_scale: Optional[Literal[
                "震度1", "震度2", "震度3", "震度4", "震度5弱", "震度5強", "震度6弱", "震度6強", "震度7"]] = None,
            hours: Optional[int] = 24
    ):
        try:
            await interaction.response.defer(ephemeral=False)

            if not CARTOPY_AVAILABLE:
                await interaction.followup.send("❌ 地図機能は現在利用できません。Bot管理者にお問い合わせください。")
                return

            limit = max(1, min(limit, 50))
            hours = max(1, min(hours, 168))

            scale_map = {
                "震度1": 10, "震度2": 20, "震度3": 30, "震度4": 40,
                "震度5弱": 45, "震度5強": 50, "震度6弱": 55, "震度6強": 60, "震度7": 70
            }
            min_scale_code = scale_map.get(min_scale, 0) if min_scale else 0

            cutoff_time = datetime.now(self.jst) - timedelta(hours=hours)

            url = f"{self.api_base_url}/history?codes=551&limit=100"
            data = await self.safe_api_request(url)

            if not data or not isinstance(data, list):
                await interaction.followup.send("❌ 地震情報の取得に失敗しました。")
                return

            filtered_quakes = []
            for item in data:
                info_type = self.classify_info_type(item)
                if info_type != InfoType.QUAKE:
                    continue

                earthquake = item.get('earthquake', {})
                max_scale = earthquake.get('maxScale', -1)

                if max_scale < min_scale_code:
                    continue

                issue = item.get('issue', {})
                quake_time = self.parse_earthquake_time(earthquake.get('time', ''), issue.get('time', ''))
                if quake_time < cutoff_time:
                    continue

                hypocenter = earthquake.get('hypocenter', {})
                lat = hypocenter.get('latitude')
                lon = hypocenter.get('longitude')

                if lat is not None and lon is not None:
                    filtered_quakes.append({
                        'lat': lat,
                        'lon': lon,
                        'magnitude': hypocenter.get('magnitude', -1),
                        'depth': hypocenter.get('depth', -1),
                        'max_scale': max_scale,
                        'name': hypocenter.get('name', '不明'),
                        'time': quake_time
                    })

                    if len(filtered_quakes) >= limit:
                        break

            if not filtered_quakes:
                filter_text = f"（{min_scale}以上、過去{hours}時間以内）" if min_scale else f"（過去{hours}時間以内）"
                await interaction.followup.send(f"ℹ️ 該当する地震情報{filter_text}が見つかりませんでした。")
                return

            image_buffer = await self.generate_earthquake_map(filtered_quakes, min_scale, hours)

            file = discord.File(fp=image_buffer, filename="earthquake_map.png")

            embed = discord.Embed(
                title=f"📍 地震発生地点マップ ({len(filtered_quakes)}件)",
                description=f"過去{hours}時間以内、最小震度: {min_scale or '指定なし'}",
                color=discord.Color.red(),
                timestamp=datetime.now(self.jst)
            )
            embed.set_image(url="attachment://earthquake_map.png")
            embed.set_footer(text="データ提供: P2P地震情報 API | PLANA by coffin299")

            await interaction.followup.send(embed=embed, file=file)

        except (APIError, DataParsingError) as e:
            logger.error(f"地図生成エラー: {e}")
            await interaction.followup.send(f"❌ 地震情報の取得中にエラーが発生しました: {e}")
        except Exception as e:
            self.exception_handler.log_generic_error(e, "地図表示コマンド")
            await interaction.followup.send(self.exception_handler.get_user_friendly_message(e))

    async def generate_earthquake_map(self, quakes: list, min_scale: Optional[str], hours: int) -> io.BytesIO:
        """地震マップ画像を生成"""
        # 実行中のイベントループを取得する（3.11 では get_event_loop は非推奨）
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._generate_map_sync, quakes, min_scale, hours)

    @app_commands.command(name="earthquake_history", description="Display recent earthquake information.")
    @app_commands.describe(
        limit="Number of earthquakes to show (1-20).",
        min_scale="Minimum intensity to display."
    )
    async def show_history(
            self,
            interaction: discord.Interaction,
            limit: Optional[int] = 10,
            min_scale: Optional[
                Literal["震度1", "震度2", "震度3", "震度4", "震度5弱", "震度5強", "震度6弱", "震度6強", "震度7"]] = None
    ):
        try:
            await interaction.response.defer(ephemeral=False)

            limit = max(1, min(limit, 20))

            scale_map = {
                "震度1": 10, "震度2": 20, "震度3": 30, "震度4": 40,
                "震度5弱": 45, "震度5強": 50, "震度6弱": 55, "震度6強": 60, "震度7": 70
            }
            min_scale_code = scale_map.get(min_scale, 0) if min_scale else 0

            url = f"{self.api_base_url}/history?codes=551&limit=100"
            data = await self.safe_api_request(url)

            if not data or not isinstance(data, list):
                await interaction.followup.send("❌ 地震情報の取得に失敗しました。")
                return

            filtered_quakes = []
            for item in data:
                info_type = self.classify_info_type(item)
                if info_type == InfoType.QUAKE:
                    max_scale = item.get('earthquake', {}).get('maxScale', -1)
                    if max_scale >= min_scale_code:
                        filtered_quakes.append(item)
                        if len(filtered_quakes) >= limit:
                            break

            if not filtered_quakes:
                filter_text = f"（{min_scale}以上）" if min_scale else ""
                await interaction.followup.send(f"ℹ️ 該当する地震情報{filter_text}が見つかりませんでした。")
                return

            map_quakes = []
            for quake in filtered_quakes:
                earthquake = quake.get('earthquake', {})
                hypocenter = earthquake.get('hypocenter', {})
                issue = quake.get('issue', {})

                lat = hypocenter.get('latitude')
                lon = hypocenter.get('longitude')

                if lat is not None and lon is not None:
                    max_scale = earthquake.get('maxScale', -1)
                    quake_time = self.parse_earthquake_time(earthquake.get('time', ''), issue.get('time', ''))
                    magnitude = hypocenter.get('magnitude', -1)
                    depth = hypocenter.get('depth', -1)

                    map_quakes.append({
                        'lat': lat,
                        'lon': lon,
                        'magnitude': magnitude,
                        'depth': depth,
                        'max_scale': max_scale,
                        'name': hypocenter.get('name', '不明'),
                        'time': quake_time
                    })

            embed = discord.Embed(
                title=f"📊 最近の地震情報 ({len(filtered_quakes)}件)",
                description=f"最小震度: {min_scale or '指定なし'}",
                color=discord.Color.blue(),
                timestamp=datetime.now(self.jst)
            )

            for idx, quake in enumerate(filtered_quakes, 1):
                earthquake = quake.get('earthquake', {})
                hypocenter = earthquake.get('hypocenter', {})
                issue = quake.get('issue', {})

                max_scale = earthquake.get('maxScale', -1)
                quake_time = self.parse_earthquake_time(earthquake.get('time', ''), issue.get('time', ''))
                magnitude = hypocenter.get('magnitude', -1)
                depth = hypocenter.get('depth', -1)
                location = hypocenter.get('name', '不明')

                emoji = "🔴" if max_scale >= 55 else "🟠" if max_scale >= 50 else "🟡" if max_scale >= 40 else "🟢" if max_scale >= 30 else "🔵"

                field_value = (
                    f"{emoji} **{self.scale_to_japanese(max_scale)}**\n"
                    f"🌏 {location}\n"
                    f"📊 {self.format_magnitude(magnitude)} / 📏 {self.format_depth(depth)}\n"
                    f"🕐 {quake_time.strftime('%m/%d %H:%M:%S')}"
                )

                embed.add_field(
                    name=f"{idx}. {quake_time.strftime('%m/%d %H:%M')}",
                    value=field_value,
                    inline=True if idx <= 3 else False
                )

                if idx % 3 == 0 and idx < len(filtered_quakes):
                    embed.add_field(name="\u200b", value="\u200b", inline=False)

            embed.set_footer(text="データ提供: P2P地震情報 API | PLANA by coffin299")
            embed.set_thumbnail(url="https://www.p2pquake.net/images/QuakeLogo_100x100.png")

            if map_quakes and CARTOPY_AVAILABLE:
                try:
                    map_buffer = await self.generate_earthquake_map(map_quakes, min_scale, None)
                    map_file = discord.File(fp=map_buffer, filename="earthquake_history_map.png")
                    embed.set_image(url="attachment://earthquake_history_map.png")
                    await interaction.followup.send(embed=embed, file=map_file)
                except Exception as e:
                    logger.warning(f"履歴地図生成に失敗: {e}")
                    await interaction.followup.send(embed=embed)
            else:
                await interaction.followup.send(embed=embed)

        except (APIError, DataParsingError) as e:
            logger.error(f"履歴取得エラー: {e}")
            await interaction.followup.send(f"❌ 地震情報の取得中にエラーが発生しました: {e}")
        except Exception as e:
            self.exception_handler.log_generic_error(e, "履歴表示コマンド")
            await interaction.followup.send(self.exception_handler.get_user_friendly_message(e))

    @app_commands.command(name="earthquake_debug", description="Detailed diagnosis of notification settings.")
    async def debug_config(self, interaction: discord.Interaction):
        try:
            await interaction.response.defer(ephemeral=False)

            guild_id = str(interaction.guild.id)
            embed = discord.Embed(
                title="🔍 通知設定診断",
                color=discord.Color.blue(),
                timestamp=datetime.now(self.jst)
            )

            embed.add_field(
                name="📁 設定ファイル",
                value=f"```json\n{json.dumps(self.config, indent=2, ensure_ascii=False)[:500]}```",
                inline=False
            )

            if guild_id in self.config:
                guild_config = self.config[guild_id]
                config_text = ""
                type_map = {
                    InfoType.EEW.value: '緊急地震速報',
                    InfoType.QUAKE.value: '地震情報',
                    InfoType.TSUNAMI.value: '津波予報',
                }
                for info_type, label in type_map.items():
                    channel_id = guild_config.get(info_type)
                    if not channel_id:
                        config_text += f"**{label}**: ⚠️ 未設定\n"
                        continue
                    channel = interaction.guild.get_channel(int(channel_id))
                    if channel:
                        perms = channel.permissions_for(interaction.guild.me)
                        config_text += f"**{label}**:\n"
                        config_text += f"  チャンネル: {channel.mention} (ID: {channel_id})\n"
                        config_text += f"  メッセージ送信: {'✅' if perms.send_messages else '❌'}\n"
                        config_text += f"  埋め込みリンク: {'✅' if perms.embed_links else '❌'}\n"
                    else:
                        config_text += f"**{label}**: ❌ チャンネル {channel_id} が見つかりません\n"

                filter_text = (
                    f"**EEW 震度:** {self.format_scales_summary(guild_id, InfoType.EEW.value)}\n"
                    f"**地震情報 震度:** {self.format_scales_summary(guild_id, InfoType.QUAKE.value)}\n"
                    f"**津波通知:** {'オン' if self.get_notify_tsunami(guild_id) else 'オフ'}"
                )
                embed.add_field(name="⚙️ このサーバーの設定", value=config_text or "設定なし", inline=False)
                embed.add_field(name="🎚️ 通知フィルタ", value=filter_text, inline=False)
            else:
                embed.add_field(name="⚙️ このサーバーの設定", value="❌ 未設定", inline=False)

            ws_info = "✅ 接続中" if self.ws_connection and not self.ws_connection.closed else "❌ 切断中"
            embed.add_field(
                name="🤖 Bot状態",
                value=(
                    f"ギルド数: {len(self.bot.guilds)}\n"
                    f"WebSocket: {ws_info}\n"
                    f"HTTPセッション: {'✅' if self.http_session and not self.http_session.closed else '❌'}\n"
                    f"WS切断回数: {self.error_stats['ws_disconnects']}"
                ),
                inline=False
            )

            await interaction.followup.send(embed=embed, ephemeral=False)

        except Exception as e:
            logger.error(f"診断コマンドエラー: {e}", exc_info=True)
            await interaction.followup.send(f"❌ エラーが発生しました: {e}", ephemeral=False)


async def setup(bot: commands.Bot):
    try:
        await bot.add_cog(EarthquakeTsunamiCog(bot))
    except Exception as e:
        logger.critical(f"Cogセットアップエラー: {e}", exc_info=True)
        raise