# command モード実行（Music は公開 hybrid_command 経路を利用）。
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, TYPE_CHECKING

import discord
from discord.ext import commands

from MOMOKA.llm.router.commands_catalog import (
    find_command_help,
    format_catalog_for_prompt,
    load_commands_catalog,
)
from MOMOKA.llm.router.json_extract import extract_completion_text, parse_llm_json_object

if TYPE_CHECKING:
    from MOMOKA.llm.llm_cog import LLMCog

logger = logging.getLogger(__name__)

# command モードが LLM に求める JSON スキーマ説明
_COMMAND_SYSTEM = """You are a Discord bot command helper.
Given the user request and the available slash commands catalog, decide ONE action.

Output ONLY a JSON object (no Markdown):
{
  "action": "execute" | "explain" | "list" | "unsupported",
  "command": "slash-command-name-without-slash-or-null",
  "parameters": {"query": "..."},
  "reply": "user-facing message in the user's language"
}

Rules:
- "execute": only for music commands: play, pause, resume, skip, stop, leave, queue.
  For play, parameters.query is required (URL or search text).
- "explain": user asks how to use a command; put help text in reply.
- "list": user asks what commands exist; summarize in reply using the catalog.
- "unsupported": cannot fulfill; explain honestly in reply. Never pretend success.
"""


class AgentMusicContext:
    """MusicCog 公開メソッド向けの薄い Context 互換オブジェクト。"""

    def __init__(
        self,
        bot: commands.Bot,
        channel: discord.abc.Messageable,
        guild: discord.Guild,
        author: discord.Member,
    ) -> None:
        # Bot 参照
        self.bot = bot
        # 送信先チャンネル
        self.channel = channel
        # ギルド
        self.guild = guild
        # 実行者（Member 必須・voice 属性のため）
        self.author = author
        # スラッシュではないので interaction は無し
        self.interaction = None
        # defer 済みフラグ
        self.deferred = False

    async def defer(self, *args: Any, **kwargs: Any) -> None:
        # プレフィックス相当なので no-op
        self.deferred = True

    async def send(self, content: Optional[str] = None, **kwargs: Any) -> discord.Message:
        # チャンネルへ通常送信（MusicCog の _send_ctx_message が使う）
        return await self.channel.send(content=content, **kwargs)


def _parse_json(raw: str) -> Optional[Dict[str, Any]]:
    """モデル出力から JSON を取り出す（thought 除去込み）。"""
    # 共有抽出ロジックへ委譲する
    return parse_llm_json_object(raw)


async def _invoke_music(
    bot: commands.Bot,
    *,
    command_name: str,
    parameters: Dict[str, Any],
    channel: discord.TextChannel,
    member: discord.Member,
    lang: str,
) -> str:
    """MusicCog の公開コマンドを呼ぶ。"""
    # MusicCog 取得
    music_cog = bot.get_cog("MusicCog") or bot.get_cog("music_cog")
    if not music_cog:
        return (
            "音楽機能が利用できません。"
            if lang.startswith("ja")
            else "Music is unavailable."
        )
    # Context 互換を組み立てる
    ctx = AgentMusicContext(bot, channel, channel.guild, member)
    name = (command_name or "").strip().lower()
    try:
        if name == "play":
            query = str(parameters.get("query") or "").strip()
            if not query:
                return (
                    "/play には曲名か URL が必要です。"
                    if lang.startswith("ja")
                    else "/play requires a query or URL."
                )
            # 公開 play を呼ぶ
            await music_cog.play(ctx, query=query)
            return (
                f"再生を開始しました: {query}"
                if lang.startswith("ja")
                else f"Started playback: {query}"
            )
        if name == "pause":
            await music_cog.pause(ctx)
            return "一時停止しました。" if lang.startswith("ja") else "Paused."
        if name == "resume":
            await music_cog.resume(ctx)
            return "再開しました。" if lang.startswith("ja") else "Resumed."
        if name == "skip":
            await music_cog.skip(ctx)
            return "スキップしました。" if lang.startswith("ja") else "Skipped."
        if name == "stop":
            await music_cog.stop(ctx)
            return "停止しました。" if lang.startswith("ja") else "Stopped."
        if name == "leave":
            await music_cog.leave(ctx)
            return "VC から退出しました。" if lang.startswith("ja") else "Left the voice channel."
        if name == "queue":
            await music_cog.queue(ctx)
            return "キューを表示しました。" if lang.startswith("ja") else "Showed the queue."
        return (
            f"未対応の音楽コマンドです: {name}"
            if lang.startswith("ja")
            else f"Unsupported music command: {name}"
        )
    except Exception as e:
        logger.error("command_executor music %s failed: %s", name, e, exc_info=True)
        return (
            f"コマンド実行中にエラーが発生しました: {e}"
            if lang.startswith("ja")
            else f"Error while running the command: {e}"
        )


async def run_command_mode(
    cog: "LLMCog",
    *,
    user_text: str,
    lang: str,
    channel: discord.abc.Messageable,
    author: discord.abc.User,
    messages_for_api: Optional[List[Dict[str, Any]]] = None,
) -> str:
    """command モード: カタログを渡し LLM が execute/explain を選び、必要なら Music を実行。"""
    # カタログ取得
    catalog = load_commands_catalog(cog.bot, lang)
    catalog_text = format_catalog_for_prompt(catalog)
    # modes.command 設定
    modes = (cog.llm_config.get("modes") or {}).get("command") or {}
    primary = modes.get("model") or "nvidia_nim/minimaxai/minimax-m3"
    chain: List[str] = [str(primary)]
    for m in modes.get("fallback_models") or []:
        if m and str(m) not in chain:
            chain.append(str(m))
    # システム＋ユーザー
    system = (
        _COMMAND_SYSTEM
        + "\n\n# Available commands\n"
        + (catalog_text or "(no catalog)")
        + f"\n\nRespond in language code: {lang}"
    )
    api_messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user_text[:4000]},
    ]
    raw = ""
    for model_string in chain:
        try:
            client = cog._get_or_create_llm_client(model_string)
            if not client:
                continue
            converted = cog._ensure_messages_for_model(api_messages, model_string)
            # 同一プロバイダーの全キーを試してから次モデルへ
            resp, client = await cog._chat_completion_with_key_rotation(
                client,
                messages=converted,
                max_tokens=1024,
                temperature=0.1,
            )
            if resp.choices:
                raw = extract_completion_text(resp)
            if raw:
                break
        except Exception as e:
            logger.warning("[%s] command model %s failed: %s", cog._bot_tag(), model_string, e)
            continue
    data = _parse_json(raw) or {}
    action = str(data.get("action") or "unsupported").lower()
    reply = str(data.get("reply") or "").strip()
    command_name = str(data.get("command") or "").strip().lstrip("/")
    parameters = data.get("parameters") if isinstance(data.get("parameters"), dict) else {}

    # list / explain はカタログで補強
    if action == "list" and not reply:
        reply = catalog_text or ("コマンドがありません。" if lang.startswith("ja") else "No commands.")
    if action == "explain":
        hit = find_command_help(catalog, command_name or user_text)
        if hit and not reply:
            reply = f"/{hit['name']}: {hit['description']}" if hit["description"] else f"/{hit['name']}"
        if not reply:
            reply = (
                "該当コマンドの説明が見つかりませんでした。"
                if lang.startswith("ja")
                else "Could not find help for that command."
            )

    if action == "execute":
        # TextChannel + Member が必要
        if not isinstance(channel, discord.TextChannel) or channel.guild is None:
            return (
                "このチャンネルではコマンドを実行できません。"
                if lang.startswith("ja")
                else "Cannot run commands in this channel."
            )
        member = channel.guild.get_member(author.id)
        if member is None:
            try:
                member = await channel.guild.fetch_member(author.id)
            except Exception:
                member = None
        if member is None:
            return (
                "ユーザー情報を取得できませんでした。"
                if lang.startswith("ja")
                else "Could not resolve the member."
            )
        result = await _invoke_music(
            cog.bot,
            command_name=command_name,
            parameters=parameters,
            channel=channel,
            member=member,
            lang=lang,
        )
        # LLM の reply があれば添える
        if reply:
            return f"{result}\n{reply}"
        return result

    if reply:
        return reply
    return (
        "その操作には対応していません。"
        if lang.startswith("ja")
        else "That request is not supported."
    )
