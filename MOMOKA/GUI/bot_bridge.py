# GUI スレッドから参照する Bot インスタンスの橋渡し

from typing import Any, Optional

# 起動前は None。main が PLANA 生成後に set_bot_ref する
_bot_ref: Optional[Any] = None


def set_bot_ref(bot: Any) -> None:
    """GUI から参照する Bot インスタンスを登録する。"""
    # モジュールグローバルへ書き込む
    global _bot_ref
    # 呼び出し側（通常は PLANA）を保持する
    _bot_ref = bot


def get_bot_ref() -> Optional[Any]:
    """登録済み Bot を返す（未登録時は None）。"""
    # 現在の参照をそのまま返す
    return _bot_ref


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


def aggregate_server_count() -> Optional[int]:
    """全 Bot が参加しているユニークなギルド数を返す。"""
    # 循環 import を避けるため関数内 import
    from MOMOKA.bots.registry import registry

    # 同一サーバーに両 Bot がいても 1 と数える
    guild_ids: set[int] = set()
    # 接続済み Bot を 1 件でも見たか
    found = False
    # 登録済み Bot を走査する
    for _, bot, _ in registry.iter_entries():
        # 未接続 Bot はスキップする
        if bot.is_closed():
            continue
        # この Bot は集計対象になる
        found = True
        # 参加ギルド ID を集める
        for guild in bot.guilds:
            guild_ids.add(int(guild.id))
    # Bot 未準備なら None（GUI は "-" 表示）
    return len(guild_ids) if found else None
