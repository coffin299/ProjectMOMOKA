# MOMOKA/utilities/channel_allowlist.py
# allowed_channel_ids 設定の共通判定。
from __future__ import annotations

from typing import Any, Collection, Optional, Sequence


def normalize_allowed_channel_ids(raw: Any) -> list[int]:
    """設定値を整数チャンネル ID のリストへ正規化する。"""
    # 未設定・空は全許可
    if raw is None:
        return []
    # 非シーケンスは全許可扱い
    if not isinstance(raw, (list, tuple, set)):
        return []
    # 結果
    ids: list[int] = []
    # 各要素を数値化
    for item in raw:
        # 文字列数字も許す
        try:
            # int 化
            ids.append(int(item))
        except (TypeError, ValueError):
            # 不正は無視
            continue
    # 返す
    return ids


def is_channel_allowed(
    channel_id: Optional[int],
    allowed_channel_ids: Any,
    *,
    allow_dm: bool = True,
    is_dm: bool = False,
) -> bool:
    """
    チャンネルが許可リストに含まれるか判定する。
    空リスト = 全チャンネル許可。
    DM は allow_dm に従う。
    """
    # DM は別方針
    if is_dm or channel_id is None:
        # DM 許可フラグ
        return bool(allow_dm)
    # 正規化
    allowed = normalize_allowed_channel_ids(allowed_channel_ids)
    # 空なら全許可
    if not allowed:
        return True
    # 含まれていれば許可
    return int(channel_id) in allowed
