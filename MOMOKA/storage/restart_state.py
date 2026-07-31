"""グレースフル再起動向けの VC 再生セッション永続化。"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from MOMOKA.storage.settings.constants import DEFAULT_DB_PATH

# 再起動セッション保存の失敗を記録する。
logger = logging.getLogger(__name__)


class VcPlaybackSessionStore:
    """bot_id × guild_id 単位で VC 再生スナップショットを保存する。"""

    def __init__(self, path: str = DEFAULT_DB_PATH) -> None:
        # DB ファイルパスを保持する。
        self.path = Path(path)
        # 同時アクセスを直列化する。
        self._lock = threading.Lock()
        # 親ディレクトリを用意する。
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # テーブルが無ければ作成する。
        self._ensure_table()

    def _connect(self) -> sqlite3.Connection:
        """新しい SQLite 接続を開く。"""
        # チェック同一スレッド制約を外して非同期呼び出しから使えるようにする。
        return sqlite3.connect(str(self.path), check_same_thread=False)

    def _ensure_table(self) -> None:
        """vc_playback_sessions テーブルを保証する。"""
        # スキーマ変更を排他する。
        with self._lock:
            # 接続を開く。
            conn = self._connect()
            try:
                # SettingsDB と同じ DDL で作成する。
                conn.execute(
                    """
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
                    )
                    """
                )
                # 変更を確定する。
                conn.commit()
            finally:
                # 接続を閉じる。
                conn.close()

    def upsert(self, session: Dict[str, Any]) -> None:
        """1 ギルド分のセッションを挿入または更新する。"""
        # 書き込みを排他する。
        with self._lock:
            # 接続を開く。
            conn = self._connect()
            try:
                # current_track を JSON 文字列化する。
                current_json = session.get("current_track_json")
                # 既に文字列ならそのまま、dict なら dumps する。
                if current_json is not None and not isinstance(current_json, str):
                    # dict を JSON にする。
                    current_json = json.dumps(current_json, ensure_ascii=False)
                # キューを JSON 文字列化する。
                queue_json = session.get("queue_json", [])
                # リストなら dumps する。
                if not isinstance(queue_json, str):
                    # キュー配列を JSON にする。
                    queue_json = json.dumps(queue_json, ensure_ascii=False)
                # UPSERT する。
                conn.execute(
                    """
                    INSERT INTO vc_playback_sessions (
                        bot_id, guild_id, voice_channel_id, text_channel_id,
                        volume, loop_mode, is_paused, position_sec,
                        current_track_json, queue_json, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(bot_id, guild_id) DO UPDATE SET
                        voice_channel_id=excluded.voice_channel_id,
                        text_channel_id=excluded.text_channel_id,
                        volume=excluded.volume,
                        loop_mode=excluded.loop_mode,
                        is_paused=excluded.is_paused,
                        position_sec=excluded.position_sec,
                        current_track_json=excluded.current_track_json,
                        queue_json=excluded.queue_json,
                        updated_at=excluded.updated_at
                    """,
                    (
                        str(session["bot_id"]),
                        int(session["guild_id"]),
                        int(session["voice_channel_id"]),
                        session.get("text_channel_id"),
                        float(session.get("volume", 0.2)),
                        str(session.get("loop_mode", "OFF")),
                        1 if session.get("is_paused") else 0,
                        int(session.get("position_sec") or 0),
                        current_json,
                        queue_json,
                        float(session.get("updated_at") or time.time()),
                    ),
                )
                # 確定する。
                conn.commit()
            finally:
                # 接続を閉じる。
                conn.close()

    def load_for_bot(self, bot_id: str) -> List[Dict[str, Any]]:
        """指定 bot の全セッションを返す。"""
        # 読み取りを排他する。
        with self._lock:
            # 接続を開く。
            conn = self._connect()
            try:
                # 行を取得する。
                rows = conn.execute(
                    """
                    SELECT bot_id, guild_id, voice_channel_id, text_channel_id,
                           volume, loop_mode, is_paused, position_sec,
                           current_track_json, queue_json, updated_at
                    FROM vc_playback_sessions
                    WHERE bot_id = ?
                    """,
                    (str(bot_id),),
                ).fetchall()
            finally:
                # 接続を閉じる。
                conn.close()
        # 辞書リストへ変換する。
        result: List[Dict[str, Any]] = []
        # 各行を公開形状へ写す。
        for row in rows:
            # current_track JSON を復元する。
            current = None
            # 文字列があれば loads する。
            if row[8]:
                try:
                    # JSON を dict にする。
                    current = json.loads(row[8])
                except json.JSONDecodeError:
                    # 壊れていれば無視する。
                    current = None
            # キュー JSON を復元する。
            queue: List[Any] = []
            # 文字列があれば loads する。
            if row[9]:
                try:
                    # JSON を list にする。
                    loaded = json.loads(row[9])
                    # list 以外は空にする。
                    queue = loaded if isinstance(loaded, list) else []
                except json.JSONDecodeError:
                    # 壊れていれば空キューにする。
                    queue = []
            # 1 セッション分を積む。
            result.append(
                {
                    "bot_id": row[0],
                    "guild_id": int(row[1]),
                    "voice_channel_id": int(row[2]),
                    "text_channel_id": int(row[3]) if row[3] is not None else None,
                    "volume": float(row[4]),
                    "loop_mode": str(row[5]),
                    "is_paused": bool(row[6]),
                    "position_sec": int(row[7] or 0),
                    "current_track": current,
                    "queue": queue,
                    "updated_at": float(row[10]),
                }
            )
        # セッション一覧を返す。
        return result

    def delete(self, bot_id: str, guild_id: int) -> None:
        """1 ギルド分のセッションを削除する。"""
        # 削除を排他する。
        with self._lock:
            # 接続を開く。
            conn = self._connect()
            try:
                # 行を消す。
                conn.execute(
                    "DELETE FROM vc_playback_sessions WHERE bot_id = ? AND guild_id = ?",
                    (str(bot_id), int(guild_id)),
                )
                # 確定する。
                conn.commit()
            finally:
                # 接続を閉じる。
                conn.close()


# プロセス共有の既定ストア（遅延生成）。
_default_store: Optional[VcPlaybackSessionStore] = None
# 既定ストア生成の排他。
_default_store_lock = threading.Lock()


def get_vc_playback_session_store() -> VcPlaybackSessionStore:
    """プロセス共有の VC セッションストアを返す。"""
    # グローバルを更新する。
    global _default_store
    # 既にあればそれを返す。
    if _default_store is not None:
        # 既存インスタンス。
        return _default_store
    # 生成を排他する。
    with _default_store_lock:
        # 二重生成を防ぐ。
        if _default_store is None:
            # 既定パスで生成する。
            _default_store = VcPlaybackSessionStore()
        # 共有インスタンスを返す。
        return _default_store
