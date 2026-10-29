"""音楽再生パネルと操作用の Discord UI を定義する。"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

import discord

from MOMOKA.music.guild_state import GuildState, LoopMode
from MOMOKA.music.music_helpers import format_duration
from MOMOKA.music.plugins.ytdlp_wrapper import Track
from MOMOKA.utilities.donation import donation_from_bot, make_subtle_link_button

# UI 操作と更新失敗を音楽機能のログとして記録する。
logger = logging.getLogger(__name__)


class QueueAddedLayoutView(discord.ui.LayoutView):
    """再生中に単曲をキュー追加したときの小さめ Components V2 パネル。"""

    def __init__(self, track: Track, requester: discord.abc.User):
        # 静的表示のためタイムアウトなし
        super().__init__(timeout=None)
        # タイトルを安全に文字列化する
        safe_title = track.title or "Unknown title"
        # 曲URLがあればリンクにする
        if track.url:
            # Markdown リンクにする
            title_line = f"[{safe_title}]({track.url})"
        else:
            # URL 無しはプレーンにする
            title_line = safe_title
        # チャンネル名を取る
        uploader_val = track.uploader or "Unknown"
        # チャンネルURLがあればリンクにする
        if track.uploader_url and uploader_val != "Unknown":
            # リンク付きチャンネル名にする
            channel_line = f"[{uploader_val}]({track.uploader_url})"
        else:
            # プレーン名にする
            channel_line = uploader_val
        # 長さをフォーマットする
        duration_str = format_duration(track.duration)
        # 小さめの本文を組み立てる
        body = (
            f"### ➕ Added to queue\n"
            f"{title_line}\n"
            f"**Channel:** {channel_line}\n"
            f"**Duration:** `{duration_str}`\n"
            f"**Requested by:** {requester.mention}"
        )
        # 水色アクセントのコンテナを作る
        container = discord.ui.Container(accent_color=discord.Color.from_rgb(79, 194, 255))
        # サムネイルがあれば Section、無ければ TextDisplay
        thumb = track.thumbnail
        if thumb and str(thumb).strip() and str(thumb) != "None":
            # 右にサムネ付きで載せる
            container.add_item(
                discord.ui.Section(
                    discord.ui.TextDisplay(body),
                    accessory=discord.ui.Thumbnail(str(thumb)),
                )
            )
        else:
            # テキストのみ載せる
            container.add_item(discord.ui.TextDisplay(body))
        # ビューにコンテナを載せる
        self.add_item(container)


_SEARCH_SOURCE_LABELS = {
    "youtube": "YouTube",
    "niconico": "Niconico",
    "soundcloud": "SoundCloud",
}


def _search_result_body(index: int, track: Track) -> str:
    """候補1件の TextDisplay 本文。"""
    safe_title = " ".join((track.title or "Untitled").split()) or "Untitled"
    safe_title = safe_title.replace("[", "［").replace("]", "］")
    if len(safe_title) > 80:
        safe_title = safe_title[:77] + "..."
    title_line = f"[{safe_title}]({track.url})" if track.url else safe_title
    uploader = " ".join((track.uploader or "Unknown").split()) or "Unknown"
    uploader = uploader.replace("[", "［").replace("]", "］")
    if len(uploader) > 60:
        uploader = uploader[:57] + "..."
    if track.uploader_url and uploader != "Unknown":
        uploader_line = f"[{uploader}]({track.uploader_url})"
    else:
        uploader_line = uploader
    return (
        f"**{index + 1}.** {title_line}\n"
        f"{uploader_line} · `{format_duration(track.duration)}`"
    )


class SearchPickLayoutView(discord.ui.LayoutView):
    """タイトル検索の候補。ボタンでキューへ入れる。"""

    def __init__(
        self,
        cog: MusicCog,
        guild_id: int,
        requester_id: int,
        query: str,
        source: str,
        candidates: list[Track],
    ):
        # 放置された候補は3分で押せなくする
        super().__init__(timeout=180)
        self.cog = cog
        self.guild_id = guild_id
        self.requester_id = requester_id
        self.query = query
        self.source = source
        self.candidates = candidates[:5]
        self._queued: set[int] = set()
        self._expired = False
        self._lock = asyncio.Lock()
        self._build()

    def _build(self) -> None:
        """見出しと、各候補の Queue ボタンを組み直す。"""
        self.clear_items()
        container = discord.ui.Container(accent_color=discord.Color.from_rgb(79, 194, 255))
        label = _SEARCH_SOURCE_LABELS.get(self.source, "YouTube")
        shown_query = " ".join(self.query.split()).replace("`", "'")
        if len(shown_query) > 80:
            shown_query = shown_query[:77] + "..."
        container.add_item(
            discord.ui.TextDisplay(
                f"### {label} results\n"
                f"`{shown_query}`\n"
                f"Press **Queue** to add a track."
            )
        )
        for index, track in enumerate(self.candidates):
            queued = index in self._queued
            button = discord.ui.Button(
                label="Queued" if queued else "➕ Queue",
                style=discord.ButtonStyle.success if queued else discord.ButtonStyle.primary,
                disabled=queued or self._expired,
            )
            button.callback = self._bind(index)
            container.add_item(discord.ui.Separator())
            container.add_item(
                discord.ui.Section(
                    discord.ui.TextDisplay(_search_result_body(index, track)),
                    accessory=button,
                )
            )
        self.add_item(container)

    def _bind(self, index: int):
        """候補番号を閉じ込めたボタンコールバック。"""

        async def callback(interaction: discord.Interaction) -> None:
            await self._pick(interaction, index)

        return callback

    async def _pick(self, interaction: discord.Interaction, index: int) -> None:
        """選んだ URL をキューに入れる。"""
        if self._expired:
            await interaction.response.send_message(
                "This search expired. Run /play again.",
                ephemeral=True,
            )
            return
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message(
                "Only the person who searched can add these.",
                ephemeral=True,
            )
            return
        if index in self._queued or not (0 <= index < len(self.candidates)):
            await interaction.response.send_message("Already in the queue.", ephemeral=True)
            return
        await interaction.response.defer()
        async with self._lock:
            if index in self._queued or self._expired:
                return
            error = await self.cog.enqueue_picked_track(interaction, self.candidates[index].url)
            # None は成功。空文字は接続失敗など、すでに通知済み。
            if error is None:
                pass
            else:
                if error:
                    await interaction.followup.send(error, ephemeral=True)
                return
            self._queued.add(index)
            self._build()
            try:
                await interaction.edit_original_response(view=self)
            except Exception as exc:
                logger.warning("Failed to mark search result queued: %s", exc)

    async def on_timeout(self) -> None:
        """期限後はボタンを止める。"""
        self._expired = True
        self._build()
        message = getattr(self, "message", None)
        if message is None:
            return
        try:
            await message.edit(view=self)
        except Exception:
            logger.debug("Search picker expired before the message could be updated")


class LoadErrorLayoutView(discord.ui.LayoutView):
    """単発再生のストリーム取得失敗用 Components V2 パネル。"""

    def __init__(self, load_error_text: str):
        # タイムアウトなし（静的表示）
        super().__init__(timeout=None)
        # 警告色のコンテナを作る
        container = discord.ui.Container(accent_color=discord.Color.orange())
        # 見出しを載せる
        container.add_item(
            discord.ui.TextDisplay("### Could not load track")
        )
        # 英語メッセージをコードブロックで最下部相当に載せる
        container.add_item(
            discord.ui.TextDisplay(f"```\n{load_error_text}\n```")
        )
        # ビューにコンテナを載せる
        self.add_item(container)


class MusicControllerView(discord.ui.LayoutView):
    def __init__(
        self,
        cog: MusicCog,
        guild_id: int,
        finished_message: Optional[str] = None,
        finished_title: Optional[str] = None,
    ):
        # タイムアウトなしで初期化する
        super().__init__(timeout=None)
        # 親のMusicCogインスタンスを保持する
        self.cog = cog
        # 対象のギルドIDを保持する
        self.guild_id = guild_id
        # 停止/終了時にメタ欄へ出す詳細文言（無ければデフォルト文）
        self.finished_message = finished_message
        # 停止/終了時の見出し（Now Playing 相当。無ければデフォルト）
        self.finished_title = finished_title
        # UI（V2コンポーネント）の構築処理を実行する
        self.rebuild_ui()

    def rebuild_ui(self):
        # 既存のビューアイテムをすべてクリアする
        self.clear_items()

        # ギルドの再生状態オブジェクトを取得する
        state = self.cog.get_existing_guild_state(self.guild_id)
        # 再生状態、または再生中のトラックが存在しないか判定する
        if not state or not state.current_track:
            # 停止確認フラグをクリアする
            if state:
                # 確認ダイアログ状態を解除する
                state.confirming_stop = False
            # 再生中と同型の終了パネルを組み立てる
            self._build_finished_ui(state)
            # 処理を終了する
            return

        # Stop 確認ダイアログ表示中なら確認専用UIを組み立てる
        if state.confirming_stop:
            # 確認UIを構築して終了する
            self._build_stop_confirm_ui(state)
            # 通常UIは組まない
            return

        # 再生中のトラック情報を取得する
        track = state.current_track
        # 一時停止状態であるか取得する
        is_paused = state.is_paused

        # 再起アイコンと再生ステータスの文言を一時停止状態に合わせて決定する
        status_icon = "⏸️" if is_paused else "▶️"
        # ステータス文字列を設定する
        status_text = "Paused" if is_paused else "Playing"

        # 水色のアクセント色でV2コンテナを初期化する
        container = discord.ui.Container(accent_color=discord.Color.from_rgb(79, 194, 255))

        # タイトル本文を構築する（title が None でも落ちないようにする）
        safe_title = track.title or "Unknown title"
        # 曲URLが無い場合はリンクにしない
        if track.url:
            # リンク付きタイトルにする（見出しで強調するため太字は付けない）
            title_line = f"[{safe_title}]({track.url})"
        else:
            # プレーンタイトルにする
            title_line = safe_title

        # チャンネル名（アップローダー）のデフォルトフォールバックを設定する
        uploader_val = track.uploader if track.uploader else "Unknown"
        # チャンネルURLがあれば Markdown リンクにする
        if track.uploader_url and uploader_val != "Unknown":
            # クリック可能なチャンネル名にする
            uploader_display = f"[{uploader_val}]({track.uploader_url})"
        else:
            # URL が無ければプレーンテキストのまま使う
            uploader_display = uploader_val

        # ステータスは小さめ、曲名は ##（# より一段小さく）、直後にチャンネル名
        title_text = (
            f"### {status_icon} Now {status_text}\n"
            f"## {title_line}\n"
            f"{uploader_display}"
        )
        # サムネイルがあるときだけ Section（accessory 必須）を使い、無いときは TextDisplay
        if track.thumbnail and track.thumbnail.strip() and track.thumbnail != "None":
            # 右上サムネイル付きセクションを追加する
            container.add_item(
                discord.ui.Section(
                    discord.ui.TextDisplay(title_text),
                    accessory=discord.ui.Thumbnail(track.thumbnail),
                )
            )
        else:
            # サムネイル無しはテキストのみ追加する
            container.add_item(discord.ui.TextDisplay(title_text))

        # リクエストユーザーのメンション文字列を設定する
        requester_mention = f"<@{track.requester_id}>" if track.requester_id else "Unknown"
        # 残りのキューの数を取得する
        remaining = state.queue.qsize()
        # ループモード表示用の短いラベルを決める
        loop_label = state.loop_mode.name.lower()

        # 現在の再生位置（秒）を取得する
        current_pos = state.get_current_position()
        # 進行状況バー（テキストアート）を生成する
        progress_bar = self.cog._create_progress_bar(current_pos, track.duration)
        # 現在時間 / 総時間を同じ行に載せ、インラインコードで表示する
        # 例: `━━━━━━━━━━○───────────────── 11:11 / 33:33`
        progress_line = (
            f"{progress_bar} "
            f"{format_duration(current_pos)} / {format_duration(track.duration)}"
        )
        # Progress を単一バッククォートの1行でまとめる
        container.add_item(
            discord.ui.TextDisplay(f"`{progress_line}`")
        )

        # Progress とメタ情報の間に区切り線を入れる
        container.add_item(discord.ui.Separator())

        # Requested By / Loop / Queue は Progress の下にまとめる
        info_text = (
            f"**Requested By:** {requester_mention}\n"
            f"**Loop:** `{loop_label}`  |  **Queue:** {remaining} songs"
        )
        # メタデータを TextDisplay で追加する
        container.add_item(discord.ui.TextDisplay(info_text))

        # 一時停止/再生ボタンを初期化する
        self.pause_resume_btn = discord.ui.Button(
            # 一時停止中なら緑色、再生中ならグレーでボタンのカラーを設定する
            style=discord.ButtonStyle.success if is_paused else discord.ButtonStyle.secondary,
            # 一時停止中なら「再開」、再生中なら「一時停止」でラベルを設定する
            label="▶️ Resume" if is_paused else "⏸️ Pause",
            # カスタムIDを設定する
            custom_id=f"music_pause_resume_{self.guild_id}"
        )
        # コールバックメソッドを紐付ける
        self.pause_resume_btn.callback = self.pause_resume_callback

        # スキップボタンをプライマリカラーで初期化する
        self.skip_btn = discord.ui.Button(
            style=discord.ButtonStyle.primary,
            label="⏭️ Skip",
            custom_id=f"music_skip_{self.guild_id}"
        )
        # コールバックメソッドを紐付ける
        self.skip_btn.callback = self.skip_callback

        # 停止ボタンをレッドカラーで初期化する
        self.stop_btn = discord.ui.Button(
            style=discord.ButtonStyle.danger,
            label="⏹️ Stop",
            custom_id=f"music_stop_{self.guild_id}"
        )
        # コールバックメソッドを紐付ける
        self.stop_btn.callback = self.stop_callback

        # 曲単体ループ（ONE）ボタンを初期化する
        loop_one_active = state.loop_mode == LoopMode.ONE
        # 有効時は緑、無効時はグレーにする
        self.loop_btn = discord.ui.Button(
            style=discord.ButtonStyle.success if loop_one_active else discord.ButtonStyle.secondary,
            label="🔂 Loop",
            custom_id=f"music_loop_one_{self.guild_id}"
        )
        # コールバックを紐付ける
        self.loop_btn.callback = self.loop_one_callback

        # キュー全体ループ（ALL）ボタンを初期化する
        loop_all_active = state.loop_mode == LoopMode.ALL
        # 有効時は緑、無効時はグレーにする
        self.queue_loop_btn = discord.ui.Button(
            style=discord.ButtonStyle.success if loop_all_active else discord.ButtonStyle.secondary,
            label="🔁 QLoop",
            custom_id=f"music_loop_all_{self.guild_id}"
        )
        # コールバックを紐付ける
        self.queue_loop_btn.callback = self.loop_all_callback

        # キューに1曲以上あるときだけ QShuffle を有効化する
        can_shuffle = state.queue.qsize() >= 1
        # キューシャッフルボタンを初期化する
        self.queue_shuffle_btn = discord.ui.Button(
            style=discord.ButtonStyle.secondary,
            label="🔀 QShuffle",
            custom_id=f"music_queue_shuffle_{self.guild_id}",
            disabled=not can_shuffle,
        )
        # コールバックを紐付ける
        self.queue_shuffle_btn.callback = self.queue_shuffle_callback

        # Row1: Pause/Resume, Skip, Stop
        action_row = discord.ui.ActionRow()
        # Pause/Resume を追加する
        action_row.add_item(self.pause_resume_btn)
        # Skip を追加する
        action_row.add_item(self.skip_btn)
        # Stop を追加する
        action_row.add_item(self.stop_btn)
        # コンテナにコントロール行を追加する
        container.add_item(action_row)

        # Row2: Loop, QLoop, QShuffle
        loop_row = discord.ui.ActionRow()
        # Loop (ONE) を追加する
        loop_row.add_item(self.loop_btn)
        # QueueLoop (ALL) を追加する
        loop_row.add_item(self.queue_loop_btn)
        # QShuffle を追加する
        loop_row.add_item(self.queue_shuffle_btn)
        # コンテナにループ行を追加する
        container.add_item(loop_row)

        # 控えめな寄付リンク（enabled 時のみ）を別行に載せる
        donation_btn = make_subtle_link_button(donation_from_bot(self.cog.bot))
        # 寄付ボタンが有効な場合のみ行を追加する
        if donation_btn is not None:
            # 寄付専用のアクション行を作る
            donation_row = discord.ui.ActionRow()
            # 寄付ボタンを追加する
            donation_row.add_item(donation_btn)
            # コンテナに寄付行を追加する
            container.add_item(donation_row)

        # キューに次曲があるときだけ Queue 一覧を出す（単発再生中は非表示）
        if not state.queue.empty():
            # コントロールとキュー一覧の間に区切り線を入れる
            container.add_item(discord.ui.Separator())

            # キュー一覧テキストとページ情報を取得する
            queue_text, page, total_pages = self.cog._build_queue_display_text(state)
            # キュー一覧を TextDisplay で追加する
            container.add_item(discord.ui.TextDisplay(queue_text))

            # 複数ページあるときだけページングボタンを付ける
            if total_pages > 1:
                # ページング用アクション行を作成する
                nav_row = discord.ui.ActionRow()
                # 先頭ページボタンを作る
                first_btn = discord.ui.Button(
                    style=discord.ButtonStyle.primary,
                    label="⏪",
                    custom_id=f"music_q_first_{self.guild_id}",
                    disabled=(page <= 0),
                )
                # コールバックを紐付ける
                first_btn.callback = self.queue_first_callback
                # 行に追加する
                nav_row.add_item(first_btn)

                # 前ページボタンを作る
                prev_btn = discord.ui.Button(
                    style=discord.ButtonStyle.primary,
                    label="◀️",
                    custom_id=f"music_q_prev_{self.guild_id}",
                    disabled=(page <= 0),
                )
                # コールバックを紐付ける
                prev_btn.callback = self.queue_prev_callback
                # 行に追加する
                nav_row.add_item(prev_btn)

                # 現在ページ表示（押下不可）を作る
                page_btn = discord.ui.Button(
                    style=discord.ButtonStyle.secondary,
                    label=f"{page + 1}/{total_pages}",
                    custom_id=f"music_q_page_{self.guild_id}",
                    disabled=True,
                )
                # 行に追加する
                nav_row.add_item(page_btn)

                # 次ページボタンを作る
                next_btn = discord.ui.Button(
                    style=discord.ButtonStyle.primary,
                    label="▶️",
                    custom_id=f"music_q_next_{self.guild_id}",
                    disabled=(page >= total_pages - 1),
                )
                # コールバックを紐付ける
                next_btn.callback = self.queue_next_callback
                # 行に追加する
                nav_row.add_item(next_btn)

                # 末尾ページボタンを作る
                last_btn = discord.ui.Button(
                    style=discord.ButtonStyle.primary,
                    label="⏩",
                    custom_id=f"music_q_last_{self.guild_id}",
                    disabled=(page >= total_pages - 1),
                )
                # コールバックを紐付ける
                last_btn.callback = self.queue_last_callback
                # 行に追加する
                nav_row.add_item(last_btn)

                # コンテナにページング行を追加する
                container.add_item(nav_row)

        # ロード失敗バナーがあればキューの下（最下部）にコードブロックで出す
        self._append_load_error_banner(container, state)

        # ビューに構築したコンテナをアタッチする
        self.add_item(container)

    def _resolve_finished_copy(self) -> tuple[str, str]:
        """終了パネルの見出しとメタ欄文言を決める。"""
        # 明示タイトルがあればそれを見出しにする
        title = (self.finished_title or "").strip()
        # 詳細文言（メタ欄 / ログ相当）
        detail = (self.finished_message or "").strip()
        # 旧形式「見出し\\n本文」だけ渡された場合は分割する
        if not title and detail and "\n" in detail:
            # 先頭行を見出し、残りを詳細にする
            head, rest = detail.split("\n", 1)
            # 太字マークを除いて見出し化する
            title = head.replace("**", "").strip()
            # 残りの本文を詳細にする
            detail = rest.strip()
        # 見出しが無ければ停止用デフォルトにする
        if not title:
            # Now Playing と同型の見出しにする
            title = "⏹️ Playback Stopped"
        # ### が無ければ見出しレベルを揃える
        if not title.startswith("#"):
            # 再生中の Now Playing と同じ ### にする
            title = f"### {title}"
        # 詳細が無ければ停止用デフォルトにする
        if not detail:
            # Requested By / Loop 欄相当の短いステータス
            detail = "Playback stopped."
        # 見出しと詳細を返す
        return title, detail

    def _build_finished_ui(self, state: Optional[GuildState]):
        """再生終了時のグレーアウト V2 パネル（再生中レイアウト準拠）を組み立てる。"""
        # グレーのアクセントで終了状態を示す
        container = discord.ui.Container(accent_color=discord.Color.light_grey())
        # 見出しとメタ欄文言を解決する
        status_heading, detail_text = self._resolve_finished_copy()
        # 終了パネル用に残した最後の曲を取る
        track = state.last_finished_track if state else None

        # 最後の曲があれば再生中と同型（見出し＋曲名＋チャンネル＋サムネ）にする
        if track:
            # タイトルを安全に文字列化する
            safe_title = track.title or "Unknown title"
            # 曲URLがあればリンクにする
            if track.url:
                # Markdown リンクの曲名にする
                title_line = f"[{safe_title}]({track.url})"
            else:
                # URL 無しはプレーンにする
                title_line = safe_title
            # チャンネル名のフォールバック
            uploader_val = track.uploader if track.uploader else "Unknown"
            # チャンネルURLがあればリンクにする
            if track.uploader_url and uploader_val != "Unknown":
                # クリック可能なチャンネル名にする
                uploader_display = f"[{uploader_val}]({track.uploader_url})"
            else:
                # プレーン名にする
                uploader_display = uploader_val
            # Now Playing 相当の見出しの下に最後の曲情報を載せる
            title_text = (
                f"{status_heading}\n"
                f"## {title_line}\n"
                f"{uploader_display}"
            )
            # サムネがあれば Section、無ければ TextDisplay
            thumb = track.thumbnail
            if thumb and str(thumb).strip() and str(thumb) != "None":
                # 右上サムネ付きで載せる
                container.add_item(
                    discord.ui.Section(
                        discord.ui.TextDisplay(title_text),
                        accessory=discord.ui.Thumbnail(str(thumb)),
                    )
                )
            else:
                # テキストのみ載せる
                container.add_item(discord.ui.TextDisplay(title_text))
        else:
            # 曲情報が無い場合の見出し本文
            title_text = status_heading
            # URL 履歴だけ残っている場合は見出し直下に載せる
            if state and state.last_history_url:
                # 見出し → URL の2行にする
                title_text = f"{title_text}\n{state.last_history_url}"
            # TextDisplay で見出しを載せる
            container.add_item(discord.ui.TextDisplay(title_text))

        # リクエスト元が分かれば、再生中と同じメタ欄位置に残す
        if track and track.requester_id:
            # 曲情報とボタンの間に区切り線を入れる
            container.add_item(discord.ui.Separator())
            # Requested By 行を載せる
            container.add_item(
                discord.ui.TextDisplay(f"**Requested By:** <@{track.requester_id}>")
            )

        # Row1: Pause / Skip / Stop（すべて無効）
        action_row = discord.ui.ActionRow()
        # 一時停止を無効化する
        action_row.add_item(
            discord.ui.Button(label="⏸️ Pause", style=discord.ButtonStyle.secondary, disabled=True)
        )
        # スキップを無効化する
        action_row.add_item(
            discord.ui.Button(label="⏭️ Skip", style=discord.ButtonStyle.secondary, disabled=True)
        )
        # 停止を無効化する
        action_row.add_item(
            discord.ui.Button(label="⏹️ Stop", style=discord.ButtonStyle.secondary, disabled=True)
        )
        # コントロール行を載せる
        container.add_item(action_row)

        # Row2: Loop / QLoop / QShuffle（すべて無効）
        loop_row = discord.ui.ActionRow()
        # 曲ループを無効化する
        loop_row.add_item(
            discord.ui.Button(label="🔂 Loop", style=discord.ButtonStyle.secondary, disabled=True)
        )
        # キューループを無効化する
        loop_row.add_item(
            discord.ui.Button(label="🔁 QLoop", style=discord.ButtonStyle.secondary, disabled=True)
        )
        # シャッフルを無効化する
        loop_row.add_item(
            discord.ui.Button(label="🔀 QShuffle", style=discord.ButtonStyle.secondary, disabled=True)
        )
        # ループ行を載せる
        container.add_item(loop_row)

        # donation だけは有効なリンクボタンのまま残す
        donation_btn = make_subtle_link_button(donation_from_bot(self.cog.bot))
        # 有効設定のときだけ行を追加する
        if donation_btn is not None:
            # 寄付専用行を作る
            donation_row = discord.ui.ActionRow()
            # リンクボタンを載せる（disabled にしない）
            donation_row.add_item(donation_btn)
            # コンテナに寄付行を追加する
            container.add_item(donation_row)

        # 最下部（ロード失敗バナーと同位置）に終了ステータスを出す
        container.add_item(discord.ui.Separator())
        # 複数行の再起動案内などはプレーン、短文はエラー欄と同型のコードブロック
        if "\n" in detail_text:
            # 長文はそのまま最下部に出す
            container.add_item(discord.ui.TextDisplay(detail_text))
        else:
            # エラー表示用と同じログ風コードブロックにする
            container.add_item(discord.ui.TextDisplay(f"```\n{detail_text}\n```"))

        # ビューにコンテナをアタッチする
        self.add_item(container)

    def _append_load_error_banner(self, container: discord.ui.Container, state: Optional[GuildState]):
        """NO audio 等のロード失敗を Components V2 最下部に英語コードブロックで付ける。"""
        # 状態またはバナー文字列が無ければ何もしない
        if not state or not state.ui_load_error:
            # 早期リターン
            return
        # 区切り線を入れてバナーを目立たせる
        container.add_item(discord.ui.Separator())
        # Discord コードブロックとして英語メッセージを載せる
        container.add_item(
            discord.ui.TextDisplay(f"```\n{state.ui_load_error}\n```")
        )
        # 一度表示したことを記録し、次曲開始で消せるようにする
        state.ui_load_error_seen = True

    def _build_stop_confirm_ui(self, state: GuildState):
        """Stop 確認用の Confirm / Cancel UI を組み立てる。"""
        # 警告色のコンテナを初期化する
        container = discord.ui.Container(accent_color=discord.Color.orange())
        # 確認メッセージ本文を構築する
        confirm_text = (
            "### ⏹️ Stop Playback?\n"
            "再生を停止し、キューをすべてクリアします。\n"
            "Stop playback and clear the entire queue."
        )
        # TextDisplay で確認文を載せる
        container.add_item(discord.ui.TextDisplay(confirm_text))

        # Confirm / Cancel 用アクション行を作る
        row = discord.ui.ActionRow()
        # 確定ボタンを作る
        confirm_btn = discord.ui.Button(
            style=discord.ButtonStyle.danger,
            label="✅ Confirm",
            custom_id=f"music_stop_confirm_{self.guild_id}",
        )
        # コールバックを紐付ける
        confirm_btn.callback = self.stop_confirm_callback
        # 行に追加する
        row.add_item(confirm_btn)

        # キャンセルボタンを作る
        cancel_btn = discord.ui.Button(
            style=discord.ButtonStyle.secondary,
            label="❌ Cancel",
            custom_id=f"music_stop_cancel_{self.guild_id}",
        )
        # コールバックを紐付ける
        cancel_btn.callback = self.stop_cancel_callback
        # 行に追加する
        row.add_item(cancel_btn)

        # コンテナに確認行を追加する
        container.add_item(row)
        # ビューにコンテナをアタッチする
        self.add_item(container)

    async def _edit_after_interaction(self, interaction: discord.Interaction, state: GuildState):
        """defer 後に LayoutView を再編集する共通処理。"""
        try:
            # コンポーネント用トークンで元メッセージを編集する
            await interaction.edit_original_response(embed=None, view=self)
        except Exception:
            # フォールバック: チャンネル経由 Message へ変換して編集する
            durable = await self.cog._to_durable_message(interaction.message)
            # 変換できた場合のみ編集する
            if durable is not None:
                # 通常 Message として編集する
                await durable.edit(embed=None, view=self)
                # 最新の参照を状態へ保存する
                state.last_now_playing_message = durable

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        # ギルドの再生状態オブジェクトを取得する
        state = self.cog.get_existing_guild_state(self.guild_id)
        # 再生状態、またはボイス接続が存在しないか判定する
        if not state or not state.voice_client:
            # エフェメラルエラーを送信する
            await interaction.response.send_message("The bot is not in a voice channel.", ephemeral=True)
            # チェック失敗
            return False

        # 操作を実行したユーザーのボイス接続状態を取得する
        user_voice = interaction.user.voice
        # ユーザーがボイスチャンネルに入っていない、またはボットと異なるチャンネルか判定する
        if not user_voice or not user_voice.channel or user_voice.channel != state.voice_client.channel:
            # エフェメラルエラーを送信する
            await interaction.response.send_message("You must be in the same voice channel as the bot to use the controls.", ephemeral=True)
            # チェック失敗
            return False
        # チェックパス
        return True

    async def pause_resume_callback(self, interaction: discord.Interaction):
        # インタラクションへの遅延応答を開始する
        await interaction.response.defer()
        # ギルドの再生状態オブジェクトを取得する
        state = self.cog.get_existing_guild_state(self.guild_id)
        # オブジェクトが存在しない場合は終了する
        if not state:
            # 処理終了
            return

        # 現在一時停止中であるか判定する
        if state.is_paused:
            # ボイス接続が存在するか判定する
            if state.voice_client:
                # 音声再生を再開する
                state.voice_client.resume()
            # 一時停止フラグをFalseに設定する
            state.is_paused = False
            # 一時停止開始のタイムスタンプが存在するか判定する
            if state.paused_at and state.playback_start_time:
                # 一時停止されていた実経過時間を算出する
                pause_duration = time.time() - state.paused_at
                # 総再生開始時刻に一時停止時間を加算して進行位置を補正する
                state.playback_start_time += pause_duration
            # 一時停止開始時刻を初期化する
            state.paused_at = None
            # 再開ログを記録する
            logger.info(f"Guild {self.guild_id}: playback resumed via UI button")
        else:
            # ボイス接続が存在するか判定する
            if state.voice_client:
                # 音声再生を一時停止する
                state.voice_client.pause()
            # 一時停止フラグをTrueに設定する
            state.is_paused = True
            # 現在の時刻を一時停止開始時刻として記録する
            state.paused_at = time.time()
            # 一時停止ログを記録する
            logger.info(f"Guild {self.guild_id}: playback paused via UI button")

        # 変更された再生状態に基づいてUIを再構築する
        self.rebuild_ui()
        # メッセージを編集して反映する
        await self._edit_after_interaction(interaction, state)
        # 一時停止/再開に合わせて VC ステータスも更新する
        await self.cog._sync_voice_channel_status(self.guild_id)

    async def skip_callback(self, interaction: discord.Interaction):
        # インタラクションへの遅延応答を開始する
        await interaction.response.defer()
        # ギルドの再生状態オブジェクトを取得する
        state = self.cog.get_existing_guild_state(self.guild_id)
        # 再生状態、または再生中のトラックが存在しない場合は終了する
        if not state or not state.current_track:
            # 処理終了
            return

        # スキップ開始をログに記録する
        logger.info(f"Guild {self.guild_id}: skipping song via UI button")
        # オーディオミキサーが存在するか判定する
        if state.mixer:
            # ミキサーから対象の音源を削除してスキップをトリガーする
            await state.mixer.remove_source('music')
        # ミキサーがなく、ボイスクライアントが直接再生中であるか判定する
        elif state.voice_client and state.voice_client.is_playing():
            # 再生を停止してスキップをトリガーする
            state.voice_client.stop()

    async def stop_callback(self, interaction: discord.Interaction):
        # インタラクションへの遅延応答を開始する
        await interaction.response.defer()
        # ギルドの再生状態オブジェクトを取得する
        state = self.cog.get_existing_guild_state(self.guild_id)
        # オブジェクトが存在しない場合は終了する
        if not state:
            # 処理終了
            return

        # Stop 確認ダイアログを有効にする
        state.confirming_stop = True
        # 確認UIに切り替える
        self.rebuild_ui()
        # メッセージを編集して確認UIを出す
        await self._edit_after_interaction(interaction, state)

    async def stop_confirm_callback(self, interaction: discord.Interaction):
        # インタラクションへの遅延応答を開始する
        await interaction.response.defer()
        # ギルドの再生状態オブジェクトを取得する
        state = self.cog.get_existing_guild_state(self.guild_id)
        # オブジェクトが存在しない場合は終了する
        if not state:
            # 処理終了
            return

        # 確認フラグを解除する
        state.confirming_stop = False
        # 停止処理の開始をログに記録する
        logger.info(f"Guild {self.guild_id}: stopping playback via UI confirm")
        # ループモードをOFFに設定する
        state.loop_mode = LoopMode.OFF
        # キューの内容をすべて消去する
        await state.clear_queue()
        # 停止起因の終了コールバックが次曲再生を始めないよう先に停止状態へ移行する
        state.stopping = True
        # キューページをリセットする
        state.queue_page = 0
        # ロード失敗バナーも消す
        state.ui_load_error = None
        # 表示済みフラグも戻す
        state.ui_load_error_seen = False
        # コールバックより先にミキサー参照を切り離して古い終了通知を無効化する
        mixer = state.mixer
        state.mixer = None
        # 切り離したミキサーが存在するか判定する
        if mixer:
            # ミキサーを完全に停止する
            mixer.stop()
        # ボイスクライアントが直接再生中であるか判定する
        if state.voice_client and state.voice_client.is_playing():
            # 再生を停止する
            state.voice_client.stop()
        # 再生中フラグを初期化する
        state.is_playing = False
        # 一時停止フラグを初期化する
        state.is_paused = False
        # 停止前に URL 再生履歴を残す
        self.cog._remember_play_history_url(state, state.current_track)
        # 再生中トラック情報を初期化する
        state.current_track = None
        # 再生時間計測情報を初期化する
        state.reset_playback_tracking()
        # UI 停止時はプログレスバー更新も止める
        state.stop_progress_updater()

        # 停止した状態に基づいてUIを再構築する
        self.rebuild_ui()
        # メッセージを編集して停止表示にする
        await self._edit_after_interaction(interaction, state)

        # 直前の Now Playing メッセージへの参照が存在するか判定する
        if state.last_now_playing_message:
            # 参照を初期化する
            state.last_now_playing_message = None
        # 停止に合わせて VC ステータスをクリアする
        await self.cog._clear_voice_channel_status(self.guild_id)

    async def stop_cancel_callback(self, interaction: discord.Interaction):
        # インタラクションへの遅延応答を開始する
        await interaction.response.defer()
        # ギルドの再生状態オブジェクトを取得する
        state = self.cog.get_existing_guild_state(self.guild_id)
        # オブジェクトが存在しない場合は終了する
        if not state:
            # 処理終了
            return

        # 確認フラグを解除する
        state.confirming_stop = False
        # 通常の再生UIへ戻す
        self.rebuild_ui()
        # メッセージを編集して通常UIに戻す
        await self._edit_after_interaction(interaction, state)

    async def loop_one_callback(self, interaction: discord.Interaction):
        # インタラクションへの遅延応答を開始する
        await interaction.response.defer()
        # ギルドの再生状態オブジェクトを取得する
        state = self.cog.get_existing_guild_state(self.guild_id)
        # オブジェクトが存在しない場合は終了する
        if not state:
            # 処理終了
            return

        # 既に ONE なら OFF、それ以外なら ONE に切り替える
        if state.loop_mode == LoopMode.ONE:
            # ループを解除する
            state.loop_mode = LoopMode.OFF
        else:
            # 曲単体ループを有効にする
            state.loop_mode = LoopMode.ONE
        # 最終操作時刻を更新する
        state.update_activity()
        # 変更ログを残す
        logger.info(f"Guild {self.guild_id}: loop mode set to {state.loop_mode.name} via Loop button")
        # UIを再構築する
        self.rebuild_ui()
        # メッセージを編集する
        await self._edit_after_interaction(interaction, state)

    async def loop_all_callback(self, interaction: discord.Interaction):
        # インタラクションへの遅延応答を開始する
        await interaction.response.defer()
        # ギルドの再生状態オブジェクトを取得する
        state = self.cog.get_existing_guild_state(self.guild_id)
        # オブジェクトが存在しない場合は終了する
        if not state:
            # 処理終了
            return

        # 既に ALL なら OFF、それ以外なら ALL に切り替える
        if state.loop_mode == LoopMode.ALL:
            # キューループを解除する
            state.loop_mode = LoopMode.OFF
        else:
            # キュー全体ループを有効にする
            state.loop_mode = LoopMode.ALL
        # 最終操作時刻を更新する
        state.update_activity()
        # 変更ログを残す
        logger.info(f"Guild {self.guild_id}: loop mode set to {state.loop_mode.name} via QLoop button")
        # UIを再構築する
        self.rebuild_ui()
        # メッセージを編集する
        await self._edit_after_interaction(interaction, state)

    async def queue_shuffle_callback(self, interaction: discord.Interaction):
        # インタラクションへの遅延応答を開始する
        await interaction.response.defer()
        # ギルドの再生状態オブジェクトを取得する
        state = self.cog.get_existing_guild_state(self.guild_id)
        # 状態が無ければ終了する
        if not state:
            # 処理終了
            return
        # キューが空なら何もしない（ボタンは通常 disabled）
        if state.queue.qsize() < 1:
            # UI だけ最新化する
            self.rebuild_ui()
            # メッセージを編集する
            await self._edit_after_interaction(interaction, state)
            # 終了する
            return
        # 待ちキューだけを in-place シャッフルする（再生中曲は触らない）
        await state.shuffle_queue()
        # 操作時刻を更新する
        state.update_activity()
        # 変更ログを残す
        logger.info(f"Guild {self.guild_id}: queue shuffled via QShuffle button")
        # UIを再構築する
        self.rebuild_ui()
        # メッセージを編集する
        await self._edit_after_interaction(interaction, state)

    async def queue_first_callback(self, interaction: discord.Interaction):
        # 先頭ページへ移動する
        await self._change_queue_page(interaction, target_page=0)

    async def queue_prev_callback(self, interaction: discord.Interaction):
        # ギルド状態を取得する
        state = self.cog.get_existing_guild_state(self.guild_id)
        # 状態が無ければ終了する
        if not state:
            # interaction を消費する
            await interaction.response.defer()
            # 処理終了
            return
        # 前ページ番号を計算する
        await self._change_queue_page(interaction, target_page=max(0, state.queue_page - 1))

    async def queue_next_callback(self, interaction: discord.Interaction):
        # ギルド状態を取得する
        state = self.cog.get_existing_guild_state(self.guild_id)
        # 状態が無ければ終了する
        if not state:
            # interaction を消費する
            await interaction.response.defer()
            # 処理終了
            return
        # 総ページ数を把握するため表示ヘルパーを呼ぶ
        _text, page, total_pages = self.cog._build_queue_display_text(state)
        # 次ページ番号を計算する
        await self._change_queue_page(interaction, target_page=min(total_pages - 1, page + 1))

    async def queue_last_callback(self, interaction: discord.Interaction):
        # ギルド状態を取得する
        state = self.cog.get_existing_guild_state(self.guild_id)
        # 状態が無ければ終了する
        if not state:
            # interaction を消費する
            await interaction.response.defer()
            # 処理終了
            return
        # 総ページ数を把握する
        _text, _page, total_pages = self.cog._build_queue_display_text(state)
        # 末尾ページへ移動する
        await self._change_queue_page(interaction, target_page=total_pages - 1)

    async def _change_queue_page(self, interaction: discord.Interaction, target_page: int):
        # インタラクションへの遅延応答を開始する
        await interaction.response.defer()
        # ギルドの再生状態オブジェクトを取得する
        state = self.cog.get_existing_guild_state(self.guild_id)
        # オブジェクトが存在しない場合は終了する
        if not state:
            # 処理終了
            return
        # ページ番号を書き換える
        state.queue_page = max(0, target_page)
        # UIを再構築する
        self.rebuild_ui()
        # メッセージを編集する
        await self._edit_after_interaction(interaction, state)


