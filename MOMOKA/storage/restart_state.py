"""グレースフル再起動向けの VC 再生セッション永続化。"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

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

    @staticmethod
    def _parse_track_requester(track: Any) -> Optional[int]:
        """トラック dict の requester_id を整数化する。"""
        # dict 以外は対象外。
        if not isinstance(track, dict):
            return None
        # 生値を取る。
        raw = track.get("requester_id")
        # 無ければ無し。
        if raw is None:
            return None
        try:
            # 整数へ変換する。
            return int(raw)
        except (TypeError, ValueError):
            # 壊れていれば無視する。
            return None

    def find_by_requester_id(self, user_id: int) -> List[Dict[str, Any]]:
        """requester_id が一致するトラックを含むセッション要約を返す。"""
        # 対象ユーザー。
        uid = int(user_id)
        # 読み取りを排他する。
        with self._lock:
            # 接続を開く。
            conn = self._connect()
            try:
                # 全セッションを取る。
                rows = conn.execute(
                    """
                    SELECT bot_id, guild_id, current_track_json, queue_json
                    FROM vc_playback_sessions
                    """
                ).fetchall()
            finally:
                # 接続を閉じる。
                conn.close()
        # 結果。
        hits: List[Dict[str, Any]] = []
        # 各行を検査する。
        for bot_id, guild_id, current_json, queue_json in rows:
            # current を復元する。
            current = None
            # 文字列があれば loads する。
            if current_json:
                try:
                    # JSON を dict にする。
                    current = json.loads(current_json)
                except json.JSONDecodeError:
                    # 壊れていれば無視する。
                    current = None
            # キューを復元する。
            queue: List[Any] = []
            # 文字列があれば loads する。
            if queue_json:
                try:
                    # JSON を list にする。
                    loaded = json.loads(queue_json)
                    # list 以外は空にする。
                    queue = loaded if isinstance(loaded, list) else []
                except json.JSONDecodeError:
                    # 壊れていれば空キューにする。
                    queue = []
            # current が対象か。
            current_hit = self._parse_track_requester(current) == uid
            # キュー内の一致タイトルを集める。
            matched_titles: List[str] = []
            # キューを走査する。
            for track in queue:
                # requester 不一致はスキップ。
                if self._parse_track_requester(track) != uid:
                    continue
                # タイトルを控える。
                title = ""
                # dict なら title を取る。
                if isinstance(track, dict):
                    title = str(track.get("title") or "")
                # 積む。
                matched_titles.append(title or "(no title)")
            # current タイトル。
            current_title = None
            # current ヒット時だけ。
            if current_hit and isinstance(current, dict):
                # タイトルを文字列化。
                current_title = str(current.get("title") or "(no title)")
            # 何も無ければスキップ。
            if not current_hit and not matched_titles:
                continue
            # 要約を積む。
            hits.append(
                {
                    "bot_id": str(bot_id),
                    "guild_id": int(guild_id),
                    "current_match": current_hit,
                    "current_title": current_title,
                    "queue_match_count": len(matched_titles),
                    "queue_titles": matched_titles[:20],
                }
            )
        # ヒット一覧を返す。
        return hits

    def scrub_requester_id(
        self,
        user_id: int,
        session_keys: Optional[List[Tuple[str, int]]] = None,
    ) -> Dict[str, Any]:
        """指定 requester_id のトラックをセッションから除去する。"""
        # 対象ユーザー。
        uid = int(user_id)
        # キー絞り込み集合。
        key_filter: Optional[Set[Tuple[str, int]]] = None
        # 指定があれば集合化。
        if session_keys is not None:
            # (bot_id, guild_id) の集合。
            key_filter = {(str(b), int(g)) for b, g in session_keys}
        # カウンタ。
        updated = 0
        deleted_sessions = 0
        removed_tracks = 0
        # 書き込みを排他する。
        with self._lock:
            # 接続を開く。
            conn = self._connect()
            try:
                # 全行を取る。
                rows = conn.execute(
                    """
                    SELECT bot_id, guild_id, current_track_json, queue_json
                    FROM vc_playback_sessions
                    """
                ).fetchall()
                # 各セッションを処理する。
                for bot_id, guild_id, current_json, queue_json in rows:
                    # キー。
                    key = (str(bot_id), int(guild_id))
                    # フィルタ外はスキップ。
                    if key_filter is not None and key not in key_filter:
                        continue
                    # current を復元する。
                    current = None
                    # 文字列があれば loads する。
                    if current_json:
                        try:
                            # JSON を dict にする。
                            current = json.loads(current_json)
                        except json.JSONDecodeError:
                            # 壊れていれば無視する。
                            current = None
                    # キューを復元する。
                    queue: List[Any] = []
                    # 文字列があれば loads する。
                    if queue_json:
                        try:
                            # JSON を list にする。
                            loaded = json.loads(queue_json)
                            # list 以外は空にする。
                            queue = loaded if isinstance(loaded, list) else []
                        except json.JSONDecodeError:
                            # 壊れていれば空キューにする。
                            queue = []
                    # 変更有無。
                    dirty = False
                    # current が対象なら落とす。
                    if self._parse_track_requester(current) == uid:
                        # current を空にする。
                        current = None
                        # 変更あり。
                        dirty = True
                        # 除去数。
                        removed_tracks += 1
                    # 残すキュー。
                    kept: List[Any] = []
                    # キューをフィルタする。
                    for track in queue:
                        # 対象 requester は落とす。
                        if self._parse_track_requester(track) == uid:
                            # 変更あり。
                            dirty = True
                            # 除去数。
                            removed_tracks += 1
                            # 次へ。
                            continue
                        # 残す。
                        kept.append(track)
                    # 変更無ければ次へ。
                    if not dirty:
                        continue
                    # current もキューも空なら行削除。
                    if current is None and not kept:
                        # セッション行を消す。
                        conn.execute(
                            "DELETE FROM vc_playback_sessions "
                            "WHERE bot_id = ? AND guild_id = ?",
                            (str(bot_id), int(guild_id)),
                        )
                        # 削除件数。
                        deleted_sessions += 1
                        # 次へ。
                        continue
                    # JSON へ戻す。
                    new_current = (
                        json.dumps(current, ensure_ascii=False)
                        if current is not None
                        else None
                    )
                    # キュー JSON。
                    new_queue = json.dumps(kept, ensure_ascii=False)
                    # 行を更新する。
                    conn.execute(
                        """
                        UPDATE vc_playback_sessions
                        SET current_track_json = ?, queue_json = ?, updated_at = ?
                        WHERE bot_id = ? AND guild_id = ?
                        """,
                        (
                            new_current,
                            new_queue,
                            float(time.time()),
                            str(bot_id),
                            int(guild_id),
                        ),
                    )
                    # 更新件数。
                    updated += 1
                # 確定する。
                conn.commit()
            finally:
                # 接続を閉じる。
                conn.close()
        # 集計を返す。
        return {
            "updated": updated,
            "deleted_sessions": deleted_sessions,
            "removed_tracks": removed_tracks,
        }


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
