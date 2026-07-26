# 討論・クロスチェック Orchestrator（プロセス内協調）。
from __future__ import annotations

import asyncio
import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Tuple, TYPE_CHECKING

import discord

from MOMOKA.bots.registry import registry
from MOMOKA.llm.concurrency import debate_limiter
from MOMOKA.llm.debate.accents import initiator_accent_color
from MOMOKA.llm.debate.channel_lock import channel_lock
from MOMOKA.llm.debate.stop_view import DebateStopView
from MOMOKA.utilities.locale import resolve_guild_lang

if TYPE_CHECKING:
    from discord.ext import commands

logger = logging.getLogger(__name__)

# LLM 出力から誤って含まれるメンション markup を除去する
_MENTION_RE = re.compile(r"<@!?\d+>")

# 発言先頭の [HH:MM] 時刻プレフィックスを落とす（通常チャット履歴由来のズレ防止）
_LEADING_TIME_RE = re.compile(r"^\[\d{1,2}:\d{2}\]\s*")

# 討論・cross_check 共通: 壁時計・API遅延による時刻言及を抑止する
_CLOCK_STABILITY_NOTE = """
# Timing (system — critical)
- A fixed session clock may appear in context. Treat it as static scenery only.
- Do NOT mention the current time, clocks, or that time has passed since the last turn.
- Apparent time gaps between turns are only API latency — never comment on them.
- Debate / review the substance only.
""".strip()


def _jst_now() -> datetime:
    """JST の現在時刻を返す。"""
    # JST タイムゾーン
    jst = timezone(timedelta(hours=9))
    # 現在時刻
    return datetime.now(jst)


def _freeze_clock() -> Tuple[str, str]:
    """セッション開始時の日付・時刻文字列を固定する。"""
    # 今を取る
    now = _jst_now()
    # 日付と時刻を返す
    return now.strftime("%Y-%m-%d"), now.strftime("%H:%M")


@dataclass
class DebateSession:
    """進行中の討論セッション状態。"""

    session_id: str
    channel_id: int
    guild_id: int
    topic: str
    starter_user_id: int
    max_rounds: int
    status: str = "running"  # running | cancelled | finished
    current_round: int = 0
    transcript: List[Dict[str, str]] = field(default_factory=list)
    panel_message: Optional[discord.Message] = None
    positions: Dict[str, str] = field(default_factory=dict)
    # セッション開始時に固定した日付・時刻（ターン間で変えない）
    frozen_date: str = ""
    frozen_time: str = ""
    # 討論を起動した Bot（plana / arona）— 先攻にも使う
    initiator_bot_id: str = "plana"
    # 直前の投稿（次ターンの Discord reply 先）
    last_post: Optional[discord.Message] = None
    # パネル UI 言語（guild locale → en）
    lang: str = "en"


class DebateOrchestrator:
    """PLANA ↔ ARONA 討論と cross_check を駆動する。"""

    def __init__(self, config: Dict[str, Any]) -> None:
        # マージ済み全体 config
        self.config = config
        # debate セクション
        self.debate_cfg = (config.get("debate") or {})
        # cross_check セクション
        self.cross_cfg = (config.get("cross_check") or {})
        # 管理者 ID
        self.admin_ids = set(config.get("admin_user_ids") or [])
        # 実行中バックグラウンドタスク
        self._tasks: Dict[str, asyncio.Task] = {}

    # ------------------------------------------------------------------
    # 公開 API: debate（即返し + バックグラウンド完走）
    # ------------------------------------------------------------------
    async def start_debate(
        self,
        *,
        channel: discord.abc.Messageable,
        guild: Optional[discord.Guild],
        topic: str,
        starter_user_id: int,
        initiator_bot_id: str = "plana",
        position_plana: Optional[str] = None,
        position_arona: Optional[str] = None,
    ) -> str:
        """討論を開始する。ツール結果は即時文字列。実処理は Task。"""
        # 無効なら拒否
        if not self.debate_cfg.get("enabled", True):
            return "Debate is disabled in config."
        # チャンネル ID
        channel_id = getattr(channel, "id", None)
        if channel_id is None:
            return "Debate requires a text channel."
        # ギルド必須
        if guild is None:
            return "Debate is only available in guild channels."
        # 起動 Bot を正規化する
        initiator = (initiator_bot_id or "plana").lower()
        if initiator not in ("plana", "arona"):
            initiator = "plana"
        # 相方 Bot（起動側の反対）
        partner_id = "arona" if initiator == "plana" else "plana"
        # プロセス全体の同時討論上限（チャンネル占有の前に取る）
        debate_slot = False
        if debate_limiter is not None:
            debate_slot = await debate_limiter.try_acquire()
            if not debate_slot:
                limit = debate_limiter.max_concurrent
                return (
                    f"Too many debates running (limit={limit}). "
                    "Please try again later. "
                    f"同時討論数が上限（{limit}）に達しています。しばらくしてから再度お試しください。"
                )
        # チャンネル排他
        acquired = await channel_lock.try_acquire(channel_id, "debate")
        if not acquired:
            # 討論枠だけ先に取っていたら戻す
            if debate_slot and debate_limiter is not None:
                await debate_limiter.release()
            owner = channel_lock.owner(channel_id) or "another session"
            return f"This channel is busy with '{owner}'. Wait until it finishes."
        # 相方在籍チェック（起動側ではなく partner を見る）
        partner_ok, partner_msg = await self._ensure_partner_in_guild(guild, partner_id)
        if not partner_ok:
            # ロック解放
            await channel_lock.release(channel_id)
            if debate_slot and debate_limiter is not None:
                await debate_limiter.release()
            return partner_msg
        # ポジション既定
        pos_p = position_plana or "cautious / skeptical perspective"
        pos_a = position_arona or "optimistic / proactive perspective"
        # セッション開始時の時計を固定する（ターン間ズレ防止）
        frozen_date, frozen_time = _freeze_clock()
        # パネル UI 言語（メッセージ起点のため guild → en）
        ui_lang = resolve_guild_lang(guild)
        # セッション作成
        session = DebateSession(
            session_id=uuid.uuid4().hex[:12],
            channel_id=channel_id,
            guild_id=guild.id,
            topic=topic,
            starter_user_id=starter_user_id,
            max_rounds=int(self.debate_cfg.get("max_rounds", 2)),
            positions={"plana": pos_p, "arona": pos_a},
            frozen_date=frozen_date,
            frozen_time=frozen_time,
            initiator_bot_id=initiator,
            lang=ui_lang,
        )
        try:
            # パネル View を作る（開会文・中止案内はパネル内に集約し、通常メッセージは送らない）
            view = DebateStopView(
                session=session,
                admin_ids=self.admin_ids,
                on_stop=self._cancel_session,
                lang=ui_lang,
            )
            # パネル投稿（起動 Bot の Client 経由）
            panel = await channel.send(view=view)
            # 参照を保存
            session.panel_message = panel
            # 最初の討論ターンはパネルへ reply する
            session.last_post = panel
        except Exception as e:
            # 失敗時はロック解放
            await channel_lock.release(channel_id)
            if debate_slot and debate_limiter is not None:
                await debate_limiter.release()
            logger.error("Failed to post debate panel: %s", e, exc_info=True)
            return f"Failed to start debate UI: {e}"
        # バックグラウンドで討論を走らせる
        task = asyncio.create_task(
            self._run_debate(session, channel),
            name=f"debate-{session.session_id}",
        )
        self._tasks[session.session_id] = task
        # ツール結果は即返し（長時間ブロックしない）
        return (
            f"Debate started (session={session.session_id}). "
            f"Topic: {topic}. "
            f"First speaker: {registry.display_name(initiator)}. "
            "A stop button is posted in the channel. "
            "Debate turns are posted by each bot as Discord replies — do not write debate arguments yourself. "
            "Tell Sensei briefly that the debate has started."
        )

    async def _cancel_session(self, session: DebateSession) -> None:
        """中止ボタンから呼ばれる。"""
        # ステータスをキャンセルへ
        session.status = "cancelled"
        # ログ
        logger.info(
            "[%s] Debate cancelled by user (session=%s)",
            "DEBATE",
            session.session_id,
        )

    async def _run_debate(
        self,
        session: DebateSession,
        channel: discord.abc.Messageable,
    ) -> None:
        """交互討論 → 評定 → 終了。"""
        try:
            # ラウンドループ
            for r in range(1, session.max_rounds + 1):
                # キャンセル確認
                if session.status == "cancelled":
                    break
                # 進捗更新
                session.current_round = r
                await self._refresh_panel(session)
                # 先攻は起動 Bot、次に相方（ARONA 開始なら ARONA→PLANA）
                first = session.initiator_bot_id or "plana"
                second = "arona" if first == "plana" else "plana"
                # 先攻ターン
                await self._debate_turn(session, channel, first, r)
                if session.status == "cancelled":
                    break
                # 相手が読む／投稿する隙間を空ける
                delay = float(self.debate_cfg.get("post_delay_sec", 1.5))
                await asyncio.sleep(delay)
                # 後攻ターン
                await self._debate_turn(session, channel, second, r)
                if session.status == "cancelled":
                    break
                # ラウンド間も同じ間隔
                await asyncio.sleep(delay)
            # 自然終了なら評定
            if session.status == "running":
                await self._judge_turn(session, channel)
                session.status = "finished"
            # パネル最終更新
            await self._refresh_panel(session)
            # 中断告知
            if session.status == "cancelled":
                await channel.send("討論は中断されました。 / Debate was cancelled.")
        except Exception as e:
            logger.error("Debate run failed: %s", e, exc_info=True)
            try:
                await channel.send(f"討論中にエラーが発生しました: {e}")
            except Exception:
                pass
            session.status = "cancelled"
        finally:
            # チャンネルロック解放
            await channel_lock.release(session.channel_id)
            # グローバル討論枠解放
            if debate_limiter is not None:
                await debate_limiter.release()
            # タスク辞書から除去
            self._tasks.pop(session.session_id, None)

    async def _refresh_panel(self, session: DebateSession) -> None:
        """パネル View を再構築して edit。"""
        # メッセージが無ければスキップ
        if session.panel_message is None:
            return
        # 新しい View（セッション保存言語を維持）
        view = DebateStopView(
            session=session,
            admin_ids=self.admin_ids,
            on_stop=self._cancel_session,
            lang=getattr(session, "lang", "en"),
        )
        try:
            await session.panel_message.edit(view=view)
        except Exception as e:
            logger.warning("Panel refresh failed: %s", e)

    async def _debate_turn(
        self,
        session: DebateSession,
        channel: discord.abc.Messageable,
        speaker_id: str,
        round_i: int,
    ) -> None:
        """1 発言者の討論ターン。"""
        # システムプロンプト（討論人格）
        system = self._build_debate_system(speaker_id, session)
        # 相手の直前発言（先頭の [HH:MM] は除去）
        opponent_text = self._strip_leading_time(
            self._last_opponent_text(session, speaker_id)
        )
        # ターン指示
        turn_tmpl = self.debate_cfg.get("turn_instruction") or ""
        turn_inst = turn_tmpl.format(
            round=round_i,
            max_rounds=session.max_rounds,
            max_chars_per_turn=self.debate_cfg.get("max_chars_per_turn", 800),
        )
        # 時刻言及抑止をターン指示へ足す
        turn_inst = f"{turn_inst}\nDo not mention clocks or time gaps between turns."
        # user メッセージ
        if opponent_text:
            user_content = f"{opponent_text}\n\n{turn_inst}"
        else:
            user_content = (
                f"Debate topic: {session.topic}\n"
                f"Open the debate from your position.\n\n{turn_inst}"
            )
        # LLM 呼び出し
        text = await self._generate(
            speaker_id=speaker_id,
            system=system,
            user_content=user_content,
            max_chars=int(self.debate_cfg.get("max_chars_per_turn", 800)),
            tag="DEBATE",
        )
        # メンション付与して投稿（直前ターンへの reply）
        partner = "arona" if speaker_id == "plana" else "plana"
        posted, msg = await self._post_with_mention(
            channel,
            speaker_id,
            partner,
            text,
            reply_to=session.last_post,
        )
        # 次ターンの reply 先を更新する
        if msg is not None:
            session.last_post = msg
        # transcript へ
        session.transcript.append(
            {"speaker": speaker_id, "text": posted, "round": str(round_i)}
        )

    async def _judge_turn(
        self,
        session: DebateSession,
        channel: discord.abc.Messageable,
    ) -> None:
        """評定ターン（デフォルト PLANA）。メンションなし。"""
        # 評定担当
        judge_id = self.debate_cfg.get("judge_bot", "plana")
        # 評定プロンプト
        judge_tmpl = self.debate_cfg.get("judge_prompt") or ""
        # transcript 文字列
        lines = []
        for entry in session.transcript:
            lines.append(f"[{entry['speaker'].upper()}] {entry['text']}")
        transcript_text = "\n".join(lines)
        # 評定用 system（討論人格ではなく judge 指示）+ 時刻言及抑止
        system = (
            judge_tmpl.format(topic=session.topic)
            + "\n\n"
            + _CLOCK_STABILITY_NOTE
        )
        user_content = f"Transcript:\n{transcript_text}"
        # 生成
        text = await self._generate(
            speaker_id=judge_id,
            system=system,
            user_content=user_content,
            max_chars=1200,
            tag="DEBATE_JUDGE",
        )
        # メンション markup を除去する
        clean = _MENTION_RE.sub("", text).strip()
        # 投稿先チャンネルを解決する
        bot = registry.require(judge_id)
        ch = bot.get_channel(session.channel_id) or channel
        # 評定は Embed で目立たせる（起動 Bot 色: PLANA 紫 / ARONA 水色）
        embed = discord.Embed(
            title="⚖️ 評定 / Verdict",
            description=clean[:4096] if clean else "（評定を生成できませんでした）",
            color=initiator_accent_color(session.initiator_bot_id),
        )
        # テーマをフィールドに載せる
        topic_text = (session.topic or "")[:1024]
        if topic_text:
            embed.add_field(name="テーマ / Topic", value=topic_text, inline=False)
        # 司会 Bot 名をフッターへ
        embed.set_footer(text=f"Moderator: {registry.display_name(judge_id)}")
        # Embed を投稿する
        await ch.send(embed=embed)
        # transcript へ残す
        session.transcript.append(
            {"speaker": f"judge:{judge_id}", "text": clean, "round": "judge"}
        )

    # ------------------------------------------------------------------
    # cross_check（同期: Step1/2 投稿、戻り値は検証コメント）
    # ------------------------------------------------------------------
    async def run_cross_check(
        self,
        *,
        channel: discord.abc.Messageable,
        guild: Optional[discord.Guild],
        question: str,
        draft_answer: str,
        initiator_bot_id: str = "plana",
    ) -> str:
        """Step1/2 を投稿し、検証全文を返す（Step3 は LLM ループ側）。

        起動 Bot が一次案、相方が検証。PLANA 起点なら ARONA 検証、逆も可。
        """
        # 無効なら拒否
        if not self.cross_cfg.get("enabled", True):
            return "cross_check is disabled in config."
        channel_id = getattr(channel, "id", None)
        if channel_id is None:
            return "cross_check requires a text channel."
        # 起動 Bot / 検証役を決める
        draft_bot = (initiator_bot_id or "plana").lower()
        if draft_bot not in ("plana", "arona"):
            draft_bot = "plana"
        # 既定 reviewer は相方（config の reviewer_bot は PLANA 起点時の既定）
        default_reviewer = self.cross_cfg.get("reviewer_bot", "arona")
        if draft_bot == "plana":
            reviewer_id = default_reviewer if default_reviewer != "plana" else "arona"
        else:
            reviewer_id = "plana"
        # 排他
        acquired = await channel_lock.try_acquire(channel_id, "cross_check")
        if not acquired:
            owner = channel_lock.owner(channel_id) or "another session"
            return f"This channel is busy with '{owner}'. Cannot cross_check now."
        try:
            # セッション時計を固定する（Step1/2 で同じ日時）
            frozen_date, frozen_time = _freeze_clock()
            max_chars = int(self.cross_cfg.get("max_chars_review", 600))
            # draft 本文（引数を優先。空なら生成）
            draft = (draft_answer or "").strip()
            if not draft:
                draft = await self._generate_cross_draft(
                    question,
                    max_chars,
                    frozen_date,
                    frozen_time,
                    draft_bot=draft_bot,
                    reviewer_bot=reviewer_id,
                )
            # Step1 投稿（cross_check はメンション可）
            draft_msg: Optional[discord.Message] = None
            if guild is not None:
                _, draft_msg = await self._post_with_mention(
                    channel,
                    draft_bot,
                    reviewer_id,
                    draft,
                    add_mention=bool(self.cross_cfg.get("mention_each_other", True)),
                )
            else:
                draft_msg = await channel.send(draft)
            # 相方在籍
            partner_present = False
            invite_msg = ""
            if guild is not None:
                partner_present, invite_msg = await self._ensure_partner_in_guild(
                    guild, reviewer_id
                )
            # Step2: 検証（同じ固定時計）
            reviewer_name = registry.display_name(reviewer_id)
            if partner_present:
                review = await self._generate_cross_review(
                    question,
                    draft,
                    max_chars,
                    frozen_date,
                    frozen_time,
                    reviewer_bot=reviewer_id,
                )
                await self._post_with_mention(
                    channel,
                    reviewer_id,
                    draft_bot,
                    review,
                    reply_to=draft_msg,
                    add_mention=False,
                )
            else:
                # フォールバック: プロセス内で相方 persona により検証（投稿省略）
                review = await self._generate_cross_review(
                    question,
                    draft,
                    max_chars,
                    frozen_date,
                    frozen_time,
                    reviewer_bot=reviewer_id,
                )
                review = (
                    f"[{reviewer_name} offline review — not posted]\n{review}\n"
                    f"(Partner not in guild. {invite_msg})"
                )
            # tool 戻り値 = 検証全文（Step3 は LLM 最終応答のみ）
            return review
        finally:
            # ロック解放
            await channel_lock.release(channel_id)

    async def _generate_cross_draft(
        self,
        question: str,
        max_chars: int,
        frozen_date: str,
        frozen_time: str,
        *,
        draft_bot: str = "plana",
        reviewer_bot: str = "arona",
    ) -> str:
        """Step1 用ドラフト生成（通常 persona・固定時計）。"""
        # 指示テンプレ（起動/検証役名を埋める）
        tmpl = self.cross_cfg.get("draft_instruction") or ""
        draft_name = registry.display_name(draft_bot)
        reviewer_name = registry.display_name(reviewer_bot)
        try:
            instruction = tmpl.format(
                max_chars_review=max_chars,
                draft_name=draft_name,
                reviewer_name=reviewer_name,
            )
        except KeyError:
            # 旧テンプレ互換（PLANA 固定文言）
            instruction = tmpl.format(max_chars_review=max_chars)
            instruction = instruction.replace("as PLANA", f"as {draft_name}")
        # 通常 persona system（時計固定）+ 時刻言及抑止
        system = (
            self._normal_persona_system(
                draft_bot, frozen_date=frozen_date, frozen_time=frozen_time
            )
            + "\n\n"
            + instruction
            + "\n\n"
            + _CLOCK_STABILITY_NOTE
        )
        return await self._generate(
            speaker_id=draft_bot,
            system=system,
            user_content=question,
            max_chars=max_chars,
            tag="CROSS_CHECK",
        )

    async def _generate_cross_review(
        self,
        question: str,
        draft: str,
        max_chars: int,
        frozen_date: str,
        frozen_time: str,
        *,
        reviewer_bot: str = "arona",
    ) -> str:
        """Step2 検証生成（通常 persona・固定時計）。"""
        # 検証対象（相方）の表示名
        draft_bot = "plana" if reviewer_bot == "arona" else "arona"
        draft_name = registry.display_name(draft_bot)
        reviewer_name = registry.display_name(reviewer_bot)
        # 呼び方（既存口調に合わせる）
        draft_address = "「プラナちゃん」" if draft_bot == "plana" else "「アロナ」"
        tmpl = self.cross_cfg.get("review_prompt") or ""
        try:
            system = tmpl.format(
                question=question,
                draft_answer=draft,
                max_chars_review=max_chars,
                draft_name=draft_name,
                reviewer_name=reviewer_name,
                draft_address=draft_address,
            )
        except KeyError:
            # 旧テンプレ互換
            system = tmpl.format(
                question=question,
                draft_answer=draft,
                max_chars_review=max_chars,
            )
        # 検証役の通常 persona を前置きする
        persona = self._normal_persona_system(
            reviewer_bot, frozen_date=frozen_date, frozen_time=frozen_time
        )
        full_system = f"{persona}\n\n{system}\n\n{_CLOCK_STABILITY_NOTE}"
        return await self._generate(
            speaker_id=reviewer_bot,
            system=full_system,
            user_content="Please review the draft above.",
            max_chars=max_chars,
            tag="CROSS_CHECK",
        )

    # ------------------------------------------------------------------
    # ヘルパ
    # ------------------------------------------------------------------
    def _format_opening(self, topic: str) -> str:
        """開会文（日英）。"""
        ja = (self.debate_cfg.get("opening_notice_ja") or "").format(topic=topic)
        en = (self.debate_cfg.get("opening_notice_en") or "").format(topic=topic)
        return f"{ja.strip()}\n{en.strip()}".strip()

    def _build_debate_system(self, speaker_id: str, session: DebateSession) -> str:
        """討論人格 system（通常 persona・tools なし）。"""
        rules_tmpl = self.debate_cfg.get("debate_rules") or ""
        position = session.positions.get(speaker_id, "free discussion from your own perspective")
        rules = rules_tmpl.format(topic=session.topic, position=position)
        persona_key = f"debate_persona_{speaker_id}"
        persona_tmpl = self.debate_cfg.get(persona_key) or ""
        # 討論人格を展開する
        body = persona_tmpl.format(debate_rules=rules)
        # 固定時計の注記（人格に日時プレースホルダが無い場合でも抑止文を足す）
        clock_line = ""
        if session.frozen_date and session.frozen_time:
            clock_line = (
                f"\n\n# Session clock (fixed)\n"
                f"Session date: {session.frozen_date}, time: {session.frozen_time} JST "
                f"(do not update or discuss this clock).\n"
            )
        # 時刻言及抑止を末尾へ連結する
        return f"{body}{clock_line}\n{_CLOCK_STABILITY_NOTE}"

    def _normal_persona_system(
        self,
        persona_key: str,
        *,
        frozen_date: Optional[str] = None,
        frozen_time: Optional[str] = None,
    ) -> str:
        """通常チャット用 persona system（tools なし・日付展開）。"""
        llm = self.config.get("llm") or {}
        personas = llm.get("personas") or {}
        entry = personas.get(persona_key) or {}
        tmpl = entry.get("system_prompt") or ""
        # 固定時計が無ければ今を使う（通常呼び出し用）
        if frozen_date and frozen_time:
            date_str, time_str = frozen_date, frozen_time
        else:
            date_str, time_str = _freeze_clock()
        try:
            return tmpl.format(
                current_date=date_str,
                current_time=time_str,
            )
        except (KeyError, ValueError):
            return (
                tmpl.replace("{current_date}", date_str)
                .replace("{current_time}", time_str)
            )

    def _strip_leading_time(self, text: str) -> str:
        """文頭の [HH:MM] を除去する。"""
        # 空ならそのまま
        if not text:
            return text
        # 先頭時刻プレフィックスを落とす
        return _LEADING_TIME_RE.sub("", text).strip()

    def _last_opponent_text(
        self, session: DebateSession, speaker_id: str
    ) -> str:
        """直前の相手発言本文。"""
        for entry in reversed(session.transcript):
            if entry["speaker"] != speaker_id and not entry["speaker"].startswith("judge"):
                return entry["text"]
        return ""

    async def _ensure_partner_in_guild(
        self, guild: discord.Guild, partner_bot_id: str
    ) -> tuple:
        """相方がギルドにいるか fetch_member で確認。"""
        # 相方 user id
        partner_uid = registry.user_id(partner_bot_id)
        # 未ログイン
        if partner_uid is None:
            invite = self._invite_url(partner_bot_id)
            name = registry.display_name(partner_bot_id)
            return False, (
                f"Partner bot {name} is not logged in. "
                f"Invite: {invite}"
            )
        try:
            # Members Intent 不要の fetch
            await guild.fetch_member(partner_uid)
            return True, ""
        except discord.NotFound:
            invite = self._invite_url(partner_bot_id)
            name = registry.display_name(partner_bot_id)
            return False, (
                f"討論には {name} がこのサーバーに必要です。"
                f"招待して、同じ権限ロールを付けてください。\n"
                f"Debate requires {name} in this server.\n"
                f"[Invite {name}]({invite})"
            )
        except discord.HTTPException as e:
            return False, f"Failed to check partner presence: {e}"

    def _invite_url(self, bot_id: str) -> str:
        """bots_config の invite_url。"""
        bots = self.config.get("bots") or {}
        entry = bots.get(bot_id) or {}
        return entry.get("invite_url") or ""

    async def _post_with_mention(
        self,
        channel: discord.abc.Messageable,
        speaker_id: str,
        mention_target_id: str,
        text: str,
        *,
        reply_to: Optional[discord.Message] = None,
        add_mention: Optional[bool] = None,
    ) -> Tuple[str, Optional[discord.Message]]:
        """発言 Bot の Client で投稿する。reply_to があれば Discord 引用返信にする。

        戻り値: (本文クリーンテキスト, 投稿 Message)。投稿失敗時は Message=None。
        他 Bot の channel オブジェクトへフォールバックしない（自分で反論に見える誤投稿防止）。
        """
        # LLM 出力の <@...> を除去
        clean = _MENTION_RE.sub("", text or "").strip()
        # 先頭時刻プレフィックスも落とす
        clean = self._strip_leading_time(clean)
        # 文字数制限
        max_len = 1900
        if len(clean) > max_len:
            clean = clean[: max_len - 1] + "…"
        # メンション要否（reply がある討論は引用で足りるので既定オフ寄り）
        if add_mention is None:
            # reply 付きなら本文メンション不要（二重ピン回避）
            mention_on = bool(self.debate_cfg.get("mention_each_other", True)) and reply_to is None
        else:
            mention_on = add_mention
        # 文頭メンション
        prefix = registry.mention(mention_target_id) if mention_on else ""
        content = f"{prefix} {clean}".strip() if prefix else clean
        # 発言 Bot の Client 必須
        bot = registry.get(speaker_id)
        if bot is None:
            logger.error(
                "Cannot post as %s: bot not registered (skipping to avoid wrong author)",
                speaker_id,
            )
            return clean, None
        # 同一チャンネルを発言 Bot 側から取得
        ch_id = getattr(channel, "id", None)
        if ch_id is None:
            logger.error("Cannot post as %s: channel has no id", speaker_id)
            return clean, None
        target = bot.get_channel(ch_id)
        if target is None:
            # fetch を試す（キャッシュ未着時）
            try:
                target = await bot.fetch_channel(ch_id)
            except Exception as e:
                logger.error(
                    "Cannot post as %s: failed to resolve channel %s: %s",
                    speaker_id,
                    ch_id,
                    e,
                )
                return clean, None
        # reply 引数（討論応答も @silent で通知抑制）
        send_kwargs: Dict[str, Any] = {"silent": True}
        # 引用返信先が指定されていれば reference を付与する
        if reply_to is not None:
            try:
                # 失敗しても落ちない reference を組み立てる
                send_kwargs["reference"] = reply_to.to_reference(fail_if_not_exists=False)
                # 引用先作者への自動メンションはしない（本文メンションと分離）
                send_kwargs["mention_author"] = False
            except Exception as e:
                # reference 構築失敗は警告のみで通常送信へ落とす
                logger.warning("Failed to build message reference: %s", e)
        # 投稿
        try:
            # silent 付きで討論ターンを投稿する
            msg = await target.send(content, **send_kwargs)
        except Exception as e:
            logger.error("Failed to post as %s: %s", speaker_id, e, exc_info=True)
            return clean, None
        # ログタグ
        tag = registry.display_name(speaker_id)
        logger.info("🤖 [LLM_RESPONSE][%s] %s", tag, clean.replace("\n", " ")[:500])
        return clean, msg

    async def _generate(
        self,
        *,
        speaker_id: str,
        system: str,
        user_content: str,
        max_chars: int,
        tag: str,
    ) -> str:
        """指定 Bot の LLMCog 経由で一括生成（tools なし）。"""
        bot = registry.require(speaker_id)
        llm_cog = bot.get_cog("LLM")
        if llm_cog is None:
            return f"(LLM cog missing on {speaker_id})"
        # Cog のヘルパを使う
        if hasattr(llm_cog, "generate_plain"):
            text = await llm_cog.generate_plain(
                system=system,
                user_content=user_content,
                max_chars=max_chars,
            )
        else:
            text = "(generate_plain not available)"
        # ログ
        display = registry.display_name(speaker_id)
        logger.info(
            "🤖 [LLM_RESPONSE][%s][%s] %s",
            display,
            tag,
            (text or "").replace("\n", " ")[:500],
        )
        return (text or "").strip()


# グローバル Orchestrator（main 起動時に config で再初期化）
orchestrator: Optional[DebateOrchestrator] = None


def init_orchestrator(config: Dict[str, Any]) -> DebateOrchestrator:
    """プロセス共通 Orchestrator を初期化する。"""
    global orchestrator
    orchestrator = DebateOrchestrator(config)
    return orchestrator
