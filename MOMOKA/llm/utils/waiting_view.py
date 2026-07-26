# LLM 応答待機中の Components V2 LayoutView。
from __future__ import annotations

from typing import Any, Dict, Optional

import discord

from MOMOKA.utilities.donation import donation_from_bot, make_subtle_link_button


class WaitingLayoutView(discord.ui.LayoutView):
    """⏳ Waiting for... 用 LayoutView（Tips + 控えめ Ko-fi）。"""

    def __init__(
        self,
        *,
        body: str,
        accent: discord.Color,
        bot: Any = None,
        tip_data: Optional[Dict[str, Any]] = None,
        model_name: Optional[str] = None,
        lang: str = "en",
        timeout: Optional[float] = 300.0,
    ) -> None:
        # 待機は短命なので適当なタイムアウト
        super().__init__(timeout=timeout)
        # 本文・色・Bot を保持する
        self.body = body
        self.accent = accent
        self.bot = bot
        # フォールバック時に tip を差し替えないよう保持する
        self.tip_data = tip_data
        # 現在試行中のモデル表示名
        self.model_name = model_name
        # UI 言語（再描画時に維持する）
        self.lang = "ja" if lang == "ja" else "en"
        # UI を組み立てる
        self._rebuild()

    def update_body(
        self,
        body: str,
        *,
        accent: Optional[discord.Color] = None,
        model_name: Optional[str] = None,
        tip_data: Optional[Dict[str, Any]] = None,
    ) -> None:
        """本文を差し替えて LayoutView を再構築する（モデル切替表示用）。"""
        # 新しい待機本文を保持する
        self.body = body
        # アクセント色が渡されたときだけ更新する
        if accent is not None:
            self.accent = accent
        # 試行中モデル名が渡されたときだけ更新する
        if model_name is not None:
            self.model_name = model_name
        # tip 再利用用データが渡されたときだけ更新する
        if tip_data is not None:
            self.tip_data = tip_data
        # 子コンポーネントを組み直す
        self._rebuild()

    def _rebuild(self) -> None:
        """TextDisplay + 任意の寄付ボタンを載せる。"""
        # 既存を消す
        self.clear_items()
        # コンテナ
        container = discord.ui.Container(accent_color=self.accent)
        # 待機本文
        container.add_item(discord.ui.TextDisplay(self.body))
        # 控えめ寄付（enabled 時のみ）
        if self.bot is not None:
            btn = make_subtle_link_button(donation_from_bot(self.bot))
            if btn is not None:
                row = discord.ui.ActionRow()
                row.add_item(btn)
                container.add_item(row)
        # ルートへ
        self.add_item(container)
