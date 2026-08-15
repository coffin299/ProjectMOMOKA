# GUI / ホスト API スレッドから参照する Bot 状態の橋渡し

from __future__ import annotations

import asyncio
import time
from concurrent.futures import Future
from typing import Any, Callable, Dict, List, Optional, TypeVar

# 起動前は None。main が PLANA 生成後に set_bot_ref する
_bot_ref: Optional[Any] = None
# ホスト GUI 用の ready 時刻（epoch 秒）
_ready_at: Optional[float] = None
# Cog 名は文字列定数（llm_cog / music_cog の import 循環を避ける）
_MUSIC_COG_NAME = "music_cog"
_LLM_COG_NAME = "llm"

# Bot ループ上で実行する同期コールの戻り値型
T = TypeVar("T")


def _call_on_bot_loop(fn: Callable[[], T], *, timeout: float = 2.0) -> T:
    """API スレッドから Bot ループへ同期スナップショットを依頼する。

    discord.py の内部状態を別スレッドから直接触らない（M-16）。
    """
    # 循環 import 回避
    from MOMOKA.bots.registry import registry

    # プライマリ Bot（無ければ登録済み参照）
    bot = registry.get("plana") or get_bot_ref()
    # Bot 未準備ならそのまま（起動直後の空応答）
    if bot is None:
        return fn()
    # イベントループを取る
    try:
        loop = bot.loop
    except Exception:
        return fn()
    # ループ未稼働なら直接実行
    try:
        if loop is None or not loop.is_running():
            return fn()
    except Exception:
        return fn()
    # 既に Bot ループ上ならデッドロック回避で直接実行
    try:
        if asyncio.get_running_loop() is loop:
            return fn()
    except RuntimeError:
        # 同期スレッド（FastAPI ワーカー）からの呼び出し
        pass
    # 結果受け渡し
    result_fut: Future = Future()

    def _run() -> None:
        """Bot ループスレッドで fn を実行する。"""
        try:
            result_fut.set_result(fn())
        except Exception as exc:
            result_fut.set_exception(exc)

    # ループへスケジュール
    loop.call_soon_threadsafe(_run)
    # タイムアウト付きで待つ
    return result_fut.result(timeout=timeout)


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


def purge_user_runtime(user_id: int) -> Dict[str, Any]:
    """稼働中 Bot のメモリ上ユーザー痕跡を可能な範囲で消す。"""
    # 結果
    result: Dict[str, Any] = {"tts": 0, "music": 0}
    # 登録 Bot
    bot = get_bot_ref()
    # 未登録なら終了
    if bot is None:
        return result
    try:
        from MOMOKA.bots.registry import registry
    except Exception:
        registry = None
    # 対象 bot 一覧
    bots = []
    if registry is not None:
        try:
            bots = list(registry.all_bots())
        except Exception:
            bots = [bot]
    else:
        bots = [bot]
    uid = int(user_id)
    for target in bots:
        # TTS
        tts = target.get_cog("tts_cog") or target.get_cog("TTSCog")
        if tts is not None and hasattr(tts, "purge_user_runtime"):
            try:
                result["tts"] += int(tts.purge_user_runtime(uid) or 0)
            except Exception:
                pass
        # Music
        music = target.get_cog(_MUSIC_COG_NAME)
        if music is not None and hasattr(music, "purge_user_runtime"):
            try:
                result["music"] += int(music.purge_user_runtime(uid) or 0)
            except Exception:
                pass
    return result


def get_ready_at() -> Optional[float]:
    """ready 起算の epoch 秒。未設定なら None。"""
    # 保持値を返す
    return _ready_at


def _aggregate_cog_metric_unlocked(cog_name: str, method_name: str) -> Optional[int]:
    """Bot ループ上で Cog メトリクスを合算する（内部用）。"""
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


def aggregate_cog_metric(cog_name: str, method_name: str) -> Optional[int]:
    """全 Bot（PLANA + ARONA）の Cog メトリクスを合算する。"""
    try:
        # Bot ループ上でスナップショット
        return _call_on_bot_loop(
            lambda: _aggregate_cog_metric_unlocked(cog_name, method_name)
        )
    except Exception:
        # タイムアウト等は未準備扱い
        return None


def _plana_server_count_unlocked() -> Optional[int]:
    """Bot ループ上で PLANA ギルド数を返す（内部用）。"""
    # 循環 import を避けるため関数内 import
    from MOMOKA.bots.registry import registry

    # プライマリ Bot のみを対象にする
    bot = registry.get("plana")
    # 未登録または切断済みなら未準備扱い
    if bot is None or bot.is_closed():
        return None
    # PLANA の参加ギルド数を返す
    return len(bot.guilds)


def plana_server_count() -> Optional[int]:
    """PLANA 単体の参加ギルド数を返す。"""
    try:
        return _call_on_bot_loop(_plana_server_count_unlocked)
    except Exception:
        return None


def _get_guild_list_unlocked() -> List[Dict[str, Any]]:
    """Bot ループ上でギルド一覧を組み立てる（内部用）。"""
    # 循環 import 回避
    from MOMOKA.bots.registry import registry

    # プライマリのみ
    bot = registry.get("plana")
    # 未準備
    if bot is None or bot.is_closed():
        return []
    # ソート用に行を集める
    rows: List[Dict[str, Any]] = []
    # ギルドを走査（メンバー一覧は取らない）
    for g in bot.guilds:
        # Bot 自身の参加時刻（GUILD_CREATE 由来・members intent 不要）
        joined = None
        try:
            # me が取れれば joined_at を使う
            me = g.me
            if me is not None and me.joined_at is not None:
                joined = me.joined_at
        except Exception:
            # 取れなければ不明のまま
            joined = None
        # ISO 文字列（UTC）または None
        joined_iso = None
        if joined is not None:
            try:
                joined_iso = joined.isoformat()
            except Exception:
                joined_iso = None
        # 1 行分
        rows.append(
            {
                "id": str(g.id),
                "name": g.name,
                "joined_at": joined_iso,
                "_joined_at": joined,
            }
        )
    # 新しい参加が上（None は最後）
    rows.sort(
        key=lambda r: (
            r["_joined_at"] is None,
            -(r["_joined_at"].timestamp()) if r["_joined_at"] is not None else 0,
        )
    )
    # 内部キーを落として返す
    return [
        {"id": r["id"], "name": r["name"], "joined_at": r["joined_at"]}
        for r in rows
    ]


def get_guild_list() -> List[Dict[str, Any]]:
    """PLANA 参加ギルドの id / name / joined_at を返す（メンバー取得なし）。

    並びは Bot 参加日時の新しい順（上が最新）。joined_at 不明は末尾。
    """
    try:
        return _call_on_bot_loop(_get_guild_list_unlocked)
    except Exception:
        return []


def _get_active_vc_snapshots_unlocked() -> List[Dict[str, Any]]:
    """Bot ループ上で Active VC を合算する（内部用）。"""
    # 循環 import 回避（Cog クラスは import しない）
    from MOMOKA.bots.registry import registry

    # 結果リスト
    rows: List[Dict[str, Any]] = []
    # 重複キー
    seen: set = set()
    # 各 Bot を走査する
    for bot_id, bot, display in registry.iter_entries():
        # 切断済みはスキップ
        if bot.is_closed():
            continue
        # Music Cog を取る
        cog = bot.get_cog(_MUSIC_COG_NAME)
        # スナップショット API が無ければ次へ
        if cog is None or not hasattr(cog, "get_active_vc_snapshots"):
            continue
        # 表示ラベル（plana→PLANA）
        label = (display or bot_id or "").upper()
        if bot_id == "plana":
            label = "PLANA"
        elif bot_id == "arona":
            label = "ARONA"
        # ギルド単位行を取り出す
        for row in cog.get_active_vc_snapshots():
            # ギルド ID
            gid = row.get("guild_id")
            # 重複キー
            key = (bot_id, gid)
            # 同一 Bot×ギルドは1件だけ
            if key in seen:
                continue
            # 記録
            seen.add(key)
            # どの Bot 由来かを付与する
            item = dict(row)
            item["bot_id"] = bot_id
            item["bot_display"] = display
            item["bot_label"] = label
            rows.append(item)
    # Bot 名 → ギルド名で見やすく並べる
    rows.sort(
        key=lambda r: (
            str(r.get("bot_label") or ""),
            str(r.get("guild_name") or ""),
        )
    )
    # 合算結果
    return rows


def get_active_vc_snapshots() -> List[Dict[str, Any]]:
    """全 Bot の Active VC スナップショットを合算する。

    (bot_id, guild_id) で重複除去。bot_label に PLANA/ARONA を付与。
    """
    try:
        return _call_on_bot_loop(_get_active_vc_snapshots_unlocked)
    except Exception:
        return []


def _get_llm_average_seconds_unlocked() -> Optional[float]:
    """Bot ループ上で LLM 平均秒を取る（内部用）。"""
    # 循環 import 回避（Cog クラスは import しない）
    from MOMOKA.bots.registry import registry

    # 各 Bot の平均値
    samples: List[float] = []
    # 登録 Bot を走査
    for _, bot, _ in registry.iter_entries():
        # 切断済みスキップ
        if bot.is_closed():
            continue
        # LLM Cog
        cog = bot.get_cog(_LLM_COG_NAME)
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


def get_llm_average_seconds() -> Optional[float]:
    """全 Bot の LLM 平均応答秒を合算平均する。"""
    try:
        return _call_on_bot_loop(_get_llm_average_seconds_unlocked)
    except Exception:
        return None


def _get_bot_alive_unlocked() -> Dict[str, bool]:
    """Bot ループ上で生存辞書を取る（内部用）。"""
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


def get_bot_alive() -> Dict[str, bool]:
    """bot_id → 生存（未 close）の辞書。"""
    try:
        return _call_on_bot_loop(_get_bot_alive_unlocked)
    except Exception:
        return {}


def _get_gateway_ping_ms_unlocked() -> Optional[float]:
    """Bot ループ上で gateway latency を取る（内部用）。"""
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


def get_gateway_ping_ms() -> Optional[float]:
    """PLANA の gateway latency（ms）。未接続時 None。"""
    try:
        return _call_on_bot_loop(_get_gateway_ping_ms_unlocked)
    except Exception:
        return None


def get_uptime_seconds() -> Optional[float]:
    """ready からの経過秒。未設定なら None。"""
    # 起算が無ければ不明
    if _ready_at is None:
        return None
    # 経過を返す
    return max(0.0, time.time() - _ready_at)


def _build_status_payload_unlocked() -> Dict[str, Any]:
    """Bot ループ上で status JSON を一括構築する（内部用）。"""
    # バージョンのみ軽量 import（Cog クラスは import しない）
    from MOMOKA.GUI.version import VERSION

    # 同一スナップショット内でメトリクスを取る
    return {
        "servers": _plana_server_count_unlocked(),
        "vc": _aggregate_cog_metric_unlocked(
            _MUSIC_COG_NAME, "get_active_vc_guild_count"
        ),
        "llm": _aggregate_cog_metric_unlocked(
            _LLM_COG_NAME, "get_active_llm_guild_count"
        ),
        "ping_ms": _get_gateway_ping_ms_unlocked(),
        "uptime_seconds": get_uptime_seconds(),
        "alive": _get_bot_alive_unlocked(),
        "version": VERSION,
    }


def build_status_payload() -> Dict[str, Any]:
    """ホスト GUI 用 status JSON。"""
    try:
        # 1 回のループ hop でまとめて読む
        return _call_on_bot_loop(_build_status_payload_unlocked)
    except Exception:
        # 失敗時は空に近いペイロード
        from MOMOKA.GUI.version import VERSION

        return {
            "servers": None,
            "vc": None,
            "llm": None,
            "ping_ms": None,
            "uptime_seconds": get_uptime_seconds(),
            "alive": {},
            "version": VERSION,
        }


def request_shutdown() -> bool:
    """全 Bot を閉じるコルーチンをスケジュールする。成功で True。"""
    # 循環 import 回避
    import asyncio
    import threading

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

    # Electron を遅延終了（HTTP 応答後・コンソール占有解除）
    def _stop_gui_later() -> None:
        # runner の stop を呼ぶ
        try:
            from MOMOKA.GUI.runner import stop_host_gui

            # GUI プロセスツリーを落とす
            stop_host_gui()
        except Exception:
            # 失敗してもシャットダウンは継続
            pass

    # 0.8 秒後に GUI 終了（Shutdown API の応答を先に返す）
    threading.Timer(0.8, _stop_gui_later).start()
    # 受け付けた
    return True
