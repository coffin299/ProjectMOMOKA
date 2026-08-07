# ホスト GUI 向け: Discord ユーザー ID のログ検索・マスクと DB 照会/削除

from __future__ import annotations

import hashlib
import re
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from MOMOKA.GUI.persistent_log import (
    categorize_logger_name,
    get_log_file_path,
    parse_log_line,
)
from MOMOKA.storage.settings.constants import DEFAULT_DB_PATH
from MOMOKA.storage.settings.database import get_default_settings_db
from MOMOKA.storage.restart_state import get_vc_playback_session_store

# ログファイル書き換えの排他
_log_rewrite_lock = threading.Lock()
# asctime 抽出用（persistent_log と同型）
_LINE_RE = re.compile(
    r"^(?P<asctime>.+?) - (?P<name>.+?) - (?P<level>[A-Z]+) - (?P<message>.*)$"
)
# マスク後の本文
_REDACTED_BODY = "[REDACTED]"


def _log_txt_path() -> Path:
    """momoka_gui.txt のパスを返す。"""
    # .log と同階層の .txt
    return get_log_file_path().with_suffix(".txt")


def _content_hash(line: str) -> str:
    """行内容の短いハッシュを返す。"""
    # 改行を除いた本文で安定させる
    text = line.rstrip("\r\n")
    # SHA256 の先頭 16 桁
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:16]


def _user_id_match_patterns(user_id: int) -> List[re.Pattern[str]]:
    """ユーザー ID を誤爆しにくいパターン一覧を返す。"""
    # 数値文字列
    uid = str(int(user_id))
    # エスケープ済み
    escaped = re.escape(uid)
    # マッチ用パターン
    return [
        # LLM_RESPONSE 用トークン
        re.compile(rf"requester_id={escaped}(?:\D|$)"),
        # name(id) / user='...(id)'
        re.compile(rf"\({escaped}\)"),
        # 単独トークン（前後が非数字）
        re.compile(rf"(?<!\d){escaped}(?!\d)"),
    ]


def line_matches_user_id(line: str, user_id: int) -> bool:
    """1 行が指定ユーザー ID を含むか判定する。"""
    # パターンを順に試す
    for pattern in _user_id_match_patterns(user_id):
        # ヒットしたら真
        if pattern.search(line):
            return True
    # どれも無し
    return False


def _mask_line_text(line: str) -> str:
    """日時だけ残して本文をマスクした行を返す。"""
    # 末尾改行を保持する
    ending = ""
    # CRLF
    if line.endswith("\r\n"):
        ending = "\r\n"
        body = line[:-2]
    # LF
    elif line.endswith("\n"):
        ending = "\n"
        body = line[:-1]
    # 改行なし
    else:
        body = line
    # 標準フォーマットなら asctime を残す
    match = _LINE_RE.match(body)
    if match:
        # 日時 + マスク
        return f"{match.group('asctime')} - {_REDACTED_BODY}{ending}"
    # パース不能なら全体マスク
    return f"{_REDACTED_BODY}{ending}"


def search_logs_for_user(user_id: int) -> List[Dict[str, Any]]:
    """momoka_gui.log からユーザー ID 一致行を返す。"""
    # 正本パス
    path = get_log_file_path()
    # 無ければ空
    if not path.is_file():
        return []
    # 結果
    hits: List[Dict[str, Any]] = []
    # 行番号は 1 始まり
    line_no = 0
    # 全文走査
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            # 行番号を進める
            line_no += 1
            # 末尾改行なし本文
            text = raw.rstrip("\r\n")
            # 空行はスキップ
            if not text.strip():
                continue
            # ID 不一致はスキップ
            if not line_matches_user_id(text, user_id):
                continue
            # GUI 用にパース
            parsed = parse_log_line(text) or {
                "name": "restored",
                "level": "INFO",
                "message": text,
                "category": "general",
            }
            # ヒットを積む
            hits.append(
                {
                    "line_no": line_no,
                    "content_hash": _content_hash(text),
                    "name": parsed.get("name"),
                    "level": parsed.get("level"),
                    "message": parsed.get("message"),
                    "category": parsed.get("category")
                    or categorize_logger_name(
                        str(parsed.get("name") or ""),
                        str(parsed.get("level") or "INFO"),
                    ),
                }
            )
    # ヒット一覧
    return hits


def _rewrite_file_masking(
    path: Path,
    targets: Dict[int, str],
    *,
    by_hash_only: bool,
) -> int:
    """1 ファイルを書き換え、マスクした行数を返す。"""
    # 無ければ 0
    if not path.is_file():
        return 0
    # 読込
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        # 全行保持（改行付き）
        lines = fh.readlines()
    # 変更フラグ
    changed = False
    # マスク件数
    masked = 0
    # ハッシュ集合（.txt 用）
    wanted_hashes: Set[str] = set(targets.values())
    # 行ごとに処理
    for idx, raw in enumerate(lines):
        # 1 始まり行番号
        line_no = idx + 1
        # 本文ハッシュ
        digest = _content_hash(raw)
        # .log: 行番号とハッシュの両方一致
        if not by_hash_only:
            # 対象外
            if line_no not in targets:
                continue
            # ハッシュ不一致（書き換え済み等）
            if targets[line_no] != digest:
                continue
        # .txt: ハッシュ一致のみ
        else:
            # 対象ハッシュでなければスキップ
            if digest not in wanted_hashes:
                continue
        # 既にマスク済みならスキップ
        if _REDACTED_BODY in raw and " - " in raw:
            # 本文がマスクだけなら再マスク不要
            if raw.rstrip("\r\n").endswith(_REDACTED_BODY):
                continue
        # マスク行へ置換
        lines[idx] = _mask_line_text(raw)
        # 変更あり
        changed = True
        # 件数
        masked += 1
    # 変更無ければ終了
    if not changed:
        return 0
    # 同一パスを seek/truncate で書き戻す（開いた Handler と共存しやすい）
    with path.open("r+", encoding="utf-8", errors="replace", newline="") as fh:
        # 先頭へ
        fh.seek(0)
        # 全行書込
        fh.writelines(lines)
        # 残りを切る
        fh.truncate()
    # マスク件数
    return masked


def mask_log_lines(items: List[Dict[str, Any]]) -> Dict[str, Any]:
    """指定行を日時のみ残してマスクする。"""
    # 行番号 → 期待ハッシュ
    targets: Dict[int, str] = {}
    # リクエスト項目を正規化
    for item in items:
        # 辞書以外は無視
        if not isinstance(item, dict):
            continue
        try:
            # 行番号
            line_no = int(item["line_no"])
            # ハッシュ
            content_hash = str(item["content_hash"])
        except (KeyError, TypeError, ValueError):
            # 不正項目はスキップ
            continue
        # 登録
        targets[line_no] = content_hash
    # 空なら何もしない
    if not targets:
        return {"ok": True, "masked_log": 0, "masked_txt": 0}
    # ファイル書き換えを直列化
    with _log_rewrite_lock:
        # 正本 .log は行番号+ハッシュ
        masked_log = _rewrite_file_masking(
            get_log_file_path(), targets, by_hash_only=False
        )
        # 姉妹 .txt は同一内容ハッシュでマスク
        masked_txt = _rewrite_file_masking(
            _log_txt_path(), targets, by_hash_only=True
        )
    # 結果
    return {
        "ok": True,
        "masked_log": masked_log,
        "masked_txt": masked_txt,
    }


def search_db_for_user(user_id: int) -> Dict[str, Any]:
    """DB 内のユーザー紐付けデータを返す。"""
    # 設定 DB
    settings_db = get_default_settings_db(DEFAULT_DB_PATH)
    # autojoin 行
    auto_join = settings_db.find_auto_join_by_user_id(int(user_id))
    # VC セッションストア
    vc_store = get_vc_playback_session_store()
    # requester 一致セッション
    vc_sessions = vc_store.find_by_requester_id(int(user_id))
    # まとめて返す
    return {
        "auto_join": auto_join,
        "vc_sessions": vc_sessions,
    }


def search_user_data(user_id: int) -> Dict[str, Any]:
    """ログと DB を横断検索する。"""
    # 整数化
    uid = int(user_id)
    # 結合結果
    return {
        "user_id": uid,
        "logs": search_logs_for_user(uid),
        "db": search_db_for_user(uid),
    }


def delete_db_user_data(
    user_id: int,
    *,
    delete_all: bool = False,
    auto_join: Optional[List[Dict[str, Any]]] = None,
    vc_sessions: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """DB 上のユーザー紐付けを削除する。"""
    # 対象ユーザー
    uid = int(user_id)
    # 設定 DB
    settings_db = get_default_settings_db(DEFAULT_DB_PATH)
    # VC ストア
    vc_store = get_vc_playback_session_store()
    # 全削除
    if delete_all:
        # autojoin 全ギルド
        deleted_auto = settings_db.delete_auto_join_by_user_id(uid)
        # VC の requester 除去
        scrubbed = vc_store.scrub_requester_id(uid)
        # 結果
        return {
            "ok": True,
            "deleted_auto_join": deleted_auto,
            "scrubbed_vc": scrubbed,
        }
    # 個別 autojoin
    deleted_auto = 0
    # 指定があれば削除
    if auto_join:
        # guild_id 集合
        guild_ids: List[str] = []
        # 各ターゲット
        for row in auto_join:
            # 辞書以外は無視
            if not isinstance(row, dict):
                continue
            # guild_id 必須
            if "guild_id" not in row:
                continue
            # 文字列化して積む
            guild_ids.append(str(row["guild_id"]))
        # 指定ギルドだけ削除
        deleted_auto = settings_db.delete_auto_join_by_user_id(
            uid, guild_ids=guild_ids
        )
    # 個別 VC
    scrubbed: Dict[str, Any] = {"updated": 0, "deleted_sessions": 0, "removed_tracks": 0}
    # 指定セッションキー
    if vc_sessions:
        # (bot_id, guild_id) の組
        keys: List[Tuple[str, int]] = []
        # 各ターゲット
        for row in vc_sessions:
            # 辞書以外は無視
            if not isinstance(row, dict):
                continue
            try:
                # キー組み立て
                keys.append((str(row["bot_id"]), int(row["guild_id"])))
            except (KeyError, TypeError, ValueError):
                # 不正はスキップ
                continue
        # 指定セッションだけ scrub
        scrubbed = vc_store.scrub_requester_id(uid, session_keys=keys)
    # 結果
    return {
        "ok": True,
        "deleted_auto_join": deleted_auto,
        "scrubbed_vc": scrubbed,
    }
