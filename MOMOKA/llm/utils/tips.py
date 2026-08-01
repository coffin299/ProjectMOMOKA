# MOMOKA/llm/utils/tips.py
import logging
import random
from collections import defaultdict, deque
from typing import List, Dict, Any, Optional

import discord

from MOMOKA.storage import NS_RESPONSE_TIMES, get_default_settings_db
from MOMOKA.utilities.locale import pick_str
from MOMOKA.utilities.support_config import SupportLinks, load_support_links

# ロガー設定
logger = logging.getLogger(__name__)

# ローリング平均に使用する直近のサンプル数
MAX_SAMPLES = 20


class ResponseTimeTracker:
    """モデルごとの応答時間をローリング平均で追跡するクラス"""

    def __init__(self, max_samples: int = MAX_SAMPLES, settings_db=None):
        # SettingsDB（未指定ならプロセス共通）
        self.settings_db = settings_db or get_default_settings_db()
        # ローリング平均に使うサンプル数上限
        self.max_samples = max_samples
        # モデル名 → 応答時間(秒)のdeque
        self._times: Dict[str, deque] = defaultdict(
            lambda: deque(maxlen=self.max_samples)
        )
        # 永続化ストアからデータを復元
        self._load()

    # ------------------------------------------------------------------
    # 永続化: ロード / セーブ
    # ------------------------------------------------------------------
    def _load(self) -> None:
        """SettingsDB から過去の記録を復元する"""
        try:
            # namespace から取る
            raw = self.settings_db.load(NS_RESPONSE_TIMES)
            # 無ければ何もしない
            if raw is None:
                logger.info("応答時間データが DB に未作成")
                return
            # dict でなければ無視
            if not isinstance(raw, dict):
                return
            # JSON → deque へ変換
            for model, times in raw.items():
                self._times[model] = deque(
                    times[-self.max_samples:], maxlen=self.max_samples
                )
            logger.info(
                "応答時間データを復元: %d モデル分", len(self._times)
            )
        except Exception as e:
            logger.warning("応答時間データの読込に失敗: %s", e)

    def _save(self) -> None:
        """現在の記録を SettingsDB に保存する"""
        try:
            # deque → list へ変換して保存
            payload = {
                model: list(times)
                for model, times in self._times.items()
            }
            self.settings_db.save(NS_RESPONSE_TIMES, payload)
        except Exception as e:
            logger.warning("応答時間データの保存に失敗: %s", e)

    # ------------------------------------------------------------------
    # 記録 / 取得
    # ------------------------------------------------------------------
    def record(self, model_name: str, elapsed_seconds: float) -> None:
        """応答完了後に呼び出し、応答にかかった秒数を記録する"""
        # 極端に短い/長い値はフィルタ（0.5秒未満 or 10分超は除外）
        if elapsed_seconds < 0.5 or elapsed_seconds > 600:
            return
        self._times[model_name].append(elapsed_seconds)
        # 記録のたびに永続化
        self._save()
        logger.debug(
            "応答時間を記録: %s = %.1f秒 (サンプル数: %d)",
            model_name, elapsed_seconds, len(self._times[model_name])
        )

    def _find_times(self, model_name: str) -> Optional[deque]:
        """記録キーと表示キーの表記ゆれを吸収してサンプル列を返す。

        記録は provider/model（例: nvidia_nim/z-ai/glm-5.2）、
        待機 UI は API 短名（例: z-ai/glm-5.2）になりがちなので両方を辿る。
        """
        # 空文字は照合しない
        if not model_name:
            return None
        # 完全一致を最優先する
        exact = self._times.get(model_name)
        # 完全一致かつサンプルありならそれを使う
        if exact:
            return exact
        # 別名候補（接尾辞一致）を集める
        matches: List[deque] = []
        # 保存済みキーを走査する
        for key, times in self._times.items():
            # 空の deque はスキップする
            if not times:
                continue
            # 記録キーが lookup の親パス付き（provider/short）か
            if key.endswith("/" + model_name):
                matches.append(times)
                continue
            # lookup が記録キーの親パス付き（逆方向）か
            if model_name.endswith("/" + key):
                matches.append(times)
        # 別名が無ければ見つからず
        if not matches:
            return None
        # サンプルが多い方を採用する（より信頼できる平均）
        return max(matches, key=len)

    def get_estimate(self, model_name: str) -> Optional[float]:
        """モデルの予想応答時間(秒)を返す。データ不足時は None"""
        # 表記ゆれを吸収してサンプルを取る
        times = self._find_times(model_name)
        # 最低3サンプル無いと予想を出さない
        if not times or len(times) < 3:
            return None
        # ローリング平均を算出
        return sum(times) / len(times)

    def get_overall_average(self) -> Optional[float]:
        """全モデル・全サンプルの単純平均秒。無ければ None。"""
        # 全サンプルを集める
        all_times: List[float] = []
        # モデルごとに伸ばす
        for times in self._times.values():
            # deque を list 化して追加
            all_times.extend(times)
        # サンプル無し
        if not all_times:
            return None
        # 単純平均
        return sum(all_times) / len(all_times)

    def format_estimate(self, model_name: str, *, lang: str = "en") -> str:
        """予想時間を人間向け文字列にフォーマットする"""
        # 予想秒数を取る
        estimate = self.get_estimate(model_name)
        # データ不足
        if estimate is None:
            return pick_str(
                lang,
                ja="⏱️ 予想応答時間: *計測中...*",
                en="⏱️ Estimated time: *Measuring...*",
            )
        # 60秒未満は秒表示
        if estimate < 60:
            return pick_str(
                lang,
                ja=f"⏱️ 予想応答時間: ~**{estimate:.0f}秒**",
                en=f"⏱️ Estimated: ~**{estimate:.0f}s**",
            )
        # 60秒以上は分+秒表示
        minutes = int(estimate // 60)
        seconds = int(estimate % 60)
        return pick_str(
            lang,
            ja=f"⏱️ 予想応答時間: ~**{minutes}分{seconds}秒**",
            en=f"⏱️ Estimated: ~**{minutes}m{seconds}s**",
        )


class TipsManager:
    """LLM待機中に表示するランダムなtipsを管理するクラス"""

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        # tips リストを構築する
        self.tips = self._create_tips_list()
        # 応答時間トラッカーを内蔵
        self.response_tracker = ResponseTimeTracker()
        # サポートリンク（docs / developer 連絡先）を config から保持する
        self._support: SupportLinks = load_support_links(config)

    def _docs_footer_text(self, lang: str) -> str:
        """障害案内フッター（docs_url があるときだけ URL を付ける）。"""
        # docs URL
        docs = self._support.docs_url
        # URL 付き／無しで文言を変える
        if docs:
            # 日本語
            return pick_str(
                lang,
                ja=(
                    "メインサーバーで技術的な問題が発生しています。\n"
                    f"ドキュメント: {docs}"
                ),
                en=(
                    "we are experiencing technical difficulties with our main server.\n"
                    f"full documentation : {docs}"
                ),
            )
        # URL 無し
        return pick_str(
            lang,
            ja="メインサーバーで技術的な問題が発生しています。",
            en="we are experiencing technical difficulties with our main server.",
        )

    def _contact_footer_text(self, lang: str) -> str:
        """GPU 寄付募集フッター（プロフィール URL があるときだけ連絡先を付ける）。"""
        # プロフィール URL
        contact = self._support.developer_profile_url
        # 連絡先がある場合
        if contact:
            # 日本語＋英語
            return pick_str(
                lang,
                ja=(
                    "-# メインサーバーのGPUを寄付してくれる方を募集しています...\n"
                    f"-# コンタクト: {contact}"
                ),
                en=(
                    "-# Looking for GPU donations for our main server...\n"
                    f"-# Contact: {contact}"
                ),
            )
        # 連絡先無し
        return pick_str(
            lang,
            ja="-# メインサーバーのGPUを寄付してくれる方を募集しています...",
            en="-# Looking for GPU donations for our main server...",
        )

    def _create_tips_list(self) -> List[Dict[str, Any]]:
        """tipsのリストを作成する（日英分離）。"""
        return [
            {
                "title_ja": "💡 AIのヒント",
                "title_en": "💡 AI Tips",
                "description_ja": (
                    "**画像を送信できます！**\n"
                    "画像URLを貼り付けるか、画像ファイルを添付してAIに説明を求めることができます。"
                ),
                "description_en": (
                    "**You can send images!**\n"
                    "Paste image URLs or attach image files to ask the AI for descriptions."
                ),
                "color": discord.Color.blue(),
            },
            {
                "title_ja": "💡 AIのヒント",
                "title_en": "💡 AI Tips",
                "description_ja": (
                    "**会話を続けるには返信機能を！**\n"
                    "Botのメッセージに返信することで、メンションなしで会話を続けられます。"
                ),
                "description_en": (
                    "**Use reply to continue conversations!**\n"
                    "Reply to bot messages to continue chatting without mentioning."
                ),
                "color": discord.Color.green(),
            },
            {
                "title_ja": "💡 AIのヒント",
                "title_en": "💡 AI Tips",
                "description_ja": (
                    "**モデルを切り替えられます！**\n"
                    "`/switch-models`コマンドでこのチャンネルのAIモデルを変更できます。"
                ),
                "description_en": (
                    "**You can switch models!**\n"
                    "Use `/switch-models` command to change the AI model for this channel."
                ),
                "color": discord.Color.orange(),
            },
            {
                "title_ja": "💡 AIのヒント",
                "title_en": "💡 AI Tips",
                "description_ja": (
                    "**画像生成も可能！**\n"
                    "AIに画像生成を依頼すると、StableDiffusionが画像を作成します。"
                ),
                "description_en": (
                    "**Image generation available!**\n"
                    "Ask the AI to generate images and it will use StableDiffusion."
                ),
                "color": discord.Color.gold(),
            },
            {
                "title_ja": "💡 AIのヒント",
                "title_en": "💡 AI Tips",
                "description_ja": (
                    "**検索機能を利用！**\n"
                    "AIに最新情報を調べてもらうことができます。リアルタイムの情報取得が可能です。"
                ),
                "description_en": (
                    "**Use search functionality!**\n"
                    "Ask the AI to search for the latest information. Real-time info is available."
                ),
                "color": discord.Color.red(),
            },
        ]

    def _tip_title(self, tip_data: Dict[str, Any], lang: str) -> str:
        """tip のタイトルを言語別に返す。"""
        # 新形式（分離キー）を優先する
        if "title_ja" in tip_data or "title_en" in tip_data:
            return pick_str(
                lang,
                ja=str(tip_data.get("title_ja") or tip_data.get("title_en") or ""),
                en=str(tip_data.get("title_en") or tip_data.get("title_ja") or ""),
            )
        # 旧形式フォールバック
        return str(tip_data.get("title") or "")

    def _tip_description(self, tip_data: Dict[str, Any], lang: str) -> str:
        """tip の本文を言語別に返す。"""
        # 新形式（分離キー）を優先する
        if "description_ja" in tip_data or "description_en" in tip_data:
            return pick_str(
                lang,
                ja=str(
                    tip_data.get("description_ja")
                    or tip_data.get("description_en")
                    or ""
                ),
                en=str(
                    tip_data.get("description_en")
                    or tip_data.get("description_ja")
                    or ""
                ),
            )
        # 旧形式フォールバック
        return str(tip_data.get("description") or "")

    def get_random_tip(self, *, lang: str = "en") -> discord.Embed:
        """ランダムなtipのembedを取得する"""
        tip_data = random.choice(self.tips)
        embed = discord.Embed(
            title=self._tip_title(tip_data, lang),
            description=self._tip_description(tip_data, lang),
            color=tip_data["color"],
        )
        embed.set_footer(
            text=self._docs_footer_text(lang)
        )
        return embed

    # 応答時間がこの秒数以上ならモデル切替の提案を表示する閾値
    SLOW_MODEL_THRESHOLD = 30

    def get_waiting_embed(self, model_name: str, *, lang: str = "en") -> discord.Embed:
        """待機中の embed（後方互換。新規は get_waiting_layout を使う）。"""
        tip_embed = self.get_random_tip(lang=lang)
        # タイトル: モデル名の応答待ち表示
        tip_embed.title = pick_str(
            lang,
            ja=f"### ⏳ '{model_name}' の応答を待っています...",
            en=f"### ⏳ Waiting for '{model_name}' response...",
        )
        # 予想応答時間をdescriptionの先頭に挿入
        time_estimate = self.response_tracker.format_estimate(model_name, lang=lang)
        # 予想時間が閾値を超える場合、モデル切替の提案を追加
        estimate = self.response_tracker.get_estimate(model_name)
        switch_hint = ""
        if estimate is not None and estimate >= self.SLOW_MODEL_THRESHOLD:
            switch_hint = pick_str(
                lang,
                ja="\n💡 応答が遅い場合は `/switch-models` で他のモデルへの切り替えもご検討ください。",
                en="\n💡 If response is slow, consider switching models with `/switch-models`.",
            )
        original_desc = tip_embed.description or ""
        # 「予想時間 → 切替提案 → 空行 → tips本文」の構成
        tip_embed.description = f"{time_estimate}{switch_hint}\n\n{original_desc}"
        return tip_embed

    def get_waiting_layout_parts(
        self,
        model_name: str,
        *,
        tip_data: Optional[Dict[str, Any]] = None,
        fallback_from: Optional[str] = None,
        lang: str = "en",
        include_model: bool = True,
        estimate_model: Optional[str] = None,
    ) -> tuple[str, discord.Color, Dict[str, Any]]:
        """待機 LayoutView 用の本文・アクセント色・使用 tip を返す。

        tip_data を渡すと同一 tip を維持したままモデル名だけ差し替えできる。
        fallback_from があるときのみクォータ起因のモデル切替案内を付ける。
        include_model=False のときは tip のみ（router 振り分け中向け）。
        estimate_model は記録キー（provider/model）。省略時は model_name で照合する。
        """
        # tip 未指定ならランダムに1つ選ぶ（再利用時は呼び出し側で渡す）
        if tip_data is None:
            tip_data = random.choice(self.tips)
        # tip 文言を言語別に取る
        tip_title = self._tip_title(tip_data, lang)
        tip_desc = self._tip_description(tip_data, lang)
        # フッター文言（GPU 寄付募集・連絡先は config 由来）
        footer = self._contact_footer_text(lang)
        # router 振り分け中はモデル名・予想時間を出さない
        if not include_model:
            body = (
                f"**{tip_title}**\n"
                f"{tip_desc}\n\n"
                f"{footer}"
            )
            accent = tip_data.get("color") or discord.Color.orange()
            return body, accent, tip_data
        # 予想時間の照合キー（記録時の provider/model を優先）
        tracker_key = estimate_model or model_name
        # 予想時間文字列（試行中モデル基準）
        time_estimate = self.response_tracker.format_estimate(tracker_key, lang=lang)
        # 遅いモデルなら切替提案
        estimate = self.response_tracker.get_estimate(tracker_key)
        switch_hint = ""
        if estimate is not None and estimate >= self.SLOW_MODEL_THRESHOLD:
            switch_hint = pick_str(
                lang,
                ja="\n💡 応答が遅い場合は `/switch-models` で他のモデルへの切り替えもご検討ください。",
                en="\n💡 If response is slow, consider switching models with `/switch-models`.",
            )
        # 別モデルへのフォールバック時のみ案内を付ける（同一モデルのキー回転では付けない）
        fallback_notice = ""
        if fallback_from:
            fallback_notice = pick_str(
                lang,
                ja=(
                    f"\n⚠️ `{fallback_from}` のクォータ/API制限のため、"
                    f"`{model_name}` に切り替え中..."
                ),
                en=(
                    f"\n⚠️ Switching to `{model_name}` because "
                    f"`{fallback_from}` hit quota/API limits..."
                ),
            )
        # 見出し
        heading = pick_str(
            lang,
            ja=f"### ⏳ '{model_name}' の応答を待っています...",
            en=f"### ⏳ Waiting for '{model_name}' response...",
        )
        # V2 TextDisplay 用本文
        body = (
            f"{heading}\n"
            f"{time_estimate}{switch_hint}{fallback_notice}\n\n"
            f"**{tip_title}**\n"
            f"{tip_desc}\n\n"
            f"{footer}"
        )
        # tip の色をアクセントに使う
        accent = tip_data.get("color") or discord.Color.orange()
        return body, accent, tip_data
