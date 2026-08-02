# MOMOKA/llm/llm_cog.py
from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import os
import re
import time
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace
from typing import (
    List,
    Dict,
    Any,
    Tuple,
    Optional,
    AsyncGenerator,
    Union,
    Callable,
    Awaitable,
)

import aiohttp
import discord
import httpx
import openai
from discord import app_commands
from discord.ext import commands

from MOMOKA.llm.error.errors import (
    LLMExceptionHandler,
    SearchAgentError,
    SearchAPIRateLimitError,
    SearchAPIServerError
)
from MOMOKA.llm.plugins import (
    SearchAgent,
    ImageGenerator
)
from MOMOKA.llm.plugins.feedback_tool import FeedbackTool
from MOMOKA.llm.concurrency import chat_limiter
from MOMOKA.llm.llm_views import ThreadCreationView
from MOMOKA.llm.message_utils import split_message_smartly
from MOMOKA.llm.utils.waiting_view import WaitingLayoutView
from MOMOKA.llm.router.mode_runner import (
    coding_attach_settings,
    coding_output_prompt,
    execute_command_baton,
    format_coding_attachment_note,
    image_gen_disabled_message,
    mode_model_chain,
    run_routed_response,
    should_attach_coding_output,
    split_coding_response,
    unsupported_message,
    waiting_phase_title,
)
from MOMOKA.llm.router.json_extract import extract_completion_text
from MOMOKA.utilities.locale import resolve_guild_lang, resolve_interaction_lang
from MOMOKA.storage import NS_CHANNEL_LLM_MODELS, resolve_settings_db
from MOMOKA.utilities.feedback import (
    create_support_report_view,
    support_footer_text,
)

try:
    from MOMOKA.llm.utils.tips import TipsManager
except ImportError:
    logging.error("Could not import TipsManager. Tips functionality will be disabled.")
    TipsManager = None

logger = logging.getLogger(__name__)

# LLM API 呼び出しでキー／モデル切替リトライ対象とする一時的ネットワークエラー
_TRANSIENT_LLM_NETWORK_ERRORS: tuple = (
    openai.APIConnectionError,
    openai.APITimeoutError,
    httpx.RemoteProtocolError,
    httpx.ReadError,
    httpx.ConnectError,
    httpx.WriteError,
    httpx.PoolTimeout,
)
# ストリーム確立〜受信の同一試行内リトライ回数（接続切断などの瞬断向け）
_LLM_STREAM_ATTEMPT_RETRIES = 2
# 一時的ネットワークエラー後の待機秒数
_LLM_TRANSIENT_RETRY_SLEEP_SEC = 1.0

# Constants
SUPPORTED_IMAGE_EXTENSIONS = ('.png', '.jpeg', '.jpg', '.gif', '.webp')
IMAGE_URL_PATTERN = re.compile(
    r'https?://[^\s]+\.(?:' + '|'.join(ext.lstrip('.') for ext in SUPPORTED_IMAGE_EXTENSIONS) + r')(?:\?[^\s]*)?',
    re.IGNORECASE
)
DISCORD_MESSAGE_MAX_LENGTH = 2000
SAFE_MESSAGE_LENGTH = 1990  # 安全マージン
# チャンネル別モデル上書きの有効期限（秒）= 3時間
MODEL_OVERRIDE_TTL_SECONDS = 3 * 60 * 60
# /chat 応答末尾に付ける案内（Discord の -# サブテキスト）
CHAT_HISTORY_HINT = (
    "\n-# 💡 会話履歴は @メンション と LLM 応答へのリプライでのみ保存されます。"
    " 続きはメンションかリプライでどうぞ"
    " / History is saved only via @mention or reply to LLM responses."
)


class LLMCog(commands.Cog, name="llm"):
    """A cog for interacting with Large Language Models, with tool support."""

    # get_cog / GUI 合算用の正式名
    COG_NAME = "llm"

    def _add_support_footer(self, embed: discord.Embed) -> None:
        current_footer = embed.footer.text if embed.footer and embed.footer.text else ""
        # フォーム + GitHub 誘導文言を付ける
        support_text = "\n" + support_footer_text()
        if current_footer:
            embed.set_footer(text=current_footer + support_text)
        else:
            embed.set_footer(text=support_footer_text())

    def _create_support_view(self) -> discord.ui.View:
        # フィードバック Modal ボタン + GitHub リンクを返す
        return create_support_report_view(self.bot)

    async def _safe_reply(
        self,
        message: discord.Message,
        *,
        content: Optional[str] = None,
        view: Optional[discord.ui.View] = None,
        silent: bool = True,
    ) -> Optional[discord.Message]:
        """権限不足でも例外を外へ漏らさない返信ヘルパー。"""
        # 送信先チャンネルを取得する
        channel = message.channel
        # ギルド内テキスト／スレッドなら事前に Send Messages を確認する
        if isinstance(channel, (discord.TextChannel, discord.Thread)):
            # Bot メンバー情報を取る
            me = channel.guild.me if channel.guild else None
            # 権限オブジェクトが取れる場合のみチェックする
            if me is not None:
                # チャンネル権限を評価する
                perms = channel.permissions_for(me)
                # 送信不可なら早期リターンする
                if not perms.send_messages:
                    # 想定内のため WARNING に留める
                    logger.warning(
                        "[%s] Skip reply: missing Send Messages in channel %s",
                        self._bot_tag(),
                        channel.id,
                    )
                    # 送信失敗として None を返す
                    return None
        # reply / send 共通のキーワード引数を組み立てる
        send_kwargs: Dict[str, Any] = {"silent": silent}
        # content 指定があれば入れる
        if content is not None:
            # 本文をセットする
            send_kwargs["content"] = content
        # view 指定があれば入れる
        if view is not None:
            # View をセットする
            send_kwargs["view"] = view
        try:
            # まずリプライで送る
            return await message.reply(**send_kwargs)
        except discord.Forbidden:
            # 権限不足は ERROR 連打にせず警告のみ
            logger.warning(
                "[%s] Forbidden when replying in channel %s",
                self._bot_tag(),
                getattr(channel, "id", "?"),
            )
            # 送信失敗
            return None
        except discord.HTTPException:
            # リプライ不可時は通常送信へフォールバックする
            try:
                # チャンネルへ直接送る
                return await channel.send(**send_kwargs)
            except (discord.Forbidden, discord.HTTPException) as send_err:
                # どちらも失敗したら警告のみで終了する
                logger.warning(
                    "[%s] Cannot send LLM message in channel %s: %s",
                    self._bot_tag(),
                    getattr(channel, "id", "?"),
                    send_err,
                )
                # 送信失敗
                return None

    def _create_waiting_view(
        self,
        model_name: str,
        *,
        lang: str = "en",
        phase: str = "conversation",
        estimate_model: Optional[str] = None,
    ) -> WaitingLayoutView:
        """応答待機中の Components V2（Tips + 控えめ Ko-fi）。"""
        # UI 言語を正規化する（zh-CN 等も非 ja として en 側へ）
        ui_lang = "ja" if str(lang).startswith("ja") else "en"
        # tip 再利用用（モデル切替時に同じ tip を維持する）
        tip_data: Optional[Dict[str, Any]] = None
        # フェーズ見出しを先頭に置く
        phase_line = waiting_phase_title(phase, lang)
        # router 中はモデル名を出さない
        show_model = phase != "router"
        # 応答時間トラッカー用キー（provider/model を優先）
        tracker_key = estimate_model or model_name
        # Tips が無ければ簡易本文
        if self.tips_manager:
            tip_body, accent, tip_data = self.tips_manager.get_waiting_layout_parts(
                model_name,
                lang=ui_lang,
                include_model=show_model,
                estimate_model=tracker_key,
            )
            body = f"{phase_line}\n\n{tip_body}"
        else:
            body = phase_line
            accent = discord.Color.orange()
        # LayoutView を返す（試行中モデル名と tip・言語を保持）
        return WaitingLayoutView(
            body=body,
            accent=accent,
            bot=self.bot,
            tip_data=tip_data,
            model_name=model_name,
            lang=ui_lang,
        )

    def _is_image_generator_disabled(self) -> bool:
        """image_generator.enabled が false、またはツール未登録なら True。"""
        # トップレベル独立 config
        top = self.config.get("image_generator") or {}
        if isinstance(top, dict) and top.get("enabled") is False:
            return True
        # 旧パス
        legacy = (self.llm_config or {}).get("image_generator") or {}
        if isinstance(legacy, dict) and legacy.get("enabled") is False:
            return True
        # プラグイン未初期化
        if self.image_generator is None:
            return True
        # プラグイン側フラグ
        return not getattr(self.image_generator, "_enabled", True)

    def _looks_like_image_gen_request(self, text: str) -> bool:
        """描画・画像生成依頼っぽいかの簡易判定。"""
        t = (text or "").lower()
        keys = (
            "画像を生成", "絵を描", "画像生成", "generate an image", "draw me",
            "generate image", "txt2img", "イラストを", "絵描いて",
        )
        return any(k.lower() in t for k in keys)

    async def _refresh_waiting_view_for_model(
        self,
        sent_message: discord.Message,
        waiting_view: WaitingLayoutView,
        model_name: str,
        *,
        fallback_from: Optional[str] = None,
        phase: Optional[str] = None,
        estimate_model: Optional[str] = None,
    ) -> None:
        """待機 UI の試行中モデル名を差し替え、必要ならフォールバック案内を付ける。"""
        # 再利用する tip（無ければ新規抽選）
        tip_data = getattr(waiting_view, "tip_data", None)
        # View に保存した UI 言語を使う
        ui_lang = getattr(waiting_view, "lang", "en") or "en"
        # 応答時間トラッカー用キー（記録時の provider/model を優先）
        tracker_key = estimate_model or model_name
        # TipsManager があれば本文を再構築する
        if self.tips_manager:
            body, accent, tip_data = self.tips_manager.get_waiting_layout_parts(
                model_name,
                tip_data=tip_data,
                fallback_from=fallback_from,
                lang=ui_lang,
                include_model=True,
                estimate_model=tracker_key,
            )
        else:
            # Tips 無し時の簡易本文
            notice = ""
            if fallback_from:
                notice = (
                    f"\n⚠️ `{fallback_from}` のクォータ/API制限のため、`{model_name}` に切り替え中..."
                    if ui_lang == "ja"
                    else (
                        f"\n⚠️ Switching to `{model_name}` because "
                        f"`{fallback_from}` hit quota/API limits..."
                    )
                )
            body = (
                f"### ⏳ '{model_name}' の応答を待っています...{notice}"
                if ui_lang == "ja"
                else f"### ⏳ Waiting for '{model_name}' response...{notice}"
            )
            accent = waiting_view.accent
        # 振り分け完了後はフェーズ見出し（Streaming 等）を先頭に付ける
        if phase:
            body = f"{waiting_phase_title(phase, ui_lang)}\n\n{body}"
        # View 内部状態を更新して再構築する
        waiting_view.update_body(
            body,
            accent=accent,
            model_name=model_name,
            tip_data=tip_data,
        )
        try:
            # Discord 上の待機メッセージを edit する
            await sent_message.edit(view=waiting_view)
        except discord.HTTPException as e:
            # 編集失敗でもストリーム本体は続行する
            logger.debug("Failed to refresh waiting view for model fallback: %s", e)

    async def _refresh_waiting_view_for_phase_only(
        self,
        sent_message: discord.Message,
        waiting_view: WaitingLayoutView,
        *,
        phase: str,
    ) -> None:
        """振り分け後の command 等、モデル名なしでフェーズだけ更新する。"""
        # View に保存した UI 言語を使う
        ui_lang = getattr(waiting_view, "lang", "en") or "en"
        # フェーズ見出し
        phase_line = waiting_phase_title(phase, ui_lang)
        # tip を維持しつつモデル情報は出さない
        tip_data = getattr(waiting_view, "tip_data", None)
        if self.tips_manager:
            tip_body, accent, tip_data = self.tips_manager.get_waiting_layout_parts(
                "",
                tip_data=tip_data,
                lang=ui_lang,
                include_model=False,
            )
            body = f"{phase_line}\n\n{tip_body}"
        else:
            body = phase_line
            accent = waiting_view.accent
        waiting_view.update_body(body, accent=accent, tip_data=tip_data)
        try:
            await sent_message.edit(view=waiting_view)
        except discord.HTTPException as e:
            logger.debug("Failed to refresh waiting view for phase: %s", e)

    async def _append_chat_history_hint(
            self,
            message: discord.Message,
            channel: discord.abc.Messageable,
            base_content: str,
    ) -> None:
        """/chat 応答の末尾に会話履歴の注意書き（-# サブテキスト）を付ける。"""
        # DM では平文でも履歴が繋がるため案内は不要
        if getattr(channel, "guild", None) is None:
            return
        # message.content は followup/edit 後に空のことがあるため、呼び出し側の本文を使う
        # 案内文言を結合した最終テキストを組み立てる
        hinted_content = f"{base_content}{CHAT_HISTORY_HINT}"
        try:
            # 文字数制限内なら同一メッセージを編集して追記する
            if len(hinted_content) <= DISCORD_MESSAGE_MAX_LENGTH:
                await message.edit(content=hinted_content, embed=None, view=None)
            else:
                # 収まらない場合は案内だけ別メッセージで @silent 送信する
                await channel.send(CHAT_HISTORY_HINT.lstrip("\n"), silent=True)
        except discord.HTTPException as e:
            # 案内付与失敗は応答本体を壊さないので警告のみ残す
            logger.warning(f"Failed to append /chat history hint: {e}")

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        if not hasattr(self.bot, 'config') or not self.bot.config: raise commands.ExtensionFailed(self.qualified_name,
                                                                                                  "Bot config not loaded.")
        self.config = self.bot.config
        self.llm_config = self.config.get('llm')
        if not isinstance(self.llm_config, dict): raise commands.ExtensionFailed(self.qualified_name,
                                                                                 "The 'llm' section in config is missing or invalid.")
        # Bot 識別子（dual-bot）
        self.bot_id = getattr(self.bot, "bot_id", "plana")
        # persona キー（plana / arona）
        self.persona_key = getattr(self.bot, "persona_key", self.bot_id)
        # role: primary | companion
        self.bot_role = getattr(self.bot, "bot_role", "primary")
        # 表示名（ログタグ）
        self.display_name = getattr(self.bot, "display_name", self.bot_id.upper())
        self.language_prompt = self.llm_config.get('language_prompt')
        if self.language_prompt: logger.info("Language prompt loaded from config for fallback.")
        self.http_session, self.bot.cfg = aiohttp.ClientSession(), self.llm_config
        self.conversation_threads: Dict[int, Dict[int, List[Dict[str, Any]]]] = {}  # {guild_id: {thread_id: messages}}
        self.message_to_thread: Dict[int, Dict[int, int]] = {}  # {guild_id: {message_id: thread_id}}
        self.llm_clients: Dict[str, openai.AsyncOpenAI] = {}
        self.provider_api_keys: Dict[str, List[str]] = {}
        self.provider_key_index: Dict[str, int] = {}
        self.model_reset_tasks: Dict[int, asyncio.Task] = {}
        self.exception_handler = LLMExceptionHandler(self.llm_config)
        # チャンネル別モデルは SettingsDB の channel_llm_models
        self.settings_db = resolve_settings_db(bot)
        # {bot_id: {channel_id: {"model": str, "expires_at": float}}} 形式
        self.channel_models: Dict[str, Any] = self._load_channel_models_nested()
        logger.info(
            f"[{self.display_name}] Loaded channel model settings from SettingsDB "
            f"('{NS_CHANNEL_LLM_MODELS}')."
        )
        self.jst = timezone(timedelta(hours=+9))
        # 応答生成中メッセージ（message_id → Message）の追跡用辞書
        self._active_response_messages: Dict[int, discord.Message] = {}
        # 応答生成中ギルド数（GUI 稼働モニタ用・finally で必ず減算）
        self._active_llm_guild_counts: Dict[int, int] = {}
        # シャットダウン通知済みならストリーム編集を止めるためのフラグ
        self._shutting_down = False
        # プラグインの初期化（BioManager/MemoryManagerは削除済み）
        (
            self.search_agent,
            self.image_generator,
            self.tips_manager,
            self.feedback_tool,
        ) = self._initialize_plugins()
        # 旧 command_manager / debate は廃止（router command モードへ移管）
        self.command_manager = None
        self.debate_tool = None
        self.cross_check_tool = None
        # 直近のルート結果（coding 添付判定などに使用）
        self._last_route_mode: Optional[str] = None
        self._last_route_lang: Optional[str] = None
        # persona / llm デフォルト model を初期化
        default_model_string = self._persona_default_model()
        if default_model_string:
            main_llm_client = self._initialize_llm_client(default_model_string)
            if main_llm_client:
                self.llm_clients[default_model_string] = main_llm_client
                logger.info(f"[{self.display_name}] Default LLM client '{default_model_string}' initialized.")
            else:
                logger.error(f"[{self.display_name}] Failed to initialize main LLM client.")
        else:
            logger.error(f"[{self.display_name}] Default LLM model is not configured.")

    async def cog_load(self) -> None:
        """Cog ロード時に期限切れ掃除とリセットタイマー復元を行う。"""
        # 永続化された expires_at を見て復元する
        await self._restore_channel_model_resets()

    def _bot_tag(self) -> str:
        """ログ用 Bot タグ。"""
        # 表示名を返す
        return self.display_name

    def _persona_default_model(self) -> Optional[str]:
        """デフォルトモデルは llm.model。任意で personas.<id>.model があれば上書き。"""
        # personas 辞書を取る
        personas = self.llm_config.get("personas") or {}
        # 自 persona エントリ
        entry = personas.get(self.persona_key) or {}
        # 明示指定があるときだけ persona 別モデルを使う（通常は未設定）
        if entry.get("model"):
            return entry["model"]
        # 共通 llm.model（通常のデフォルト）
        return self.llm_config.get("model")

    def _active_tools_list(self) -> List[str]:
        """role に応じた active_tools。"""
        # companion は専用リストがあればそれを使う
        if self.bot_role == "companion":
            companion = self.llm_config.get("active_tools_companion")
            if isinstance(companion, list):
                return companion
        # それ以外は通常リスト
        return list(self.llm_config.get("active_tools") or [])

    def _bot_channel_map(self) -> Dict[str, Any]:
        """自 Bot 用の channel_id→上書き設定マップを返す。"""
        # bot_id キーが無ければ作る
        if self.bot_id not in self.channel_models or not isinstance(
            self.channel_models.get(self.bot_id), dict
        ):
            self.channel_models[self.bot_id] = {}
        # マップを返す
        return self.channel_models[self.bot_id]

    def _parse_channel_override(self, entry: Any) -> Tuple[Optional[str], Optional[float]]:
        """上書きエントリから (model, expires_at) を取り出す。"""
        # 旧形式: モデル文字列のみ
        if isinstance(entry, str):
            # 期限情報は無いので None
            return entry, None
        # 新形式: dict
        if isinstance(entry, dict):
            # モデル名を取り出す
            model = entry.get("model")
            # 期限（UNIX秒）を取り出す
            expires_raw = entry.get("expires_at")
            # モデルが文字列でなければ無効
            if not isinstance(model, str) or not model:
                return None, None
            # 期限を float へ正規化する
            expires_at: Optional[float]
            try:
                expires_at = float(expires_raw) if expires_raw is not None else None
            except (TypeError, ValueError):
                expires_at = None
            # 正規化結果を返す
            return model, expires_at
        # 想定外形式
        return None, None

    def _get_channel_override_model(self, channel_id: int) -> Optional[str]:
        """有効なチャンネル上書きモデルを返す（期限切れは無視）。"""
        # チャンネルキー文字列
        channel_id_str = str(channel_id)
        # Bot 用マップを取得する
        bot_map = self._bot_channel_map()
        # エントリが無ければ上書きなし
        if channel_id_str not in bot_map:
            return None
        # model / expires_at をパースする
        model, expires_at = self._parse_channel_override(bot_map[channel_id_str])
        # モデルが取れなければ無効
        if not model:
            # 壊れたエントリを除去する
            bot_map.pop(channel_id_str, None)
            return None
        # 期限切れならメモリ上から消し、可能なら JSON も更新する
        if expires_at is not None and expires_at <= time.time():
            # 期限切れエントリを削除する
            bot_map.pop(channel_id_str, None)
            # 実行中ループがあれば非同期保存する
            try:
                # 現在のイベントループを取得する
                loop = asyncio.get_running_loop()
                # 保存タスクを投げる（失敗しても読み取りは続行）
                loop.create_task(self._save_channel_models())
            except RuntimeError:
                # ループ無しならメモリ削除のみ
                pass
            # 上書き無しとして扱う
            return None
        # 有効な上書きモデルを返す
        return model

    def _set_channel_override(self, channel_id: int, model: str, expires_at: float) -> None:
        """チャンネル上書きを新形式で書き込む。"""
        # Bot 用マップへ dict 形式で保存する
        self._bot_channel_map()[str(channel_id)] = {
            "model": model,
            "expires_at": float(expires_at),
        }

    def _clear_channel_override(self, channel_id: int) -> bool:
        """チャンネル上書きを削除する。削除したら True。"""
        # 削除結果を返す
        return self._bot_channel_map().pop(str(channel_id), None) is not None

    def _cancel_model_reset_task(self, channel_id: int) -> None:
        """既存のリセットタイマーをキャンセルする。"""
        # タスクが無ければ何もしない
        task = self.model_reset_tasks.pop(channel_id, None)
        if task is None:
            return
        # 実行中ならキャンセルする
        task.cancel()

    async def _restore_channel_model_resets(self) -> None:
        """起動時: 期限切れ掃除・旧形式移行・残り時間の再スケジュール。"""
        # 現在時刻（UNIX秒）
        now = time.time()
        # 自 Bot のマップ
        bot_map = self._bot_channel_map()
        # デフォルトモデル
        default_model = self._persona_default_model()
        # JSON 書き換えが必要か
        changed = False
        # 再スケジュール対象 (channel_id, expires_at)
        to_schedule: List[Tuple[int, float]] = []

        # 全チャンネル上書きを走査する
        for channel_id_str, entry in list(bot_map.items()):
            # model / expires_at を取り出す
            model, expires_at = self._parse_channel_override(entry)
            # 無効エントリは削除する
            if not model:
                bot_map.pop(channel_id_str, None)
                changed = True
                continue
            # デフォルトと同じなら上書き不要なので削除する
            if default_model and model == default_model:
                bot_map.pop(channel_id_str, None)
                changed = True
                continue
            # 旧形式（文字列）や期限欠落は、アップグレード後に新たに 3 時間を付与する
            if expires_at is None:
                # 新しい期限を計算する
                expires_at = now + MODEL_OVERRIDE_TTL_SECONDS
                # 新形式へ書き換える
                bot_map[channel_id_str] = {"model": model, "expires_at": expires_at}
                changed = True
                logger.info(
                    f"[{self._bot_tag()}] Migrated channel {channel_id_str} override "
                    f"to expires_at format (TTL {MODEL_OVERRIDE_TTL_SECONDS}s)."
                )
            # 既に期限切れなら削除する
            if expires_at <= now:
                bot_map.pop(channel_id_str, None)
                changed = True
                logger.info(
                    f"[{self._bot_tag()}] Removed expired model override for channel {channel_id_str}."
                )
                continue
            # チャンネル ID を int 化してスケジュール対象へ入れる
            try:
                channel_id_int = int(channel_id_str)
            except ValueError:
                bot_map.pop(channel_id_str, None)
                changed = True
                continue
            # 残り時間でタイマーを張り直す対象へ追加する
            to_schedule.append((channel_id_int, expires_at))

        # 変更があれば JSON へ保存する
        if changed:
            await self._save_channel_models()
            logger.info(f"[{self._bot_tag()}] Channel model overrides cleaned/migrated and saved.")

        # 有効な上書きのリセットタイマーを復元する
        for channel_id_int, expires_at in to_schedule:
            # 既存があればキャンセルする
            self._cancel_model_reset_task(channel_id_int)
            # 残り時間で再スケジュールする
            task = asyncio.create_task(self._schedule_model_reset(channel_id_int, expires_at))
            # タスク辞書へ登録する
            self.model_reset_tasks[channel_id_int] = task
            # 残り秒をログする
            remaining = max(0.0, expires_at - now)
            logger.info(
                f"[{self._bot_tag()}] Restored model reset timer for channel {channel_id_int} "
                f"({remaining:.0f}s remaining)."
            )

    def _load_channel_models_nested(self) -> Dict[str, Any]:
        """channel_llm_models を {bot_id:{channel:override}} として読む。"""
        # DB から生データを読む
        raw = self._load_settings_dict(NS_CHANNEL_LLM_MODELS)
        # 空なら自 Bot 用空 dict
        if not raw:
            return {self.bot_id: {}}
        # ネスト形式をそのまま返す
        return raw

    def _initialize_plugins(self) -> Tuple[
        Optional[SearchAgent],
        Optional[ImageGenerator],
        Optional[TipsManager],
        Optional[FeedbackTool],
    ]:
        """プラグインの初期化と返却。"""
        plugins = {
            "SearchAgent": None,
            "ImageGenerator": None,
            "TipsManager": None,
            "FeedbackTool": None,
        }

        # TipsManagerの初期化（support / docs は bot.config から）
        if TipsManager:
            plugins["TipsManager"] = TipsManager(
                getattr(self.bot, "config", None)
            )

        # role 別ツール一覧
        active_tools = self._active_tools_list()
        if 'search' in active_tools:
            logger.info(f"[{self.display_name}] Initializing SearchAgent.")
            if SearchAgent:
                plugins["SearchAgent"] = SearchAgent(self.bot, self.llm_config)

        # companion には image_generator を載せない
        if 'image_generator' in active_tools and ImageGenerator and self.bot_role != "companion":
            plugins["ImageGenerator"] = ImageGenerator(self.bot)

        # フィードバック（PLANA / ARONA 両方）
        if 'feedback' in active_tools:
            plugins["FeedbackTool"] = FeedbackTool(self.bot)

        # 初期化状態のログ出力
        for name, instance in plugins.items():
            if instance:
                logger.info(f"[{self.display_name}] {name} initialized successfully.")
            else:
                logger.info(f"[{self.display_name}] {name} is not active or failed to initialize.")

        return (
            plugins["SearchAgent"],
            plugins["ImageGenerator"],
            plugins["TipsManager"],
            plugins["FeedbackTool"],
        )

    async def cog_unload(self):
        await self.http_session.close()
        for task in self.model_reset_tasks.values(): task.cancel()
        logger.info(f"Cancelled {len(self.model_reset_tasks)} pending model reset tasks.")
        if self.image_generator: await self.image_generator.close()
        logger.info("LLMCog's aiohttp session has been closed.")

    def _register_active_response(self, message: discord.Message) -> None:
        """応答生成中メッセージを追跡辞書へ登録する（再起動通知用）。"""
        # メッセージが無ければ何もしない
        if message is None:
            # 早期リターン
            return
        # message id をキーに Message オブジェクトを保存する
        self._active_response_messages[message.id] = message

    def _unregister_active_response(self, message: Optional[discord.Message]) -> None:
        """応答生成中メッセージの追跡を解除する（再起動通知用）。"""
        # メッセージが無ければ何もしない
        if message is None:
            # 早期リターン
            return
        # 辞書から該当 ID を取り除く（無ければ無視）
        self._active_response_messages.pop(message.id, None)

    def _begin_active_llm_guild(self, guild_id: Optional[int]) -> None:
        """LLM 応答生成開始時にギルド稼働カウントを加算する。"""
        # DM 等 guild 無しは GUI 対象外
        if guild_id is None:
            # 加算しない
            return
        # 同一ギルドの同時応答数をインクリメントする
        self._active_llm_guild_counts[guild_id] = (
            self._active_llm_guild_counts.get(guild_id, 0) + 1
        )

    def _end_active_llm_guild(self, guild_id: Optional[int]) -> None:
        """LLM 応答生成終了時にギルド稼働カウントを減算する。"""
        # DM 等 guild 無しはスキップ
        if guild_id is None:
            # 減算しない
            return
        # 現在の同時応答数を取得する
        count = self._active_llm_guild_counts.get(guild_id, 0)
        # 1 以下ならギルドエントリごと削除する
        if count <= 1:
            self._active_llm_guild_counts.pop(guild_id, None)
        else:
            # 複数同時応答中なら 1 だけ減らす
            self._active_llm_guild_counts[guild_id] = count - 1

    def get_average_response_seconds(self) -> Optional[float]:
        """GUI 用: 記録済み応答時間の全体平均秒。無ければ None。"""
        # tips が無効なら不明
        if not self.tips_manager:
            return None
        # トラッカーの全体平均
        return self.tips_manager.response_tracker.get_overall_average()

    def get_active_llm_guild_count(self) -> int:
        """応答生成中のユニークギルド数を返す（GUI 稼働モニタ用）。"""
        # 稼働中（count > 0）のギルド数を返す
        return len(self._active_llm_guild_counts)

    async def notify_admin_restart(self) -> None:
        """再起動前に、生成中の LLM 応答メッセージを再起動文言で上書きする。"""
        # config 由来の再起動文言を遅延インポートする
        from MOMOKA.utilities.restart_notice import restart_notice_from_config
        # bot.config から日英併記文言を組み立てる
        restart_notice = restart_notice_from_config(
            self.bot.config if hasattr(self.bot, "config") else None
        )
        # 以降のストリーム編集を抑止する
        self._shutting_down = True
        # 通知時点の対象一覧をスナップショットする
        active_messages = list(self._active_response_messages.values())
        # 追跡辞書を先に空にして二重編集を防ぐ
        self._active_response_messages.clear()
        # GUI 用ギルドカウントもリセットする
        self._active_llm_guild_counts.clear()
        # 各メッセージを再起動文言で上書きする
        for message in active_messages:
            try:
                # 待機 embed / ストリーム途中本文を再起動案内に差し替える
                await message.edit(content=restart_notice, embed=None, view=None)
            except Exception as e:
                # 1件失敗しても他メッセージの通知は続ける
                logger.warning(
                    "Failed to overwrite LLM response message %s on restart: %s",
                    getattr(message, "id", "?"),
                    e,
                )

    def _load_settings_dict(self, namespace: str) -> Dict[str, Any]:
        """SettingsDB から dict を読む（無ければ空）。"""
        try:
            # namespace の payload を取る
            raw = self.settings_db.load(namespace)
            # 無ければ空
            if raw is None:
                return {}
            # dict 以外は拒否
            if not isinstance(raw, dict):
                return {}
            # キーを文字列に揃える
            return {str(k): v for k, v in raw.items()}
        except Exception as e:  # noqa: BLE001
            logger.error(f"Failed to load settings namespace '{namespace}': {e}")
        return {}

    async def _save_channel_models(self) -> None:
        """チャンネル別モデル設定を SettingsDB へ保存する。"""
        try:
            # メモリ上の全設定を書く
            await self.settings_db.save_async(NS_CHANNEL_LLM_MODELS, self.channel_models)
        except Exception as e:  # noqa: BLE001
            logger.error(f"Failed to save settings namespace '{NS_CHANNEL_LLM_MODELS}': {e}")
            raise

    def _openai_client_kwargs(
        self,
        provider_name: str,
        provider_config: Dict[str, Any],
        api_key: str,
        base_url: Optional[str] = None,
    ) -> Dict[str, Any]:
        """AsyncOpenAI 生成用の共通 kwargs を組み立てる。"""
        # 呼び出し先 URL（未指定時は設定の base_url）
        resolved_base_url = base_url if base_url is not None else provider_config.get("base_url")
        # 必須引数を先に入れる
        kwargs: Dict[str, Any] = {
            "base_url": resolved_base_url,
            "api_key": api_key,
        }
        # KoboldCPP のみ timeout を明示する（他は SDK 既定）
        if provider_name.lower() == "koboldcpp":
            # 設定値または 300 秒を使う
            kwargs["timeout"] = provider_config.get("timeout", 300.0)
        # OpenRouter 推奨ヘッダ（ランキング・識別用）
        if provider_name.lower() == "openrouter":
            # Referer / アプリ名は設定値のみ使う（個人・サイトのコード既定なし）
            referer = str(provider_config.get("http_referer") or "").strip()
            # アプリ表示名（X-Title）
            title = str(provider_config.get("x_title") or "").strip()
            # どちらかあれば default_headers を付ける
            headers: Dict[str, str] = {}
            # Referer があるときだけ載せる
            if referer:
                # HTTP-Referer
                headers["HTTP-Referer"] = referer
            # Title があるときだけ載せる
            if title:
                # X-Title
                headers["X-Title"] = title
            # ヘッダが1つ以上あれば kwargs に入れる
            if headers:
                # default_headers として渡す
                kwargs["default_headers"] = headers
        # 組み立て結果を返す
        return kwargs

    def _is_google_gemini_model(self, model_string: Optional[str]) -> bool:
        """Google プロバイダー直結の Gemini のみ True（OpenRouter 経由は除外）。"""
        # 空や不正形式は対象外
        if not model_string or "/" not in model_string:
            return False
        # provider / 以降のモデル ID に分割する
        provider_name, model_name = model_string.split("/", 1)
        # Google API 向けアダプタは google プロバイダー限定
        return provider_name.lower() == "google" and "gemini" in model_name.lower()

    def _initialize_llm_client(self, model_string: Optional[str]) -> Optional[openai.AsyncOpenAI]:
        if not model_string or '/' not in model_string:
            logger.error(f"Invalid model format: '{model_string}'. Expected 'provider_name/model_name'.")
            return None
        try:
            provider_name, model_name = model_string.split('/', 1)
            provider_config = self.llm_config.get('providers', {}).get(provider_name)
            if not provider_config:
                logger.error(f"Configuration for LLM provider '{provider_name}' not found.")
                return None

            # OpenRouter 公式ルータ（例: openrouter/free）は API 上も openrouter/<id> が必要
            if provider_name.lower() == "openrouter" and "/" not in model_name:
                # free / auto など著者プレフィックス無しの場合に復元する
                if model_name.lower() in ("free", "auto"):
                    # API に渡す正式スラッグへ書き換える
                    model_name = f"openrouter/{model_name}"
                    # 補正内容をログに残す
                    logger.info(
                        f"🔧 [OpenRouter] Normalized model slug to '{model_name}' "
                        f"(from '{model_string}')."
                    )
            
            # KoboldCPP固有の処理
            is_koboldcpp = provider_name.lower() == 'koboldcpp'
            if is_koboldcpp:
                logger.info(f"🔧 [KoboldCPP] Detected KoboldCPP provider. Applying KoboldCPP-specific settings.")
            
            if provider_name not in self.provider_api_keys:
                api_keys, i = [], 1
                while True:
                    if provider_config.get(f'api_key{i}'):
                        api_keys.append(provider_config[f'api_key{i}']); i += 1
                    else:
                        break
                if not api_keys and provider_config.get('api_key'): api_keys.append(provider_config['api_key'])
                if not api_keys:
                    logger.info(
                        f"No API keys found for provider '{provider_name}'. Assuming local model or keyless API.")
                    # KoboldCPPの場合、ダミーキーを使用
                    if is_koboldcpp:
                        self.provider_api_keys[provider_name] = ["koboldcpp-dummy-key"]
                        logger.info(f"🔧 [KoboldCPP] Using dummy API key (KoboldCPP usually doesn't require authentication)")
                    else:
                        self.provider_api_keys[provider_name] = ["no-key-required"]
                else:
                    self.provider_api_keys[provider_name] = api_keys
                    logger.info(f"Loaded {len(api_keys)} API key(s) for provider '{provider_name}'.")
            self.provider_key_index.setdefault(provider_name, 0)
            key_list, current_key_index = self.provider_api_keys[provider_name], self.provider_key_index[provider_name]
            if current_key_index >= len(key_list): current_key_index = 0; self.provider_key_index[provider_name] = 0
            api_key_to_use = key_list[current_key_index]
            
            base_url = provider_config.get('base_url')
            if is_koboldcpp:
                # KoboldCPPのベースURLが正しい形式か確認
                if not base_url.endswith('/v1'):
                    if base_url.endswith('/'):
                        base_url = base_url.rstrip('/') + '/v1'
                    else:
                        base_url = base_url + '/v1'
                    logger.info(f"🔧 [KoboldCPP] Adjusted base_url to: {base_url}")
            
            # 共通 kwargs（OpenRouter ヘッダ含む）でクライアントを生成する
            client = openai.AsyncOpenAI(
                **self._openai_client_kwargs(
                    provider_name, provider_config, api_key_to_use, base_url=base_url
                )
            )
            client.model_name_for_api_calls, client.provider_name = model_name, provider_name
            # 設定上の model 文字列（例: openrouter/free）を保持する
            client.config_model_string = model_string
            # KoboldCPP固有のメタデータを設定
            if is_koboldcpp:
                client.supports_tools = provider_config.get('supports_tools', True)
                logger.info(f"🔧 [KoboldCPP] Initialized client with model '{model_name}'")
                logger.info(f"🔧 [KoboldCPP] Base URL: {base_url}")
                logger.info(f"🔧 [KoboldCPP] Tools support: {client.supports_tools}")
                logger.info(f"🔧 [KoboldCPP] Timeout: {provider_config.get('timeout', 300.0)}s")
            else:
                client.supports_tools = True  # 他のプロバイダーはデフォルトでTrue
            
            logger.info(
                f"[{self._bot_tag()}] Initialized LLM client for provider '{provider_name}' with model '{model_name}'.")
            return client
        except Exception as e:
            logger.error(f"Error initializing LLM client for '{model_string}': {e}", exc_info=True)
            return None

    def _resolve_model_string(self, channel_id: int) -> Optional[str]:
        """チャンネル上書き ＞（任意）persona.model ＞ llm.model。"""
        # 有効期限内のチャンネル上書きを確認する
        override = self._get_channel_override_model(channel_id)
        # 上書きがあればそれを返す
        if override:
            return override
        # persona / 共通デフォルト
        return self._persona_default_model()

    async def _get_llm_client_for_channel(self, channel_id: int) -> Optional[openai.AsyncOpenAI]:
        model_string = self._resolve_model_string(channel_id)
        if not model_string:
            logger.error("[%s] No default model is configured.", self._bot_tag())
            return None
        if model_string in self.llm_clients: return self.llm_clients[model_string]
        logger.info(f"[{self._bot_tag()}] Initializing LLM client '{model_string}' for channel {channel_id}")
        client = self._initialize_llm_client(model_string)
        if client: self.llm_clients[model_string] = client
        return client

    # 日本語 few-shot / キャラ設定より優先させる言語固定指示（config 非依存）
    _LANGUAGE_ENFORCEMENT_BLOCK = (
        "# Language Control (ABSOLUTE — overrides character examples)\n"
        "- Always reply in the exact same language as the user's LATEST message.\n"
        "- Dialogue examples are TONE/STYLE only. Never copy their language.\n"
        "- Do NOT default to Japanese. Use Japanese only if the latest user message is Japanese.\n"
        "- English → English. Thai → Thai. Other languages → that same language."
    )

    async def _prepare_system_prompt(self, channel_id: int, user_id: int, user_display_name: str) -> str:
        """persona system_prompt + tools_prompt + language を1本に結合する。"""
        # personas から自人格テンプレートを取る
        personas = self.llm_config.get("personas") or {}
        persona_entry = personas.get(self.persona_key) or {}
        system_prompt_template = persona_entry.get("system_prompt") or self.llm_config.get("system_prompt", "")

        # 日付は ISO 形式にし、システム側の日本語文字混入を避ける
        current_date_str = datetime.now(self.jst).strftime('%Y-%m-%d')
        # 現在時刻を JST で取得する（テンプレート置換用）
        current_time_str = datetime.now(self.jst).strftime('%H:%M')
        try:
            # テンプレート変数を置換する（未使用プレースホルダは空文字）
            system_prompt = system_prompt_template.format(
                current_date=current_date_str,
                current_time=current_time_str,
                available_commands=""
            )
        except (KeyError, ValueError) as e:
            # format 失敗時は警告だけ出して手動置換へフォールバックする
            logger.warning(f"Could not format system_prompt: {e}")
            # プレースホルダを個別に置換してプロンプトを組み立てる
            system_prompt = (
                system_prompt_template
                .replace('{current_date}', current_date_str)
                .replace('{current_time}', current_time_str)
                .replace('{available_commands}', '')
            )

        # role に応じた tools_prompt を末尾へ連結する
        if self.bot_role == "companion":
            tools_prompt = self.llm_config.get("tools_prompt_arona") or ""
        else:
            tools_prompt = self.llm_config.get("tools_prompt") or ""
        if tools_prompt and tools_prompt.strip():
            system_prompt = f"{system_prompt.rstrip()}\n\n{tools_prompt.strip()}"

        # 先頭に言語固定を置き、日本語例より先に制約を効かせる
        system_prompt = f"{self._LANGUAGE_ENFORCEMENT_BLOCK}\n\n{system_prompt.lstrip()}"

        # language_prompt があれば末尾にも結合し、few-shot より後で再強調する
        if self.language_prompt and self.language_prompt.strip():
            # config の言語指示を同一 system 末尾へ付ける
            system_prompt = f"{system_prompt.rstrip()}\n\n{self.language_prompt.strip()}"
            # 結合済みであることをログに残す
            logger.info("[%s] 🌐 [LANG] Merged language_prompt into single system message", self._bot_tag())

        # 末尾にもコード側の言語固定を重ね、最終指示として残す
        system_prompt = f"{system_prompt.rstrip()}\n\n{self._LANGUAGE_ENFORCEMENT_BLOCK}"

        # 最終プロンプト長をログに出す
        logger.info(f"[{self._bot_tag()}] 🔧 [SYSTEM] System prompt prepared ({len(system_prompt)} chars)")
        # 結合済みの単一システムプロンプトを返す
        return system_prompt

    def _redirect_to_plana_message(self, guild: Optional[discord.Guild] = None) -> str:
        """ARONA→PLANA 誘導文（在籍時メンション / 不在時 invite）。"""
        # 文言を config から取る
        redirect = self.config.get("redirect_to_plana") or {}
        ja = redirect.get("ja") or "その機能は PLANA 側で使えます。"
        en = redirect.get("en") or "That feature is available on PLANA."
        # PLANA invite
        bots = self.config.get("bots") or {}
        plana = bots.get("plana") or {}
        invite = plana.get("invite_url") or ""
        # 在籍確認
        mention = "PLANA"
        try:
            from MOMOKA.bots.registry import registry
            plana_uid = registry.user_id("plana")
            if guild is not None and plana_uid is not None:
                # 同期的にキャッシュを見る（詳細確認は呼び出し側で可）
                member = guild.get_member(plana_uid)
                if member is not None:
                    mention = f"<@{plana_uid}>"
                else:
                    mention = f"PLANA ({invite})" if invite else "PLANA"
            elif invite:
                mention = f"PLANA ({invite})"
        except Exception:
            pass
        return f"{ja}\n{en}\n→ {mention}"

    async def generate_plain(
        self,
        *,
        system: str,
        user_content: str,
        max_chars: int = 800,
    ) -> str:
        """tools なしの一括生成（短い要約・内部用）。"""
        # モデル解決（チャンネル無し → persona デフォルト）
        model_string = self._persona_default_model()
        if not model_string:
            return ""
        # クライアント取得
        if model_string in self.llm_clients:
            client = self.llm_clients[model_string]
        else:
            client = self._initialize_llm_client(model_string)
            if client:
                self.llm_clients[model_string] = client
        if not client:
            return ""
        # メッセージ組み立て
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user_content},
        ]
        # extra パラメータ
        extra = dict(self.llm_config.get("extra_api_parameters") or {})
        # max_tokens を控えめに
        extra.setdefault("max_tokens", min(1024, max_chars * 2))
        try:
            # 非ストリーム完了
            resp = await client.chat.completions.create(
                model=client.model_name_for_api_calls,
                messages=messages,
                stream=False,
                **{k: v for k, v in extra.items() if k != "stream"},
            )
            # 本文取り出し（message が null の応答にも対応）
            text = extract_completion_text(resp)
            # 文字数制限
            if len(text) > max_chars:
                text = text[: max_chars - 1] + "…"
            return text
        except Exception as e:
            logger.error("[%s] generate_plain failed: %s", self._bot_tag(), e, exc_info=True)
            return f"(generation error: {e})"

    def _format_user_text_for_api(self, timestamp: str, text: str, *, mirror_language: bool = False) -> str:
        """API 送信用のユーザー本文を組み立てる。

        mirror_language=True のときだけ、最新発話の言語追従リマインダを付与する。
        履歴側には付けず、文脈チェーンは role 履歴で維持する。
        """
        # 時刻プレフィックス付きの本文を先に作る
        body = f"{timestamp} {text}"
        # 最新ターン以外は言語リマインダを付けない
        if not mirror_language:
            # 履歴用はそのまま返す
            return body
        # 最新ユーザー発話の直後に言語追従を明示する（Mistral 対策）
        return (
            f"{body}\n\n"
            "[Language: Reply in the same language as the user message above. "
            "Do not switch to Japanese unless that message is Japanese.]"
        )

    def get_tools_definition(self) -> Optional[List[Dict[str, Any]]]:
        definitions = []
        active_tools = self._active_tools_list()

        logger.info(f"[{self._bot_tag()}] 🔍 [TOOLS] Active tools: {active_tools}")

        if 'search' in active_tools:
            if self.search_agent:
                definitions.append(self.search_agent.tool_spec)
            else:
                logger.warning(f"⚠️ [TOOLS] 'search' is in active_tools but search_agent is None")

        if 'image_generator' in active_tools:
            if self.image_generator:
                definitions.append(self.image_generator.tool_spec)
            else:
                logger.warning(f"⚠️ [TOOLS] 'image_generator' is in active_tools but image_generator is None")

        if 'feedback' in active_tools:
            if self.feedback_tool:
                definitions.append(self.feedback_tool.tool_spec)
            else:
                logger.warning(f"⚠️ [TOOLS] 'feedback' is in active_tools but feedback_tool is None")

        logger.info(f"[{self._bot_tag()}] 🔧 [TOOLS] Total tools: {len(definitions)}")

        return definitions or None

    async def _get_conversation_thread_id(self, message: discord.Message) -> int:
        guild_id = message.guild.id if message.guild else 0  # DMの場合は0
        
        # ギルド固有の辞書を初期化
        if guild_id not in self.message_to_thread:
            self.message_to_thread[guild_id] = {}
        
        if message.id in self.message_to_thread[guild_id]: 
            return self.message_to_thread[guild_id][message.id]

        # DM はチャンネル単位で1会話に固定する（平文でも履歴を繋ぐ）
        if message.guild is None:
            thread_id = message.channel.id
            self.message_to_thread[guild_id][message.id] = thread_id
            return thread_id
        
        current_msg, visited_ids = message, set()
        while current_msg.reference and current_msg.reference.message_id:
            if current_msg.id in visited_ids: break
            visited_ids.add(current_msg.id)
            try:
                parent_msg = current_msg.reference.resolved or await message.channel.fetch_message(
                    current_msg.reference.message_id)
                if parent_msg.author != self.bot.user: break
                current_msg = parent_msg
            except (discord.NotFound, discord.HTTPException):
                break
        thread_id = current_msg.id
        self.message_to_thread[guild_id][message.id] = thread_id
        return thread_id

    def _truncate_conversation_history(
            self, history: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """会話履歴を max_messages ターン（user+assistant）に切り詰める。"""
        # guild / DM 共通で同じ上限式を使う
        max_history_entries = self.llm_config.get('max_messages', 10) * 2
        if len(history) > max_history_entries:
            return history[-max_history_entries:]
        return history

    async def _collect_conversation_history(self, message: discord.Message) -> List[Dict[str, Any]]:
        guild_id = message.guild.id if message.guild else 0  # DMの場合は0
        
        # ギルド固有の会話履歴を初期化
        if guild_id not in self.conversation_threads:
            self.conversation_threads[guild_id] = {}

        # DM: reply チェーンではなくチャンネル単位の保存済み履歴を使う
        if message.guild is None:
            thread_id = message.channel.id
            stored = self.conversation_threads[guild_id].get(thread_id, [])
            history: List[Dict[str, Any]] = []
            for msg in stored:
                role = msg.get("role")
                # API には role / content のみ渡す（message_id 等は内部用）
                if role in ("user", "assistant") and "content" in msg:
                    history.append({"role": role, "content": msg["content"]})
            return self._truncate_conversation_history(history)
        
        history, current_msg, visited_ids = [], message, set()
        while current_msg.reference and current_msg.reference.message_id:
            if current_msg.reference.message_id in visited_ids: break
            visited_ids.add(current_msg.reference.message_id)
            try:
                parent_msg = current_msg.reference.resolved or await message.channel.fetch_message(
                    current_msg.reference.message_id)
                if isinstance(parent_msg, discord.DeletedReferencedMessage):
                    logger.debug(f"Encountered deleted referenced message in history collection.")
                    break
                if parent_msg.author != self.bot.user:
                    # 履歴ターンは親メッセージ自体の画像のみ（チェーン二重取り込みを防ぐ）
                    image_contents, text_content = await self._prepare_multimodal_content(
                        parent_msg, include_reply_chain=False
                    )
                    text_content = text_content.replace(f'<@!{self.bot.user.id}>', '').replace(f'<@{self.bot.user.id}>',
                                                                                               '').strip()
                    if text_content or image_contents:
                        user_content_parts = []
                        if text_content:
                            # 履歴の親メッセージにも言語リマインダは付けない
                            user_content_parts.append({
                                "type": "text",
                                "text": self._format_user_text_for_api(
                                    parent_msg.created_at.astimezone(self.jst).strftime('[%H:%M]'),
                                    text_content,
                                    mirror_language=False,
                                )
                            })
                        user_content_parts.extend(image_contents)
                        history.append({"role": "user", "content": user_content_parts})
                else:
                    thread_id = await self._get_conversation_thread_id(parent_msg)
                    if thread_id in self.conversation_threads[guild_id]:
                        for msg in self.conversation_threads[guild_id][thread_id]:
                            if msg.get("role") == "assistant" and msg.get("message_id") == parent_msg.id:
                                history.append({"role": "assistant", "content": msg["content"]})
                                break
                current_msg = parent_msg
            except (discord.NotFound, discord.HTTPException):
                break
        history.reverse()
        return self._truncate_conversation_history(history)

    async def _process_image_url(self, url: str) -> Optional[Dict[str, Any]]:
        # 画像バイト上限（未設定時は従来どおり 20 MiB）
        max_image_bytes = int(self.llm_config.get('max_image_bytes', 20 * 1024 * 1024))
        try:
            async with self.http_session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as response:
                if response.status != 200:
                    logger.warning(f"Failed to download image from {url} (Status: {response.status})")
                    return None
                # Content-Length があれば本文を読む前に拒否する
                content_length = response.headers.get('Content-Length')
                if content_length is not None:
                    try:
                        # ヘッダ値を整数化し上限と比較する
                        declared_size = int(content_length)
                        # 宣言サイズが上限超なら本文を一切読まない
                        if declared_size > max_image_bytes:
                            logger.warning(
                                f"Image too large (Content-Length {declared_size} bytes): {url}")
                            return None
                    except (TypeError, ValueError):
                        # 不正な Content-Length は無視してチャンク読込に進む
                        pass
                # チャンク単位で累積し、超過時点で中断する（全文 read による OOM 防止）
                chunks: List[bytes] = []
                # これまでに受け取ったバイト数
                total_size = 0
                # 64 KiB ずつ読み、上限を超えたら接続を捨てる
                async for chunk in response.content.iter_chunked(64 * 1024):
                    # 空チャンクはスキップする
                    if not chunk:
                        continue
                    # 累積サイズを更新する
                    total_size += len(chunk)
                    # 上限超過なら以降を読まずに破棄する
                    if total_size > max_image_bytes:
                        logger.warning(f"Image too large (>{max_image_bytes} bytes while streaming): {url}")
                        # 残レスポンスを読まず接続を閉じる
                        response.close()
                        return None
                    # 上限内のチャンクのみ保持する
                    chunks.append(chunk)
                # チャンクを結合して画像バイト列にする
                image_bytes = b''.join(chunks)
                # 結合後の保険チェック（チャンク境界の取りこぼし対策）
                if len(image_bytes) > max_image_bytes:
                    logger.warning(f"Image too large ({len(image_bytes)} bytes): {url}")
                    return None
                # レスポンスの Content-Type を取得する（パラメータ無し）
                mime_type = response.content_type
                # 画像以外の Content-Type が明示されている場合は推測せず破棄する
                if mime_type and not mime_type.startswith('image/'):
                    # HTML 等（share.google など）を画像として送らない
                    logger.warning(
                        f"URL did not return an image Content-Type ({mime_type}): {url}")
                    return None
                # Content-Type が無い場合のみ URL 拡張子から MIME を推測する
                if not mime_type:
                    # クエリを除き、最後のドット以降を拡張子として取り出す
                    path_without_query = url.split('?', 1)[0]
                    # 拡張子が無い URL ではパス全体が候補になるため小文字化のみ行う
                    ext = path_without_query.rsplit('.', 1)[-1].lower()
                    # 既知拡張子のみ採用し、不明なら JPEG にフォールバックする
                    mime_type = {
                        'png': 'image/png',
                        'jpg': 'image/jpeg',
                        'jpeg': 'image/jpeg',
                        'gif': 'image/gif',
                        'webp': 'image/webp',
                    }.get(ext, 'image/jpeg')
                if mime_type == 'image/gif':
                    try:
                        from PIL import Image
                        gif_image = Image.open(io.BytesIO(image_bytes))
                        if getattr(gif_image, 'is_animated', False):
                            logger.info(
                                f"🎬 [IMAGE] Detected animated GIF. Converting to static image: {url[:100]}...")
                            gif_image.seek(0)
                            if gif_image.mode != 'RGBA': gif_image = gif_image.convert('RGBA')
                            output_buffer = io.BytesIO()
                            gif_image.save(output_buffer, format='PNG', optimize=True)
                            image_bytes, mime_type = output_buffer.getvalue(), 'image/png'
                            # GIF→PNG 変換で膨らんだ場合も上限超過なら破棄する
                            if len(image_bytes) > max_image_bytes:
                                logger.warning(
                                    f"Image too large after GIF conversion ({len(image_bytes)} bytes): {url}")
                                return None
                            logger.debug(
                                f"🖼️ [IMAGE] Converted animated GIF to PNG (Size: {len(image_bytes)} bytes)")
                        else:
                            logger.debug(f"🖼️ [IMAGE] Static GIF detected, processing normally")
                    except ImportError:
                        logger.warning(
                            "⚠️ Pillow (PIL) library not found. Cannot process animated GIFs. Skipping image.")
                        return None
                    except Exception as gif_error:
                        logger.error(f"❌ Error processing GIF image: {gif_error}", exc_info=True)
                        return None
                encoded_image = base64.b64encode(image_bytes).decode('utf-8')
                logger.debug(
                    f"🖼️ [IMAGE] Successfully processed image: {url[:100]}... (MIME: {mime_type}, Size: {len(image_bytes)} bytes)")
                return {"type": "image_url",
                        "image_url": {"url": f"data:{mime_type};base64,{encoded_image}", "detail": "auto"}}
        except asyncio.TimeoutError:
            # 外部画像の一時的な取得失敗は想定内のため WARNING にする
            logger.warning(f"Timeout while downloading image: {url}")
            return None
        except aiohttp.ClientError as e:
            # 接続エラーも画像なしで続行できるため WARNING にする
            logger.warning(f"Failed to download image from {url}: {e}")
            return None
        except Exception as e:
            logger.error(f"Error processing image URL {url}: {e}", exc_info=True)
            return None

    async def _prepare_multimodal_content(
        self,
        message: discord.Message,
        include_reply_chain: bool = True,
    ) -> Tuple[List[Dict[str, Any]], str]:
        """画像を収集し、本文は対象メッセージのみ返す。

        文脈は `_collect_conversation_history` 側の role 履歴で維持する。
        本文を親メッセージと結合すると、最新入力に過去言語が混ざるため分離する。

        include_reply_chain:
            True  … 返信チェーンを遡って画像を集める（最新発話向け）
            False … 当該メッセージ単体のみ（履歴向け。チェーン二重取り込み防止）
        """
        # 画像収集用の走査リストと訪問済み ID を初期化する
        image_inputs, processed_urls, messages_to_scan, visited_ids, current_msg = [], set(), [], set(), message
        # チェーン走査する深さ（履歴用は1件＝当該メッセージのみ）
        scan_depth = 5 if include_reply_chain else 1
        # 返信チェーンを最大 scan_depth 件まで遡って画像ソースを集める
        for i in range(scan_depth):
            # 無効・循環参照なら走査を止める
            if not current_msg or current_msg.id in visited_ids: break
            # 削除済み参照はこれ以上辿れない
            if isinstance(current_msg, discord.DeletedReferencedMessage): break
            # 画像スキャン対象に現在メッセージを追加する
            messages_to_scan.append(current_msg)
            # 同一メッセージの再訪を防ぐ
            visited_ids.add(current_msg.id)
            # チェーン走査しない場合は1メッセージで終了する
            if not include_reply_chain:
                break
            # 親メッセージがあれば続行する
            if current_msg.reference and current_msg.reference.message_id:
                try:
                    # resolved があれば使い、無ければ fetch する
                    current_msg = current_msg.reference.resolved or await message.channel.fetch_message(
                        current_msg.reference.message_id)
                except (discord.NotFound, discord.HTTPException):
                    # 親が取れなければチェーン終端とする
                    break
            else:
                # 参照が無ければ終端とする
                break

        # 画像 URL だけチェーン全体から集める（本文は混ぜない）
        source_urls = []
        # 添付の早期スキップ用にバイト上限を取得する
        max_image_bytes = int(self.llm_config.get('max_image_bytes', 20 * 1024 * 1024))
        for msg in reversed(messages_to_scan):
            # 本文中の直リンク画像を拾う
            for url in IMAGE_URL_PATTERN.findall(msg.content):
                if url not in processed_urls: source_urls.append(url); processed_urls.add(url)
            # 添付画像を拾う（Discord が宣言した size が上限超なら DL しない）
            for attachment in msg.attachments:
                # 画像以外・重複 URL は対象外とする
                if not (attachment.content_type and attachment.content_type.startswith('image/')):
                    continue
                # 既に処理済み URL はスキップする
                if attachment.url in processed_urls:
                    continue
                # attachment.size が上限超なら HTTP 取得前に捨てる
                if attachment.size is not None and attachment.size > max_image_bytes:
                    logger.warning(
                        f"Skipping oversized Discord attachment ({attachment.size} bytes): {attachment.url}")
                    # 再評価を避けるため訪問済みに入れる
                    processed_urls.add(attachment.url)
                    continue
                # 上限内の添付 URL を収集する
                source_urls.append(attachment.url)
                processed_urls.add(attachment.url)
            # embed の image / thumbnail を拾う
            for embed in msg.embeds:
                if embed.image and embed.image.url and embed.image.url not in processed_urls: source_urls.append(
                    embed.image.url); processed_urls.add(embed.image.url)
                if embed.thumbnail and embed.thumbnail.url and embed.thumbnail.url not in processed_urls: source_urls.append(
                    embed.thumbnail.url); processed_urls.add(embed.thumbnail.url)

        # 本文は「引数の message」だけを使う（親テキストは履歴 role に任せる）
        text_content = IMAGE_URL_PATTERN.sub('', message.content).strip()

        # 設定上限まで画像をダウンロードして multimodal 化する
        max_images = self.llm_config.get('max_images', 1)
        for url in source_urls[:max_images]:
            if image_data := await self._process_image_url(url): image_inputs.append(image_data)
        # 上限超過時はチャンネルへ警告する（最新発話のチェーン収集時のみ）
        if include_reply_chain and len(source_urls) > max_images:
            try:
                await message.channel.send(self.llm_config.get('error_msg', {}).get('msg_max_image_size',
                                                                                    "⚠️ Max images ({max_images}) reached.\n⚠️ 一度に処理できる画像の最大枚数({max_images}枚)を超えました。").format(
                    max_images=max_images), delete_after=10, silent=True)
            except discord.HTTPException:
                pass
        # 画像リストと、対象メッセージ単独の本文を返す
        return image_inputs, text_content

    def _dedupe_and_trim_images_in_messages(self, messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """リクエスト全体の画像を重複排除し、枚数上限に収める。

        新しいメッセージ側の画像を優先して残す（古い履歴から削除）。
        Mistral 等の vision API は合計枚数上限（既定8）があり超過で400になるため必須。
        """
        # プロバイダ全体の上限（未設定時は Mistral 互換の8）
        max_total = int(self.llm_config.get('max_images_per_request', 8))
        # 上限が0以下なら画像をすべて落とす意図とみなし早期処理する
        if max_total <= 0:
            max_total = 0

        # (msg_idx, part_idx, image_fingerprint) を時系列で集める
        image_refs: List[Tuple[int, int, str]] = []
        for msg_idx, msg in enumerate(messages):
            # content がパーツ配列でないメッセージは画像を持たない
            content = msg.get('content')
            if not isinstance(content, list):
                continue
            for part_idx, part in enumerate(content):
                # image_url パーツだけを対象にする
                if not isinstance(part, dict) or part.get('type') != 'image_url':
                    continue
                # URL（data URL含む）をフィンガープリントにする
                image_url = (part.get('image_url') or {}).get('url', '')
                # 指紋が空ならインデックスで一意化し誤削除を避ける
                fingerprint = image_url or f"msg{msg_idx}-part{part_idx}"
                image_refs.append((msg_idx, part_idx, fingerprint))

        # 画像が無ければそのまま返す
        if not image_refs:
            return messages

        # 新しい順に走査し、同一画像は最新1件だけ残す
        keep_keys: set = set()
        seen_fingerprints: set = set()
        for msg_idx, part_idx, fingerprint in reversed(image_refs):
            # 既に同じ画像を残しているなら古い方は破棄候補
            if fingerprint in seen_fingerprints:
                continue
            # 上限に達したらこれ以上は残さない
            if len(keep_keys) >= max_total:
                continue
            # このパーツを残す対象に登録する
            seen_fingerprints.add(fingerprint)
            keep_keys.add((msg_idx, part_idx))

        # 削除対象が無ければコピー不要でそのまま返す
        if len(keep_keys) == len(image_refs):
            return messages

        removed = len(image_refs) - len(keep_keys)
        logger.info(
            "🖼️ [IMAGE] Trimmed/deduped images for API request: kept=%d removed=%d limit=%d",
            len(keep_keys), removed, max_total,
        )

        # 各メッセージの content から破棄対象パーツを除く
        for msg_idx, msg in enumerate(messages):
            content = msg.get('content')
            if not isinstance(content, list):
                continue
            # 残すパーツだけ再構築する（テキストは常に残す）
            filtered_parts = []
            for part_idx, part in enumerate(content):
                is_image = isinstance(part, dict) and part.get('type') == 'image_url'
                # 画像でなければ無条件で残す
                if not is_image:
                    filtered_parts.append(part)
                    continue
                # 画像は keep 判定に通ったものだけ残す
                if (msg_idx, part_idx) in keep_keys:
                    filtered_parts.append(part)
            msg['content'] = filtered_parts

        return messages

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot: return

        # DM は 1:1 のためメンション／リプライなしでも反応する
        is_dm = message.guild is None
        # スレッド内ではBotのメッセージへのリプライのみに反応
        is_thread = isinstance(message.channel, discord.Thread)
        is_mentioned = self.bot.user.mentioned_in(message) and not message.mention_everyone
        is_reply_to_bot = (message.reference and message.reference.resolved and 
                           isinstance(message.reference.resolved, discord.Message) and 
                           message.reference.resolved.author == self.bot.user)
        
        # DM 以外: スレッドはリプライのみ、通常チャンネルはメンション・リプライが必要
        if not is_dm:
            if is_thread:
                if not is_reply_to_bot:
                    return
            else:
                if not (is_mentioned or is_reply_to_bot):
                    return
        try:
            llm_client = await self._get_llm_client_for_channel(message.channel.id)
            if not llm_client:
                # 修正点：デフォルトのエラーメッセージを一度変数に格納する
                default_error_msg = 'LLM client is not available for this channel.\nこのチャンネルではLLMクライアントが利用できません。'
                error_msg = self.llm_config.get('error_msg', {}).get('general_error', default_error_msg)

                await self._safe_reply(
                    message,
                    content=f"❌ **Error / エラー** ❌\n\n{error_msg}",
                    view=self._create_support_view(),
                    silent=True,
                )
                return
        except Exception as e:
            logger.error(f"Failed to get LLM client for channel {message.channel.id}: {e}", exc_info=True)
            await self._safe_reply(
                message,
                content=f"❌ **Error / エラー** ❌\n\n{self.exception_handler.handle_exception(e)}",
                view=self._create_support_view(),
                silent=True,
            )
            return
        guild_log = f"guild='{message.guild.name}({message.guild.id})'" if message.guild else "guild='DM'"
        user_log = f"user='{message.author.name}({message.author.id})'"
        model_in_use = llm_client.model_name_for_api_calls
        image_contents, text_content = await self._prepare_multimodal_content(message)
        text_content = text_content.replace(f'<@!{self.bot.user.id}>', '').replace(f'<@{self.bot.user.id}>', '').strip()
        if not text_content and not image_contents:
            error_key = 'empty_reply' if is_reply_to_bot and not is_mentioned else 'empty_mention_reply'
            await self._safe_reply(
                message,
                content=self.llm_config.get('error_msg', {}).get(
                    error_key,
                    "Please say something.\n何かお話しください。"
                    if error_key == 'empty_reply'
                    else "Yes, how can I help you?\nはい、何か御用でしょうか?",
                ),
                view=self._create_support_view(),
                silent=True,
            )
            return
        logger.info(
            f"📨 Received LLM request | {guild_log} | {user_log} | model='{model_in_use}' | text_length={len(text_content)} chars | images={len(image_contents)}")
        if text_content: logger.info(
            f"[{self._bot_tag()}] [on_message] {message.guild.name if message.guild else 'DM'}({message.guild.id if message.guild else 0}),{message.author.name}({message.author.id})💬 [USER_INPUT] {((text_content[:200] + '...') if len(text_content) > 203 else text_content).replace(chr(10), ' ')}")
        thread_id = await self._get_conversation_thread_id(message)
        system_prompt = await self._prepare_system_prompt(message.channel.id, message.author.id,
                                                          message.author.display_name)
        # language_prompt は _prepare_system_prompt 内で既に結合済み
        messages_for_api: List[Dict[str, Any]] = [{"role": "system", "content": system_prompt}]
        # Discord 返信チェーンから会話履歴を収集する
        conversation_history = await self._collect_conversation_history(message)
        messages_for_api.extend(conversation_history)
        user_content_parts = []
        if text_content:
            # 最新発話だけ言語追従リマインダを付与する（履歴は別 role で維持）
            user_content_parts.append({
                "type": "text",
                "text": self._format_user_text_for_api(
                    message.created_at.astimezone(self.jst).strftime('[%H:%M]'),
                    text_content,
                    mirror_language=True,
                )
            })
        user_content_parts.extend(image_contents)
        if image_contents: logger.debug(f"Including {len(image_contents)} image(s) in request")
        user_message_for_api = {"role": "user", "content": user_content_parts}
        messages_for_api.append(user_message_for_api)
        logger.info(f"🔵 [API] Sending {len(messages_for_api)} messages to LLM")
        # system に言語固定が入っているかをデバッグログへ出す
        logger.debug(
            f"Messages structure: system={len(messages_for_api[0]['content'])} chars, "
            f"lang_enforced={'present' if 'Language Control' in messages_for_api[0]['content'] else 'absent'}"
        )
        # 通常応答の並列枠を確保する
        slot_held = False
        if chat_limiter is not None:
            slot_held = await chat_limiter.try_acquire(message.channel.id, self.bot_id)
            if not slot_held:
                busy = self.llm_config.get("error_msg", {}).get(
                    "busy_error",
                    "⚠️ 現在混雑しています。しばらくしてからもう一度お試しください。\n"
                    "⚠️ The bot is busy right now. Please try again shortly.",
                )
                await self._safe_reply(
                    message,
                    content=busy,
                    view=self._create_support_view(),
                    silent=True,
                )
                return
        try:
            # スレッド作成ボタンは削除（常にFalse）
            is_first_response = False
            sent_messages, llm_response, used_key_index = await self._handle_llm_streaming_response(
                message,
                messages_for_api,
                llm_client,
                is_first_response,
                user_text_for_router=text_content,
            )
            if sent_messages and llm_response:
                # フォールバック後の実使用モデルで完了ログを出す
                model_in_use = self._effective_model_label(llm_client, message.channel.id)
                logger.info(
                    f"✅ LLM response completed | model='{model_in_use}' | response_length={len(llm_response)} chars")
                log_response = (llm_response[:200] + '...') if len(llm_response) > 203 else llm_response
                key_log_str = f" [key{used_key_index + 1}]" if used_key_index is not None else ""
                logger.info(f"🤖 [LLM_RESPONSE][{self._bot_tag()}]{key_log_str} {log_response.replace(chr(10), ' ')}")
                logger.debug(f"LLM full response (length: {len(llm_response)} chars):\n{llm_response}")
                guild_id = message.guild.id if message.guild else 0  # DMの場合は0
                
                # ギルド固有の会話履歴を初期化
                if guild_id not in self.conversation_threads:
                    self.conversation_threads[guild_id] = {}
                if thread_id not in self.conversation_threads[guild_id]: 
                    self.conversation_threads[guild_id][thread_id] = []
                
                self.conversation_threads[guild_id][thread_id].append(user_message_for_api)
                assistant_message = {"role": "assistant", "content": llm_response, "message_id": sent_messages[0].id}
                self.conversation_threads[guild_id][thread_id].append(assistant_message)
                # 保存側も max_messages と同じ上限で切り詰める（特に DM の単一スレッド肥大を防ぐ）
                self.conversation_threads[guild_id][thread_id] = self._truncate_conversation_history(
                    self.conversation_threads[guild_id][thread_id]
                )
                for msg in sent_messages: 
                    guild_id_for_msg = msg.guild.id if msg.guild else 0
                    if guild_id_for_msg not in self.message_to_thread:
                        self.message_to_thread[guild_id_for_msg] = {}
                    self.message_to_thread[guild_id_for_msg][msg.id] = thread_id
                self._cleanup_old_threads()



        except Exception as e:
            # 権限不足などでエラー報告すら送れない場合は握りつぶす
            await self._safe_reply(
                message,
                content=f"❌ **Error / エラー** ❌\n\n{self.exception_handler.handle_exception(e)}",
                view=self._create_support_view(),
                silent=True,
            )
        finally:
            # 並列枠を必ず解放する
            if slot_held and chat_limiter is not None:
                await chat_limiter.release(message.channel.id, self.bot_id)

    def _cleanup_old_threads(self):
        for guild_id in list(self.conversation_threads.keys()):
            guild_threads = self.conversation_threads[guild_id]
            if len(guild_threads) > 100:
                threads_to_remove = list(guild_threads.keys())[:len(guild_threads) - 100]
                for thread_id in threads_to_remove:
                    del guild_threads[thread_id]
                    if guild_id in self.message_to_thread:
                        self.message_to_thread[guild_id] = {
                            k: v for k, v in self.message_to_thread[guild_id].items() 
                            if v != thread_id
                        }

    async def _handle_llm_streaming_response(
        self,
        message: discord.Message,
        initial_messages: List[Dict[str, Any]],
        client: openai.AsyncOpenAI,
        is_first_response: bool = False,
        *,
        user_text_for_router: str = "",
    ) -> Tuple[Optional[List[discord.Message]], str, Optional[int]]:
        sent_message = None
        try:
            model_name = client.model_name_for_api_calls
            # メッセージ起点: guild locale → en（ルーター後に lang で上書き）
            ui_lang = resolve_guild_lang(message.guild)
            # 応答時間照合用の provider/model キー
            estimate_key = getattr(client, "config_model_string", None) or model_name
            # まず router 待機フェーズで出す
            waiting_view = self._create_waiting_view(
                model_name,
                lang=ui_lang,
                phase="router",
                estimate_model=estimate_key,
            )
            # 権限不足でも例外を外へ出さない送信ヘルパーで待機メッセージを出す
            sent_message = await self._safe_reply(
                message,
                view=waiting_view,
                silent=True,
            )
            # 送信できなければ LLM 処理自体を中止する（エラー返信の連鎖を防ぐ）
            if sent_message is None:
                # 権限不足等を WARNING で残す
                logger.warning(
                    "[%s] Abort LLM streaming: cannot send waiting message in channel %s",
                    self._bot_tag(),
                    message.channel.id,
                )
                # 空結果を返す
                return None, "", None
            # ルーター分類（メンション / reply 共通）
            route = await run_routed_response(
                self,
                user_text=user_text_for_router or "",
                channel_id=message.channel.id,
            )
            # 直近ルートを保持する
            self._last_route_mode = route.mode
            self._last_route_lang = route.lang
            # UI 言語をルーター判定で上書きする
            ui_lang = route.lang or ui_lang
            waiting_view.lang = "ja" if str(ui_lang).startswith("ja") else "en"
            # unsupported: 正直に拒否して終了
            if route.mode == "unsupported":
                await self._replace_waiting_with_content(
                    sent_message,
                    message.channel,
                    unsupported_message(route.lang, route.reason),
                    view=self._create_support_view(),
                )
                return [sent_message], unsupported_message(route.lang, route.reason), None
            # command: バトンタッチ（ストリーム本体へは戻さない）
            if route.mode == "command":
                await self._refresh_waiting_view_for_phase_only(
                    sent_message,
                    waiting_view,
                    phase="command",
                )
                cmd_text = await execute_command_baton(
                    self,
                    user_text=user_text_for_router or "",
                    lang=route.lang,
                    channel=message.channel,
                    author=message.author,
                )
                await self._replace_waiting_with_content(
                    sent_message,
                    message.channel,
                    cmd_text,
                    view=None,
                )
                return [sent_message], cmd_text, None
            # conversation: 画像生成オフ＋描画依頼なら案内
            if route.mode == "conversation" and self._looks_like_image_gen_request(user_text_for_router):
                if self._is_image_generator_disabled():
                    notice = image_gen_disabled_message(route.lang)
                    await self._replace_waiting_with_content(
                        sent_message,
                        message.channel,
                        notice,
                        view=self._create_support_view(),
                    )
                    return [sent_message], notice, None
            # mode 用クライアントへ切替（coding / conversation）
            mode_chain = mode_model_chain(self.llm_config, route.mode)
            if mode_chain:
                mode_client = self._get_or_create_llm_client(mode_chain[0])
                if mode_client:
                    client = mode_client
            # 待機 UI をモード別（モデル名表示）へ更新
            phase = "coding" if route.mode == "coding" else "conversation"
            routed_model = getattr(client, "model_name_for_api_calls", model_name)
            # フォールバック後も記録キーと揃えるため config 文字列を渡す
            routed_estimate = getattr(client, "config_model_string", None) or routed_model
            await self._refresh_waiting_view_for_model(
                sent_message,
                waiting_view,
                routed_model,
                phase=phase,
                estimate_model=routed_estimate,
            )
            # coding 向け出力形式指示を API メッセージへ追加
            api_messages = initial_messages
            if route.mode == "coding":
                api_messages = await self._inject_coding_mode_prompt(initial_messages)
            # ストリーミング開始前に計測タイマーをスタート
            stream_start_time = time.time()
            result = await self._process_streaming_and_send_response(
                sent_message=sent_message, channel=message.channel,
                user=message.author,
                messages_for_api=api_messages, llm_client=client,
                is_first_response=is_first_response,
                waiting_view=waiting_view,
                route_mode=route.mode,
                route_lang=route.lang,
            )
            # ストリーミング完了後の経過時間を算出
            elapsed = time.time() - stream_start_time
            # 応答時間をトラッカーに記録（tips_manager が有効な場合のみ）
            if self.tips_manager and result[0] is not None:
                # フォールバック後の実使用モデル名で記録する
                used_model_name = self._effective_model_label(client, message.channel.id)
                self.tips_manager.response_tracker.record(used_model_name, elapsed)
                logger.info(
                    f"⏱️ Response time recorded: {used_model_name} = {elapsed:.1f}s"
                )
            return result
        except openai.RateLimitError as e:
            # クォータ枯渇は想定内。ERROR / traceback にしない
            logger.warning("[%s] ⚠️ LLM rate limit / quota exhausted: %s", self._bot_tag(), e)
            error_msg = f"❌ **Error / エラー** ❌\n\n{self.exception_handler.handle_exception(e)}"
            if sent_message and not self._shutting_down:
                try:
                    await self._replace_waiting_with_content(
                        sent_message,
                        message.channel,
                        error_msg,
                        view=self._create_support_view(),
                    )
                except (discord.Forbidden, discord.HTTPException):
                    # 権限不足時は追加送信しない
                    pass
            elif not sent_message:
                # 安全ヘルパーでエラー報告を試みる
                await self._safe_reply(
                    message,
                    content=error_msg,
                    view=self._create_support_view(),
                    silent=True,
                )
            return None, "", None
        except discord.Forbidden as e:
            # Missing Permissions は想定内。スタックトレースを出さない
            logger.warning(
                "[%s] Missing permissions during LLM streaming in channel %s: %s",
                self._bot_tag(),
                message.channel.id,
                e,
            )
            # 追加のエラー返信は行わない（連鎖 Forbidden を防ぐ）
            return None, "", None
        except Exception as e:
            logger.error(f"❌ Error during LLM streaming response: {e}", exc_info=True)
            error_msg = f"❌ **Error / エラー** ❌\n\n{self.exception_handler.handle_exception(e)}"
            if sent_message and not self._shutting_down:
                try:
                    await self._replace_waiting_with_content(
                        sent_message,
                        message.channel,
                        error_msg,
                        view=self._create_support_view(),
                    )
                except (discord.Forbidden, discord.HTTPException):
                    # 権限不足時は追加送信しない
                    pass
            elif not sent_message:
                # 安全ヘルパーでエラー報告を試みる
                await self._safe_reply(
                    message,
                    content=error_msg,
                    view=self._create_support_view(),
                    silent=True,
                )
            return None, "", None

    async def _replace_waiting_with_content(
        self,
        sent_message: discord.Message,
        channel: discord.abc.Messageable,
        content: str,
        *,
        view: Optional[discord.ui.View] = None,
        track_active: bool = False,
    ) -> discord.Message:
        """Components V2 待機メッセージを通常の content メッセージへ切り替える。

        V2 は content と併用できないため、edit できなければ削除して送り直す。
        track_active=True のときのみ再起動通知用のメッセージ追跡を更新する。
        """
        # まず edit で V2 解除を試す
        try:
            await sent_message.edit(content=content, embed=None, view=view)
            return sent_message
        except discord.HTTPException as e:
            logger.debug("Waiting V2 edit-to-content failed (%s); replacing message", e)
        # ストリーム追跡中のみ旧メッセージを追跡から外す
        if track_active:
            self._unregister_active_response(sent_message)
        # 元メッセージの返信先を可能な範囲で引き継ぐ
        reference = getattr(sent_message, "reference", None)
        try:
            await sent_message.delete()
        except discord.HTTPException:
            pass
        # 新規に content メッセージを送る（@silent で通知抑制）
        send_kwargs: Dict[str, Any] = {"silent": True}
        # view 指定があれば付与する
        if view is not None:
            # View を送信引数へ入れる
            send_kwargs["view"] = view
        try:
            # 返信参照がある場合は引用付きで送る
            if reference is not None:
                # reference 付きで silent 送信する
                new_msg = await channel.send(content, reference=reference, **send_kwargs)
            else:
                # 参照なしで silent 送信する
                new_msg = await channel.send(content, **send_kwargs)
        except discord.HTTPException:
            # 参照付きが失敗したら参照なしで再送する
            new_msg = await channel.send(content, **send_kwargs)
        # ストリーム追跡中のみ新メッセージを追跡する
        if track_active:
            self._register_active_response(new_msg)
        return new_msg

    async def _process_streaming_and_send_response(self, sent_message: discord.Message,
                                                   channel: discord.abc.Messageable,
                                                   user: Union[discord.User, discord.Member],
                                                   messages_for_api: List[Dict[str, Any]],
                                                   llm_client: openai.AsyncOpenAI,
                                                   is_first_response: bool = False,
                                                   waiting_view: Optional[WaitingLayoutView] = None,
                                                   route_mode: Optional[str] = None,
                                                   route_lang: Optional[str] = None) -> Tuple[
        Optional[List[discord.Message]], str, Optional[int]]:
        # 応答生成中ギルド数を加算する（finally で必ず減算）
        guild_id = sent_message.guild.id if sent_message.guild else None
        self._begin_active_llm_guild(guild_id)
        # 応答生成中として追跡登録する（再起動通知の対象にする）
        self._register_active_response(sent_message)
        try:
            full_response_text, last_update, last_displayed_length, chunk_count = "", 0.0, 0, 0
            update_interval, min_update_chars, retry_sleep_time = 0.5, 15, 2.0
            # coding は coding 絵文字、それ以外は stream 絵文字
            if route_mode == "coding":
                emoji_prefix, emoji_suffix = "<a:coding:1531206254574178346> ", " <a:coding:1531206254574178346>"
            else:
                emoji_prefix, emoji_suffix = "<:stream:1313474295372058758> ", " <:stream:1313474295372058758>"
            max_final_retries, final_retry_delay = 3, 2.0
            is_first_update = True
            # 待機 V2 から通常 content へ切り替えたか
            waiting_v2_cleared = False
            logger.debug(f"Starting LLM stream for message {sent_message.id}")

            async def _on_model_fallback(
                new_model: str,
                failed_model: str,
                estimate_model: Optional[str] = None,
            ) -> None:
                """別モデルへフォールバックしたとき待機 UI のモデル名を更新する。"""
                # 待機 UI が無い／既に本文へ切替済みなら何もしない
                if waiting_view is None or waiting_v2_cleared:
                    return
                # シャットダウン中は編集しない
                if self._shutting_down:
                    return
                # 振り分け後フェーズ見出しを維持したままモデル名だけ差し替える
                fallback_phase = "coding" if route_mode == "coding" else "conversation"
                await self._refresh_waiting_view_for_model(
                    sent_message,
                    waiting_view,
                    new_model,
                    fallback_from=failed_model,
                    phase=fallback_phase,
                    # 記録キー（provider/model）で予想時間を照合する
                    estimate_model=estimate_model or new_model,
                )

            stream_generator = self._llm_stream_and_tool_handler(
                messages_for_api,
                llm_client,
                channel.id,
                user.id,
                on_model_fallback=_on_model_fallback if waiting_view is not None else None,
                route_mode=route_mode,
            )
            # coding は生成中に本文を流さず、完了後に要約+.md 添付へ
            coding_cfg = (
                coding_attach_settings(self.llm_config)
                if route_mode == "coding"
                else None
            )
            skip_stream_preview = bool(
                coding_cfg and coding_cfg.get("skip_stream_preview", True)
            )
            async for content_chunk in stream_generator:
                # シャットダウン通知後はストリーム編集を打ち切る
                if self._shutting_down:
                    # 再起動文言を上書き済みなのでループを抜ける
                    break
                if not content_chunk:
                    continue
                chunk_count += 1
                full_response_text += content_chunk
                if chunk_count % 100 == 0: logger.debug(
                    f"Stream chunk #{chunk_count}, total length: {len(full_response_text)} chars")
                current_time, chars_accumulated = time.time(), len(full_response_text) - last_displayed_length

                should_update = (
                    not skip_stream_preview
                    and (
                        is_first_update
                        or (
                            current_time - last_update > update_interval
                            and chars_accumulated >= min_update_chars
                        )
                    )
                )

                if should_update and full_response_text:
                    is_first_update = False
                    display_length = len(full_response_text)
                    if display_length > SAFE_MESSAGE_LENGTH:
                        display_text = f"{emoji_prefix}{full_response_text[:SAFE_MESSAGE_LENGTH - len(emoji_prefix) - len(emoji_suffix) - 100]}\n\n⚠️ (Output is long, will be split...)\n⚠️ (出力が長いため分割します...){emoji_suffix}"
                    else:
                        display_text = f"{emoji_prefix}{full_response_text[:SAFE_MESSAGE_LENGTH - len(emoji_prefix) - len(emoji_suffix)]}{emoji_suffix}"
                    if display_text != sent_message.content:
                        try:
                            # 初回は V2 待機 UI を通常 content に切り替える（寄付ボタンも消える）
                            if not waiting_v2_cleared:
                                sent_message = await self._replace_waiting_with_content(
                                    sent_message, channel, display_text, track_active=True
                                )
                                waiting_v2_cleared = True
                            else:
                                await sent_message.edit(content=display_text, view=None)
                            last_update, last_displayed_length = current_time, len(full_response_text)
                            logger.debug(f"Updated Discord message (displayed: {len(display_text)} chars)")
                        except discord.NotFound:
                            logger.warning(f"⚠️ Message deleted during stream (ID: {sent_message.id}). Aborting.")
                            return None, "", None
                        except discord.HTTPException as e:
                            if e.status == 429:
                                retry_after = (e.retry_after or 1.0) + 0.5
                                logger.warning(
                                    f"⚠️ Rate limited on message edit (ID: {sent_message.id}). Waiting {retry_after:.2f}s")
                                await asyncio.sleep(retry_after)
                                last_update = time.time()
                            else:
                                logger.warning(
                                    f"⚠️ Failed to edit message (ID: {sent_message.id}): {e.status} - {getattr(e, 'text', str(e))}")
                                await asyncio.sleep(retry_sleep_time)
            # シャットダウン済みなら最終編集をせずに終了する
            if self._shutting_down:
                # 再起動通知メッセージを維持したまま返す
                return None, "", None
            logger.debug(f"Stream completed | Total chunks: {chunk_count} | Final length: {len(full_response_text)} chars")
            if full_response_text:
                # coding 長文はファイル添付を優先する
                if route_mode == "coding":
                    attached = await self._maybe_send_coding_attachment(
                        sent_message=sent_message,
                        channel=channel,
                        full_text=full_response_text,
                        route_lang=route_lang or "en",
                        waiting_v2_cleared=waiting_v2_cleared,
                    )
                    if attached is not None:
                        # 添付処理でメッセージ差し替えがあれば finally 用に同期する
                        if attached[0]:
                            sent_message = attached[0][0]
                        return attached
                if len(full_response_text) <= SAFE_MESSAGE_LENGTH:
                    # 最終本文のみ（寄付ボタンは付けない — 待機中のみ表示）
                    final_content = full_response_text
                    for attempt in range(max_final_retries):
                        try:
                            if not waiting_v2_cleared:
                                # ストリーム更新が無かった場合も V2 待機を外す
                                sent_message = await self._replace_waiting_with_content(
                                    sent_message, channel, final_content, track_active=True
                                )
                                waiting_v2_cleared = True
                            elif final_content != sent_message.content:
                                await sent_message.edit(
                                    content=final_content, embed=None, view=None
                                )
                            logger.debug(f"Final message updated successfully (attempt {attempt + 1})")
                            break
                        except discord.NotFound:
                            logger.error(f"❌ Message was deleted before final update")
                            return None, "", None
                        except discord.HTTPException as e:
                            if e.status == 429:
                                retry_after = (e.retry_after or 1.0) + 0.5
                                logger.warning(
                                    f"⚠️ Rate limited on final update (attempt {attempt + 1}/{max_final_retries}). Waiting {retry_after:.2f}s")
                                await asyncio.sleep(retry_after)
                            else:
                                logger.warning(
                                    f"⚠️ Failed to update final message (attempt {attempt + 1}/{max_final_retries}): {e.status} - {getattr(e, 'text', str(e))}")
                                if attempt < max_final_retries - 1: await asyncio.sleep(final_retry_delay)
                    return [sent_message], full_response_text, getattr(llm_client, 'last_used_key_index', None)
                else:
                    logger.debug(f"Response is {len(full_response_text)} chars, splitting into multiple messages")
                    # 修正: タプル作成のバグを修正
                    chunks = split_message_smartly(
                        full_response_text,
                        SAFE_MESSAGE_LENGTH,
                    )
                    all_messages = []
                    # 先頭チャンクを取り出す
                    first_chunk = chunks[0]

                    for attempt in range(max_final_retries):
                        try:
                            if not waiting_v2_cleared:
                                sent_message = await self._replace_waiting_with_content(
                                    sent_message, channel, first_chunk, track_active=True
                                )
                                waiting_v2_cleared = True
                            else:
                                await sent_message.edit(content=first_chunk, embed=None, view=None)
                            all_messages.append(sent_message)
                            logger.debug(f"Updated first message (1/{len(chunks)})")
                            break
                        except discord.HTTPException as e:
                            if e.status == 429:
                                retry_after = (e.retry_after or 1.0) + 0.5
                                logger.warning(f"⚠️ Rate limited on first chunk update, waiting {retry_after:.2f}s")
                                await asyncio.sleep(retry_after)
                            else:
                                logger.error(f"❌ Failed to update first message: {e}")
                                if attempt < max_final_retries - 1: await asyncio.sleep(final_retry_delay)
                    for i, chunk in enumerate(chunks[1:], start=2):
                        for attempt in range(max_final_retries):
                            try:
                                # 続きチャンクも @silent で通知抑制する
                                continuation_msg = await channel.send(chunk, silent=True)
                                all_messages.append(continuation_msg)
                                logger.debug(f"Sent continuation message {i}/{len(chunks)}")
                                break
                            except discord.HTTPException as e:
                                if e.status == 429:
                                    retry_after = (e.retry_after or 1.0) + 0.5
                                    logger.warning(f"⚠️ Rate limited on continuation {i}, waiting {retry_after:.2f}s")
                                    await asyncio.sleep(retry_after)
                                else:
                                    logger.error(f"❌ Failed to send continuation message {i}: {e}")
                                    if attempt < max_final_retries - 1: await asyncio.sleep(final_retry_delay)
                    return all_messages, full_response_text, getattr(llm_client, 'last_used_key_index', None)
            else:
                finish_reason = getattr(llm_client, 'last_finish_reason', None)
                if finish_reason == 'content_filter':
                    error_msg = self.llm_config.get('error_msg', {}).get('content_filter_error',
                                                                         "The response was blocked by the content filter.\nAIの応答がコンテンツフィルターによってブロックされました。");
                    logger.warning(
                        f"⚠️ Empty response from LLM due to content filter.")
                else:
                    error_msg = self.llm_config.get('error_msg', {}).get('empty_response_error',
                                                                         "There was no response from the AI. Please try rephrasing your message.\nAIから応答がありませんでした。表現を変えてもう一度お試しください。");
                    logger.warning(
                        f"⚠️ Empty response from LLM (Finish reason: {finish_reason})")
                await self._replace_waiting_with_content(
                    sent_message,
                    channel,
                    f"❌ **Error / エラー** ❌\n\n{error_msg}",
                    view=self._create_support_view(),
                    track_active=True,
                )
                return None, "", None
        finally:
            # 正常終了・中断・例外いずれでも追跡を解除する
            self._unregister_active_response(sent_message)
            # GUI 用ギルドカウントも必ず減算する
            self._end_active_llm_guild(guild_id)

    def _convert_messages_for_gemini(self, messages: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], str]:
        system_prompts_content, other_messages, has_system_message = [], [], False
        for message in messages:
            if message.get("role") == "system":
                if isinstance(message.get("content"), str) and message["content"].strip():
                    system_prompts_content.append(message["content"])
                    has_system_message = True
            else:
                other_messages.append(message)
        if not has_system_message: return messages, ""
        combined_system_prompt = "\n\n".join(system_prompts_content)
        # 確認応答は英語にし、Gemini 経路でも日本語プライミングしない
        converted_messages = [{"role": "user", "content": combined_system_prompt},
                              {"role": "assistant", "content": "Understood. I will follow the instructions."}]
        converted_messages.extend(other_messages)
        return converted_messages, combined_system_prompt

    def _get_model_fallback_chain(
        self, primary_model: Optional[str], *, mode: Optional[str] = None
    ) -> List[str]:
        """メインモデル＋fallback_models の試行順リストを返す。mode 指定時は modes.<mode> を優先。"""
        # モード別チェーンがあればそれを使う
        if mode:
            chain = mode_model_chain(self.llm_config, mode)
            if chain:
                return chain
        # 試行順を格納するリストを用意する
        chain: List[str] = []
        # primary が有効なら先頭に入れる
        if primary_model:
            # 重複防止のためそのまま append する
            chain.append(primary_model)
        # 設定のフォールバック一覧を順に読む
        for model in self.llm_config.get("fallback_models") or []:
            # 空文字や primary 重複はスキップする
            if model and model not in chain:
                # 次候補として末尾へ追加する
                chain.append(model)
        # 完成した試行順を返す
        return chain

    def _client_model_string(self, client: openai.AsyncOpenAI, channel_id: int) -> Optional[str]:
        """クライアントから provider/model 文字列を組み立てる。"""
        # 設定時の正規文字列があればそれを優先する（OpenRouter free 補正後の二重化を防ぐ）
        config_model = getattr(client, "config_model_string", None)
        # 設定文字列があればそのまま返す
        if config_model:
            return config_model
        # クライアントに付与された provider 名を取得する
        provider_name = getattr(client, "provider_name", None)
        # クライアントに付与された API 用モデル名を取得する
        model_name = getattr(client, "model_name_for_api_calls", None)
        # 両方揃っていれば正規形式で返す
        if provider_name and model_name:
            return f"{provider_name}/{model_name}"
        # 欠ける場合はチャンネル解決結果へフォールバックする
        return self._resolve_model_string(channel_id)

    def _effective_model_label(self, client: openai.AsyncOpenAI, channel_id: Optional[int] = None) -> str:
        """ログ用: フォールバック後の実使用モデルを優先して返す。"""
        # ストリーム成功時に書き戻した実モデルを優先する
        used = getattr(client, "last_used_model_string", None)
        # 実モデルがあればそれを返す
        if used:
            return used
        # channel_id があれば正規の provider/model を組み立てる
        if channel_id is not None:
            resolved = self._client_model_string(client, channel_id)
            if resolved:
                return resolved
        # 最後の手段として API 用モデル名のみ返す
        return str(getattr(client, "model_name_for_api_calls", "unknown"))

    def _get_or_create_llm_client(self, model_string: str) -> Optional[openai.AsyncOpenAI]:
        """モデル文字列に対応するクライアントをキャッシュ優先で取得する。"""
        # 既に初期化済みならキャッシュを返す
        if model_string in self.llm_clients:
            return self.llm_clients[model_string]
        # 未初期化なら新規作成する
        client = self._initialize_llm_client(model_string)
        # 成功時のみキャッシュへ登録する
        if client:
            self.llm_clients[model_string] = client
        # 作成結果（失敗時は None）を返す
        return client

    def _ensure_messages_for_model(
        self, messages: List[Dict[str, Any]], model_string: Optional[str]
    ) -> List[Dict[str, Any]]:
        """切替先モデル向けにメッセージ形式を必要なら変換する。"""
        # Google 直結 Gemini のみアダプタ対象（OpenRouter 等は除外）
        is_gemini = self._is_google_gemini_model(model_string)
        # system role が残っているか確認する（未変換の目安）
        has_system = any(m.get("role") == "system" for m in messages)
        # Gemini かつ system が残っていれば変換する
        if is_gemini and has_system:
            # Gemini 用アダプタで system を潰す
            converted, _combined = self._convert_messages_for_gemini(messages)
            # 変換後メッセージを返す
            return converted
        # それ以外はそのまま返す
        return messages

    def _apply_tools_to_api_kwargs(
        self,
        api_kwargs: Dict[str, Any],
        tools_def: Optional[List[Dict[str, Any]]],
        provider_name: str,
        supports_tools: bool,
    ) -> None:
        """api_kwargs に tools を付与／除去する。"""
        # 既存 tools 指定をいったん消す（モデル切替時の残留防止）
        api_kwargs.pop("tools", None)
        # tool_choice も同様に消す
        api_kwargs.pop("tool_choice", None)
        # KoboldCPP 判定用フラグ
        is_koboldcpp = provider_name.lower() == "koboldcpp"
        # ツール定義がありサポートも有効なら渡す
        if tools_def and supports_tools:
            # tools 定義を API 引数へ載せる
            api_kwargs["tools"] = tools_def
            # 自動選択を指定する
            api_kwargs["tool_choice"] = "auto"
            # ログ用にツール名一覧を集める
            tool_names = []
            # 各ツール定義を走査する
            for t in tools_def:
                try:
                    # dict 形式を想定して名前を取り出す
                    if isinstance(t, dict):
                        if "function" in t and isinstance(t["function"], dict):
                            tool_names.append(t["function"].get("name", "unnamed_function"))
                        elif "name" in t:
                            tool_names.append(t["name"])
                        else:
                            tool_names.append("unnamed_tool")
                    else:
                        tool_names.append(str(t))
                except Exception as e:
                    logger.warning(f"⚠️ [TOOLS] Error processing tool: {e}")
                    tool_names.append("error_processing_tool")
            logger.info(f"🔧 [TOOLS] Passing {len(tools_def)} tools to API: {tool_names}")
            if is_koboldcpp:
                logger.info("🔧 [KoboldCPP] Tools are enabled for this model")
        elif tools_def and not supports_tools:
            logger.warning(
                f"⚠️ [TOOLS] Tools are disabled for provider '{provider_name}' "
                f"(supports_tools=false). Skipping tools."
            )
            if is_koboldcpp:
                logger.warning(
                    "⚠️ [KoboldCPP] This KoboldCPP model may not support tools. "
                    "Consider enabling 'supports_tools: true' in config if the model supports it."
                )
        else:
            # ツール未設定、または強制最終回答などで意図的に渡さない場合
            logger.info("🔧 [TOOLS] No tools passed to API this turn")

    async def _rotate_provider_api_key(
        self, client: openai.AsyncOpenAI, provider_name: str, api_keys: List[str], current_key_index: int
    ) -> openai.AsyncOpenAI:
        """同一プロバイダー内で次の API キーへローテーションする。"""
        # キー総数を取得する
        num_keys = len(api_keys)
        # 次インデックスを環状に計算する
        next_key_index = (current_key_index + 1) % num_keys
        # プロバイダーの現在キー位置を更新する
        self.provider_key_index[provider_name] = next_key_index
        # 次に使うキー文字列を取り出す
        next_key = api_keys[next_key_index]
        # 切替をログに残す
        logger.info(
            f"🔄 Switching to next API key for provider '{provider_name}' "
            f"(index: {next_key_index}) and retrying."
        )
        # プロバイダー設定を読む
        provider_config = self.llm_config.get("providers", {}).get(provider_name, {})
        # KoboldCPP かどうか判定する
        is_koboldcpp = provider_name.lower() == "koboldcpp"
        # 共通 kwargs（timeout / OpenRouter ヘッダ）で作り直す
        new_client = openai.AsyncOpenAI(
            **self._openai_client_kwargs(
                provider_name, provider_config, next_key, base_url=str(client.base_url)
            )
        )
        # モデル名メタデータを引き継ぐ
        new_client.model_name_for_api_calls = client.model_name_for_api_calls
        # プロバイダー名メタデータを引き継ぐ
        new_client.provider_name = client.provider_name
        # 設定上の model 文字列も引き継ぐ
        new_client.config_model_string = getattr(client, "config_model_string", None)
        # ツール対応フラグを引き継ぐ
        if is_koboldcpp:
            new_client.supports_tools = getattr(
                client, "supports_tools", provider_config.get("supports_tools", True)
            )
        else:
            new_client.supports_tools = getattr(client, "supports_tools", True)
        # キャッシュキーは設定文字列を優先する
        cache_key = getattr(new_client, "config_model_string", None) or (
            f"{provider_name}/{new_client.model_name_for_api_calls}"
        )
        # キャッシュを新クライアントで更新する
        self.llm_clients[cache_key] = new_client
        # 連打抑制のため短く待つ
        await asyncio.sleep(1)
        # 新しいクライアントを返す
        return new_client

    async def _chat_completion_with_key_rotation(
        self,
        client: openai.AsyncOpenAI,
        *,
        messages: List[Dict[str, Any]],
        max_tokens: int,
        temperature: float,
        stream: bool = False,
        suppress_thinking: bool = False,
    ) -> Tuple[Any, openai.AsyncOpenAI]:
        """非ストリーム completion を同一プロバイダーの全キーで試す。尽きたら最後の例外を再送出。"""
        # プロバイダー名をクライアントから取る
        provider_name = getattr(client, "provider_name", None) or "unknown"
        # 登録済み API キー一覧を取る
        api_keys = self.provider_api_keys.get(provider_name, [])
        # キー数（最低 1 回は試す）
        num_keys = max(len(api_keys), 1)
        # 最後に起きた例外を保持する
        last_error: Optional[Exception] = None
        # 作業用クライアント参照
        working = client
        # Gemma / Google 向け thinking 抑制（OpenAI 互換の extra_body 二重ネスト）
        thinking_extra: Optional[Dict[str, Any]] = None
        # Google 直結のみ（NIM 等に google.thinking_config を送らない）
        if suppress_thinking and provider_name.lower() == "google":
            # SDK の extra_body はリクエスト本体へマージされるため、
            # REST の top-level "extra_body.google" になるようもう一段ネストする
            thinking_extra = {
                "extra_body": {
                    "google": {
                        "thinking_config": {
                            "thinking_level": "minimal",
                            "include_thoughts": False,
                        }
                    }
                }
            }
        # thinking 抑制を外して同キー再試行したか
        thinking_retry_done = False
        # 同一モデル内でキーを順に試す
        for attempt in range(num_keys):
            # 現在のキーインデックスを読む
            current_key_index = self.provider_key_index.get(provider_name, 0)
            # ログ用に使用キーを記録する
            working.last_used_key_index = current_key_index
            try:
                # create 引数を組み立てる
                create_kwargs: Dict[str, Any] = {
                    "model": working.model_name_for_api_calls,
                    "messages": messages,
                    "stream": stream,
                    "max_tokens": max_tokens,
                    "temperature": temperature,
                }
                # thinking 抑制を付ける
                if thinking_extra is not None:
                    create_kwargs["extra_body"] = thinking_extra
                # completion を発行する
                resp = await working.chat.completions.create(**create_kwargs)
                # 成功したら応答とクライアントを返す
                return resp, working
            except (openai.RateLimitError, openai.InternalServerError) as e:
                # 最終失敗として保持する
                last_error = e
                # エラー種別ラベルを決める
                error_type = "Rate limit" if isinstance(e, openai.RateLimitError) else "Server"
                # ステータスを取り出す
                status_code = getattr(e, "status_code", "N/A")
                logger.warning(
                    "⚠️ %s error (%s) for provider '%s' with key index %s (router/non-stream). Details: %s",
                    error_type,
                    status_code,
                    provider_name,
                    current_key_index,
                    e,
                )
                # キーが無い、または使い切ったら抜ける
                if not api_keys or attempt + 1 >= num_keys:
                    break
                # 次キーへローテーションする
                working = await self._rotate_provider_api_key(
                    working, provider_name, api_keys, current_key_index
                )
            except (openai.BadRequestError, openai.APIStatusError) as e:
                # 最終失敗として保持する
                last_error = e
                # ステータスを取り出す
                status_code = getattr(e, "status_code", None)
                # thinking 抑制パラメータ非対応なら外して同キー再試行する（1 回だけ）
                if (
                    thinking_extra is not None
                    and not thinking_retry_done
                    and isinstance(status_code, int)
                    and status_code == 400
                ):
                    logger.warning(
                        "⚠️ thinking suppress rejected by '%s' (400); retrying without it. Details: %s",
                        provider_name,
                        e,
                    )
                    # 以降の試行では付けない
                    thinking_extra = None
                    # 再試行済みフラグ
                    thinking_retry_done = True
                    # 同キーでもう一度（attempt は進めない扱いのため continue）
                    continue
                logger.warning(
                    "⚠️ API status error (%s) for provider '%s' with key index %s (router/non-stream). Details: %s",
                    status_code,
                    provider_name,
                    current_key_index,
                    e,
                )
                # キーが無い、または使い切ったら抜ける
                if not api_keys or attempt + 1 >= num_keys:
                    break
                # 次キーへローテーションする
                working = await self._rotate_provider_api_key(
                    working, provider_name, api_keys, current_key_index
                )
            except _TRANSIENT_LLM_NETWORK_ERRORS as e:
                last_error = e
                logger.warning(
                    "⚠️ Transient network error for provider '%s' with key index %s "
                    "(router/non-stream). Details: %s",
                    provider_name,
                    current_key_index,
                    e,
                )
                await asyncio.sleep(_LLM_TRANSIENT_RETRY_SLEEP_SEC)
                if not api_keys or attempt + 1 >= num_keys:
                    break
                working = await self._rotate_provider_api_key(
                    working, provider_name, api_keys, current_key_index
                )
            except Exception:
                # 想定外はキー巡回せず呼び出し側へ渡す
                raise
        # 全キー失敗時は最後の例外を上げる
        if last_error is not None:
            raise last_error
        # キー無し等で例外が無い場合の保険
        raise Exception(f"No API keys available for provider {provider_name}")

    async def _llm_stream_and_tool_handler(
        self,
        messages: List[Dict[str, Any]],
        client: openai.AsyncOpenAI,
        channel_id: int,
        user_id: int,
        on_model_fallback: Optional[Callable[..., Awaitable[None]]] = None,
        route_mode: Optional[str] = None,
    ) -> AsyncGenerator[str, None]:
        # 呼び出し元クライアント参照を保持（フォールバック後のメタ書き戻し用）
        request_client = client
        # 前回の実使用モデル情報をクリアする
        request_client.last_used_model_string = None
        # チャンネルの選択モデル文字列を解決する（conversation のみ上書き適用）
        if route_mode and route_mode != "conversation":
            # coding / command 等は modes 設定を優先（チャンネル上書きを無視）
            chain_primary = (mode_model_chain(self.llm_config, route_mode) or [None])[0]
            primary_model_string = chain_primary or self._client_model_string(client, channel_id)
        else:
            model_string = self._resolve_model_string(channel_id)
            # 実クライアントの provider/model を優先して試行チェーンを組む
            primary_model_string = self._client_model_string(client, channel_id) or model_string
        # Google 直結 Gemini のみ初回変換する（OpenRouter 経由は除外）
        is_gemini = self._is_google_gemini_model(primary_model_string)

        if is_gemini:
            original_messages_for_log = messages
            messages, combined_system_prompt = self._convert_messages_for_gemini(messages)
            if combined_system_prompt:
                logger.info(f"🔄 [GEMINI ADAPTER] Converting system prompts for Gemini model '{primary_model_string}'.")
                logger.debug(
                    f"  - Combined system prompt ({len(combined_system_prompt)} chars): {combined_system_prompt.replace(chr(10), ' ')[:300]}...")
                logger.debug(f"  - Message count changed: {len(original_messages_for_log)} -> {len(messages)}")

        current_messages = messages.copy()
        # API送信前に履歴＋最新分の画像を重複排除し枚数上限に収める
        current_messages = self._dedupe_and_trim_images_in_messages(current_messages)
        # ツール往復の上限（超過時はツールなしの最終回答を1回追加する）
        max_iterations = self.llm_config.get('max_tool_iterations', 5)
        extra_params = self.llm_config.get('extra_api_parameters', {})
        # 強制最終回答用の誘導メッセージを既に入れたか
        force_final_nudge_injected = False

        provider_name = getattr(client, 'provider_name', None)
        if not provider_name:
            if primary_model_string and '/' in primary_model_string:
                provider_name = primary_model_string.split('/', 1)[0]
                logger.debug(
                    "Provider name missing on client; inferring from model string as '%s'", provider_name
                )
            else:
                provider_name = "unknown"
                logger.warning(
                    "Provider name missing on client and could not be inferred from model string."
                )
            client.provider_name = provider_name

        # メイン→fallback_models の試行順を構築する（モード別優先）
        model_chain = self._get_model_fallback_chain(primary_model_string, mode=route_mode)
        logger.debug(f"LLM model attempt chain: {model_chain}")

        # +1 回目はツール上限超過後の「ツールなし強制回答」枠
        for iteration in range(max_iterations + 1):
            # 通常枠を使い切ったあとの最終回答ターンか
            force_final = iteration >= max_iterations
            # 最終回答枠なのにツール結果が無いならエラーへ落とす
            if force_final and not self._messages_have_tool_results(current_messages):
                # これ以上合成できる材料が無いのでループを抜ける
                break
            if force_final:
                # 検索などを繰り返して上限に達した旨をログに残す
                logger.warning(
                    f"⚠️ Tool processing reached max iterations ({max_iterations}); "
                    "forcing final answer without tools"
                )
                # 誘導メッセージ未投入なら、手元の結果で答えるよう指示する
                if not force_final_nudge_injected:
                    # ユーザー向けではなく内部指示として明示する
                    current_messages.append({
                        "role": "user",
                        "content": (
                            "[Internal] Tool call budget is exhausted. "
                            "Answer the user now using previous tool results. "
                            "If information is incomplete or uncertain, say so clearly. "
                            "Do not call tools."
                        ),
                    })
                    # 二重投入を防ぐ
                    force_final_nudge_injected = True
            else:
                # 通常のツール往復ターンをログする
                logger.debug(f"Starting LLM API call (iteration {iteration + 1}/{max_iterations})")
            # 利用可能なツール定義を取得する（最終回答時は渡さない）
            tools_def = self.get_tools_definition()
            # 強制最終回答では tools を無効化する
            tools_for_api = None if force_final else tools_def

            api_kwargs = {
                "model": client.model_name_for_api_calls,
                "messages": current_messages,
                "stream": True,
                "temperature": extra_params.get('temperature', 0.7),
                "max_tokens": extra_params.get('max_tokens', 4096)
            }

            # 初期クライアント向けに tools を載せる（最終回答時は除去）
            self._apply_tools_to_api_kwargs(
                api_kwargs,
                tools_for_api,
                provider_name,
                getattr(client, "supports_tools", True),
            )

            stream = None
            # モデル横断での最後の例外を保持する
            last_model_error: Optional[Exception] = None
            # 次モデル切替理由（keys / empty）を保持する
            fallback_reason = "keys_exhausted"
            # ストリーム読取結果（モデルループ外のツール処理でも参照する）
            tool_calls_buffer: List[Dict[str, Any]] = []
            # アシスタント本文の蓄積用
            assistant_response_content = ""
            # 終了理由の初期値
            finish_reason = None

            # メインモデルの全キー失敗後、fallback_models を順に試す
            for model_idx, attempt_model_string in enumerate(model_chain):
                # ストリーム参照をモデル試行ごとにリセットする
                stream = None
                # 2件目以降はフォールバック先クライアントへ切り替える
                if model_idx > 0:
                    # 直前に失敗したモデル名を特定する
                    failed_model = model_chain[model_idx - 1]
                    # 待機 UI 表示用に、切替前クライアントの短名を控える
                    failed_label = getattr(client, "model_name_for_api_calls", None)
                    # 短名が取れなければ provider/model のモデル部分を使う
                    if not failed_label:
                        failed_label = failed_model.split("/", 1)[-1]
                    # 切替理由に応じてログ文言を変える
                    if fallback_reason == "empty_response":
                        # 空応答からの切替であることを明示する
                        logger.warning(
                            f"🔄 [MODEL FALLBACK] Empty response from '{failed_model}'. "
                            f"Trying fallback model '{attempt_model_string}' "
                            f"({model_idx + 1}/{len(model_chain)})."
                        )
                    else:
                        # キー枯渇など接続失敗からの切替を明示する
                        logger.warning(
                            f"🔄 [MODEL FALLBACK] All keys failed for '{failed_model}'. "
                            f"Trying fallback model '{attempt_model_string}' "
                            f"({model_idx + 1}/{len(model_chain)})."
                        )
                    # 次試行の既定理由へ戻す（空応答フラグの持ち越し防止）
                    fallback_reason = "keys_exhausted"
                    # フォールバック先クライアントを取得する
                    fallback_client = self._get_or_create_llm_client(attempt_model_string)
                    # 初期化失敗なら次候補へ進む
                    if not fallback_client:
                        logger.error(
                            f"❌ [MODEL FALLBACK] Failed to initialize client for '{attempt_model_string}'. Skipping."
                        )
                        continue
                    # 以降の API 呼び出しに使うクライアントを差し替える
                    client = fallback_client
                    # プロバイダー名を更新する
                    provider_name = getattr(client, "provider_name", attempt_model_string.split("/", 1)[0])
                    # メッセージ形式を切替先モデル向けに整える
                    current_messages = self._ensure_messages_for_model(current_messages, attempt_model_string)
                    # API 引数のモデル名を更新する
                    api_kwargs["model"] = client.model_name_for_api_calls
                    # API 引数の messages も更新する
                    api_kwargs["messages"] = current_messages
                    # tools 可否を切替先に合わせて再設定する（最終回答時は tools_for_api が None）
                    self._apply_tools_to_api_kwargs(
                        api_kwargs,
                        tools_for_api,
                        provider_name,
                        getattr(client, "supports_tools", True),
                    )
                    # 別モデル切替時のみ待機 UI を更新（同一モデルのキー回転では呼ばない）
                    if on_model_fallback is not None:
                        try:
                            await on_model_fallback(
                                client.model_name_for_api_calls,
                                str(failed_label),
                                # 予想時間は記録時と同じ provider/model で照合する
                                attempt_model_string,
                            )
                        except Exception as callback_error:
                            # UI 更新失敗でストリーム自体は止めない
                            logger.debug(
                                "on_model_fallback callback failed: %s", callback_error
                            )

                # 現在プロバイダーの API キー一覧を取得する
                api_keys = self.provider_api_keys.get(client.provider_name, [])
                # キー数を数える
                num_keys = len(api_keys)

                # キーが無い場合は次モデルへ進む（最後なら例外）
                if num_keys == 0:
                    last_model_error = Exception(
                        f"No API keys available for provider {provider_name}"
                    )
                    logger.warning(str(last_model_error))
                    continue

                # このモデルでのキー全枯れフラグ
                keys_exhausted = False
                # 同一モデル内でキーを順に試す
                for attempt in range(num_keys):
                    try:
                        # 現在のキーインデックスを読む
                        current_key_index = self.provider_key_index.get(provider_name, 0)
                        # 使用キーをクライアントへ記録する
                        client.last_used_key_index = current_key_index
                        logger.debug(
                            f"Attempting API call to '{attempt_model_string}' "
                            f"with key index {current_key_index} (Attempt {attempt + 1}/{num_keys})."
                        )
                        # ストリーム接続を開始する
                        stream = await client.chat.completions.create(**api_kwargs)
                        logger.debug(
                            f"Stream connection established successfully "
                            f"(model='{attempt_model_string}')."
                        )
                        # 成功したのでキーループを抜ける
                        break
                    except (openai.RateLimitError, openai.InternalServerError) as e:
                        # エラー種別ラベルを決める
                        error_type = "Rate limit" if isinstance(e, openai.RateLimitError) else "Server"
                        # ステータスコードを取り出す
                        status_code = getattr(e, 'status_code', 'N/A')
                        logger.warning(
                            f"⚠️ {error_type} error ({status_code}) for provider '{provider_name}' "
                            f"with key index {current_key_index}. Details: {e}"
                        )
                        # 最終失敗として保持する
                        last_model_error = e
                        # 全キー使い切ったら次モデルへ回す
                        if attempt + 1 >= num_keys:
                            keys_exhausted = True
                            logger.warning(
                                f"⚠️ All {num_keys} API keys for provider '{provider_name}' "
                                f"have failed ({error_type})."
                            )
                            break
                        # 次キーへローテーションする
                        client = await self._rotate_provider_api_key(
                            client, provider_name, api_keys, current_key_index
                        )
                    except (openai.BadRequestError, openai.APIStatusError) as e:
                        # ステータスコードを取り出す
                        status_code = getattr(e, 'status_code', None)
                        if isinstance(status_code, int) and status_code >= 500:
                            logger.warning(
                                f"⚠️ Server-like status error ({status_code}) for provider '{provider_name}' "
                                f"with key index {current_key_index}. Details: {e}"
                            )
                        elif isinstance(status_code, int) and status_code >= 400:
                            logger.warning(
                                f"⚠️ Client error ({status_code}) for provider '{provider_name}' "
                                f"with key index {current_key_index}. Details: {e}"
                            )
                        else:
                            logger.warning(
                                f"⚠️ Bad request/API status error for provider '{provider_name}' "
                                f"with key index {current_key_index}. Details: {e}"
                            )
                        # 最終失敗として保持する
                        last_model_error = e
                        # 全キー使い切ったら次モデルへ回す
                        if attempt + 1 >= num_keys:
                            keys_exhausted = True
                            if status_code == 429:
                                logger.warning(
                                    f"⚠️ All {num_keys} API keys for provider '{provider_name}' "
                                    f"have failed (429)."
                                )
                            else:
                                logger.error(
                                    f"❌ All {num_keys} API keys for provider '{provider_name}' have failed."
                                )
                            break
                        # 次キーへローテーションする
                        client = await self._rotate_provider_api_key(
                            client, provider_name, api_keys, current_key_index
                        )
                    except _TRANSIENT_LLM_NETWORK_ERRORS as e:
                        logger.warning(
                            f"⚠️ Transient network error for provider '{provider_name}' "
                            f"with key index {current_key_index}. Details: {e}"
                        )
                        last_model_error = e
                        await asyncio.sleep(_LLM_TRANSIENT_RETRY_SLEEP_SEC)
                        if attempt + 1 >= num_keys:
                            keys_exhausted = True
                            logger.warning(
                                f"⚠️ All {num_keys} API keys for provider '{provider_name}' "
                                f"have failed (transient network)."
                            )
                            break
                        client = await self._rotate_provider_api_key(
                            client, provider_name, api_keys, current_key_index
                        )
                    except Exception as e:
                        logger.error(f"❌ Unhandled error calling LLM API: {e}", exc_info=True)
                        raise

                # ストリーム未確立なら次モデル／例外へ進む
                if stream is None:
                    # キー枯渇で次モデルがあるなら続行する
                    if keys_exhausted and model_idx + 1 < len(model_chain):
                        # 既定の切替理由（キー枯渇）を維持する
                        fallback_reason = "keys_exhausted"
                        continue
                    # これ以上候補が無い場合は最後の例外を投げる
                    if last_model_error is not None and model_idx + 1 >= len(model_chain):
                        raise last_model_error
                    # 例外も次モデルも無い場合はループ継続（最終検査は後段）
                    continue

                # 実使用モデルを呼び出し元クライアントへ書き戻す
                request_client.last_used_model_string = attempt_model_string
                # 実使用キー index も書き戻す（ログ用）
                request_client.last_used_key_index = getattr(client, "last_used_key_index", None)
                if model_idx > 0:
                    # 接続成功時点ではチェーンを組み替えない（空応答 continue でインデックスがずれないようにする）
                    logger.info(
                        f"✅ [MODEL FALLBACK] Connected with fallback model '{attempt_model_string}' "
                        f"(primary was '{primary_model_string}')."
                    )
                else:
                    logger.debug(
                        f"Stream connected with primary model '{attempt_model_string}'."
                    )

                # このモデルのストリーム読取結果を初期化する
                tool_calls_buffer = []
                # 本文蓄積をリセットする
                assistant_response_content = ""
                # 終了理由をリセットする
                finish_reason = None

                # ストリームからチャンクを非同期で順番に受け取るループ処理
                try:
                    async for chunk in stream:
                        # チャンク内に選択肢（choices）が含まれていない場合は処理をスキップする
                        if not chunk.choices:
                            # 次のチャンクの処理へ進む
                            continue
                        # ストリームの最初の選択肢オブジェクトを取得する
                        choice = chunk.choices[0]
                        # 選択肢に終了理由（finish_reason）が設定されているか確認する
                        if choice.finish_reason:
                            # 終了理由を後続処理のために記録しておく
                            finish_reason = choice.finish_reason
                        # 差分（delta）オブジェクトを取得する
                        delta = choice.delta
                        # 差分オブジェクトが存在し、かつ内容（content）が含まれているか判定する
                        if delta and delta.content:
                            # 抽出した文字列を格納するための変数を初期化する
                            content_str = ""
                            # 内容が通常の文字列型であるか判定する
                            if isinstance(delta.content, str):
                                # 文字列型であればそのまま内容を代入する
                                content_str = delta.content
                            # 内容がリスト型（Gemini APIなどで稀に返る形式）であるか判定する
                            elif isinstance(delta.content, list):
                                # リスト内の各要素を順番に処理してテキストを抽出する
                                for part in delta.content:
                                    # 要素が文字列型である場合
                                    if isinstance(part, str):
                                        # 文字列をそのまま結合用変数に追加する
                                        content_str += part
                                    # 要素が辞書型である場合
                                    elif isinstance(part, dict):
                                        # "text"キーの値を取り出し、なければ要素全体を文字列化して追加する
                                        content_str += part.get("text", str(part))
                                    # それ以外の型（オブジェクトなど）である場合
                                    else:
                                        # オブジェクトに"text"属性があるか確認し、取得する
                                        text_attr = getattr(part, "text", None)
                                        # "text"属性が存在する場合
                                        if text_attr is not None:
                                            # 属性の値を文字列として追加する
                                            content_str += text_attr
                                        # 属性が存在しない場合
                                        else:
                                            # 要素自体を文字列に変換して追加する
                                            content_str += str(part)
                            # 内容が辞書型であるか判定する
                            elif isinstance(delta.content, dict):
                                # "text"キーの値を取得し、なければ辞書全体を文字列化して格納する
                                content_str = delta.content.get("text", str(delta.content))
                            # 文字列、リスト、辞書のいずれでもない未知の型の場合
                            else:
                                # 安全性のためにオブジェクト全体を文字列に変換して格納する
                                content_str = str(delta.content)

                            # 抽出された文字列が空でないか確認する
                            if content_str:
                                # アシスタントの応答全体を記録する変数に文字列を追加する
                                assistant_response_content += content_str
                                # 呼び出し元へストリーミングのチャンク文字列を返却する
                                yield content_str
                        if delta and delta.tool_calls:
                            for tool_call_chunk in delta.tool_calls:
                                chunk_index = tool_call_chunk.index if tool_call_chunk.index is not None else 0
                                if len(tool_calls_buffer) <= chunk_index:
                                    tool_calls_buffer.append(
                                        {"id": "", "type": "function", "function": {"name": "", "arguments": ""}})
                                buffer = tool_calls_buffer[chunk_index]
                                if tool_call_chunk.id:
                                    buffer["id"] = tool_call_chunk.id
                                if tool_call_chunk.function:
                                    if tool_call_chunk.function.name:
                                        buffer["function"]["name"] = tool_call_chunk.function.name
                                    if tool_call_chunk.function.arguments:
                                        buffer["function"]["arguments"] += tool_call_chunk.function.arguments
                except _TRANSIENT_LLM_NETWORK_ERRORS as e:
                    if assistant_response_content or tool_calls_buffer:
                        logger.warning(
                            "⚠️ Stream interrupted after partial data "
                            "(content=%d chars, tool_calls=%d); continuing. Details: %s",
                            len(assistant_response_content),
                            len(tool_calls_buffer),
                            e,
                        )
                    else:
                        logger.error(
                            "❌ Stream interrupted before any data received: %s", e
                        )
                        raise

                # 終了理由を実クライアントへ記録する
                client.last_finish_reason = finish_reason
                # 呼び出し元クライアントへも書き戻す（空応答エラー表示用）
                request_client.last_finish_reason = finish_reason

                # ツール要求があれば履歴へ載せてモデルループを抜ける
                if tool_calls_buffer:
                    # 成功モデルを後続ツール反復の先頭に固定する
                    model_chain = [attempt_model_string] + [
                        m for m in model_chain if m != attempt_model_string
                    ]
                    # アシスタントメッセージを組み立てる
                    assistant_message = {
                        "role": "assistant",
                        "content": assistant_response_content or None,
                        "tool_calls": tool_calls_buffer,
                    }
                    # 会話履歴へ追加する
                    current_messages.append(assistant_message)
                    # ツール処理へ進む
                    break

                # 本文があれば最終応答として完了する
                if (assistant_response_content or "").strip():
                    # 成功モデルを後続反復用に固定する（念のため）
                    model_chain = [attempt_model_string] + [
                        m for m in model_chain if m != attempt_model_string
                    ]
                    # ツール無しの最終アシスタント応答を履歴へ残す
                    current_messages.append({
                        "role": "assistant",
                        "content": assistant_response_content,
                    })
                    logger.debug(
                        f"No tool calls, returning final response (Finish reason: {finish_reason})"
                    )
                    # 生成完了
                    return

                # 空応答かつ次候補があればフォールバックへ回す
                if model_idx + 1 < len(model_chain):
                    logger.warning(
                        f"⚠️ Empty response from LLM (Finish reason: {finish_reason}) "
                        f"on '{attempt_model_string}'. Falling back to next model."
                    )
                    # 次ループの切替理由を空応答にする
                    fallback_reason = "empty_response"
                    # 次モデルの接続試行へ進む
                    continue

                # 候補が尽きたので空のまま呼び出し元へ返す
                logger.warning(
                    f"⚠️ Empty response from LLM (Finish reason: {finish_reason}); "
                    "no more fallback models."
                )
                # 空アシスタントを履歴へ残す（既存挙動に合わせる）
                current_messages.append({
                    "role": "assistant",
                    "content": None,
                })
                # 呼び出し元が empty_response_error を表示する
                return

            # ストリームを一度も確立できなかった場合の最終例外
            if stream is None and not tool_calls_buffer:
                # フォールバック含めて全滅した場合の最終例外
                if last_model_error is not None:
                    raise last_model_error
                raise Exception("Failed to establish stream with any API key or fallback model.")

            # 強制最終回答中にツール要求が来ても実行せず打ち切る
            if force_final:
                # 既にストリームした本文があればそれを最終応答とする
                if assistant_response_content:
                    logger.warning(
                        "⚠️ Model requested tools during forced final answer; "
                        "ignoring tool calls and keeping streamed text"
                    )
                    return
                # 本文も無い場合はループ後のエラーメッセージへ進む
                logger.warning(
                    "⚠️ Model requested tools during forced final answer with empty content; aborting"
                )
                break

            logger.info(f"🔧 [TOOL] LLM requested {len(tool_calls_buffer)} tool call(s)")
            for tc in tool_calls_buffer:
                logger.debug(
                    f"Tool call details: {tc['function']['name']} with args: {tc['function']['arguments'][:200]}")

            tool_calls_obj = [
                SimpleNamespace(
                    id=tc['id'],
                    function=SimpleNamespace(
                        name=tc['function']['name'],
                        arguments=tc['function']['arguments']
                    )
                ) for tc in tool_calls_buffer
            ]
            await self._process_tool_calls(tool_calls_obj, current_messages, channel_id, user_id)

        logger.warning(f"⚠️ Tool processing exceeded max iterations ({max_iterations})")
        yield self.llm_config.get('error_msg', {}).get('tool_loop_timeout',
                                                       "Tool processing exceeded max iterations.\nツールの処理が最大反復回数を超えました.")

    @staticmethod
    def _messages_have_tool_results(messages: List[Dict[str, Any]]) -> bool:
        """会話履歴に tool 結果メッセージが1件でもあるか判定する。"""
        # 各メッセージを走査して role=tool を探す
        for msg in messages:
            # tool 結果があれば True
            if msg.get("role") == "tool":
                return True
        # 見つからなければ False
        return False

    async def _inject_coding_mode_prompt(
        self,
        messages: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """coding モード用の出力形式指示を system に追加したコピーを返す。"""
        prompt = coding_output_prompt(self.llm_config)
        if not prompt:
            return messages
        copied = [dict(m) for m in messages]
        copied.append({"role": "system", "content": prompt})
        return copied

    async def _maybe_send_coding_attachment(
        self,
        *,
        sent_message: discord.Message,
        channel: discord.abc.Messageable,
        full_text: str,
        route_lang: str,
        waiting_v2_cleared: bool,
    ) -> Optional[Tuple[Optional[List[discord.Message]], str, Optional[int]]]:
        """coding 応答を plain 本文 + article.md + 言語別コードファイルで送る。"""
        # 添付設定を読む
        cfg = coding_attach_settings(self.llm_config)
        # 添付条件を満たさなければ通常送信へ
        if not should_attach_coding_output(full_text, cfg):
            return None
        if not cfg.get("ask_attach_if_over", True):
            return None
        # 応答を分割する
        route_lang_norm = route_lang or "en"
        split = split_coding_response(
            full_text,
            lang=route_lang_norm,
            plain_text_max_chars=int(cfg.get("plain_text_max_chars", 500)),
        )
        # 添付対象が無ければ通常送信へ
        if not split.article_md.strip() and not split.code_files:
            return None
        # チャンネル権限を確認する
        me = channel.guild.me if getattr(channel, "guild", None) else None
        perms = channel.permissions_for(me) if me is not None else None
        can_attach = bool(perms and perms.attach_files)
        ja = str(route_lang_norm).startswith("ja")
        if not can_attach:
            notice = (
                "説明・コードをファイル添付したいですが、このチャンネルに"
                "『ファイルを添付』権限がありません。権限を付与してください。"
                if ja
                else "The explanation and code should be sent as files, but I lack "
                "the Attach Files permission in this channel. Please grant it."
            )
            sent_message = await self._replace_waiting_with_content(
                sent_message, channel, notice, track_active=True
            )
            return [sent_message], notice, None
        article_filename = str(cfg.get("article_filename", "article.md"))
        attach_note = format_coding_attachment_note(
            split,
            lang=route_lang_norm,
            article_filename=article_filename,
        )
        channel_content = f"{split.plain_text}{attach_note}"
        # 添付ファイルを組み立てる
        discord_files: List[discord.File] = []
        if split.article_md.strip():
            article_bytes = io.BytesIO(split.article_md.encode("utf-8"))
            discord_files.append(
                discord.File(article_bytes, filename=article_filename)
            )
        for filename, content in split.code_files:
            code_bytes = io.BytesIO(content.encode("utf-8"))
            discord_files.append(discord.File(code_bytes, filename=filename))
        # 履歴用に truncate
        max_chars = int(cfg.get("max_attached_text_chars", 50000))
        history_text = (
            full_text
            if len(full_text) <= max_chars
            else full_text[: max_chars - 20] + "\n...(truncated)"
        )
        try:
            if not waiting_v2_cleared:
                try:
                    await sent_message.delete()
                except Exception:
                    pass
                new_msg = await channel.send(
                    content=channel_content,
                    files=discord_files,
                    silent=True,
                )
            else:
                try:
                    await sent_message.edit(content=channel_content, embed=None, view=None)
                except Exception:
                    pass
                new_msg = await channel.send(files=discord_files, silent=True)
        except discord.Forbidden:
            notice = (
                "ファイル添付権限が不足しています。"
                if ja
                else "Missing Attach Files permission."
            )
            sent_message = await self._replace_waiting_with_content(
                sent_message, channel, notice, track_active=True
            )
            return [sent_message], notice, None
        return [new_msg], history_text, None

    async def _process_tool_calls(self, tool_calls: List[Any], messages: List[Dict[str, Any]], channel_id: int,
                                  user_id: int) -> None:
        for tool_call in tool_calls:
            raw_function_name = tool_call.function.name
            error_content = None
            tool_response_content = ""
            function_args = {}

            # ✅ Gemini の "default_api.search" → "search" に正規化
            function_name = raw_function_name.split('.')[-1] if '.' in raw_function_name else raw_function_name

            try:
                function_args = json.loads(tool_call.function.arguments)
                logger.info(f"🔧 [TOOL] Executing {raw_function_name} (normalized: {function_name})")
                logger.debug(f"🔧 [TOOL] Arguments: {json.dumps(function_args, ensure_ascii=False, indent=2)}")

                if self.search_agent and function_name == self.search_agent.name:
                    # SearchAgentはテキスト結果（str）を返す
                    tool_response_content = await self.search_agent.run(
                        arguments=function_args, bot=self.bot, channel_id=channel_id
                    )
                    logger.debug(
                        f"🔧 [TOOL] Result (length: {len(str(tool_response_content))} chars):\n{str(tool_response_content)[:1000]}")
                elif self.image_generator and function_name == self.image_generator.name:
                    tool_response_content = await self.image_generator.run(arguments=function_args,
                                                                           channel_id=channel_id)
                    logger.debug(f"🔧 [TOOL] Result:\n{tool_response_content}")
                elif self.feedback_tool and function_name == self.feedback_tool.name:
                    # フィードバック UI（form / confirm）をチャンネルへ送る
                    tool_response_content = await self.feedback_tool.run(
                        arguments=function_args,
                        channel_id=channel_id,
                        user_id=user_id,
                    )
                    logger.info("[%s] feedback tool completed", self._bot_tag())
                elif function_name == "image_generator" and self.bot_role == "companion":
                    # ARONA に画像ツールが誤って来た場合の PLANA 誘導
                    ch = self.bot.get_channel(channel_id)
                    guild = getattr(ch, "guild", None) if ch else None
                    tool_response_content = self._redirect_to_plana_message(guild)
                else:
                    logger.warning(f"⚠️ Unsupported tool called: {raw_function_name} (normalized: {function_name})")
                    error_content = f"Error: Tool '{function_name}' is not available."
            except json.JSONDecodeError as e:
                logger.error(f"❌ Error decoding tool arguments for {function_name}: {e}", exc_info=True)
                error_content = f"Error: Invalid JSON arguments - {str(e)}"
            except SearchAPIRateLimitError as e:
                logger.warning(f"⚠️ SearchAgent rate limit hit: {e}")
                error_content = "[Mistral Search Error]\nThe Mistral Search API rate limit has been reached. Please tell the user to try again later."
            except SearchAPIServerError as e:
                logger.error(f"❌ SearchAgent server error: {e}")
                error_content = "[Mistral Search Error]\nA temporary server error occurred with the search service. Please tell the user to try again later."
            except SearchAgentError as e:
                logger.error(f"❌ Error during SearchAgent execution for {function_name}: {e}", exc_info=True)
                error_content = f"[Mistral Search Error]\nAn error occurred during the search execution: {str(e)}"
            except Exception as e:
                logger.error(f"❌ Unexpected error during tool call for {function_name}: {e}", exc_info=True)
                error_content = f"[Tool Error]\nAn unexpected error occurred: {str(e)}"

            final_content = error_content if error_content else tool_response_content
            logger.debug(f"🔧 [TOOL] Sending tool response back to LLM (length: {len(final_content)} chars)")
            messages.append(
                {"tool_call_id": tool_call.id, "role": "tool", "name": function_name, "content": final_content})

    async def _schedule_model_reset(self, channel_id: int, expires_at: Optional[float] = None):
        """指定時刻（省略時は今+3時間）まで待ち、チャンネル上書きを解除する。"""
        try:
            # 期限が無ければ今から TTL 後を期限にする
            if expires_at is None:
                expires_at = time.time() + MODEL_OVERRIDE_TTL_SECONDS
            # 残り待機秒数を計算する（負なら即実行）
            delay = max(0.0, float(expires_at) - time.time())
            logger.info(
                f"[{self._bot_tag()}] Scheduled model reset for channel {channel_id} "
                f"in {delay:.0f}s (expires_at={expires_at:.0f})."
            )
            # 期限まで待機する
            await asyncio.sleep(delay)
            logger.info(f"Executing scheduled model reset for channel {channel_id}.")
            # 生エントリを直接読み、期限直後の lazy 削除に邪魔されないようにする
            channel_id_str = str(channel_id)
            bot_map = self._bot_channel_map()
            entry = bot_map.get(channel_id_str)
            current_model, _saved_expires = (
                self._parse_channel_override(entry) if entry is not None else (None, None)
            )
            # デフォルトモデルを取得する
            default_model = self._persona_default_model()
            # 上書きが残っておりデフォルトと違う場合のみクリアする
            if current_model and current_model != default_model:
                # 上書きを削除する（チャンネルへの通知は送らない）
                self._clear_channel_override(channel_id)
                # JSON へ反映する
                await self._save_channel_models()
                # サイレント復帰のため Discord 通知はせずログのみ残す
                logger.info(
                    f"[{self._bot_tag()}] Model for channel {channel_id} auto-reset to '{default_model}'."
                )
            elif entry is not None:
                # 無効／デフォルト相当の残骸があれば掃除して保存する
                self._clear_channel_override(channel_id)
                await self._save_channel_models()
        except asyncio.CancelledError:
            logger.info(f"Model reset task for channel {channel_id} was cancelled.")
        except Exception as e:
            logger.error(f"An error occurred in the model reset task for channel {channel_id}: {e}", exc_info=True)
        finally:
            self.model_reset_tasks.pop(channel_id, None)

    @app_commands.command(name="chat",
                          description="Chat with the AI without needing to mention.")
    @app_commands.describe(message="The message you want to send to the AI.",
                           image_url="URL of an image (optional).")
    async def chat_slash(self, interaction: discord.Interaction, message: str, image_url: str = None):
        await interaction.response.defer(ephemeral=False)
        temp_message = None
        try:
            llm_client = await self._get_llm_client_for_channel(interaction.channel_id)
            if not llm_client:
                # 修正点：デフォルトのエラーメッセージを一度変数に格納する
                default_error_msg = 'LLM client is not available for this channel.\nこのチャンネルではLLMクライアントが利用できません。'
                error_msg = self.llm_config.get('error_msg', {}).get('general_error', default_error_msg)

                await interaction.followup.send(
                    content=f"❌ **Error / エラー** ❌\n\n{error_msg}",  # 修正点：変数を使ってf-stringを構成する
                    view=self._create_support_view())
                return
            if not message.strip():
                await interaction.followup.send(
                    content="⚠️ **Input Required / 入力が必要です** ⚠️\n\nPlease enter a message.\nメッセージを入力してください。",
                    view=self._create_support_view())
                return
            model_in_use, image_contents = llm_client.model_name_for_api_calls, []
            if image_url:
                if image_data := await self._process_image_url(image_url):
                    image_contents.append(image_data)
                else:
                    await interaction.followup.send(
                        content="⚠️ **Image Error / 画像エラー** ⚠️\n\nFailed to process the specified image URL.\n指定された画像URLの処理に失敗しました。",
                        view=self._create_support_view())
                    return
            guild_log, user_log = f"guild='{interaction.guild.name}({interaction.guild.id})'" if interaction.guild else "guild='DM'", f"user='{interaction.user.name}({interaction.user.id})'"
            logger.info(
                f"📨 Received /chat request | {guild_log} | {user_log} | model='{model_in_use}' | text_length={len(message)} chars | images={len(image_contents)}")
            logger.info(
                f"[/chat] {interaction.guild.name if interaction.guild else 'DM'}({interaction.guild.id if interaction.guild else 0}),{interaction.user.name}({interaction.user.id})💬 [USER_INPUT] {((message[:200] + '...') if len(message) > 203 else message).replace(chr(10), ' ')}")
            system_prompt = await self._prepare_system_prompt(interaction.channel_id, interaction.user.id,
                                                              interaction.user.display_name)
            # language_prompt は _prepare_system_prompt 内で既に結合済み
            messages_for_api: List[Dict[str, Any]] = [{"role": "system", "content": system_prompt}]
            # /chat も最新発話として言語追従リマインダを付与する
            user_content_parts = [{
                "type": "text",
                "text": self._format_user_text_for_api(
                    interaction.created_at.astimezone(self.jst).strftime('[%H:%M]'),
                    message,
                    mirror_language=True,
                )
            }]
            # 添付画像があれば multimodal パートへ追加する
            user_content_parts.extend(image_contents)
            # ユーザーメッセージを API 用リストへ追加する
            messages_for_api.append({"role": "user", "content": user_content_parts})
            logger.info(f"🔵 [API] Sending {len(messages_for_api)} messages to LLM")
            # 通常応答の並列枠を確保する
            slot_held = False
            channel_id = interaction.channel_id
            if chat_limiter is not None and channel_id is not None:
                slot_held = await chat_limiter.try_acquire(channel_id, self.bot_id)
                if not slot_held:
                    busy = self.llm_config.get("error_msg", {}).get(
                        "busy_error",
                        "⚠️ 現在混雑しています。しばらくしてからもう一度お試しください。\n"
                        "⚠️ The bot is busy right now. Please try again shortly.",
                    )
                    await interaction.followup.send(content=busy, view=self._create_support_view())
                    return
            try:
                model_name = llm_client.model_name_for_api_calls
                # /chat: app → guild → en（ルーター後に上書き）
                ui_lang = resolve_interaction_lang(interaction)
                # 応答時間照合用の provider/model キー
                estimate_key = getattr(llm_client, "config_model_string", None) or model_name
                # まず router 待機
                waiting_view = self._create_waiting_view(
                    model_name,
                    lang=ui_lang,
                    phase="router",
                    estimate_model=estimate_key,
                )
                temp_message = await interaction.followup.send(
                    view=waiting_view, ephemeral=False, wait=True
                )
                # ルーター分類
                route = await run_routed_response(
                    self,
                    user_text=message,
                    channel_id=interaction.channel_id or 0,
                )
                self._last_route_mode = route.mode
                self._last_route_lang = route.lang
                ui_lang = route.lang or ui_lang
                waiting_view.lang = "ja" if str(ui_lang).startswith("ja") else "en"
                if route.mode == "unsupported":
                    text = unsupported_message(route.lang, route.reason)
                    await self._replace_waiting_with_content(
                        temp_message, interaction.channel, text, view=self._create_support_view()
                    )
                    return
                if route.mode == "command":
                    await self._refresh_waiting_view_for_phase_only(
                        temp_message,
                        waiting_view,
                        phase="command",
                    )
                    cmd_text = await execute_command_baton(
                        self,
                        user_text=message,
                        lang=route.lang,
                        channel=interaction.channel,
                        author=interaction.user,
                    )
                    await self._replace_waiting_with_content(
                        temp_message, interaction.channel, cmd_text, view=None
                    )
                    return
                if route.mode == "conversation" and self._looks_like_image_gen_request(message):
                    if self._is_image_generator_disabled():
                        notice = image_gen_disabled_message(route.lang)
                        await self._replace_waiting_with_content(
                            temp_message, interaction.channel, notice, view=self._create_support_view()
                        )
                        return
                mode_chain = mode_model_chain(self.llm_config, route.mode)
                if mode_chain:
                    mode_client = self._get_or_create_llm_client(mode_chain[0])
                    if mode_client:
                        llm_client = mode_client
                phase = "coding" if route.mode == "coding" else "conversation"
                routed_model = getattr(llm_client, "model_name_for_api_calls", model_name)
                # モード切替後クライアントの記録キーで予想時間を照合する
                routed_estimate = (
                    getattr(llm_client, "config_model_string", None) or routed_model
                )
                await self._refresh_waiting_view_for_model(
                    temp_message,
                    waiting_view,
                    routed_model,
                    phase=phase,
                    estimate_model=routed_estimate,
                )
                api_messages = messages_for_api
                if route.mode == "coding":
                    api_messages = await self._inject_coding_mode_prompt(messages_for_api)
                # スレッド作成ボタンは削除（常にFalse）
                sent_messages, full_response_text, used_key_index = await self._process_streaming_and_send_response(
                    sent_message=temp_message, channel=interaction.channel, user=interaction.user,
                    messages_for_api=api_messages, llm_client=llm_client, is_first_response=False,
                    waiting_view=waiting_view,
                    route_mode=route.mode,
                    route_lang=route.lang,
                )
                if sent_messages and full_response_text:
                    # フォールバック後の実使用モデルで完了ログを出す
                    model_in_use = self._effective_model_label(llm_client, interaction.channel_id)
                    logger.info(
                        f"✅ LLM response completed | model='{model_in_use}' | response_length={len(full_response_text)} chars")
                    log_response, key_log_str = (full_response_text[:200] + '...') if len(
                        full_response_text) > 203 else full_response_text, f" [key{used_key_index + 1}]" if used_key_index is not None else ""
                    logger.info(f"🤖 [LLM_RESPONSE][{self._bot_tag()}]{key_log_str} {log_response.replace(chr(10), ' ')}")
                    logger.debug(
                        f"LLM full response for /chat (length: {len(full_response_text)} chars):\n{full_response_text}")
                    # /chat は履歴非保存のため、メンション/リプライへ誘導する案内を末尾に付与する
                    # followup Message の .content が空のことがあるので、確定済み本文を明示的に渡す
                    last_msg = sent_messages[-1]
                    if len(sent_messages) == 1:
                        # 単一メッセージならストリーム完了後の全文を使う
                        hint_base = full_response_text
                    else:
                        # 分割送信時は最終チャンクを案内の付け先にする
                        hint_base = last_msg.content or split_message_smartly(
                            full_response_text, SAFE_MESSAGE_LENGTH
                        )[-1]
                    await self._append_chat_history_hint(last_msg, interaction.channel, hint_base)

                elif not sent_messages:
                    logger.warning("LLM response for /chat was empty or an error occurred.")
            finally:
                # 並列枠を必ず解放する
                if slot_held and chat_limiter is not None and channel_id is not None:
                    await chat_limiter.release(channel_id, self.bot_id)
        except openai.RateLimitError as e:
            # クォータ枯渇は想定内
            logger.warning("[%s] ⚠️ /chat rate limit / quota exhausted: %s", self._bot_tag(), e)
            error_msg = f"❌ **Error / エラー** ❌\n\n{self.exception_handler.handle_exception(e)}"
            try:
                if temp_message:
                    await temp_message.edit(content=error_msg, embed=None, view=self._create_support_view())
                else:
                    await interaction.followup.send(content=error_msg, view=self._create_support_view())
            except discord.HTTPException:
                pass
        except Exception as e:
            logger.error(f"❌ Error during /chat command execution: {e}", exc_info=True)
            error_msg = f"❌ **Error / エラー** ❌\n\n{self.exception_handler.handle_exception(e)}"
            try:
                if temp_message:
                    await temp_message.edit(content=error_msg, embed=None, view=self._create_support_view())
                else:
                    await interaction.followup.send(content=error_msg, view=self._create_support_view())
            except discord.HTTPException:
                pass


    async def model_autocomplete(self, interaction: discord.Interaction, current: str) -> List[
        app_commands.Choice[str]]:
        available_models = self.llm_config.get('available_models', [])
        return [app_commands.Choice(name=model, value=model) for model in available_models if
                current.lower() in model.lower()][:25]

    @app_commands.command(name="switch-models",
                          description="Switches the AI model used for this channel.")
    @app_commands.describe(model="Select the model you want to use.")
    @app_commands.autocomplete(model=model_autocomplete)
    async def switch_model_slash(self, interaction: discord.Interaction, model: str):
        await interaction.response.defer(ephemeral=False)
        available_models = self.llm_config.get('available_models', [])
        if model not in available_models:
            embed = discord.Embed(title="⚠️ Invalid Model / 無効なモデル",
                                  description=f"The specified model '{model}' is not available.\n指定されたモデル '{model}' は利用できません。",
                                  color=discord.Color.gold())
            self._add_support_footer(embed)
            await interaction.followup.send(embed=embed, view=self._create_support_view())
            return
        channel_id, default_model = interaction.channel_id, self._persona_default_model()
        # 既存タイマーをキャンセルする
        self._cancel_model_reset_task(channel_id)
        try:
            if model != default_model:
                # 3時間後の期限を計算する
                expires_at = time.time() + MODEL_OVERRIDE_TTL_SECONDS
                # 新形式で上書きを保存する
                self._set_channel_override(channel_id, model, expires_at)
                # JSON へ永続化する
                await self._save_channel_models()
                # クライアントを先に用意する
                await self._get_llm_client_for_channel(interaction.channel_id)
                # 期限付きリセットタイマーを起動する
                task = asyncio.create_task(self._schedule_model_reset(channel_id, expires_at))
                self.model_reset_tasks[channel_id] = task
                embed = discord.Embed(title="✅ Model Switched / モデルを切り替えました",
                                      description=f"The AI model for this channel has been switched to `{model}`.\nIt will automatically revert to the default model (`{default_model}`) **after 3 hours**.\nこのチャンネルのAIモデルが `{model}` に切り替えられました。\n**3時間後**にデフォルトモデル (`{default_model}`) に自動的に戻ります。",
                                      color=discord.Color.green())
                self._add_support_footer(embed)
                await interaction.followup.send(embed=embed, view=self._create_support_view(), ephemeral=False)
                logger.info(
                    f"[{self._bot_tag()}] Model for channel {channel_id} switched to '{model}' by {interaction.user.name} "
                    f"(expires_at={expires_at:.0f}).")
            else:
                # デフォルト選択時は上書きを消す
                self._clear_channel_override(channel_id)
                await self._save_channel_models()
                await self._get_llm_client_for_channel(interaction.channel_id)
                embed = discord.Embed(title="✅ Model Reset to Default / モデルをデフォルトに戻しました",
                                      description=f"The AI model for this channel has been reset to the default `{model}`.\nこのチャンネルのAIモデルがデフォルトの `{model}` に戻されました。",
                                      color=discord.Color.green())
                self._add_support_footer(embed)
                await interaction.followup.send(embed=embed, view=self._create_support_view(), ephemeral=False)
                logger.info(f"[{self._bot_tag()}] Model for channel {channel_id} switched to default '{model}'.")
        except Exception as e:
            logger.error(f"Failed to save channel model settings: {e}", exc_info=True)
            embed = discord.Embed(title="❌ Save Error / 保存エラー",
                                  description="Failed to save settings.\n設定の保存に失敗しました。",
                                  color=discord.Color.red())
            self._add_support_footer(embed)
            await interaction.followup.send(embed=embed, view=self._create_support_view())

    @app_commands.command(name="switch-models-default-server",
                          description="Resets the AI model for this channel to the server default.")
    async def reset_model_slash(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=False)
        channel_id = interaction.channel_id
        # スケジュール済みリセットをキャンセルする
        self._cancel_model_reset_task(channel_id)
        # 上書きがあれば削除する
        if self._clear_channel_override(channel_id):
            try:
                await self._save_channel_models()
                default_model = self._persona_default_model() or 'Not set / 未設定'
                embed = discord.Embed(title="✅ Model Reset to Default / モデルをデフォルトに戻しました",
                                      description=f"The AI model for this channel has been reset to the default (`{default_model}`).\nこのチャンネルのAIモデルをデフォルト (`{default_model}`) に戻しました。",
                                      color=discord.Color.green())
                self._add_support_footer(embed)
                await interaction.followup.send(embed=embed, view=self._create_support_view(), ephemeral=False)
                logger.info(f"Model for channel {interaction.channel_id} reset to default by {interaction.user.name}")
            except Exception as e:
                logger.error(f"Failed to save channel model settings after reset: {e}", exc_info=True)
                embed = discord.Embed(title="❌ Save Error / 保存エラー",
                                      description="Failed to save settings.\n設定の保存に失敗しました。",
                                      color=discord.Color.red())
                self._add_support_footer(embed)
                await interaction.followup.send(embed=embed, view=self._create_support_view())
        else:
            embed = discord.Embed(title="ℹ️ No Custom Model Set / 専用モデルはありません",
                                  description="No custom model is set for this channel.\nこのチャンネルには専用のモデルが設定されていません。",
                                  color=discord.Color.blue())
            self._add_support_footer(embed)
            await interaction.followup.send(embed=embed, view=self._create_support_view(), ephemeral=False)

    @switch_model_slash.error
    async def switch_model_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        logger.error(f"Error in /switch-model command: {error}", exc_info=True)
        error_message = f"An unexpected error occurred: {error}\n予期せぬエラーが発生しました: {error}"
        embed = discord.Embed(title="❌ Unexpected Error / 予期せぬエラー", description=error_message,
                              color=discord.Color.red())
        self._add_support_footer(embed)
        view = self._create_support_view()
        if not interaction.response.is_done():
            await interaction.response.send_message(embed=embed, view=view, ephemeral=False)
        else:
            await interaction.followup.send(embed=embed, view=view, ephemeral=False)

    async def image_model_autocomplete(self, interaction: discord.Interaction, current: str) -> List[
        app_commands.Choice[str]]:
        if not self.image_generator: return []
        available_models, current_lower = self.image_generator.get_available_models(), current.lower()
        filtered = [model for model in available_models if current_lower in model.lower()]
        if len(filtered) > 25:
            models_by_provider, choices = self.image_generator.get_models_by_provider(), []
            for provider, models in sorted(models_by_provider.items()):
                if current_lower in provider.lower():
                    for model in models[:5]:
                        if len(choices) >= 25: break
                        choices.append(app_commands.Choice(name=model, value=model))
                    if len(choices) >= 25: break
            return choices[:25]
        return [app_commands.Choice(name=model, value=model) for model in filtered][:25]

    @app_commands.command(name="switch-image-model",
                          description="Switch the image generation model for this channel.")
    @app_commands.describe(
        model="Select the image generation model you want to use.")
    @app_commands.autocomplete(model=image_model_autocomplete)
    async def switch_image_model_slash(self, interaction: discord.Interaction, model: str):
        await interaction.response.defer(ephemeral=False)
        # companion (ARONA) では画像機能を拒否して PLANA へ誘導する
        if self.bot_role == "companion":
            await interaction.followup.send(
                self._redirect_to_plana_message(interaction.guild),
                ephemeral=True,
            )
            return
        if not self.image_generator:
            embed = discord.Embed(title="❌ Plugin Error / プラグインエラー",
                                  description="ImageGenerator is not available.\nImageGeneratorが利用できません。",
                                  color=discord.Color.red())
            self._add_support_footer(embed)
            await interaction.followup.send(embed=embed, view=self._create_support_view())
            return
        # プロバイダー付き形式（provider/model_name）の場合は実際のモデル名を抽出
        actual_model = model.split('/', 1)[1] if '/' in model else model
        available_models = self.image_generator.get_available_models()
        if actual_model not in available_models:
            embed = discord.Embed(title="⚠️ Invalid Model / 無効なモデル",
                                  description=f"The specified model `{model}` is not available.\n指定されたモデル `{model}` は利用できません。",
                                  color=discord.Color.gold())
            self._add_support_footer(embed)
            await interaction.followup.send(embed=embed, view=self._create_support_view())
            return
        try:
            await self.image_generator.set_model_for_channel(interaction.channel_id, actual_model)
            default_model = self.image_generator.default_model
            try:
                provider, model_name = model.split('/', 1)
            except ValueError:
                provider, model_name = "local", model

            if model != default_model:
                embed = discord.Embed(title="✅ Image Model Switched / 画像生成モデルを切り替えました",
                                      description="The image generation model for this channel has been switched.\nこのチャンネルの画像生成モデルを切り替えました。",
                                      color=discord.Color.green())
                embed.add_field(name="New Model / 新しいモデル", value=f"```\n{model}\n```", inline=False)
                embed.add_field(name="Provider / プロバイダー", value=f"`{provider}`", inline=True)
                embed.add_field(name="Model Name / モデル名", value=f"`{model_name}`", inline=True)
                embed.add_field(name="💡 Tip / ヒント",
                                value=f"To reset to default (`{default_model}`), use `/reset-image-model`\nデフォルト (`{default_model}`) に戻すには `/reset-image-model`",
                                inline=False)
            else:
                embed = discord.Embed(title="✅ Image Model Set to Default / 画像生成モデルをデフォルトに設定しました",
                                      description="The image generation model for this channel is now the default.\nこのチャンネルの画像生成モデルがデフォルトになりました。",
                                      color=discord.Color.green())
                embed.add_field(name="Model / モデル", value=f"```\n{model}\n```", inline=False)
            self._add_support_footer(embed)
            await interaction.followup.send(embed=embed, view=self._create_support_view(), ephemeral=False)
            logger.info(
                f"Image model for channel {interaction.channel_id} switched to '{model}' by {interaction.user.name}")
        except Exception as e:
            logger.error(f"Failed to save channel image model settings: {e}", exc_info=True)
            embed = discord.Embed(title="❌ Save Error / 保存エラー",
                                  description="Failed to save settings.\n設定の保存に失敗しました。",
                                  color=discord.Color.red())
            self._add_support_footer(embed)
            await interaction.followup.send(embed=embed, view=self._create_support_view())

    @app_commands.command(name="show-image-model",
                          description="Show the current image generation model for this channel.")
    async def show_image_model_slash(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=False)
        # companion では PLANA へ誘導する
        if self.bot_role == "companion":
            await interaction.followup.send(
                self._redirect_to_plana_message(interaction.guild),
                ephemeral=True,
            )
            return
        if not self.image_generator:
            embed = discord.Embed(title="❌ Plugin Error / プラグインエラー",
                                  description="ImageGenerator is not available.\nImageGeneratorが利用できません。",
                                  color=discord.Color.red())
            self._add_support_footer(embed)
            await interaction.followup.send(embed=embed, view=self._create_support_view())
            return
        current_model, default_model, is_default = self.image_generator.get_model_for_channel(
            interaction.channel_id), self.image_generator.default_model, self.image_generator.get_model_for_channel(
            interaction.channel_id) == self.image_generator.default_model
        try:
            provider, model_name = current_model.split('/', 1)
        except ValueError:
            provider, model_name = "local", current_model

        embed = discord.Embed(title="🎨 Current Image Generation Model / 現在の画像生成モデル",
                              color=discord.Color.blue() if is_default else discord.Color.purple())
        embed.add_field(name="Current Model / 現在のモデル", value=f"```\n{current_model}\n```", inline=False)
        embed.add_field(name="Provider / プロバイダー", value=f"`{provider}`", inline=True)
        embed.add_field(name="Status / 状態", value='`Default / デフォルト`' if is_default else '`Custom / カスタム`',
                        inline=True)
        models_by_provider = self.image_generator.get_models_by_provider()
        for provider_name, models in sorted(models_by_provider.items()):
            model_list = "\n".join([f"• `{m.split('/', 1)[1]}`" for m in models[:5]])
            if len(models) > 5: model_list += f"\n• ... and {len(models) - 5} more"
            embed.add_field(name=f"📦 {provider_name.title()} Models", value=model_list or "None", inline=True)
        embed.add_field(name="💡 Commands / コマンド",
                        value="• `/switch-image-model` - Change model / モデル変更\n• `/reset-image-model` - Reset to default / デフォルトに戻す",
                        inline=False)
        self._add_support_footer(embed)
        await interaction.followup.send(embed=embed, view=self._create_support_view(), ephemeral=False)

    @app_commands.command(name="list-image-models",
                          description="List all available image generation models.")
    @app_commands.describe(provider="Filter by provider (optional).")
    async def list_image_models_slash(self, interaction: discord.Interaction, provider: str = None):
        await interaction.response.defer(ephemeral=False)
        # companion では PLANA へ誘導する
        if self.bot_role == "companion":
            await interaction.followup.send(
                self._redirect_to_plana_message(interaction.guild),
                ephemeral=True,
            )
            return
        if not self.image_generator:
            embed = discord.Embed(title="❌ Plugin Error / プラグインエラー",
                                  description="ImageGenerator is not available.\nImageGeneratorが利用できません。",
                                  color=discord.Color.red())
            self._add_support_footer(embed)
            await interaction.followup.send(embed=embed, view=self._create_support_view())
            return
        models_by_provider = self.image_generator.get_models_by_provider()
        if provider:
            provider_lower = provider.lower()
            models_by_provider = {k: v for k, v in models_by_provider.items() if provider_lower in k.lower()}
            if not models_by_provider:
                embed = discord.Embed(title="⚠️ No Models Found / モデルが見つかりません",
                                      description=f"No models found for provider: `{provider}`\nプロバイダー `{provider}` のモデルが見つかりません。",
                                      color=discord.Color.gold())
                self._add_support_footer(embed)
                await interaction.followup.send(embed=embed, view=self._create_support_view())
                return
        total_models = sum(len(models) for models in models_by_provider.values())
        embed = discord.Embed(title="🎨 Available Image Generation Models / 利用可能な画像生成モデル",
                              description=f"Total: {total_models} models across {len(models_by_provider)} provider(s)\n合計: {len(models_by_provider)}プロバイダー、{total_models}モデル",
                              color=discord.Color.blue())
        for provider_name, models in sorted(models_by_provider.items()):
            # モデル名からプロバイダー部分を除去（表示用）
            model_names = [m.split('/', 1)[1] if '/' in m else m for m in models]
            if len(model_names) > 10:
                model_text = "\n".join([f"{i + 1}. `{m}`" for i, m in enumerate(model_names[:10])])
                model_text += f"\n... and {len(model_names) - 10} more"
            else:
                model_text = "\n".join([f"{i + 1}. `{m}`" for i, m in enumerate(model_names)])
            embed.add_field(name=f"📦 {provider_name.title()} ({len(models)} models)", value=model_text or "None",
                            inline=False)
        embed.add_field(name="💡 How to Use / 使い方",
                        value="Use `/switch-image-model` to change the model for this channel.\n`/switch-image-model` でこのチャンネルのモデルを変更できます。",
                        inline=False)
        self._add_support_footer(embed)
        await interaction.followup.send(embed=embed, view=self._create_support_view(), ephemeral=False)

    @switch_image_model_slash.error
    async def switch_image_model_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        logger.error(f"Error in /switch-image-model command: {error}", exc_info=True)
        error_message = f"An unexpected error occurred: {error}\n予期せぬエラーが発生しました: {error}"
        embed = discord.Embed(title="❌ Unexpected Error / 予期せぬエラー", description=error_message,
                              color=discord.Color.red())
        self._add_support_footer(embed)
        view = self._create_support_view()
        if not interaction.response.is_done():
            await interaction.response.send_message(embed=embed, view=view, ephemeral=False)
        else:
            await interaction.followup.send(embed=embed, view=view, ephemeral=False)

    @app_commands.command(name="clear_history",
                          description="Clears the history of the current conversation thread.")
    async def clear_history_slash(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=False)
        guild_id = interaction.guild.id if interaction.guild else 0  # DMの場合は0
        cleared_count, threads_to_clear = 0, set()
        
        try:
            async for msg in interaction.channel.history(limit=200):
                if guild_id in self.message_to_thread and msg.id in self.message_to_thread[guild_id]: 
                    threads_to_clear.add(self.message_to_thread[guild_id][msg.id])
        except (discord.Forbidden, discord.HTTPException):
            embed = discord.Embed(title="⚠️ Permission Error / 権限エラー",
                                  description="Could not read the channel's message history.\nチャンネルのメッセージ履歴を読み取れませんでした。",
                                  color=discord.Color.gold())
            self._add_support_footer(embed)
            await interaction.followup.send(embed=embed, view=self._create_support_view())
            return
        
        for thread_id in threads_to_clear:
            if guild_id in self.conversation_threads and thread_id in self.conversation_threads[guild_id]:
                del self.conversation_threads[guild_id][thread_id]
                if guild_id in self.message_to_thread:
                    self.message_to_thread[guild_id] = {
                        k: v for k, v in self.message_to_thread[guild_id].items() 
                        if v != thread_id
                    }
                cleared_count += 1
        
        if cleared_count > 0:
            embed = discord.Embed(title="✅ History Cleared / 履歴をクリアしました",
                                  description=f"Cleared the history of {cleared_count} conversation thread(s) related to this channel.\nこのチャンネルに関連する {cleared_count} 個の会話スレッドの履歴をクリアしました。",
                                  color=discord.Color.green())
            self._add_support_footer(embed)
            await interaction.followup.send(embed=embed, view=self._create_support_view())
        else:
            embed = discord.Embed(title="ℹ️ No History Found / 履歴がありません",
                                  description="No conversation history to clear was found.\nクリア対象の会話履歴が見つかりませんでした。",
                                  color=discord.Color.blue())
            self._add_support_footer(embed)
            await interaction.followup.send(embed=embed, view=self._create_support_view())


async def setup(bot: commands.Bot):
    """Sets up the LLMCog."""
    try:
        await bot.add_cog(LLMCog(bot))
        logger.info("LLMCog loaded successfully.")
    except Exception as e:
        logger.critical(f"Failed to set up LLMCog: {e}", exc_info=True)
        raise