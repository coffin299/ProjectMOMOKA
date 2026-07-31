# MOMOKA/storage/settings_db.py
# ランタイム設定を正規化 SQLite テーブルへ保存する共通ストア。
from __future__ import annotations

import asyncio
import logging
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

# モジュールロガー
logger = logging.getLogger(__name__)

# デフォルト DB パス（リポジトリルート相対）
DEFAULT_DB_PATH = "data/momoka.db"

# 正規化スキーマ版
SCHEMA_VERSION = 2

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
    """namespace 単位の load/save を正規化テーブルへ写像するストア。"""

    def __init__(self, path: str = DEFAULT_DB_PATH) -> None:
        # DB ファイルパスを保持する
        self.path = Path(path)
        # 同時読込/書込を直列化する
        self._lock = threading.Lock()
        # 親ディレクトリを用意する
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # 正規化テーブルを保証する
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        """新しい接続を開く（呼び出し側で close する）。"""
        # check_same_thread=False で asyncio スレッドからも使えるようにする
        conn = sqlite3.connect(str(self.path), check_same_thread=False)
        # 外部キーを有効化する
        conn.execute("PRAGMA foreign_keys = ON")
        # 行をタプルで返す（デフォルト）
        return conn

    def _ensure_schema(self) -> None:
        """正規化テーブルと schema_meta を用意する。"""
        # スキーマ作成もロック下で行う
        with self._lock:
            # 接続を開く
            conn = self._connect()
            try:
                # 版管理テーブル
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS schema_meta (
                        id INTEGER PRIMARY KEY CHECK (id = 1),
                        version INTEGER NOT NULL
                    )
                    """
                )
                # 正規化テーブル群を作成する
                self._create_normalized_tables(conn)
                # 旧 blob / バックアップが残っていれば捨てる
                conn.execute("DROP TABLE IF EXISTS settings")
                conn.execute("DROP TABLE IF EXISTS settings_blob_legacy")
                # 版を現行に固定する
                conn.execute(
                    """
                    INSERT OR REPLACE INTO schema_meta (id, version)
                    VALUES (1, ?)
                    """,
                    (SCHEMA_VERSION,),
                )
                # 確定する
                conn.commit()
            finally:
                # 接続を閉じる
                conn.close()

    @staticmethod
    def _create_normalized_tables(conn: sqlite3.Connection) -> None:
        """正規化テーブルをすべて CREATE IF NOT EXISTS する。"""
        # チャンネル別 LLM モデル
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS channel_llm_models (
                bot_id TEXT NOT NULL,
                channel_id TEXT NOT NULL,
                model TEXT NOT NULL,
                expires_at REAL,
                PRIMARY KEY (bot_id, channel_id)
            )
            """
        )
        # チャンネル別画像モデル
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS channel_image_models (
                channel_id TEXT PRIMARY KEY,
                model TEXT NOT NULL
            )
            """
        )
        # Link Fix ギルド
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS link_fix_guilds (
                guild_id TEXT PRIMARY KEY,
                enabled INTEGER
            )
            """
        )
        # Link Fix サイト上書き
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS link_fix_sites (
                guild_id TEXT NOT NULL,
                site_id TEXT NOT NULL,
                enabled INTEGER,
                fix_domain TEXT,
                PRIMARY KEY (guild_id, site_id),
                FOREIGN KEY (guild_id) REFERENCES link_fix_guilds(guild_id)
                    ON DELETE CASCADE
            )
            """
        )
        # Link Fix マッチ元ドメイン
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS link_fix_site_match_domains (
                guild_id TEXT NOT NULL,
                site_id TEXT NOT NULL,
                domain TEXT NOT NULL,
                PRIMARY KEY (guild_id, site_id, domain),
                FOREIGN KEY (guild_id, site_id)
                    REFERENCES link_fix_sites(guild_id, site_id)
                    ON DELETE CASCADE
            )
            """
        )
        # TTS チャンネル設定
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS tts_channel_settings (
                channel_id TEXT PRIMARY KEY,
                model_id INTEGER NOT NULL,
                style TEXT NOT NULL,
                style_weight REAL NOT NULL,
                speed REAL NOT NULL
            )
            """
        )
        # 読み上げギルド設定
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS speech_guild_settings (
                guild_id TEXT PRIMARY KEY,
                speech_channel_id INTEGER,
                enable_notifications INTEGER NOT NULL DEFAULT 1,
                volume REAL NOT NULL DEFAULT 1.0
            )
            """
        )
        # 自動参加ユーザー
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS speech_auto_join_users (
                guild_id TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                PRIMARY KEY (guild_id, user_id),
                FOREIGN KEY (guild_id) REFERENCES speech_guild_settings(guild_id)
                    ON DELETE CASCADE
            )
            """
        )
        # 読み上げ辞書
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS speech_dictionary (
                guild_id TEXT NOT NULL,
                word TEXT NOT NULL,
                reading TEXT NOT NULL,
                PRIMARY KEY (guild_id, word)
            )
            """
        )
        # Twitch 監視
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS twitch_watch (
                guild_id TEXT NOT NULL,
                twitch_user_id TEXT NOT NULL,
                twitch_login_name TEXT NOT NULL,
                twitch_display_name TEXT NOT NULL,
                notification_channel_id INTEGER NOT NULL,
                last_status TEXT NOT NULL,
                message TEXT,
                PRIMARY KEY (guild_id, twitch_user_id)
            )
            """
        )
        # 地震通知ギルド
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS earthquake_guild_config (
                guild_id TEXT PRIMARY KEY,
                eew_channel_id INTEGER,
                quake_channel_id INTEGER,
                tsunami_channel_id INTEGER,
                notify_tsunami INTEGER NOT NULL DEFAULT 1
            )
            """
        )
        # 地震震度フィルタ
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS earthquake_notify_scales (
                guild_id TEXT NOT NULL,
                info_type TEXT NOT NULL,
                scale INTEGER NOT NULL,
                PRIMARY KEY (guild_id, info_type, scale),
                FOREIGN KEY (guild_id) REFERENCES earthquake_guild_config(guild_id)
                    ON DELETE CASCADE
            )
            """
        )
        # Discord ログチャンネル
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS logging_channels (
                channel_id INTEGER PRIMARY KEY
            )
            """
        )
        # 応答時間サンプル
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS response_time_samples (
                model_name TEXT NOT NULL,
                sample_index INTEGER NOT NULL,
                elapsed_seconds REAL NOT NULL,
                PRIMARY KEY (model_name, sample_index)
            )
            """
        )
        # ログビューア設定（単一行）
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS log_viewer_config (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                font_name TEXT NOT NULL,
                font_size INTEGER NOT NULL,
                max_lines INTEGER NOT NULL,
                auto_scroll INTEGER NOT NULL,
                level_general TEXT NOT NULL,
                level_llm TEXT NOT NULL,
                level_tts TEXT NOT NULL,
                level_error TEXT NOT NULL
            )
            """
        )
        # Google Drive 削除予定
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS gdrive_deletion_schedule (
                file_id TEXT PRIMARY KEY,
                delete_at REAL NOT NULL
            )
            """
        )

    def load(self, namespace: str) -> Any:
        """namespace のデータを組み立てて返す。無ければ None。"""
        # 読込を排他する
        with self._lock:
            # 接続を開く
            conn = self._connect()
            try:
                # namespace 別に組み立てる
                return self._load_namespace_conn(conn, namespace)
            finally:
                # 接続を閉じる
                conn.close()

    def save(self, namespace: str, data: Any) -> None:
        """namespace のデータを正規化テーブルへ保存する。"""
        # 書込を排他する
        with self._lock:
            # 接続を開く
            conn = self._connect()
            try:
                # namespace 別に書き込む
                self._save_namespace_conn(conn, namespace, data)
                # 確定する
                conn.commit()
            finally:
                # 接続を閉じる
                conn.close()

    async def save_async(self, namespace: str, data: Any) -> None:
        """save をスレッドプールで実行する（イベントループを塞がない）。"""
        # 同期 save を別スレッドへ逃がす
        await asyncio.to_thread(self.save, namespace, data)

    def _load_namespace_conn(self, conn: sqlite3.Connection, namespace: str) -> Any:
        """接続済みで namespace を読む。"""
        # ディスパッチする
        if namespace == NS_CHANNEL_LLM_MODELS:
            return self._load_channel_llm(conn)
        if namespace == NS_CHANNEL_IMAGE_MODELS:
            return self._load_channel_image(conn)
        if namespace == NS_LINK_FIX_SETTINGS:
            return self._load_link_fix(conn)
        if namespace == NS_TTS_SETTINGS:
            return self._load_tts(conn)
        if namespace == NS_SPEECH_SETTINGS:
            return self._load_speech_settings(conn)
        if namespace == NS_SPEECH_DICTIONARY:
            return self._load_speech_dictionary(conn)
        if namespace == NS_TWITCH_SETTINGS:
            return self._load_twitch(conn)
        if namespace == NS_EARTHQUAKE_CONFIG:
            return self._load_earthquake(conn)
        if namespace == NS_LOGGING_CHANNELS:
            return self._load_logging_channels(conn)
        if namespace == NS_RESPONSE_TIMES:
            return self._load_response_times(conn)
        if namespace == NS_LOG_VIEWER_CONFIG:
            return self._load_log_viewer(conn)
        if namespace == NS_GDRIVE_DELETION_SCHEDULE:
            return self._load_gdrive(conn)
        # 未知 namespace
        logger.error("未知の settings namespace: %s", namespace)
        return None

    def _save_namespace_conn(
        self, conn: sqlite3.Connection, namespace: str, data: Any
    ) -> None:
        """接続済みで namespace を書く。"""
        # ディスパッチする
        if namespace == NS_CHANNEL_LLM_MODELS:
            self._save_channel_llm(conn, data)
            return
        if namespace == NS_CHANNEL_IMAGE_MODELS:
            self._save_channel_image(conn, data)
            return
        if namespace == NS_LINK_FIX_SETTINGS:
            self._save_link_fix(conn, data)
            return
        if namespace == NS_TTS_SETTINGS:
            self._save_tts(conn, data)
            return
        if namespace == NS_SPEECH_SETTINGS:
            self._save_speech_settings(conn, data)
            return
        if namespace == NS_SPEECH_DICTIONARY:
            self._save_speech_dictionary(conn, data)
            return
        if namespace == NS_TWITCH_SETTINGS:
            self._save_twitch(conn, data)
            return
        if namespace == NS_EARTHQUAKE_CONFIG:
            self._save_earthquake(conn, data)
            return
        if namespace == NS_LOGGING_CHANNELS:
            self._save_logging_channels(conn, data)
            return
        if namespace == NS_RESPONSE_TIMES:
            self._save_response_times(conn, data)
            return
        if namespace == NS_LOG_VIEWER_CONFIG:
            self._save_log_viewer(conn, data)
            return
        if namespace == NS_GDRIVE_DELETION_SCHEDULE:
            self._save_gdrive(conn, data)
            return
        # 未知 namespace
        raise ValueError(f"unknown settings namespace: {namespace}")

    # ------------------------------------------------------------------
    # channel_llm_models
    # ------------------------------------------------------------------
    @staticmethod
    def _load_channel_llm(conn: sqlite3.Connection) -> Optional[Dict[str, Any]]:
        """bot → channel → override を組み立てる。"""
        # 全行を取る
        rows = conn.execute(
            "SELECT bot_id, channel_id, model, expires_at FROM channel_llm_models"
        ).fetchall()
        # 空なら未設定
        if not rows:
            return None
        # 入れ物
        result: Dict[str, Any] = {}
        # 行をネストする
        for bot_id, channel_id, model, expires_at in rows:
            # bot 辞書を確保する
            bot_map = result.setdefault(str(bot_id), {})
            # 上書きエントリを書く
            entry: Dict[str, Any] = {"model": str(model)}
            # expires_at があれば付ける
            if expires_at is not None:
                entry["expires_at"] = float(expires_at)
            # チャンネルキーは文字列
            bot_map[str(channel_id)] = entry
        # 返す
        return result

    @staticmethod
    def _save_channel_llm(conn: sqlite3.Connection, data: Any) -> None:
        """channel_llm_models を全置換する。"""
        # 既存を消す
        conn.execute("DELETE FROM channel_llm_models")
        # dict 以外は空保存
        if not isinstance(data, dict):
            return
        # bot → channels を走査する
        for bot_id, channels in data.items():
            # チャンネル map でなければスキップ
            if not isinstance(channels, dict):
                continue
            # 各チャンネル
            for channel_id, override in channels.items():
                # dict 形式のみ受け付ける
                if not isinstance(override, dict):
                    continue
                # モデル名
                model = override.get("model")
                # 必須
                if not isinstance(model, str):
                    continue
                # 期限
                expires_at = override.get("expires_at")
                # 挿入する
                conn.execute(
                    """
                    INSERT INTO channel_llm_models
                        (bot_id, channel_id, model, expires_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        str(bot_id),
                        str(channel_id),
                        model,
                        float(expires_at) if expires_at is not None else None,
                    ),
                )

    # ------------------------------------------------------------------
    # channel_image_models
    # ------------------------------------------------------------------
    @staticmethod
    def _load_channel_image(conn: sqlite3.Connection) -> Optional[Dict[str, str]]:
        """channel_id → model を返す。"""
        # 全行
        rows = conn.execute(
            "SELECT channel_id, model FROM channel_image_models"
        ).fetchall()
        # 空
        if not rows:
            return None
        # 辞書化
        return {str(channel_id): str(model) for channel_id, model in rows}

    @staticmethod
    def _save_channel_image(conn: sqlite3.Connection, data: Any) -> None:
        """channel_image_models を全置換する。"""
        # 既存削除
        conn.execute("DELETE FROM channel_image_models")
        # dict 以外は空
        if not isinstance(data, dict):
            return
        # 各エントリ
        for channel_id, model in data.items():
            # モデル名は文字列のみ
            if not isinstance(model, str):
                continue
            # 挿入
            conn.execute(
                "INSERT INTO channel_image_models (channel_id, model) VALUES (?, ?)",
                (str(channel_id), model),
            )

    # ------------------------------------------------------------------
    # link_fix_settings
    # ------------------------------------------------------------------
    @staticmethod
    def _load_link_fix(conn: sqlite3.Connection) -> Optional[Dict[str, Any]]:
        """ギルド別 Link Fix 設定を組み立てる。"""
        # ギルド行
        guild_rows = conn.execute(
            "SELECT guild_id, enabled FROM link_fix_guilds"
        ).fetchall()
        # サイト行
        site_rows = conn.execute(
            """
            SELECT guild_id, site_id, enabled, fix_domain
            FROM link_fix_sites
            """
        ).fetchall()
        # マッチドメイン
        match_rows = conn.execute(
            """
            SELECT guild_id, site_id, domain
            FROM link_fix_site_match_domains
            """
        ).fetchall()
        # すべて空なら未設定
        if not guild_rows and not site_rows and not match_rows:
            return None
        # 結果
        result: Dict[str, Any] = {}
        # ギルドフラグ
        for guild_id, enabled in guild_rows:
            # ギルドエントリ
            entry = result.setdefault(str(guild_id), {})
            # enabled が明示されているときだけ書く
            if enabled is not None:
                entry["enabled"] = bool(enabled)
        # サイト
        for guild_id, site_id, enabled, fix_domain in site_rows:
            # ギルド
            entry = result.setdefault(str(guild_id), {})
            # sites
            sites = entry.setdefault("sites", {})
            # サイト dict
            site: Dict[str, Any] = {}
            # enabled
            if enabled is not None:
                site["enabled"] = bool(enabled)
            # fix_domain
            if fix_domain:
                site["fix_domain"] = str(fix_domain)
            # 書く
            sites[str(site_id)] = site
        # マッチドメインをサイトへ付ける
        for guild_id, site_id, domain in match_rows:
            # ギルド
            entry = result.setdefault(str(guild_id), {})
            # sites
            sites = entry.setdefault("sites", {})
            # サイト
            site = sites.setdefault(str(site_id), {})
            # リスト確保
            domains = site.setdefault("match_domains", [])
            # 追加
            domains.append(str(domain))
        # 返す
        return result

    @staticmethod
    def _save_link_fix(conn: sqlite3.Connection, data: Any) -> None:
        """link_fix_* を全置換する。"""
        # 子→親の順で削除
        conn.execute("DELETE FROM link_fix_site_match_domains")
        conn.execute("DELETE FROM link_fix_sites")
        conn.execute("DELETE FROM link_fix_guilds")
        # dict 以外は空
        if not isinstance(data, dict):
            return
        # ギルドごと
        for guild_id, guild_data in data.items():
            # dict のみ
            if not isinstance(guild_data, dict):
                continue
            # guild_id 文字列
            gid = str(guild_id)
            # enabled（未設定は NULL）
            enabled = guild_data.get("enabled") if "enabled" in guild_data else None
            # ギルド行
            conn.execute(
                "INSERT INTO link_fix_guilds (guild_id, enabled) VALUES (?, ?)",
                (
                    gid,
                    None if enabled is None else (1 if enabled else 0),
                ),
            )
            # sites
            sites = guild_data.get("sites") or {}
            # dict でなければスキップ
            if not isinstance(sites, dict):
                continue
            # サイトごと
            for site_id, site_data in sites.items():
                # dict のみ
                if not isinstance(site_data, dict):
                    continue
                # site_id
                sid = str(site_id)
                # enabled
                site_enabled = (
                    site_data.get("enabled") if "enabled" in site_data else None
                )
                # fix_domain
                fix_domain = site_data.get("fix_domain")
                # サイト行
                conn.execute(
                    """
                    INSERT INTO link_fix_sites
                        (guild_id, site_id, enabled, fix_domain)
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        gid,
                        sid,
                        None if site_enabled is None else (1 if site_enabled else 0),
                        str(fix_domain) if fix_domain else None,
                    ),
                )
                # match_domains
                match_domains = site_data.get("match_domains") or []
                # list のみ
                if not isinstance(match_domains, list):
                    continue
                # 各ドメイン
                for domain in match_domains:
                    # 空は無視
                    if not domain:
                        continue
                    # 挿入
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO link_fix_site_match_domains
                            (guild_id, site_id, domain)
                        VALUES (?, ?, ?)
                        """,
                        (gid, sid, str(domain)),
                    )

    # ------------------------------------------------------------------
    # tts_settings
    # ------------------------------------------------------------------
    @staticmethod
    def _load_tts(conn: sqlite3.Connection) -> Optional[Dict[str, Any]]:
        """channel_id → TTS 設定を返す。"""
        # 全行
        rows = conn.execute(
            """
            SELECT channel_id, model_id, style, style_weight, speed
            FROM tts_channel_settings
            """
        ).fetchall()
        # 空
        if not rows:
            return None
        # 組み立て
        result: Dict[str, Any] = {}
        for channel_id, model_id, style, style_weight, speed in rows:
            result[str(channel_id)] = {
                "model_id": int(model_id),
                "style": str(style),
                "style_weight": float(style_weight),
                "speed": float(speed),
            }
        return result

    @staticmethod
    def _save_tts(conn: sqlite3.Connection, data: Any) -> None:
        """tts_channel_settings を全置換する。"""
        # 削除
        conn.execute("DELETE FROM tts_channel_settings")
        # dict 以外
        if not isinstance(data, dict):
            return
        # 各チャンネル
        for channel_id, settings in data.items():
            # dict のみ
            if not isinstance(settings, dict):
                continue
            # 必須フィールドが無ければスキップ
            if "model_id" not in settings:
                continue
            # 挿入
            conn.execute(
                """
                INSERT INTO tts_channel_settings
                    (channel_id, model_id, style, style_weight, speed)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    str(channel_id),
                    int(settings.get("model_id", 0)),
                    str(settings.get("style", "Neutral")),
                    float(settings.get("style_weight", 5.0)),
                    float(settings.get("speed", 1.0)),
                ),
            )

    # ------------------------------------------------------------------
    # speech_settings
    # ------------------------------------------------------------------
    @staticmethod
    def _load_speech_settings(conn: sqlite3.Connection) -> Optional[Dict[str, Any]]:
        """ギルド別読み上げ設定を返す。"""
        # ギルド行
        guild_rows = conn.execute(
            """
            SELECT guild_id, speech_channel_id, enable_notifications, volume
            FROM speech_guild_settings
            """
        ).fetchall()
        # 自動参加
        user_rows = conn.execute(
            "SELECT guild_id, user_id FROM speech_auto_join_users"
        ).fetchall()
        # 空
        if not guild_rows and not user_rows:
            return None
        # 結果
        result: Dict[str, Any] = {}
        # ギルド
        for guild_id, speech_channel_id, enable_notifications, volume in guild_rows:
            result[str(guild_id)] = {
                "speech_channel_id": speech_channel_id,
                "auto_join_users": [],
                "enable_notifications": bool(enable_notifications),
                "volume": float(volume),
            }
        # ユーザーを付ける
        for guild_id, user_id in user_rows:
            # ギルド確保
            entry = result.setdefault(
                str(guild_id),
                {
                    "speech_channel_id": None,
                    "auto_join_users": [],
                    "enable_notifications": True,
                    "volume": 1.0,
                },
            )
            # リストへ追加
            entry.setdefault("auto_join_users", []).append(int(user_id))
        return result

    @staticmethod
    def _save_speech_settings(conn: sqlite3.Connection, data: Any) -> None:
        """speech_* を全置換する。"""
        # 子→親
        conn.execute("DELETE FROM speech_auto_join_users")
        conn.execute("DELETE FROM speech_guild_settings")
        # dict 以外
        if not isinstance(data, dict):
            return
        # ギルドごと
        for guild_id, settings in data.items():
            # dict のみ
            if not isinstance(settings, dict):
                continue
            # gid
            gid = str(guild_id)
            # 親行
            conn.execute(
                """
                INSERT INTO speech_guild_settings
                    (guild_id, speech_channel_id, enable_notifications, volume)
                VALUES (?, ?, ?, ?)
                """,
                (
                    gid,
                    settings.get("speech_channel_id"),
                    1 if settings.get("enable_notifications", True) else 0,
                    float(settings.get("volume", 1.0)),
                ),
            )
            # 自動参加
            users = settings.get("auto_join_users") or []
            # list のみ
            if not isinstance(users, list):
                continue
            # 各ユーザー
            for user_id in users:
                try:
                    # int 化
                    uid = int(user_id)
                except (TypeError, ValueError):
                    continue
                # 挿入
                conn.execute(
                    """
                    INSERT OR IGNORE INTO speech_auto_join_users
                        (guild_id, user_id)
                    VALUES (?, ?)
                    """,
                    (gid, uid),
                )

    # ------------------------------------------------------------------
    # speech_dictionary
    # ------------------------------------------------------------------
    @staticmethod
    def _load_speech_dictionary(conn: sqlite3.Connection) -> Optional[Dict[str, Any]]:
        """ギルド → 単語 → 読み を返す。"""
        # 全行
        rows = conn.execute(
            "SELECT guild_id, word, reading FROM speech_dictionary"
        ).fetchall()
        # 空
        if not rows:
            return None
        # 組み立て
        result: Dict[str, Any] = {}
        for guild_id, word, reading in rows:
            result.setdefault(str(guild_id), {})[str(word)] = str(reading)
        return result

    @staticmethod
    def _save_speech_dictionary(conn: sqlite3.Connection, data: Any) -> None:
        """speech_dictionary を全置換する。"""
        # 削除
        conn.execute("DELETE FROM speech_dictionary")
        # dict 以外
        if not isinstance(data, dict):
            return
        # ギルドごと
        for guild_id, entries in data.items():
            # dict のみ
            if not isinstance(entries, dict):
                continue
            # 単語ごと
            for word, reading in entries.items():
                # 両方文字列
                if not isinstance(word, str) or not isinstance(reading, str):
                    continue
                # 挿入
                conn.execute(
                    """
                    INSERT INTO speech_dictionary (guild_id, word, reading)
                    VALUES (?, ?, ?)
                    """,
                    (str(guild_id), word, reading),
                )

    # ------------------------------------------------------------------
    # twitch_settings
    # ------------------------------------------------------------------
    @staticmethod
    def _load_twitch(conn: sqlite3.Connection) -> Optional[Dict[str, Any]]:
        """ギルド → twitch_user_id → 設定 を返す。"""
        # 全行
        rows = conn.execute(
            """
            SELECT guild_id, twitch_user_id, twitch_login_name, twitch_display_name,
                   notification_channel_id, last_status, message
            FROM twitch_watch
            """
        ).fetchall()
        # 空
        if not rows:
            return None
        # 組み立て
        result: Dict[str, Any] = {}
        for (
            guild_id,
            twitch_user_id,
            login,
            display,
            channel_id,
            last_status,
            message,
        ) in rows:
            # ギルド
            guild_map = result.setdefault(str(guild_id), {})
            # エントリ
            entry: Dict[str, Any] = {
                "twitch_login_name": str(login),
                "twitch_display_name": str(display),
                "notification_channel_id": int(channel_id),
                "last_status": str(last_status),
            }
            # message があれば付ける
            if message is not None:
                entry["message"] = str(message)
            # 書く
            guild_map[str(twitch_user_id)] = entry
        return result

    @staticmethod
    def _save_twitch(conn: sqlite3.Connection, data: Any) -> None:
        """twitch_watch を全置換する。"""
        # 削除
        conn.execute("DELETE FROM twitch_watch")
        # dict 以外
        if not isinstance(data, dict):
            return
        # ギルドごと
        for guild_id, users in data.items():
            # dict のみ
            if not isinstance(users, dict):
                continue
            # ユーザーごと
            for twitch_user_id, settings in users.items():
                # dict のみ
                if not isinstance(settings, dict):
                    continue
                # 必須
                if "notification_channel_id" not in settings:
                    continue
                # 挿入
                conn.execute(
                    """
                    INSERT INTO twitch_watch (
                        guild_id, twitch_user_id, twitch_login_name,
                        twitch_display_name, notification_channel_id,
                        last_status, message
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(guild_id),
                        str(twitch_user_id),
                        str(settings.get("twitch_login_name", "")),
                        str(settings.get("twitch_display_name", "")),
                        int(settings["notification_channel_id"]),
                        str(settings.get("last_status", "offline")),
                        settings.get("message"),
                    ),
                )

    # ------------------------------------------------------------------
    # earthquake
    # ------------------------------------------------------------------
    @staticmethod
    def _load_earthquake(conn: sqlite3.Connection) -> Optional[Dict[str, Any]]:
        """ギルド別地震通知設定を返す。"""
        # ギルド
        guild_rows = conn.execute(
            """
            SELECT guild_id, eew_channel_id, quake_channel_id,
                   tsunami_channel_id, notify_tsunami
            FROM earthquake_guild_config
            """
        ).fetchall()
        # 震度
        scale_rows = conn.execute(
            """
            SELECT guild_id, info_type, scale
            FROM earthquake_notify_scales
            """
        ).fetchall()
        # 空
        if not guild_rows and not scale_rows:
            return None
        # 結果
        result: Dict[str, Any] = {}
        # ギルド
        for (
            guild_id,
            eew,
            quake,
            tsunami,
            notify_tsunami,
        ) in guild_rows:
            result[str(guild_id)] = {
                "eew": eew,
                "quake": quake,
                "tsunami": tsunami,
                "notify_scales_eew": [],
                "notify_scales_quake": [],
                "notify_tsunami": bool(notify_tsunami),
            }
        # 震度リスト
        for guild_id, info_type, scale in scale_rows:
            # ギルド確保
            entry = result.setdefault(
                str(guild_id),
                {
                    "eew": None,
                    "quake": None,
                    "tsunami": None,
                    "notify_scales_eew": [],
                    "notify_scales_quake": [],
                    "notify_tsunami": True,
                },
            )
            # 種別キー
            key = (
                "notify_scales_eew"
                if info_type == "eew"
                else "notify_scales_quake"
            )
            # 追加
            entry.setdefault(key, []).append(int(scale))
        return result

    @staticmethod
    def _save_earthquake(conn: sqlite3.Connection, data: Any) -> None:
        """earthquake_* を全置換する。"""
        # 子→親
        conn.execute("DELETE FROM earthquake_notify_scales")
        conn.execute("DELETE FROM earthquake_guild_config")
        # dict 以外
        if not isinstance(data, dict):
            return
        # ギルドごと
        for guild_id, settings in data.items():
            # dict のみ
            if not isinstance(settings, dict):
                continue
            # gid
            gid = str(guild_id)
            # 親行
            conn.execute(
                """
                INSERT INTO earthquake_guild_config (
                    guild_id, eew_channel_id, quake_channel_id,
                    tsunami_channel_id, notify_tsunami
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    gid,
                    settings.get("eew"),
                    settings.get("quake"),
                    settings.get("tsunami"),
                    1 if settings.get("notify_tsunami", True) else 0,
                ),
            )
            # 震度リストを書く
            for info_type, key in (
                ("eew", "notify_scales_eew"),
                ("quake", "notify_scales_quake"),
            ):
                # リスト取得
                scales = settings.get(key) or []
                # list のみ
                if not isinstance(scales, list):
                    continue
                # 各震度
                for scale in scales:
                    try:
                        # int 化
                        scale_i = int(scale)
                    except (TypeError, ValueError):
                        continue
                    # 挿入
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO earthquake_notify_scales
                            (guild_id, info_type, scale)
                        VALUES (?, ?, ?)
                        """,
                        (gid, info_type, scale_i),
                    )

    # ------------------------------------------------------------------
    # logging_channels
    # ------------------------------------------------------------------
    @staticmethod
    def _load_logging_channels(conn: sqlite3.Connection) -> Optional[List[int]]:
        """チャンネル ID リストを返す。"""
        # 全行
        rows = conn.execute(
            "SELECT channel_id FROM logging_channels ORDER BY channel_id"
        ).fetchall()
        # 空でも [] と None を区別: 行が無ければ None（未設定）
        # ただし空リストを保存したケースもあるので、schema 上は空=未設定扱いでも
        # 呼び出し側は list を期待する。空配列を返すと「設定済みだが空」。
        # テーブルに一度でも書いたら行がある。行ゼロ = 未設定 → None。
        if not rows:
            return None
        # int リスト
        return [int(channel_id) for (channel_id,) in rows]

    @staticmethod
    def _save_logging_channels(conn: sqlite3.Connection, data: Any) -> None:
        """logging_channels を全置換する。"""
        # 削除
        conn.execute("DELETE FROM logging_channels")
        # list 以外
        if not isinstance(data, list):
            return
        # 各 ID
        for channel_id in data:
            try:
                # int 化
                cid = int(channel_id)
            except (TypeError, ValueError):
                continue
            # 挿入
            conn.execute(
                "INSERT OR IGNORE INTO logging_channels (channel_id) VALUES (?)",
                (cid,),
            )

    # ------------------------------------------------------------------
    # response_times
    # ------------------------------------------------------------------
    @staticmethod
    def _load_response_times(conn: sqlite3.Connection) -> Optional[Dict[str, List[float]]]:
        """model → [seconds...] を返す。"""
        # 順序付きで取る
        rows = conn.execute(
            """
            SELECT model_name, elapsed_seconds
            FROM response_time_samples
            ORDER BY model_name, sample_index
            """
        ).fetchall()
        # 空
        if not rows:
            return None
        # 組み立て
        result: Dict[str, List[float]] = {}
        for model_name, elapsed in rows:
            result.setdefault(str(model_name), []).append(float(elapsed))
        return result

    @staticmethod
    def _save_response_times(conn: sqlite3.Connection, data: Any) -> None:
        """response_time_samples を全置換する。"""
        # 削除
        conn.execute("DELETE FROM response_time_samples")
        # dict 以外
        if not isinstance(data, dict):
            return
        # モデルごと
        for model_name, times in data.items():
            # list のみ
            if not isinstance(times, list):
                continue
            # インデックス付きで挿入
            for index, elapsed in enumerate(times):
                try:
                    # float 化
                    value = float(elapsed)
                except (TypeError, ValueError):
                    continue
                # 挿入
                conn.execute(
                    """
                    INSERT INTO response_time_samples
                        (model_name, sample_index, elapsed_seconds)
                    VALUES (?, ?, ?)
                    """,
                    (str(model_name), int(index), value),
                )

    # ------------------------------------------------------------------
    # log_viewer_config
    # ------------------------------------------------------------------
    @staticmethod
    def _load_log_viewer(conn: sqlite3.Connection) -> Optional[Dict[str, Any]]:
        """ログビューア設定を返す。"""
        # 単一行
        row = conn.execute(
            """
            SELECT font_name, font_size, max_lines, auto_scroll,
                   level_general, level_llm, level_tts, level_error
            FROM log_viewer_config
            WHERE id = 1
            """
        ).fetchone()
        # 無ければ未設定
        if row is None:
            return None
        # 分解
        (
            font_name,
            font_size,
            max_lines,
            auto_scroll,
            level_general,
            level_llm,
            level_tts,
            level_error,
        ) = row
        # GUI が期待する形
        return {
            "font": [str(font_name), int(font_size)],
            "max_lines": int(max_lines),
            "auto_scroll": bool(auto_scroll),
            "log_levels": {
                "general": str(level_general),
                "llm": str(level_llm),
                "tts": str(level_tts),
                "error": str(level_error),
            },
        }

    @staticmethod
    def _save_log_viewer(conn: sqlite3.Connection, data: Any) -> None:
        """log_viewer_config を単一行で保存する。"""
        # dict 以外は何もしない（既存を消さない方が安全だが、置換方針に合わせる）
        if not isinstance(data, dict):
            conn.execute("DELETE FROM log_viewer_config")
            return
        # font
        font = data.get("font") or ["Meiryo UI", 9]
        # list/tuple
        if isinstance(font, (list, tuple)) and len(font) >= 2:
            font_name = str(font[0])
            font_size = int(font[1])
        else:
            font_name = "Meiryo UI"
            font_size = 9
        # log_levels
        levels = data.get("log_levels") or {}
        if not isinstance(levels, dict):
            levels = {}
        # UPSERT
        conn.execute(
            """
            INSERT OR REPLACE INTO log_viewer_config (
                id, font_name, font_size, max_lines, auto_scroll,
                level_general, level_llm, level_tts, level_error
            ) VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                font_name,
                font_size,
                int(data.get("max_lines", 1000)),
                1 if data.get("auto_scroll", True) else 0,
                str(levels.get("general", "INFO")),
                str(levels.get("llm", "INFO")),
                str(levels.get("tts", "INFO")),
                str(levels.get("error", "WARNING")),
            ),
        )

    # ------------------------------------------------------------------
    # gdrive_deletion_schedule
    # ------------------------------------------------------------------
    @staticmethod
    def _load_gdrive(conn: sqlite3.Connection) -> Optional[Dict[str, float]]:
        """file_id → delete_at を返す。"""
        # 全行
        rows = conn.execute(
            "SELECT file_id, delete_at FROM gdrive_deletion_schedule"
        ).fetchall()
        # 空
        if not rows:
            return None
        # 辞書化
        return {str(file_id): float(delete_at) for file_id, delete_at in rows}

    @staticmethod
    def _save_gdrive(conn: sqlite3.Connection, data: Any) -> None:
        """gdrive_deletion_schedule を全置換する。"""
        # 削除
        conn.execute("DELETE FROM gdrive_deletion_schedule")
        # dict 以外
        if not isinstance(data, dict):
            return
        # 各予定
        for file_id, delete_at in data.items():
            try:
                # float 化
                when = float(delete_at)
            except (TypeError, ValueError):
                continue
            # 挿入
            conn.execute(
                """
                INSERT INTO gdrive_deletion_schedule (file_id, delete_at)
                VALUES (?, ?)
                """,
                (str(file_id), when),
            )
