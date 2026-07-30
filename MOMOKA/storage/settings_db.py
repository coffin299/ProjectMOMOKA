# MOMOKA/storage/settings_db.py
# ランタイム設定を SQLite（data/momoka.db）に保存する共通ストア。
from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Optional

# モジュールロガー
logger = logging.getLogger(__name__)

# デフォルト DB パス（リポジトリルート相対）
DEFAULT_DB_PATH = "data/momoka.db"

# --- namespace 定数 ---
NS_CHANNEL_LLM_MODELS = "channel_llm_models"
NS_CHANNEL_IMAGE_MODELS = "channel_image_models"
NS_LINK_FIX_SETTINGS = "link_fix_settings"
NS_TTS_SETTINGS = "tts_settings"
NS_SPEECH_SETTINGS = "speech_settings"
NS_SPEECH_DICTIONARY = "speech_dictionary"
NS_TWITCH_SETTINGS = "twitch_settings"
NS_EARTHQUAKE_CONFIG = "earthquake_tsunami_notification_config"
NS_LOGGING_CHANNELS = "logging_channels"
NS_RESPONSE_TIMES = "response_times"
NS_LOG_VIEWER_CONFIG = "log_viewer_config"
NS_GDRIVE_DELETION_SCHEDULE = "gdrive_deletion_schedule"

# プロセス内で共有するデフォルトインスタンス
_default_db: Optional["SettingsDB"] = None
# デフォルトインスタンス生成の排他
_default_lock = threading.Lock()


def get_default_settings_db(path: str = DEFAULT_DB_PATH) -> "SettingsDB":
    """プロセス共通の SettingsDB を返す（無ければ生成）。"""
    # グローバルを更新する
    global _default_db
    # 二重生成を防ぐ
    with _default_lock:
        # 未作成なら作る
        if _default_db is None:
            # デフォルトパスでインスタンス化する
            _default_db = SettingsDB(path)
        # 共有インスタンスを返す
        return _default_db


def resolve_settings_db(bot: Any = None) -> "SettingsDB":
    """bot.settings_db があればそれを、無ければデフォルトを返す。"""
    # bot から注入済みストアを取る
    if bot is not None:
        # 属性が無ければ None
        injected = getattr(bot, "settings_db", None)
        # 注入済みならそれを使う
        if injected is not None:
            return injected
    # GUI / 単体利用向けフォールバック
    return get_default_settings_db()


class SettingsDB:
    """namespace 単位で JSON 文書を SQLite に保存するストア。"""

    def __init__(self, path: str = DEFAULT_DB_PATH) -> None:
        # DB ファイルパスを保持する
        self.path = Path(path)
        # 同時読込/書込を直列化する
        self._lock = threading.Lock()
        # 親ディレクトリを用意する
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # テーブルを保証する
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        """新しい接続を開く（呼び出し側で close する）。"""
        # check_same_thread=False で asyncio スレッドからも使えるようにする
        conn = sqlite3.connect(str(self.path), check_same_thread=False)
        # 行をタプルで返す（デフォルト）
        return conn

    def _ensure_schema(self) -> None:
        """settings テーブルが無ければ作る。"""
        # スキーマ作成もロック下で行う
        with self._lock:
            # 接続を開く
            conn = self._connect()
            try:
                # namespace を主キーにした JSON 文書テーブル
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS settings (
                        namespace TEXT PRIMARY KEY,
                        payload TEXT NOT NULL,
                        updated_at REAL NOT NULL
                    )
                    """
                )
                # 変更を確定する
                conn.commit()
            finally:
                # 接続を閉じる
                conn.close()

    def load(self, namespace: str) -> Any:
        """namespace の payload をデコードして返す。無ければ None。"""
        # 読込を排他する
        with self._lock:
            # 接続を開く
            conn = self._connect()
            try:
                # 主キーで1行取る
                row = conn.execute(
                    "SELECT payload FROM settings WHERE namespace = ?",
                    (namespace,),
                ).fetchone()
            finally:
                # 接続を閉じる
                conn.close()
        # 行が無ければ未設定
        if row is None:
            return None
        try:
            # JSON 文字列を Python オブジェクトへ戻す
            return json.loads(row[0])
        except json.JSONDecodeError as exc:
            # 壊れていれば警告して未設定扱い
            logger.error("SettingsDB: namespace '%s' の JSON デコード失敗: %s", namespace, exc)
            return None

    def save(self, namespace: str, data: Any) -> None:
        """namespace へ data を JSON として保存する。"""
        # シリアライズする（ensure_ascii=False で日本語をそのまま）
        payload = json.dumps(data, ensure_ascii=False)
        # 更新時刻（UNIX 秒）
        updated_at = time.time()
        # 書込を排他する
        with self._lock:
            # 接続を開く
            conn = self._connect()
            try:
                # 既存行があれば置き換える
                conn.execute(
                    """
                    INSERT OR REPLACE INTO settings (namespace, payload, updated_at)
                    VALUES (?, ?, ?)
                    """,
                    (namespace, payload, updated_at),
                )
                # 確定する
                conn.commit()
            finally:
                # 接続を閉じる
                conn.close()

    async def save_async(self, namespace: str, data: Any) -> None:
        """save をスレッドプールで実行する（イベントループを塞がない）。"""
        # 同期 save を別スレッドへ逃がす
        await asyncio.to_thread(self.save, namespace, data)
