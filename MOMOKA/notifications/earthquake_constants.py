"""地震・津波通知で共有する定数と震度表示ヘルパー。"""

from __future__ import annotations

from enum import Enum

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
    """情報タイプの定義。"""

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


def scale_to_japanese(scale_code: int | None) -> str:
    """配信用の震度コードを日本語表示へ変換する。"""
    # None / -1 は配信文脈で「情報なし」と出す
    if scale_code is None or scale_code == -1:
        return "震度情報なし"
    # 設定 UI と同じラベル表を使う
    return NOTIFY_SCALE_LABELS.get(scale_code, f"不明({scale_code})")


def map_marker_color_and_size(scale: int, *, multi: bool = False):
    """地図マーカーの色・サイズ（複数マップ時はラベルも）を返す。"""
    # 震度帯ごとの色を決定する
    if scale >= 70:
        color = "#8B0000"
    elif scale >= 60:
        color = "#DC143C"
    elif scale >= 55:
        color = "#FF0000"
    elif scale >= 50:
        color = "#FF4500"
    elif scale >= 45:
        color = "#FF8C00"
    elif scale >= 40:
        color = "#FFA500"
    elif scale >= 30:
        color = "#FFD700"
    elif multi and scale >= 20:
        color = "#90EE90"
    else:
        color = "#87CEEB"
    # 単一震源マップは大きめマーカーを返す
    if not multi:
        if scale >= 70:
            size = 550
        elif scale >= 60:
            size = 500
        elif scale >= 55:
            size = 450
        elif scale >= 50:
            size = 400
        elif scale >= 45:
            size = 350
        elif scale >= 40:
            size = 300
        elif scale >= 30:
            size = 250
        else:
            size = 200
        return color, size
    # 複数震源マップは小さめサイズと凡例ラベルを返す
    if scale >= 70:
        size, label = 350, "震度7"
    elif scale >= 60:
        size, label = 300, "震度6強"
    elif scale >= 55:
        size, label = 250, "震度6弱"
    elif scale >= 50:
        size, label = 200, "震度5強"
    elif scale >= 45:
        size, label = 150, "震度5弱"
    elif scale >= 40:
        size, label = 120, "震度4"
    elif scale >= 30:
        size, label = 100, "震度3"
    elif scale >= 20:
        size, label = 80, "震度2"
    else:
        size, label = 60, "震度1"
    return color, size, label
