# GUI / ホスト API スレッドから参照する Bot 状態の橋渡し

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

# 起動前は None。main が PLANA 生成後に set_bot_ref する
_bot_ref: Optional[Any] = None
# ホスト GUI 用の ready 時刻（epoch 秒）
_ready_at: Optional[float] = None


def set_bot_ref(bot: Any) -> None:
    """GUI から参照する Bot インスタンスを登録する。"""
    # モジュールグローバルへ書き込む
    global _bot_ref, _ready_at
    # 呼び出し側（通常は PLANA）を保持する
    _bot_ref = bot
    # 未記録なら ready 相当の時刻を刻む
    if _ready_at is None:
        _ready_at = time.time()


def get_bot_ref() -> Optional[Any]:
    """登録済み Bot を返す（未登録時は None）。"""
    # 現在の参照をそのまま返す
    return _bot_ref


def mark_ready_at(ts: Optional[float] = None) -> None:
    """uptime 起算時刻を明示設定する。"""
    # グローバルを更新する
    global _ready_at
    # 未指定なら現在時刻
    _ready_at = time.time() if ts is None else float(ts)


def get_ready_at() -> Optional[float]:
    """ready 起算の epoch 秒。未設定なら None。"""
    # 保持値を返す
    return _ready_at


def aggregate_cog_metric(cog_name: str, method_name: str) -> Optional[int]:
    """全 Bot（PLANA + ARONA）の Cog メトリクスを合算する。"""
    # 循環 import を避けるため関数内 import
    from MOMOKA.bots.registry import registry

    # 合算値
    total = 0
    # 1 件でも取得できたか
    found = False
    # 登録済み Bot を走査する
    for _, bot, _ in registry.iter_entries():
        # 未接続 Bot はスキップする
        if bot.is_closed():
            continue
        # 対象 Cog を取得する
        cog = bot.get_cog(cog_name)
        # メソッドが無ければ次へ
        if cog is None or not hasattr(cog, method_name):
            continue
        # メトリクスを加算する
        total += int(getattr(cog, method_name)())
        found = True
    # 1 件も取れなければ None（GUI は "-" 表示）
    return total if found else None


def plana_server_count() -> Optional[int]:
    """PLANA 単体の参加ギルド数を返す。"""
    # 循環 import を避けるため関数内 import
    from MOMOKA.bots.registry import registry

    # プライマリ Bot のみを対象にする
    bot = registry.get("plana")
    # 未登録または切断済みなら未準備扱い
    if bot is None or bot.is_closed():
        return None
    # PLANA の参加ギルド数を返す
    return len(bot.guilds)


def get_guild_list() -> List[Dict[str, Any]]:
    """PLANA 参加ギルドの id / name のみ返す（メンバー取得なし）。"""
    # 循環 import 回避
    from MOMOKA.bots.registry import registry

    # プライマリのみ
    bot = registry.get("plana")
    # 未準備
    if bot is None or bot.is_closed():
        return []
    # name と id だけ列挙する
    return [{"id": str(g.id), "name": g.name} for g in bot.guilds]


def get_active_vc_snapshots() -> List[Dict[str, Any]]:
    """全 Bot の Active VC スナップショットを合算する。"""
    # 循環 import 回避
    from MOMOKA.bots.registry import registry
    from MOMOKA.music.music_cog import MusicCog

    # 結果リスト
    rows: List[Dict[str, Any]] = []
    # 各 Bot を走査する
    for bot_id, bot, display in registry.iter_entries():
        # 切断済みはスキップ
        if bot.is_closed():
            continue
        # Music Cog を取る
        cog = bot.get_cog(MusicCog.COG_NAME)
        # スナップショット API が無ければ次へ
        if cog is None or not hasattr(cog, "get_active_vc_snapshots"):
            continue
        # ギルド単位行を取り出す
        for row in cog.get_active_vc_snapshots():
            # どの Bot 由来かを付与する
            item = dict(row)
            item["bot_id"] = bot_id
            item["bot_display"] = display
            rows.append(item)
    # 合算結果
    return rows


def get_llm_average_seconds() -> Optional[float]:
    """全 Bot の LLM 平均応答秒を合算平均する。"""
    # 循環 import 回避
    from MOMOKA.bots.registry import registry
    from MOMOKA.llm.llm_cog import LLMCog

    # 各 Bot の平均値
    samples: List[float] = []
    # 登録 Bot を走査
    for _, bot, _ in registry.iter_entries():
        # 切断済みスキップ
        if bot.is_closed():
            continue
        # LLM Cog
        cog = bot.get_cog(LLMCog.COG_NAME)
        # getter が無ければ次
        if cog is None or not hasattr(cog, "get_average_response_seconds"):
            continue
        # 平均を取る
        avg = cog.get_average_response_seconds()
        # 数値なら集める
        if isinstance(avg, (int, float)):
            samples.append(float(avg))
    # サンプル無し
    if not samples:
        return None
    # 単純平均
    return sum(samples) / len(samples)


def get_bot_alive() -> Dict[str, bool]:
    """bot_id → 生存（未 close）の辞書。"""
    # 循環 import 回避
    from MOMOKA.bots.registry import registry

    # 結果
    alive: Dict[str, bool] = {}
    # 登録を走査
    for bot_id, bot, _ in registry.iter_entries():
        # close されていなければ生存
        alive[bot_id] = not bot.is_closed()
    # 返す
    return alive


def get_gateway_ping_ms() -> Optional[float]:
    """PLANA の gateway latency（ms）。未接続時 None。"""
    # 循環 import 回避
    from MOMOKA.bots.registry import registry

    # プライマリ
    bot = registry.get("plana")
    # 未準備
    if bot is None or bot.is_closed():
        return None
    # latency は秒
    try:
        # ms に変換
        return float(bot.latency) * 1000.0
    except Exception:
        # 取得失敗
        return None


def get_uptime_seconds() -> Optional[float]:
    """ready からの経過秒。未設定なら None。"""
    # 起算が無ければ不明
    if _ready_at is None:
        return None
    # 経過を返す
    return max(0.0, time.time() - _ready_at)


def build_status_payload() -> Dict[str, Any]:
    """ホスト GUI 用 status JSON。"""
    # 遅延 import（循環回避）
    from MOMOKA.GUI.version import VERSION
    from MOMOKA.llm.llm_cog import LLMCog
    from MOMOKA.music.music_cog import MusicCog

    # ペイロードを組み立てる
    return {
        "servers": plana_server_count(),
        "vc": aggregate_cog_metric(MusicCog.COG_NAME, "get_active_vc_guild_count"),
        "llm": aggregate_cog_metric(LLMCog.COG_NAME, "get_active_llm_guild_count"),
        "ping_ms": get_gateway_ping_ms(),
        "uptime_seconds": get_uptime_seconds(),
        "alive": get_bot_alive(),
        "version": VERSION,
    }


def request_shutdown() -> bool:
    """全 Bot を閉じるコルーチンをスケジュールする。成功で True。"""
    # 循環 import 回避
    import asyncio

    from MOMOKA.bots.registry import registry

    # PLANA のループを使う（無ければ失敗）
    bot = registry.get("plana") or get_bot_ref()
    # Bot が無ければ失敗
    if bot is None or bot.is_closed():
        return False
    # ループを取る
    try:
        # discord.py の loop
        loop = bot.loop
    except Exception:
        # 失敗
        return False
    # close_all をスレッドセーフに投げる
    asyncio.run_coroutine_threadsafe(registry.close_all(), loop)
    # 受け付けた
    return True
