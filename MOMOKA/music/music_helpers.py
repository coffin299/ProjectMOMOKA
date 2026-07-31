"""音楽機能で共有する表示用ヘルパーを定義する。"""

from typing import Optional


def format_duration(duration_seconds: Optional[int]) -> str:
    """秒単位の再生時間を Discord 表示用に整形する。"""
    # 未設定または無効な時間は代替表示にする。
    if duration_seconds is None or duration_seconds < 0:
        # 不明な再生時間を返す。
        return "N/A"
    # 時・分・秒へ分解する。
    hours, remainder = divmod(duration_seconds, 3600)
    # 残り秒数を分・秒へ分解する。
    minutes, seconds = divmod(remainder, 60)
    # 時間がある場合だけ時を含む形式を返す。
    if hours > 0:
        # 時・分・秒形式で返す。
        return f"{int(hours):02}:{int(minutes):02}:{int(seconds):02}"
    # 分・秒形式で返す。
    return f"{int(minutes):02}:{int(seconds):02}"
