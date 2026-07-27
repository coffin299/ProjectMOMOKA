# 通常チャットの独立並列背圧（待たずに即拒否）。
from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


class ChatLimiter:
    """通常 LLM 応答用。グローバル枠 + チャンネル単位枠（待たない）。"""

    def __init__(self, max_inflight: int = 64, max_per_channel: int = 1) -> None:
        # プロセス全体の同時通常応答上限
        self.max_inflight = max(1, int(max_inflight))
        # 同一 channel の同時通常応答上限
        self._max_per_channel = max(1, int(max_per_channel))
        # 現在のグローバル in-flight
        self._global_count = 0
        # channel キー（bot_id:channel_id）-> in-flight 数
        self._per_channel: Dict[str, int] = {}
        # カウンタ更新用ロック（取得判定は短いクリティカルセクションのみ）
        self._lock = asyncio.Lock()

    async def try_acquire(self, channel_id: int, bot_id: str = "") -> bool:
        """枠が空いていれば即取得。待たない。失敗時 False。

        per-channel キーは bot_id+channel（Bot ごとに独立）。
        """
        # Bot 単位でチャンネル枠を分ける
        key = f"{bot_id or '_'}:{channel_id}"
        # 排他で空きを判定して予約する
        async with self._lock:
            # グローバル上限
            if self._global_count >= self.max_inflight:
                return False
            # チャンネル上限（Bot ごと）
            current = self._per_channel.get(key, 0)
            if current >= self._max_per_channel:
                return False
            # 予約
            self._global_count += 1
            self._per_channel[key] = current + 1
            return True

    async def release(self, channel_id: int, bot_id: str = "") -> None:
        """取得済み枠を解放する。"""
        # Bot 単位キー
        key = f"{bot_id or '_'}:{channel_id}"
        # 排他でカウンタを戻す
        async with self._lock:
            # グローバル
            if self._global_count > 0:
                self._global_count -= 1
            # チャンネル
            left = self._per_channel.get(key, 1) - 1
            if left <= 0:
                self._per_channel.pop(key, None)
            else:
                self._per_channel[key] = left


# プロセス共通インスタンス（init_concurrency で再初期化）
chat_limiter: Optional[ChatLimiter] = None


def init_concurrency(config: Dict[str, Any]) -> None:
    """config から Chat リミッタを初期化する。"""
    global chat_limiter
    # llm 直下またはトップレベル concurrency
    llm_cfg = config.get("llm") or {}
    conc = llm_cfg.get("concurrency") or config.get("concurrency") or {}
    max_chat = int(conc.get("max_inflight_chat", 64))
    max_per_ch = int(conc.get("max_inflight_per_channel", 1))
    # インスタンス生成
    chat_limiter = ChatLimiter(max_inflight=max_chat, max_per_channel=max_per_ch)
    # ログ
    logger.info(
        "Concurrency init: chat_inflight=%s per_channel=%s",
        max_chat,
        max_per_ch,
    )
