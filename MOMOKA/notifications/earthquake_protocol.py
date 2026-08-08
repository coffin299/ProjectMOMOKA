"""P2P地震情報 WebSocket / HTTP プロトコル処理。"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from typing import Any, Dict, Optional

import aiohttp
from discord.ext import tasks

from MOMOKA.notifications.earthquake_constants import InfoType
from MOMOKA.notifications.error.earthquake_errors import (
    APIError,
    DataParsingError,
    NotificationError,
)

logger = logging.getLogger("EarthquakeTsunamiCog")


class EarthquakeProtocolMixin:
    """WebSocket 受信、API 要求、受信済み ID 管理を提供する。"""

    async def websocket_listener(self) -> None:
        """WebSocket で地震情報を再接続付きでリアルタイム受信する。"""
        # 再接続待機秒数を初期値から始める
        reconnect_delay = self.ws_reconnect_delay
        # 停止フラグが立つまで再接続ループを回す
        while self.ws_running:
            try:
                # 未作成またはクローズ済みの接続セッションを作る
                if not self.ws_session or self.ws_session.closed:
                    self.ws_session = aiohttp.ClientSession(
                        headers=self.request_headers,
                    )
                # WebSocket 接続を確立して参照を保持する
                async with self.ws_session.ws_connect(self.ws_url) as ws:
                    self.ws_connection = ws
                    reconnect_delay = self.ws_reconnect_delay
                    logger.info("✅ WebSocket接続成功")
                    # 受信フレームを順番に処理する
                    async for message in ws:
                        if message.type == aiohttp.WSMsgType.TEXT:
                            try:
                                data = json.loads(message.data)
                                await self.process_websocket_message(data)
                            except json.JSONDecodeError as error:
                                logger.error(
                                    "WebSocketメッセージのJSON解析エラー: %s",
                                    error,
                                )
                                self.error_stats["parsing_errors"] += 1
                            except Exception as error:  # noqa: BLE001
                                self.exception_handler.log_generic_error(
                                    error,
                                    "WebSocketメッセージ処理",
                                )
                        elif message.type == aiohttp.WSMsgType.ERROR:
                            logger.error("WebSocketエラー: %s", ws.exception())
                            break
                        elif message.type in (
                            aiohttp.WSMsgType.CLOSED,
                            aiohttp.WSMsgType.CLOSE,
                        ):
                            logger.info("WebSocketがクローズされました。再接続します。")
                            break
            except (
                aiohttp.ClientConnectionError,
                ConnectionResetError,
                BrokenPipeError,
                aiohttp.ClientError,
            ) as error:
                # 切断は再接続対象として統計に反映する
                logger.info("WebSocket接続エラー: %s", error)
                self.error_stats["network_errors"] += 1
                self.error_stats["ws_disconnects"] += 1
                await self._reset_ws_session()
            except Exception as error:  # noqa: BLE001
                self.exception_handler.log_generic_error(error, "WebSocket接続")
                await self._reset_ws_session()
            finally:
                # 終了済み接続を次回の状態確認に残さない
                self.ws_connection = None
            if self.ws_running:
                logger.warning(
                    "⚠️ WebSocket切断。%s秒後に再接続...",
                    reconnect_delay,
                )
                await asyncio.sleep(reconnect_delay)
                reconnect_delay = min(
                    reconnect_delay * 2,
                    self.ws_max_reconnect_delay,
                )

    async def _reset_ws_session(self) -> None:
        """壊れた WebSocket 用 ClientSession を安全に破棄する。"""
        # 現在のセッションを退避して先に参照を消す
        session = self.ws_session
        self.ws_session = None
        self.ws_connection = None
        # 開いたセッションだけを閉じる
        if session is not None and not session.closed:
            try:
                await session.close()
            except Exception as error:  # noqa: BLE001
                logger.warning("WebSocketセッションクローズ失敗: %s", error)

    async def process_websocket_message(self, data: Dict[str, Any]) -> None:
        """受信した対象メッセージを重複排除して通知へ渡す。"""
        # 辞書以外は P2P API の通知形式ではないため無視する
        if not isinstance(data, dict):
            return
        # 地震、津波、EEW 以外のコードを無視する
        if data.get("code", 0) not in (551, 552, 556):
            return
        # 起動中は欠報防止のためバッファへ退避する
        if not getattr(self, "_ws_accepting", True):
            # ロックで同時追記を直列化する
            async with self._ws_pending_lock:
                # 生メッセージを保持する
                self._ws_pending.append(data)
            # 本処理は flush 時に行う
            return
        # 本処理へ委譲する
        await self._dispatch_earthquake_payload(data)

    async def flush_ws_pending(self) -> None:
        """起動バッファに溜めた WS メッセージを順に処理する。"""
        # バッファを原子的に取り出す
        async with self._ws_pending_lock:
            # 退避リストを移す
            pending = list(self._ws_pending)
            # バッファを空にする
            self._ws_pending.clear()
        # 受信順に配信処理する
        for payload in pending:
            # 各メッセージを通常経路で処理する
            await self._dispatch_earthquake_payload(payload)

    async def reconcile_missed_history(self) -> None:
        """起動直後の history 再取得で WS ギャップを HTTP 側から補完する。"""
        # API コードと内部種別の対応
        sources = (
            (551, InfoType.QUAKE, "地震情報"),
            (552, InfoType.TSUNAMI, "津波情報"),
            (556, InfoType.EEW, "緊急地震速報"),
        )
        for code, info_type, label in sources:
            try:
                # 直近履歴を再取得する
                data = await self.safe_api_request(
                    f"{self.api_base_url}/history?codes={code}&limit=20",
                )
                # 配列以外は無視する
                if not isinstance(data, list):
                    continue
                # API は新しい順のため古い順へ反転して配信する
                for item in reversed(data):
                    # 対象コードのみ処理する
                    if isinstance(item, dict) and item.get("code") == code:
                        # 未処理なら通知経路へ渡す
                        await self._dispatch_earthquake_payload(item)
            except (APIError, DataParsingError) as error:
                logger.error("%sの履歴補完に失敗: %s", label, error)
            except Exception as error:  # noqa: BLE001
                self.exception_handler.log_generic_error(
                    error,
                    f"{label}の履歴補完",
                )

    async def _dispatch_earthquake_payload(self, data: Dict[str, Any]) -> None:
        """1件の地震/津波ペイロードを配信し、成功時のみ処理済みにする。"""
        # 重複排除に使う ID を安全に取り出す
        info_id = self.extract_id_safe(data)
        if not info_id:
            logger.warning("IDを抽出できませんでした: %s", data)
            return
        # API コードを内部の通知種別へ変換する
        info_type = self.classify_info_type(data)
        if info_type == InfoType.UNKNOWN:
            self.processing_stats["unknown_skipped"] += 1
            return
        # 同種別で処理済みなら通知を繰り返さない
        if info_id in self.processed_ids[info_type.value]:
            return
        # 配信結果（True=完了扱い / False=再試行余地あり）
        delivered = False
        # 種別に対応した通知処理を呼び出す
        if info_type == InfoType.EEW:
            delivered = await self.send_eew_notification(data)
            if delivered:
                self.processing_stats["eew_processed"] += 1
        elif info_type == InfoType.QUAKE:
            delivered = await self.send_quake_notification(data)
            if delivered:
                self.processing_stats["quake_processed"] += 1
        else:
            tsunami_info = self.get_tsunami_info(data)
            if not tsunami_info.get("has_tsunami", False):
                # 津波なしは完了扱い（再通知不要）
                self.add_processed_id(info_type.value, info_id)
                self.last_ids[info_type.value] = info_id
                return
            delivered = await self.send_tsunami_notification(data, tsunami_info)
            if delivered:
                self.processing_stats["tsunami_processed"] += 1
        # 1件以上配信成功、または対象チャンネル無しのときだけ処理済みにする
        if delivered:
            self.add_processed_id(info_type.value, info_id)
            self.last_ids[info_type.value] = info_id
        else:
            logger.warning(
                "地震通知の配信に失敗したため未処理のまま残します: type=%s id=%s",
                info_type.value,
                info_id,
            )

    async def recreate_http_session(self) -> None:
        """地震 API 用 HTTP セッションを再生成する。"""
        # 既存セッションがあれば先に閉じる
        if self.http_session and not self.http_session.closed:
            await self.http_session.close()
        # 既存と同じタイムアウト、ヘッダー、接続数で作る
        self.http_session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=15),
            headers=self.request_headers,
            connector=aiohttp.TCPConnector(limit=10),
        )

    async def safe_api_request(
        self,
        url: str,
        timeout: int = 15,
    ) -> Optional[Dict[str, Any]]:
        """HTTP API を取得し、既存のドメイン例外へ正規化する。"""
        try:
            # 必要時に HTTP セッションを再生成する
            if not self.http_session or self.http_session.closed:
                await self.recreate_http_session()
            async with self.http_session.get(
                url,
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as response:
                if response.status == 200:
                    try:
                        return await response.json()
                    except json.JSONDecodeError as error:
                        self.error_stats["last_error_time"] = datetime.now(self.jst)
                        raise self.exception_handler.handle_json_decode_error(
                            error,
                            url,
                        )
                self.error_stats["last_error_time"] = datetime.now(self.jst)
                raise self.exception_handler.handle_api_response_error(
                    response.status,
                    url,
                )
        except Exception as error:  # noqa: BLE001
            if isinstance(error, (APIError, DataParsingError)):
                raise
            self.error_stats["last_error_time"] = datetime.now(self.jst)
            raise self.exception_handler.handle_api_error(error, url)

    def manage_processed_ids(self, info_type: str) -> None:
        """処理済み ID を受信順のまま上限件数へ切り詰める。"""
        # 最古の ID から上限超過分を削除する
        processed_ids = self.processed_ids[info_type]
        while len(processed_ids) > self.max_processed_ids:
            processed_ids.popitem(last=False)

    def add_processed_id(self, info_type: str, info_id: str) -> None:
        """処理済み ID を受信順で記録する。"""
        # 既存 ID は末尾へ動かして受信順を保つ
        processed_ids = self.processed_ids[info_type]
        processed_ids.pop(info_id, None)
        processed_ids[info_id] = None
        self.manage_processed_ids(info_type)

    async def initialize_processed_ids(self) -> None:
        """起動前の履歴を読み、既存情報を通知しないようにする。"""
        # API コードごとに履歴を初期化する
        sources = (
            (551, InfoType.QUAKE, "地震情報"),
            (552, InfoType.TSUNAMI, "津波情報"),
            (556, InfoType.EEW, "緊急地震速報"),
        )
        for code, info_type, label in sources:
            try:
                data = await self.safe_api_request(
                    f"{self.api_base_url}/history?codes={code}&limit=100",
                )
                if not isinstance(data, list):
                    continue
                # API の新しい順を古い順に反転して記録する
                for item in reversed(data):
                    if isinstance(item, dict) and item.get("code") == code:
                        info_id = self.extract_id_safe(item)
                        if info_id is not None:
                            self.add_processed_id(info_type.value, info_id)
                # 最初の有効な最新 ID を状態として保持する
                for item in data:
                    if isinstance(item, dict) and item.get("code") == code:
                        info_id = self.extract_id_safe(item)
                        if info_id is not None:
                            self.last_ids[info_type.value] = info_id
                            break
            except (APIError, DataParsingError) as error:
                logger.error("%sのID初期化に失敗: %s", label, error)
            except Exception as error:  # noqa: BLE001
                self.exception_handler.log_generic_error(
                    error,
                    f"{label}のID初期化",
                )

    def extract_id_safe(self, item: Dict[str, Any]) -> Optional[str]:
        """P2P API の ID を安全に文字列として取得する。"""
        # _id を優先し、旧形式の id をフォールバックにする
        item_id = item.get("_id") or item.get("id")
        return str(item_id) if item_id is not None else None

    @tasks.loop(seconds=3600)
    async def output_stats_task(self) -> None:
        """統計情報を定期的にログへ出力する。"""
        # 既存の統計キーだけを集計する
        error_total = sum(
            value
            for key, value in self.error_stats.items()
            if key.endswith("_errors") or key == "ws_disconnects"
        )
        logger.info(
            "[統計] EEW:%s QUAKE:%s TSUNAMI:%s UNKNOWN:%s エラー:%s WS切断:%s",
            self.processing_stats["eew_processed"],
            self.processing_stats["quake_processed"],
            self.processing_stats["tsunami_processed"],
            self.processing_stats["unknown_skipped"],
            error_total,
            self.error_stats["ws_disconnects"],
        )

    def classify_info_type(self, item: Dict[str, Any]) -> InfoType:
        """P2P API コードから内部情報種別を判定する。"""
        # コードと種別の対応表を使って未対応を UNKNOWN とする
        return {
            556: InfoType.EEW,
            552: InfoType.TSUNAMI,
            551: InfoType.QUAKE,
        }.get(item.get("code", 0), InfoType.UNKNOWN)
