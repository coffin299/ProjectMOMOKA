"""SQLite 正規化テーブルへ SettingsDB の公開データ形状を写像する。"""
from __future__ import annotations

import asyncio
import logging
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from MOMOKA.storage.settings.constants import (
    DEFAULT_DB_PATH,
    GUILD_ADMIN_NAMESPACES,
    NS_CHANNEL_IMAGE_MODELS,
    NS_CHANNEL_LLM_MODELS,
    NS_EARTHQUAKE_CONFIG,
    NS_FILEIO_DELETION_SCHEDULE,
    NS_GDRIVE_DELETION_SCHEDULE,
    NS_LINK_FIX_SETTINGS,
    NS_LOG_VIEWER_CONFIG,
    NS_LOGGING_CHANNELS,
    NS_RESPONSE_TIMES,
    NS_SPEECH_DICTIONARY,
    NS_SPEECH_SETTINGS,
    NS_TTS_SETTINGS,
    NS_TWITCH_SETTINGS,
    SCHEMA_VERSION,
)

logger = logging.getLogger(__name__)


class SettingsDB:
    """namespace 単位の load/save を正規化テーブルへ写像するストア。"""

    def __init__(self, path: str = DEFAULT_DB_PATH) -> None:
        # DB ファイルのパスを保持する。
        self.path = Path(path)
        # 同時読込と書込を直列化する。
        self._lock = threading.Lock()
        # DB 用の親ディレクトリを用意する。
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # 正規化テーブルを初期化する。
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        """新しい SQLite 接続を開く。"""
        # 非同期処理から別スレッドで利用できる接続を作成する。
        conn = sqlite3.connect(str(self.path), check_same_thread=False)
        # 子テーブルの整合性を維持する。
        conn.execute("PRAGMA foreign_keys = ON")
        # 呼び出し元が閉じる接続を返す。
        return conn

    def _ensure_schema(self) -> None:
        """現行の正規化スキーマを保証する。"""
        # スキーマ更新を他の DB 操作と競合させない。
        with self._lock:
            # スキーマ専用の接続を開く。
            conn = self._connect()
            try:
                # 版管理テーブルを作成する。
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS schema_meta ("
                    "id INTEGER PRIMARY KEY CHECK (id = 1), "
                    "version INTEGER NOT NULL)"
                )
                # 全設定テーブルを作成する。
                self._create_normalized_tables(conn)
                # 廃止済み blob テーブルを削除する。
                conn.execute("DROP TABLE IF EXISTS settings")
                # 廃止済みバックアップテーブルを削除する。
                conn.execute("DROP TABLE IF EXISTS settings_blob_legacy")
                # 現行スキーマ版を書き込む。
                conn.execute(
                    "INSERT OR REPLACE INTO schema_meta (id, version) VALUES (1, ?)",
                    (SCHEMA_VERSION,),
                )
                # スキーマ変更を確定する。
                conn.commit()
            finally:
                # 成否に関係なく接続を閉じる。
                conn.close()

    @staticmethod
    def _create_normalized_tables(conn: sqlite3.Connection) -> None:
        """全正規化テーブルを作成する。"""
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS channel_llm_models (
                bot_id TEXT NOT NULL, channel_id TEXT NOT NULL,
                model TEXT NOT NULL, expires_at REAL,
                PRIMARY KEY (bot_id, channel_id)
            );
            CREATE TABLE IF NOT EXISTS channel_image_models (
                channel_id TEXT PRIMARY KEY, model TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS link_fix_guilds (
                guild_id TEXT PRIMARY KEY, enabled INTEGER
            );
            CREATE TABLE IF NOT EXISTS link_fix_sites (
                guild_id TEXT NOT NULL, site_id TEXT NOT NULL, enabled INTEGER,
                fix_domain TEXT, PRIMARY KEY (guild_id, site_id),
                FOREIGN KEY (guild_id) REFERENCES link_fix_guilds(guild_id)
                    ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS link_fix_site_match_domains (
                guild_id TEXT NOT NULL, site_id TEXT NOT NULL, domain TEXT NOT NULL,
                PRIMARY KEY (guild_id, site_id, domain),
                FOREIGN KEY (guild_id, site_id)
                    REFERENCES link_fix_sites(guild_id, site_id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS tts_channel_settings (
                channel_id TEXT PRIMARY KEY, model_id INTEGER NOT NULL,
                style TEXT NOT NULL, style_weight REAL NOT NULL, speed REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS speech_guild_settings (
                guild_id TEXT PRIMARY KEY, speech_channel_id INTEGER,
                enable_notifications INTEGER NOT NULL DEFAULT 1,
                volume REAL NOT NULL DEFAULT 1.0
            );
            CREATE TABLE IF NOT EXISTS speech_auto_join_users (
                guild_id TEXT NOT NULL, user_id INTEGER NOT NULL,
                PRIMARY KEY (guild_id, user_id),
                FOREIGN KEY (guild_id) REFERENCES speech_guild_settings(guild_id)
                    ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS speech_dictionary (
                guild_id TEXT NOT NULL, word TEXT NOT NULL, reading TEXT NOT NULL,
                PRIMARY KEY (guild_id, word)
            );
            CREATE TABLE IF NOT EXISTS twitch_watch (
                guild_id TEXT NOT NULL, twitch_user_id TEXT NOT NULL,
                twitch_login_name TEXT NOT NULL, twitch_display_name TEXT NOT NULL,
                notification_channel_id INTEGER NOT NULL, last_status TEXT NOT NULL,
                message TEXT, PRIMARY KEY (guild_id, twitch_user_id)
            );
            CREATE TABLE IF NOT EXISTS earthquake_guild_config (
                guild_id TEXT PRIMARY KEY, eew_channel_id INTEGER,
                quake_channel_id INTEGER, tsunami_channel_id INTEGER,
                notify_tsunami INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS earthquake_notify_scales (
                guild_id TEXT NOT NULL, info_type TEXT NOT NULL, scale INTEGER NOT NULL,
                PRIMARY KEY (guild_id, info_type, scale),
                FOREIGN KEY (guild_id) REFERENCES earthquake_guild_config(guild_id)
                    ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS logging_channels (
                channel_id INTEGER PRIMARY KEY
            );
            CREATE TABLE IF NOT EXISTS response_time_samples (
                model_name TEXT NOT NULL, sample_index INTEGER NOT NULL,
                elapsed_seconds REAL NOT NULL, PRIMARY KEY (model_name, sample_index)
            );
            CREATE TABLE IF NOT EXISTS log_viewer_config (
                id INTEGER PRIMARY KEY CHECK (id = 1), font_name TEXT NOT NULL,
                font_size INTEGER NOT NULL, max_lines INTEGER NOT NULL,
                auto_scroll INTEGER NOT NULL, level_general TEXT NOT NULL,
                level_llm TEXT NOT NULL, level_tts TEXT NOT NULL,
                level_error TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS fileio_deletion_schedule (
                file_key TEXT PRIMARY KEY, delete_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS gdrive_deletion_schedule (
                file_id TEXT PRIMARY KEY, delete_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS vc_playback_sessions (
                bot_id TEXT NOT NULL,
                guild_id INTEGER NOT NULL,
                voice_channel_id INTEGER NOT NULL,
                text_channel_id INTEGER,
                volume REAL NOT NULL,
                loop_mode TEXT NOT NULL,
                is_paused INTEGER NOT NULL DEFAULT 0,
                position_sec INTEGER NOT NULL DEFAULT 0,
                current_track_json TEXT,
                queue_json TEXT NOT NULL DEFAULT '[]',
                updated_at REAL NOT NULL,
                PRIMARY KEY (bot_id, guild_id)
            );
            """
        )

    def load(self, namespace: str) -> Any:
        """namespace のデータを組み立てて返す。無ければ None。"""
        # 読込中の接続利用を排他する。
        with self._lock:
            # 読込専用の接続を開く。
            conn = self._connect()
            try:
                # namespace に対応する公開データ形状を復元する。
                return self._load_namespace_conn(conn, namespace)
            finally:
                # 読込後に接続を閉じる。
                conn.close()

    def load_guild(self, namespace: str, guild_id: Any) -> Any:
        """ギルド管理 namespace の指定ギルド設定を返す。"""
        # ギルド管理対象外への誤用を拒否する。
        self._validate_guild_namespace(namespace)
        # namespace 全体の既存復元処理を利用して形状を統一する。
        data = self.load(namespace)
        # 未設定または予期しない形状なら未設定として返す。
        if not isinstance(data, dict):
            return None
        # 永続化キーと同じ文字列化したギルド ID で抽出する。
        return data.get(str(guild_id))

    def save(self, namespace: str, data: Any) -> None:
        """namespace のデータを正規化テーブルへ保存する。"""
        # 書込中の接続利用を排他する。
        with self._lock:
            # 書込専用の接続を開く。
            conn = self._connect()
            try:
                # namespace に対応する全体保存を実行する。
                self._save_namespace_conn(conn, namespace, data)
                # 全処理完了後に一度だけ確定する。
                conn.commit()
            finally:
                # 成否に関係なく接続を閉じる。
                conn.close()

    def save_guild(self, namespace: str, guild_id: Any, data: Any) -> None:
        """ギルド管理 namespace の指定ギルド設定だけを保存する。"""
        # ギルド管理対象外への誤用を拒否する。
        self._validate_guild_namespace(namespace)
        # 書込中の接続利用を排他する。
        with self._lock:
            # 書込専用の接続を開く。
            conn = self._connect()
            try:
                # 指定ギルドだけを書き換える。
                self._save_guild_conn(conn, namespace, str(guild_id), data)
                # 指定ギルドの更新を原子的に確定する。
                conn.commit()
            finally:
                # 成否に関係なく接続を閉じる。
                conn.close()

    async def save_async(self, namespace: str, data: Any) -> None:
        """save をスレッドプールで実行する。"""
        # イベントループをブロックしないスレッドへ同期保存を移す。
        await asyncio.to_thread(self.save, namespace, data)

    async def save_guild_async(
        self, namespace: str, guild_id: Any, data: Any
    ) -> None:
        """save_guild をスレッドプールで実行する。"""
        # イベントループをブロックしないスレッドへギルド保存を移す。
        await asyncio.to_thread(self.save_guild, namespace, guild_id, data)

    @staticmethod
    def _validate_guild_namespace(namespace: str) -> None:
        """ギルド管理 API の対象 namespace を検証する。"""
        # ギルド管理対象外なら明示的なエラーにする。
        if namespace not in GUILD_ADMIN_NAMESPACES:
            raise ValueError(f"unsupported guild settings namespace: {namespace}")

    def _load_namespace_conn(self, conn: sqlite3.Connection, namespace: str) -> Any:
        """接続済みで namespace を読む。"""
        # namespace ごとの復元関数を対応付ける。
        loaders = {
            NS_CHANNEL_LLM_MODELS: self._load_channel_llm,
            NS_CHANNEL_IMAGE_MODELS: self._load_channel_image,
            NS_LINK_FIX_SETTINGS: self._load_link_fix,
            NS_TTS_SETTINGS: self._load_tts,
            NS_SPEECH_SETTINGS: self._load_speech_settings,
            NS_SPEECH_DICTIONARY: self._load_speech_dictionary,
            NS_TWITCH_SETTINGS: self._load_twitch,
            NS_EARTHQUAKE_CONFIG: self._load_earthquake,
            NS_LOGGING_CHANNELS: self._load_logging_channels,
            NS_RESPONSE_TIMES: self._load_response_times,
            NS_LOG_VIEWER_CONFIG: self._load_log_viewer,
            NS_FILEIO_DELETION_SCHEDULE: self._load_fileio,
            NS_GDRIVE_DELETION_SCHEDULE: self._load_fileio,
        }
        # 未知 namespace は従来どおり None とログで扱う。
        loader = loaders.get(namespace)
        if loader is None:
            logger.error("未知の settings namespace: %s", namespace)
            return None
        # 対応する復元関数を実行する。
        return loader(conn)

    def _save_namespace_conn(
        self, conn: sqlite3.Connection, namespace: str, data: Any
    ) -> None:
        """接続済みで namespace を書く。"""
        # namespace ごとの保存関数を対応付ける。
        savers = {
            NS_CHANNEL_LLM_MODELS: self._save_channel_llm,
            NS_CHANNEL_IMAGE_MODELS: self._save_channel_image,
            NS_LINK_FIX_SETTINGS: self._save_link_fix,
            NS_TTS_SETTINGS: self._save_tts,
            NS_SPEECH_SETTINGS: self._save_speech_settings,
            NS_SPEECH_DICTIONARY: self._save_speech_dictionary,
            NS_TWITCH_SETTINGS: self._save_twitch,
            NS_EARTHQUAKE_CONFIG: self._save_earthquake,
            NS_LOGGING_CHANNELS: self._save_logging_channels,
            NS_RESPONSE_TIMES: self._save_response_times,
            NS_LOG_VIEWER_CONFIG: self._save_log_viewer,
            NS_FILEIO_DELETION_SCHEDULE: self._save_fileio,
            NS_GDRIVE_DELETION_SCHEDULE: self._save_fileio,
        }
        # 未知 namespace は保存前に例外にする。
        saver = savers.get(namespace)
        if saver is None:
            raise ValueError(f"unknown settings namespace: {namespace}")
        # 対応する保存関数を実行する。
        saver(conn, data)

    def _save_guild_conn(
        self, conn: sqlite3.Connection, namespace: str, guild_id: str, data: Any
    ) -> None:
        """接続済みで指定ギルドだけを書き込む。"""
        # namespace ごとのギルド保存関数を対応付ける。
        savers = {
            NS_LINK_FIX_SETTINGS: self._save_link_fix_guild,
            NS_SPEECH_SETTINGS: self._save_speech_settings_guild,
            NS_SPEECH_DICTIONARY: self._save_speech_dictionary_guild,
            NS_TWITCH_SETTINGS: self._save_twitch_guild,
            NS_EARTHQUAKE_CONFIG: self._save_earthquake_guild,
        }
        # 検証済み namespace の保存関数を取得する。
        saver = savers[namespace]
        # 対象ギルドだけを保存する。
        saver(conn, guild_id, data)

    @staticmethod
    def _delete_values_not_in(
        conn: sqlite3.Connection,
        table: str,
        value_column: str,
        values: set[Any],
        where_sql: str = "",
        where_params: tuple[Any, ...] = (),
    ) -> None:
        """絞り込み範囲で指定値に含まれない行を削除する。"""
        # 範囲条件を WHERE 句へ変換する。
        condition = f" WHERE {where_sql}" if where_sql else ""
        # 残す値が無ければ範囲内の全行を孤児として削除する。
        if not values:
            conn.execute(f"DELETE FROM {table}{condition}", where_params)
            return
        # 指定値以外を削除する条件を作る。
        placeholders = ", ".join("?" for _ in values)
        # 既存範囲へ孤児条件を追加する。
        conjunction = " AND " if where_sql else " WHERE "
        # 範囲内で今回保存しなかった行だけを削除する。
        conn.execute(
            f"DELETE FROM {table}{condition}{conjunction}"
            f"{value_column} NOT IN ({placeholders})",
            (*where_params, *values),
        )

    @staticmethod
    def _guild_data_items(data: Any) -> list[tuple[str, dict[str, Any]]]:
        """有効なギルド ID と辞書設定の組を返す。"""
        # 辞書以外の全体保存は空の設定集合として扱う。
        if not isinstance(data, dict):
            return []
        # 辞書形式のギルド設定だけを保存対象として収集する。
        return [(str(guild_id), value) for guild_id, value in data.items()
                if isinstance(value, dict)]

    # ------------------------------------------------------------------
    # channel_llm_models
    # ------------------------------------------------------------------
    @staticmethod
    def _load_channel_llm(conn: sqlite3.Connection) -> Optional[Dict[str, Any]]:
        """bot → channel → override を組み立てる。"""
        rows = conn.execute(
            "SELECT bot_id, channel_id, model, expires_at FROM channel_llm_models"
        ).fetchall()
        if not rows:
            return None
        result: Dict[str, Any] = {}
        for bot_id, channel_id, model, expires_at in rows:
            entry: Dict[str, Any] = {"model": str(model)}
            if expires_at is not None:
                entry["expires_at"] = float(expires_at)
            result.setdefault(str(bot_id), {})[str(channel_id)] = entry
        return result

    @staticmethod
    def _save_channel_llm(conn: sqlite3.Connection, data: Any) -> None:
        """channel_llm_models を全置換する。"""
        conn.execute("DELETE FROM channel_llm_models")
        if not isinstance(data, dict):
            return
        for bot_id, channels in data.items():
            if not isinstance(channels, dict):
                continue
            for channel_id, override in channels.items():
                if not isinstance(override, dict):
                    continue
                model = override.get("model")
                if not isinstance(model, str):
                    continue
                expires_at = override.get("expires_at")
                conn.execute(
                    "INSERT INTO channel_llm_models "
                    "(bot_id, channel_id, model, expires_at) VALUES (?, ?, ?, ?)",
                    (str(bot_id), str(channel_id), model,
                     float(expires_at) if expires_at is not None else None),
                )

    # ------------------------------------------------------------------
    # channel_image_models
    # ------------------------------------------------------------------
    @staticmethod
    def _load_channel_image(conn: sqlite3.Connection) -> Optional[Dict[str, str]]:
        """channel_id → model を返す。"""
        rows = conn.execute(
            "SELECT channel_id, model FROM channel_image_models"
        ).fetchall()
        return {str(channel_id): str(model) for channel_id, model in rows} or None

    @staticmethod
    def _save_channel_image(conn: sqlite3.Connection, data: Any) -> None:
        """channel_image_models を全置換する。"""
        conn.execute("DELETE FROM channel_image_models")
        if not isinstance(data, dict):
            return
        for channel_id, model in data.items():
            if isinstance(model, str):
                conn.execute(
                    "INSERT INTO channel_image_models (channel_id, model) "
                    "VALUES (?, ?)",
                    (str(channel_id), model),
                )

    # ------------------------------------------------------------------
    # link_fix_settings
    # ------------------------------------------------------------------
    @staticmethod
    def _load_link_fix(conn: sqlite3.Connection) -> Optional[Dict[str, Any]]:
        """ギルド別 Link Fix 設定を組み立てる。"""
        guild_rows = conn.execute(
            "SELECT guild_id, enabled FROM link_fix_guilds"
        ).fetchall()
        site_rows = conn.execute(
            "SELECT guild_id, site_id, enabled, fix_domain FROM link_fix_sites"
        ).fetchall()
        domain_rows = conn.execute(
            "SELECT guild_id, site_id, domain FROM link_fix_site_match_domains"
        ).fetchall()
        if not guild_rows and not site_rows and not domain_rows:
            return None
        result: Dict[str, Any] = {}
        for guild_id, enabled in guild_rows:
            entry = result.setdefault(str(guild_id), {})
            if enabled is not None:
                entry["enabled"] = bool(enabled)
        for guild_id, site_id, enabled, fix_domain in site_rows:
            site: Dict[str, Any] = {}
            if enabled is not None:
                site["enabled"] = bool(enabled)
            if fix_domain:
                site["fix_domain"] = str(fix_domain)
            result.setdefault(str(guild_id), {}).setdefault("sites", {})[
                str(site_id)
            ] = site
        for guild_id, site_id, domain in domain_rows:
            site = result.setdefault(str(guild_id), {}).setdefault(
                "sites", {}
            ).setdefault(str(site_id), {})
            site.setdefault("match_domains", []).append(str(domain))
        return result

    def _save_link_fix(self, conn: sqlite3.Connection, data: Any) -> None:
        """link_fix_* を upsert 後に孤児削除で全体保存する。"""
        # 有効なギルド設定を抽出する。
        items = self._guild_data_items(data)
        # 各ギルドを先に upsert する。
        for guild_id, guild_data in items:
            self._save_link_fix_guild(conn, guild_id, guild_data)
        # 今回含まれなかったギルドと子要素を最後に削除する。
        self._delete_values_not_in(
            conn, "link_fix_guilds", "guild_id", {guild_id for guild_id, _ in items}
        )

    def _save_link_fix_guild(
        self, conn: sqlite3.Connection, guild_id: str, data: Any
    ) -> None:
        """Link Fix の指定ギルドだけを upsert と孤児削除で保存する。"""
        # 辞書以外は対象ギルドの設定削除として扱う。
        if not isinstance(data, dict):
            conn.execute("DELETE FROM link_fix_guilds WHERE guild_id = ?", (guild_id,))
            return
        # 未指定 enabled を NULL として既存データ形状を維持する。
        enabled = data.get("enabled") if "enabled" in data else None
        # ギルド親行を upsert する。
        conn.execute(
            "INSERT INTO link_fix_guilds (guild_id, enabled) VALUES (?, ?) "
            "ON CONFLICT(guild_id) DO UPDATE SET enabled = excluded.enabled",
            (guild_id, None if enabled is None else int(bool(enabled))),
        )
        # サイト辞書を正規化する。
        sites = data.get("sites") if isinstance(data.get("sites"), dict) else {}
        # 有効なサイト ID を収集する。
        site_ids: set[str] = set()
        for site_id, site_data in sites.items():
            if not isinstance(site_data, dict):
                continue
            # サイト ID を保存用の文字列へ統一する。
            saved_site_id = str(site_id)
            site_ids.add(saved_site_id)
            # サイトの任意フィールドを既存仕様で整形する。
            site_enabled = (
                site_data.get("enabled") if "enabled" in site_data else None
            )
            fix_domain = site_data.get("fix_domain")
            # サイト行を upsert する。
            conn.execute(
                "INSERT INTO link_fix_sites "
                "(guild_id, site_id, enabled, fix_domain) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(guild_id, site_id) DO UPDATE SET "
                "enabled = excluded.enabled, fix_domain = excluded.fix_domain",
                (guild_id, saved_site_id,
                 None if site_enabled is None else int(bool(site_enabled)),
                 str(fix_domain) if fix_domain else None),
            )
            # 有効なマッチ元ドメインを収集する。
            domains = site_data.get("match_domains")
            domain_values = {
                str(domain) for domain in domains
                if domain
            } if isinstance(domains, list) else set()
            # 現在のドメインを upsert する。
            for domain in domain_values:
                conn.execute(
                    "INSERT INTO link_fix_site_match_domains "
                    "(guild_id, site_id, domain) VALUES (?, ?, ?) "
                    "ON CONFLICT(guild_id, site_id, domain) DO NOTHING",
                    (guild_id, saved_site_id, domain),
                )
            # このサイト内だけで消えたドメインを削除する。
            self._delete_values_not_in(
                conn, "link_fix_site_match_domains", "domain", domain_values,
                "guild_id = ? AND site_id = ?", (guild_id, saved_site_id),
            )
        # このギルド内だけで消えたサイトを削除する。
        self._delete_values_not_in(
            conn, "link_fix_sites", "site_id", site_ids, "guild_id = ?", (guild_id,)
        )

    # ------------------------------------------------------------------
    # tts_settings
    # ------------------------------------------------------------------
    @staticmethod
    def _load_tts(conn: sqlite3.Connection) -> Optional[Dict[str, Any]]:
        """channel_id → TTS 設定を返す。"""
        rows = conn.execute(
            "SELECT channel_id, model_id, style, style_weight, speed "
            "FROM tts_channel_settings"
        ).fetchall()
        if not rows:
            return None
        return {
            str(channel_id): {
                "model_id": int(model_id), "style": str(style),
                "style_weight": float(style_weight), "speed": float(speed),
            }
            for channel_id, model_id, style, style_weight, speed in rows
        }

    @staticmethod
    def _save_tts(conn: sqlite3.Connection, data: Any) -> None:
        """tts_channel_settings を全置換する。"""
        # 非 dict のまま DELETE するとテーブルが空になるため先に拒否する
        if not isinstance(data, dict):
            # 呼び出し元へ不正入力を明示する
            raise TypeError(
                f"tts_channel_settings save expects dict, got {type(data).__name__}"
            )
        # 形状検証後にだけ全削除する
        conn.execute("DELETE FROM tts_channel_settings")
        for channel_id, settings in data.items():
            if not isinstance(settings, dict) or "model_id" not in settings:
                continue
            conn.execute(
                "INSERT INTO tts_channel_settings "
                "(channel_id, model_id, style, style_weight, speed) "
                "VALUES (?, ?, ?, ?, ?)",
                (str(channel_id), int(settings.get("model_id", 0)),
                 str(settings.get("style", "Neutral")),
                 float(settings.get("style_weight", 5.0)),
                 float(settings.get("speed", 1.0))),
            )

    # ------------------------------------------------------------------
    # speech_settings
    # ------------------------------------------------------------------
    @staticmethod
    def _load_speech_settings(conn: sqlite3.Connection) -> Optional[Dict[str, Any]]:
        """ギルド別読み上げ設定を返す。"""
        guild_rows = conn.execute(
            "SELECT guild_id, speech_channel_id, enable_notifications, volume "
            "FROM speech_guild_settings"
        ).fetchall()
        user_rows = conn.execute(
            "SELECT guild_id, user_id FROM speech_auto_join_users"
        ).fetchall()
        if not guild_rows and not user_rows:
            return None
        result: Dict[str, Any] = {}
        for guild_id, channel_id, enabled, volume in guild_rows:
            result[str(guild_id)] = {
                "speech_channel_id": channel_id, "auto_join_users": [],
                "enable_notifications": bool(enabled), "volume": float(volume),
            }
        for guild_id, user_id in user_rows:
            result.setdefault(str(guild_id), {
                "speech_channel_id": None, "auto_join_users": [],
                "enable_notifications": True, "volume": 1.0,
            }).setdefault("auto_join_users", []).append(int(user_id))
        return result

    def _save_speech_settings(
        self, conn: sqlite3.Connection, data: Any
    ) -> None:
        """speech_* を upsert 後に孤児削除で全体保存する。"""
        # 有効なギルド設定を抽出する。
        items = self._guild_data_items(data)
        # 各ギルドを先に upsert する。
        for guild_id, guild_data in items:
            self._save_speech_settings_guild(conn, guild_id, guild_data)
        # 今回含まれなかったギルドと子要素を最後に削除する。
        self._delete_values_not_in(
            conn, "speech_guild_settings", "guild_id",
            {guild_id for guild_id, _ in items},
        )

    def _save_speech_settings_guild(
        self, conn: sqlite3.Connection, guild_id: str, data: Any
    ) -> None:
        """読み上げ設定の指定ギルドだけを保存する。"""
        # 辞書以外は対象ギルドの設定削除として扱う。
        if not isinstance(data, dict):
            conn.execute(
                "DELETE FROM speech_guild_settings WHERE guild_id = ?", (guild_id,)
            )
            return
        # 親行を upsert する。
        conn.execute(
            "INSERT INTO speech_guild_settings "
            "(guild_id, speech_channel_id, enable_notifications, volume) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(guild_id) DO UPDATE SET "
            "speech_channel_id = excluded.speech_channel_id, "
            "enable_notifications = excluded.enable_notifications, "
            "volume = excluded.volume",
            (guild_id, data.get("speech_channel_id"),
             int(bool(data.get("enable_notifications", True))),
             float(data.get("volume", 1.0))),
        )
        # 数値に変換できる自動参加ユーザーだけを収集する。
        user_ids: set[int] = set()
        users = data.get("auto_join_users")
        if isinstance(users, list):
            for user_id in users:
                try:
                    user_ids.add(int(user_id))
                except (TypeError, ValueError):
                    continue
        # 現在の自動参加ユーザーを upsert する。
        for user_id in user_ids:
            conn.execute(
                "INSERT INTO speech_auto_join_users (guild_id, user_id) "
                "VALUES (?, ?) ON CONFLICT(guild_id, user_id) DO NOTHING",
                (guild_id, user_id),
            )
        # このギルド内だけで消えたユーザーを削除する。
        self._delete_values_not_in(
            conn, "speech_auto_join_users", "user_id", user_ids,
            "guild_id = ?", (guild_id,),
        )

    # ------------------------------------------------------------------
    # speech_dictionary
    # ------------------------------------------------------------------
    @staticmethod
    def _load_speech_dictionary(
        conn: sqlite3.Connection,
    ) -> Optional[Dict[str, Any]]:
        """ギルド → 単語 → 読み を返す。"""
        rows = conn.execute(
            "SELECT guild_id, word, reading FROM speech_dictionary"
        ).fetchall()
        if not rows:
            return None
        result: Dict[str, Any] = {}
        for guild_id, word, reading in rows:
            result.setdefault(str(guild_id), {})[str(word)] = str(reading)
        return result

    def _save_speech_dictionary(
        self, conn: sqlite3.Connection, data: Any
    ) -> None:
        """speech_dictionary を upsert 後に孤児削除で全体保存する。"""
        # 辞書形式のギルドデータだけを保存対象にする。
        items = self._guild_data_items(data)
        # 各ギルドの辞書を先に upsert する。
        for guild_id, guild_data in items:
            self._save_speech_dictionary_guild(conn, guild_id, guild_data)
        # 今回含まれなかったギルドの辞書項目を最後に削除する。
        self._delete_values_not_in(
            conn, "speech_dictionary", "guild_id",
            {guild_id for guild_id, _ in items},
        )

    def _save_speech_dictionary_guild(
        self, conn: sqlite3.Connection, guild_id: str, data: Any
    ) -> None:
        """読み上げ辞書の指定ギルドだけを保存する。"""
        # 辞書以外は対象ギルドの辞書削除として扱う。
        if not isinstance(data, dict):
            conn.execute(
                "DELETE FROM speech_dictionary WHERE guild_id = ?", (guild_id,)
            )
            return
        # 有効な単語と読みだけを収集する。
        entries = {
            word: reading for word, reading in data.items()
            if isinstance(word, str) and isinstance(reading, str)
        }
        # 現在の単語を upsert する。
        for word, reading in entries.items():
            conn.execute(
                "INSERT INTO speech_dictionary (guild_id, word, reading) "
                "VALUES (?, ?, ?) ON CONFLICT(guild_id, word) DO UPDATE SET "
                "reading = excluded.reading",
                (guild_id, word, reading),
            )
        # このギルド内だけで消えた単語を削除する。
        self._delete_values_not_in(
            conn, "speech_dictionary", "word", set(entries),
            "guild_id = ?", (guild_id,),
        )

    # ------------------------------------------------------------------
    # twitch_settings
    # ------------------------------------------------------------------
    @staticmethod
    def _load_twitch(conn: sqlite3.Connection) -> Optional[Dict[str, Any]]:
        """ギルド → twitch_user_id → 設定 を返す。"""
        rows = conn.execute(
            "SELECT guild_id, twitch_user_id, twitch_login_name, "
            "twitch_display_name, notification_channel_id, last_status, message "
            "FROM twitch_watch"
        ).fetchall()
        if not rows:
            return None
        result: Dict[str, Any] = {}
        for guild_id, user_id, login, display, channel_id, status, message in rows:
            entry: Dict[str, Any] = {
                "twitch_login_name": str(login),
                "twitch_display_name": str(display),
                "notification_channel_id": int(channel_id),
                "last_status": str(status),
            }
            if message is not None:
                entry["message"] = str(message)
            result.setdefault(str(guild_id), {})[str(user_id)] = entry
        return result

    def _save_twitch(self, conn: sqlite3.Connection, data: Any) -> None:
        """twitch_watch を upsert 後に孤児削除で全体保存する。"""
        # 有効なギルド設定を抽出する。
        items = self._guild_data_items(data)
        # 各ギルドを先に upsert する。
        for guild_id, guild_data in items:
            self._save_twitch_guild(conn, guild_id, guild_data)
        # 今回含まれなかったギルドの通知設定を最後に削除する。
        self._delete_values_not_in(
            conn, "twitch_watch", "guild_id", {guild_id for guild_id, _ in items}
        )

    def _save_twitch_guild(
        self, conn: sqlite3.Connection, guild_id: str, data: Any
    ) -> None:
        """Twitch 通知の指定ギルドだけを保存する。"""
        # 辞書以外は対象ギルドの通知設定削除として扱う。
        if not isinstance(data, dict):
            conn.execute("DELETE FROM twitch_watch WHERE guild_id = ?", (guild_id,))
            return
        # 保存可能な Twitch ユーザー ID を収集する。
        user_ids: set[str] = set()
        for user_id, settings in data.items():
            if not isinstance(settings, dict):
                continue
            if "notification_channel_id" not in settings:
                continue
            # 保存用のユーザー ID を統一する。
            saved_user_id = str(user_id)
            user_ids.add(saved_user_id)
            # 通知設定を upsert する。
            conn.execute(
                "INSERT INTO twitch_watch (guild_id, twitch_user_id, "
                "twitch_login_name, twitch_display_name, "
                "notification_channel_id, last_status, message) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(guild_id, twitch_user_id) DO UPDATE SET "
                "twitch_login_name = excluded.twitch_login_name, "
                "twitch_display_name = excluded.twitch_display_name, "
                "notification_channel_id = excluded.notification_channel_id, "
                "last_status = excluded.last_status, message = excluded.message",
                (guild_id, saved_user_id,
                 str(settings.get("twitch_login_name", "")),
                 str(settings.get("twitch_display_name", "")),
                 int(settings["notification_channel_id"]),
                 str(settings.get("last_status", "offline")),
                 settings.get("message")),
            )
        # このギルド内だけで消えた監視対象を削除する。
        self._delete_values_not_in(
            conn, "twitch_watch", "twitch_user_id", user_ids,
            "guild_id = ?", (guild_id,),
        )

    # ------------------------------------------------------------------
    # earthquake
    # ------------------------------------------------------------------
    @staticmethod
    def _load_earthquake(conn: sqlite3.Connection) -> Optional[Dict[str, Any]]:
        """ギルド別地震通知設定を返す。"""
        guild_rows = conn.execute(
            "SELECT guild_id, eew_channel_id, quake_channel_id, "
            "tsunami_channel_id, notify_tsunami FROM earthquake_guild_config"
        ).fetchall()
        scale_rows = conn.execute(
            "SELECT guild_id, info_type, scale FROM earthquake_notify_scales"
        ).fetchall()
        if not guild_rows and not scale_rows:
            return None
        result: Dict[str, Any] = {}
        for guild_id, eew, quake, tsunami, notify_tsunami in guild_rows:
            result[str(guild_id)] = {
                "eew": eew, "quake": quake, "tsunami": tsunami,
                "notify_scales_eew": [], "notify_scales_quake": [],
                "notify_tsunami": bool(notify_tsunami),
            }
        for guild_id, info_type, scale in scale_rows:
            entry = result.setdefault(str(guild_id), {
                "eew": None, "quake": None, "tsunami": None,
                "notify_scales_eew": [], "notify_scales_quake": [],
                "notify_tsunami": True,
            })
            key = (
                "notify_scales_eew"
                if info_type == "eew" else "notify_scales_quake"
            )
            entry.setdefault(key, []).append(int(scale))
        return result

    def _save_earthquake(self, conn: sqlite3.Connection, data: Any) -> None:
        """earthquake_* を upsert 後に孤児削除で全体保存する。"""
        # 有効なギルド設定を抽出する。
        items = self._guild_data_items(data)
        # 各ギルドを先に upsert する。
        for guild_id, guild_data in items:
            self._save_earthquake_guild(conn, guild_id, guild_data)
        # 今回含まれなかったギルドと子要素を最後に削除する。
        self._delete_values_not_in(
            conn, "earthquake_guild_config", "guild_id",
            {guild_id for guild_id, _ in items},
        )

    def _save_earthquake_guild(
        self, conn: sqlite3.Connection, guild_id: str, data: Any
    ) -> None:
        """地震通知の指定ギルドだけを保存する。"""
        # 辞書以外は対象ギルドの通知設定削除として扱う。
        if not isinstance(data, dict):
            conn.execute(
                "DELETE FROM earthquake_guild_config WHERE guild_id = ?", (guild_id,)
            )
            return
        # ギルド親行を upsert する。
        conn.execute(
            "INSERT INTO earthquake_guild_config "
            "(guild_id, eew_channel_id, quake_channel_id, tsunami_channel_id, "
            "notify_tsunami) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(guild_id) DO UPDATE SET "
            "eew_channel_id = excluded.eew_channel_id, "
            "quake_channel_id = excluded.quake_channel_id, "
            "tsunami_channel_id = excluded.tsunami_channel_id, "
            "notify_tsunami = excluded.notify_tsunami",
            (guild_id, data.get("eew"), data.get("quake"), data.get("tsunami"),
             int(bool(data.get("notify_tsunami", True)))),
        )
        # 各通知種別の有効な震度を upsert して孤児を削除する。
        for info_type, key in (
            ("eew", "notify_scales_eew"),
            ("quake", "notify_scales_quake"),
        ):
            scales = data.get(key)
            scale_values: set[int] = set()
            if isinstance(scales, list):
                for scale in scales:
                    try:
                        scale_values.add(int(scale))
                    except (TypeError, ValueError):
                        continue
            for scale in scale_values:
                conn.execute(
                    "INSERT INTO earthquake_notify_scales "
                    "(guild_id, info_type, scale) VALUES (?, ?, ?) "
                    "ON CONFLICT(guild_id, info_type, scale) DO NOTHING",
                    (guild_id, info_type, scale),
                )
            self._delete_values_not_in(
                conn, "earthquake_notify_scales", "scale", scale_values,
                "guild_id = ? AND info_type = ?", (guild_id, info_type),
            )
        # 定義外の通知種別は指定ギルドからだけ削除する。
        conn.execute(
            "DELETE FROM earthquake_notify_scales "
            "WHERE guild_id = ? AND info_type NOT IN ('eew', 'quake')",
            (guild_id,),
        )

    # ------------------------------------------------------------------
    # host-only namespaces
    # ------------------------------------------------------------------
    @staticmethod
    def _load_logging_channels(conn: sqlite3.Connection) -> Optional[List[int]]:
        """チャンネル ID リストを返す。"""
        rows = conn.execute(
            "SELECT channel_id FROM logging_channels ORDER BY channel_id"
        ).fetchall()
        return [int(channel_id) for (channel_id,) in rows] or None

    @staticmethod
    def _save_logging_channels(conn: sqlite3.Connection, data: Any) -> None:
        """logging_channels を全置換する。"""
        conn.execute("DELETE FROM logging_channels")
        if not isinstance(data, list):
            return
        for channel_id in data:
            try:
                conn.execute(
                    "INSERT OR IGNORE INTO logging_channels (channel_id) VALUES (?)",
                    (int(channel_id),),
                )
            except (TypeError, ValueError):
                continue

    @staticmethod
    def _load_response_times(
        conn: sqlite3.Connection,
    ) -> Optional[Dict[str, List[float]]]:
        """model → [seconds...] を返す。"""
        rows = conn.execute(
            "SELECT model_name, elapsed_seconds FROM response_time_samples "
            "ORDER BY model_name, sample_index"
        ).fetchall()
        if not rows:
            return None
        result: Dict[str, List[float]] = {}
        for model_name, elapsed in rows:
            result.setdefault(str(model_name), []).append(float(elapsed))
        return result

    @staticmethod
    def _save_response_times(conn: sqlite3.Connection, data: Any) -> None:
        """response_time_samples を全置換する。"""
        conn.execute("DELETE FROM response_time_samples")
        if not isinstance(data, dict):
            return
        for model_name, times in data.items():
            if not isinstance(times, list):
                continue
            for index, elapsed in enumerate(times):
                try:
                    conn.execute(
                        "INSERT INTO response_time_samples "
                        "(model_name, sample_index, elapsed_seconds) "
                        "VALUES (?, ?, ?)",
                        (str(model_name), int(index), float(elapsed)),
                    )
                except (TypeError, ValueError):
                    continue

    @staticmethod
    def _load_log_viewer(conn: sqlite3.Connection) -> Optional[Dict[str, Any]]:
        """ログビューア設定を返す。"""
        row = conn.execute(
            "SELECT font_name, font_size, max_lines, auto_scroll, level_general, "
            "level_llm, level_tts, level_error FROM log_viewer_config "
            "WHERE id = 1"
        ).fetchone()
        if row is None:
            return None
        font_name, font_size, max_lines, auto_scroll, general, llm, tts, error = row
        return {
            "font": [str(font_name), int(font_size)],
            "max_lines": int(max_lines), "auto_scroll": bool(auto_scroll),
            "log_levels": {
                "general": str(general), "llm": str(llm), "tts": str(tts),
                "error": str(error),
            },
        }

    @staticmethod
    def _save_log_viewer(conn: sqlite3.Connection, data: Any) -> None:
        """log_viewer_config を単一行で保存する。"""
        if not isinstance(data, dict):
            conn.execute("DELETE FROM log_viewer_config")
            return
        font = data.get("font") or ["Meiryo UI", 9]
        if isinstance(font, (list, tuple)) and len(font) >= 2:
            font_name, font_size = str(font[0]), int(font[1])
        else:
            font_name, font_size = "Meiryo UI", 9
        levels = data.get("log_levels")
        levels = levels if isinstance(levels, dict) else {}
        conn.execute(
            "INSERT INTO log_viewer_config "
            "(id, font_name, font_size, max_lines, auto_scroll, level_general, "
            "level_llm, level_tts, level_error) VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET font_name = excluded.font_name, "
            "font_size = excluded.font_size, max_lines = excluded.max_lines, "
            "auto_scroll = excluded.auto_scroll, "
            "level_general = excluded.level_general, level_llm = excluded.level_llm, "
            "level_tts = excluded.level_tts, level_error = excluded.level_error",
            (font_name, font_size, int(data.get("max_lines", 1000)),
             int(bool(data.get("auto_scroll", True))),
             str(levels.get("general", "INFO")),
             str(levels.get("llm", "INFO")),
             str(levels.get("tts", "INFO")),
             str(levels.get("error", "WARNING"))),
        )

    def find_auto_join_by_user_id(self, user_id: int) -> List[Dict[str, Any]]:
        """speech_auto_join_users から指定ユーザー行を返す。"""
        # 読み取りを排他する。
        with self._lock:
            # 接続を開く。
            conn = self._connect()
            try:
                # 一致行を取得する。
                rows = conn.execute(
                    "SELECT guild_id, user_id FROM speech_auto_join_users "
                    "WHERE user_id = ?",
                    (int(user_id),),
                ).fetchall()
            finally:
                # 接続を閉じる。
                conn.close()
        # 公開形状へ写す。
        return [
            {"guild_id": str(guild_id), "user_id": int(uid)}
            for guild_id, uid in rows
        ]

    def delete_auto_join_by_user_id(
        self,
        user_id: int,
        guild_ids: Optional[List[str]] = None,
    ) -> int:
        """指定ユーザーの autojoin 行を削除し、削除件数を返す。"""
        # 書き込みを排他する。
        with self._lock:
            # 接続を開く。
            conn = self._connect()
            try:
                # ギルド限定が無ければ全削除。
                if not guild_ids:
                    # 全ギルド対象。
                    cursor = conn.execute(
                        "DELETE FROM speech_auto_join_users WHERE user_id = ?",
                        (int(user_id),),
                    )
                else:
                    # 指定ギルドだけ削除する。
                    placeholders = ",".join("?" for _ in guild_ids)
                    # パラメータを組み立てる。
                    params: List[Any] = [int(user_id), *[str(g) for g in guild_ids]]
                    # 削除を実行する。
                    cursor = conn.execute(
                        "DELETE FROM speech_auto_join_users "
                        f"WHERE user_id = ? AND guild_id IN ({placeholders})",
                        params,
                    )
                # 確定する。
                conn.commit()
                # 削除件数を返す。
                return int(cursor.rowcount or 0)
            finally:
                # 接続を閉じる。
                conn.close()

    @staticmethod
    def _load_fileio(conn: sqlite3.Connection) -> Optional[Dict[str, float]]:
        """file_key → delete_at を返す（旧 gdrive テーブルも読む）。"""
        # 新テーブルを優先する
        rows = conn.execute(
            "SELECT file_key, delete_at FROM fileio_deletion_schedule"
        ).fetchall()
        result = {
            str(file_key): float(delete_at) for file_key, delete_at in rows
        }
        # 旧テーブルがあればマージする
        try:
            old_rows = conn.execute(
                "SELECT file_id, delete_at FROM gdrive_deletion_schedule"
            ).fetchall()
            for file_id, delete_at in old_rows:
                result.setdefault(str(file_id), float(delete_at))
        except sqlite3.Error:
            pass
        return result or None

    @staticmethod
    def _save_fileio(conn: sqlite3.Connection, data: Any) -> None:
        """fileio_deletion_schedule を全置換する。"""
        conn.execute("DELETE FROM fileio_deletion_schedule")
        # 旧テーブルも空にして移行完了扱いにする
        try:
            conn.execute("DELETE FROM gdrive_deletion_schedule")
        except sqlite3.Error:
            pass
        if not isinstance(data, dict):
            return
        for file_key, delete_at in data.items():
            try:
                conn.execute(
                    "INSERT INTO fileio_deletion_schedule (file_key, delete_at) "
                    "VALUES (?, ?)",
                    (str(file_key), float(delete_at)),
                )
            except (TypeError, ValueError):
                continue


_default_db: Optional[SettingsDB] = None
_default_lock = threading.Lock()


def get_default_settings_db(path: str = DEFAULT_DB_PATH) -> SettingsDB:
    """プロセス共通の SettingsDB を返す。"""
    # 共有インスタンスを更新する。
    global _default_db
    # 二重生成を避ける。
    with _default_lock:
        # 初回だけデフォルトストアを作成する。
        if _default_db is None:
            _default_db = SettingsDB(path)
        # 共有ストアを返す。
        return _default_db


def resolve_settings_db(bot: Any = None) -> SettingsDB:
    """bot.settings_db があればそれを、無ければデフォルトを返す。"""
    # 注入済みストアを優先する。
    injected = getattr(bot, "settings_db", None) if bot is not None else None
    # 注入済みならそのまま返す。
    if injected is not None:
        return injected
    # GUI と単体利用では共有ストアを返す。
    return get_default_settings_db()
