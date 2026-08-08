# MOMOKA/media_downloader/ytdlp_downloader_cog.py
# yt-dlp + file.io 一時共有ダウンローダー（Components V2 UI）。
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from typing import Any, Dict, Optional

import discord
import yt_dlp
from discord import app_commands
from discord.ext import commands

from MOMOKA.media_downloader.allowlist import (
    check_download_url_allowed,
    check_extracted_info_allowed,
    load_allowed_extractors,
)
from MOMOKA.media_downloader.error.errors import YTDLPExceptionHandler
from MOMOKA.media_downloader.fileio_uploader import FileIoUploader
from MOMOKA.music.plugins.ytdlp_wrapper import apply_youtube_ejs_opts
from MOMOKA.storage import NS_FILEIO_DELETION_SCHEDULE, resolve_settings_db
from MOMOKA.utilities.locale import pick_str, resolve_interaction_lang

# --- 設定項目 ---
# ローカル削除までの秒数（file.io expires=10m と揃える）
DELETE_DELAY_SECONDS = 600
# 一時ダウンロードディレクトリ
DOWNLOAD_DIR = "temp_media_download"
# --- 設定項目ここまで ---

logger = logging.getLogger(__name__)

# UI アクセント色
_ACCENT_SELECT = discord.Color.from_rgb(220, 40, 40)
_ACCENT_PROGRESS = discord.Color.from_rgb(79, 194, 255)
_ACCENT_READY = discord.Color.green()
_ACCENT_ERROR = discord.Color.dark_red()


def _format_duration(duration: Optional[int]) -> str:
    """秒数を HH:MM:SS / MM:SS 文字列へ変換する。"""
    # 未設定や 0 は N/A
    if not duration:
        return "N/A"
    # 時・分・秒へ分解する
    minutes, seconds = divmod(int(duration), 60)
    hours, minutes = divmod(minutes, 60)
    # 1 時間以上なら時も付ける
    if hours > 0:
        return f"{hours:02}:{minutes:02}:{seconds:02}"
    # 分秒のみ
    return f"{minutes:02}:{seconds:02}"


def _container_priority(ext: Optional[str]) -> int:
    """同一解像度内の並び用。値が高いほど優先（mp4 > その他 > webm）。"""
    # 拡張子を小文字で正規化する
    normalized = (ext or "").lower()
    # mp4 を最優先にする
    if normalized == "mp4":
        return 3
    # mp4 系コンテナを次点にする
    if normalized in ("m4v", "mov"):
        return 2
    # webm は同解像度内で後ろへ回す
    if normalized == "webm":
        return 0
    # 上記以外は中間優先度
    return 1


def _select_download_info(info: Any) -> Optional[Dict[str, Any]]:
    """単体情報または検索結果から最初の有効なメディア情報を返す。"""
    # 情報が辞書以外なら利用できない
    if not isinstance(info, dict):
        return None
    # 検索結果のエントリ一覧を取得する
    entries = info.get("entries")
    # 単体メディア情報ならそのまま返す
    if entries is None:
        return info
    # None を除外して最初に見つかったエントリを返す
    return next(
        (
            entry
            for entry in entries
            if isinstance(entry, dict)
        ),
        None,
    )


class StatusLayoutView(discord.ui.LayoutView):
    """進捗・エラー表示用の簡易 Components V2 LayoutView。"""

    def __init__(
        self,
        body: str,
        *,
        accent: discord.Color = _ACCENT_PROGRESS,
        timeout: Optional[float] = None,
    ) -> None:
        # 進捗表示は明示的に消すまで残す
        super().__init__(timeout=timeout)
        # 本文と色を保持する
        self.body = body
        self.accent = accent
        # UI を組み立てる
        self._rebuild()

    def update(self, body: str, *, accent: Optional[discord.Color] = None) -> None:
        """本文（と任意で色）を差し替えて再構築する。"""
        # 新しい本文を保持する
        self.body = body
        # 色指定があれば更新する
        if accent is not None:
            self.accent = accent
        # 子コンポーネントを組み直す
        self._rebuild()

    def _rebuild(self) -> None:
        """TextDisplay のみのコンテナを載せる。"""
        # 既存を消す
        self.clear_items()
        # コンテナを作る
        container = discord.ui.Container(accent_color=self.accent)
        # 本文を載せる
        container.add_item(discord.ui.TextDisplay(self.body))
        # ルートへ追加する
        self.add_item(container)


class DownloadReadyLayoutView(discord.ui.LayoutView):
    """ダウンロード完了表示（リンクボタン付き）Components V2 LayoutView。"""

    def __init__(
        self,
        *,
        title: str,
        download_link: str,
        expire_minutes: int,
        thumbnail_url: Optional[str] = None,
        lang: str = "en",
        timeout: Optional[float] = None,
    ) -> None:
        # 完了メッセージは明示削除まで残す
        super().__init__(timeout=timeout)
        # 表示用データを保持する
        self.title = title
        self.download_link = download_link
        self.expire_minutes = expire_minutes
        self.thumbnail_url = thumbnail_url
        # UI 言語
        self.lang = "ja" if lang == "ja" else "en"
        # UI を組み立てる
        self._rebuild()

    def _rebuild(self) -> None:
        """完了本文・サムネ・リンクボタンを載せる。"""
        # 既存を消す
        self.clear_items()
        # 成功色のコンテナ
        container = discord.ui.Container(accent_color=_ACCENT_READY)
        # 見出し＋説明本文（単一言語）
        body = pick_str(
            self.lang,
            ja=(
                f"### ✅ ダウンロード準備完了\n"
                f"**{self.title}**\n\n"
                f"以下のリンクからダウンロードしてください。\n\n"
                f"このリンクは**約{self.expire_minutes}分後**に無効になります。"
            ),
            en=(
                f"### ✅ Download Ready\n"
                f"**{self.title}**\n\n"
                f"Please download from the link below.\n\n"
                f"This link will expire in **about {self.expire_minutes} minutes**."
            ),
        )
        # サムネがあれば Section（accessory 必須）を使う
        if self.thumbnail_url and str(self.thumbnail_url).strip():
            container.add_item(
                discord.ui.Section(
                    discord.ui.TextDisplay(body),
                    accessory=discord.ui.Thumbnail(self.thumbnail_url),
                )
            )
        else:
            # サムネ無しは TextDisplay のみ
            container.add_item(discord.ui.TextDisplay(body))
        # ダウンロード用リンクボタン行
        row = discord.ui.ActionRow()
        row.add_item(
            discord.ui.Button(
                label=pick_str(self.lang, ja="ダウンロード", en="Download"),
                style=discord.ButtonStyle.link,
                url=self.download_link,
                emoji="📥",
            )
        )
        container.add_item(row)
        # ルートへ追加する
        self.add_item(container)


class VideoFormatSelect(discord.ui.Select):
    """動画フォーマット選択セレクト（音声は後段で自動結合）。"""

    def __init__(
        self,
        cog_instance: "YtdlpDownloaderCog",
        info: Dict[str, Any],
        url: str,
        *,
        requester_user_id: int,
        lang: str = "en",
    ) -> None:
        # Cog・メタ・URL を保持する
        self.cog = cog_instance
        self.info = info
        self.url = url
        # 操作を許可するリクエスト元ユーザー ID
        self.requester_user_id = requester_user_id
        # UI 言語
        self.lang = "ja" if lang == "ja" else "en"
        # 選択肢リスト
        options = []
        # 映像ありフォーマットを解像度・コンテナ優先度・bitrate 降順で並べる
        sorted_formats = sorted(
            [f for f in info.get("formats", []) if f.get("vcodec") != "none"],
            key=lambda f: (
                f.get("height") or 0,
                _container_priority(f.get("ext")),
                f.get("tbr") or 0,
            ),
            reverse=True,
        )
        # Discord 上限 25 件まで載せる
        for f in sorted_formats[:25]:
            # ファイルサイズ表示用
            filesize = f.get("filesize") or f.get("filesize_approx")
            filesize_mb = f"{filesize / (1024 * 1024):.2f}MB" if filesize else "N/A"
            # 拡張子を先頭・大文字で目立たせる（映像のみ注記は付けない＝後で音声結合するため）
            ext_label = (f.get("ext") or "N/A").upper()
            # ラベルは拡張子・解像度・サイズの順
            label = f"{ext_label} | {f.get('resolution', 'N/A')} | {filesize_mb}"
            # 説明は映像コーデック中心（Audio: none 等は出さない）
            description = f"Video: {f.get('vcodec', 'n/a')} | ID: {f.get('format_id')}"
            options.append(
                discord.SelectOption(
                    label=label[:100],
                    value=str(f.get("format_id")),
                    description=description[:100],
                )
            )
        # フォーマットが無い場合のフォールバック
        if not options:
            options = [
                discord.SelectOption(
                    label=pick_str(
                        self.lang,
                        ja="利用可能なフォーマットなし",
                        en="No formats",
                    ),
                    value="none",
                    description=pick_str(
                        self.lang,
                        ja="ダウンロード可能な動画形式がありません",
                        en="No downloadable video formats found",
                    ),
                )
            ]
        # Select 本体を初期化する
        super().__init__(
            placeholder=pick_str(
                self.lang,
                ja="動画フォーマットを選択...",
                en="Select a video format...",
            ),
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        """選択後にダウンロード→結合→GDrive アップロードする。"""
        # 押した人の locale を優先する（無ければ作成時言語）
        lang = resolve_interaction_lang(interaction) or self.lang
        # リクエスト元以外の選択操作を拒否する
        if interaction.user.id != self.requester_user_id:
            await interaction.response.send_message(
                pick_str(
                    lang,
                    ja="このフォーマット選択はコマンドを実行したユーザーのみ操作できます。",
                    en="Only the user who requested this download can select a format.",
                ),
                ephemeral=True,
            )
            return
        # 無効値ならエラー表示へ
        if self.values[0] == "none":
            err_view = StatusLayoutView(
                pick_str(
                    lang,
                    ja="### ❌ エラー\n利用可能な動画フォーマットがありません。",
                    en="### ❌ Error\nNo downloadable video formats found.",
                ),
                accent=_ACCENT_ERROR,
            )
            await interaction.response.edit_message(view=err_view)
            return
        # 進捗 LayoutView に切り替える
        progress = StatusLayoutView(
            pick_str(
                lang,
                ja=(
                    f"### 📥 ダウンロード中\n"
                    f"**{interaction.user.display_name}** がフォーマットを選択しました。\n\n"
                    f"ダウンロードと音声結合を開始します..."
                ),
                en=(
                    f"### 📥 Downloading\n"
                    f"**{interaction.user.display_name}** has selected a format.\n\n"
                    f"Starting download and audio merge..."
                ),
            ),
            accent=_ACCENT_PROGRESS,
        )
        # 元メッセージを V2 進捗表示へ更新する
        await interaction.response.edit_message(view=progress)
        # 選択フォーマット ID
        format_id = self.values[0]
        # タイトル
        video_title = self.info.get("title", "video")
        # 一時ファイル用 UUID
        base_uuid = str(uuid.uuid4())
        # yt-dlp オプション（映像 + 最良音声を結合）
        ydl_opts = apply_youtube_ejs_opts(
            {
                "format": f"{format_id}+bestaudio[acodec^=mp4a]/bestvideo+bestaudio",
                "outtmpl": os.path.join(DOWNLOAD_DIR, f"{base_uuid}.%(ext)s"),
                "merge_output_format": "mp4",
                "quiet": True,
                "no_warnings": True,
                "noprogress": True,
            }
        )
        downloaded_file_path = None
        try:
            # ダウンロード直前に URL / メタの許可を再確認する
            reject_msg = self.cog._reject_if_not_allowed(
                lang=lang,
                url_or_query=self.url,
                info=self.info if isinstance(self.info, dict) else None,
            )
            # 拒否ならエラー表示して終了
            if reject_msg:
                progress.update(
                    pick_str(
                        lang,
                        ja=f"### ❌ エラー\n{reject_msg}",
                        en=f"### ❌ Error\n{reject_msg}",
                    ),
                    accent=_ACCENT_ERROR,
                )
                await interaction.edit_original_response(
                    content=None, embed=None, view=progress
                )
                return
            # 同期ダウンロードをスレッドへ逃がす
            def download_sync():
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(self.url, download=False)
                    final_path = ydl.prepare_filename(info).rsplit(".", 1)[0] + ".mp4"
                    ydl.download([self.url])
                    return final_path if os.path.exists(final_path) else None

            # ダウンロード実行
            downloaded_file_path = await asyncio.to_thread(download_sync)

            # 結合失敗
            if not downloaded_file_path:
                progress.update(
                    self.cog.exception_handler.get_merge_error(lang=lang),
                    accent=_ACCENT_ERROR,
                )
                await interaction.edit_original_response(content=None, embed=None, view=progress)
                return

            # アップロード進捗へ更新
            progress.update(
                pick_str(
                    lang,
                    ja=(
                        f"### 🔼 アップロード中\n"
                        f"**{video_title}** を file.io にアップロードしています..."
                    ),
                    en=(
                        f"### 🔼 Uploading\n"
                        f"Uploading **{video_title}** to file.io..."
                    ),
                ),
                accent=_ACCENT_PROGRESS,
            )
            await interaction.edit_original_response(content=None, embed=None, view=progress)
            # アップロードファイル名
            upload_filename = f"{video_title}.mp4"
            # file.io へアップロードする
            file_key, download_link = await self.cog.fileio_uploader.upload_file(
                downloaded_file_path,
                upload_filename,
            )
            # アップロード失敗
            if not download_link:
                progress.update(
                    self.cog.exception_handler.get_upload_error(lang=lang),
                    accent=_ACCENT_ERROR,
                )
                await interaction.edit_original_response(content=None, embed=None, view=progress)
                return

            # 有効期限（分）
            minutes = int(DELETE_DELAY_SECONDS / 60)
            # 完了 LayoutView
            ready = DownloadReadyLayoutView(
                title=video_title,
                download_link=download_link,
                expire_minutes=minutes,
                thumbnail_url=self.info.get("thumbnail"),
                lang=lang,
            )
            # 完了表示へ差し替え
            await interaction.edit_original_response(content=None, embed=None, view=ready)
            # 期限後削除をスケジュール
            if file_key:
                await self.cog.schedule_fileio_deletion(file_key)
        except Exception as e:
            # 例外メッセージをエラー LayoutView で表示
            progress.update(
                self.cog.exception_handler.handle_exception(e, lang=lang),
                accent=_ACCENT_ERROR,
            )
            await interaction.edit_original_response(content=None, embed=None, view=progress)
        finally:
            # 一時ファイル掃除
            logger.debug("[DEBUG] Cleaning up temporary files...")
            for item in os.listdir(DOWNLOAD_DIR):
                if item.startswith(base_uuid):
                    try:
                        os.remove(os.path.join(DOWNLOAD_DIR, item))
                    except OSError:
                        pass


class VideoSelectLayoutView(discord.ui.LayoutView):
    """動画情報 + フォーマット選択の Components V2 LayoutView。"""

    def __init__(
        self,
        cog_instance: "YtdlpDownloaderCog",
        info: Dict[str, Any],
        url: str,
        *,
        requester_user_id: int,
        lang: str = "en",
        timeout: float = 300.0,
    ) -> None:
        # 選択待ちタイムアウト
        super().__init__(timeout=timeout)
        # 参照を保持する
        self.cog = cog_instance
        self.info = info
        self.url = url
        # 操作を許可するリクエスト元ユーザー ID
        self.requester_user_id = requester_user_id
        # UI 言語
        self.lang = "ja" if lang == "ja" else "en"
        # UI を組み立てる
        self._rebuild()

    def _rebuild(self) -> None:
        """タイトル・メタ・セレクトを載せる。"""
        # 既存を消す
        self.clear_items()
        # コンテナ
        container = discord.ui.Container(accent_color=_ACCENT_SELECT)
        # メタ情報を取り出す
        video_title = self.info.get(
            "title",
            pick_str(self.lang, ja="不明なタイトル", en="Unknown title"),
        )
        thumbnail_url = self.info.get("thumbnail")
        uploader = self.info.get("uploader", "N/A")
        duration_str = _format_duration(self.info.get("duration"))
        # 見出し本文
        title_text = pick_str(
            self.lang,
            ja=f"### 🎬 {video_title}\n[元ページを開く]({self.url})",
            en=f"### 🎬 {video_title}\n[Open source]({self.url})",
        )
        # サムネ付きなら Section
        if thumbnail_url and str(thumbnail_url).strip():
            container.add_item(
                discord.ui.Section(
                    discord.ui.TextDisplay(title_text),
                    accessory=discord.ui.Thumbnail(thumbnail_url),
                )
            )
        else:
            container.add_item(discord.ui.TextDisplay(title_text))
        # チャンネル・再生時間・案内
        meta_text = pick_str(
            self.lang,
            ja=(
                f"**チャンネル:** {uploader}\n"
                f"**再生時間:** `{duration_str}`\n\n"
                f"ダウンロードしたい動画のフォーマットを選択してください。\n"
                f"（選択後に最良の音声と自動結合します）"
            ),
            en=(
                f"**Channel:** {uploader}\n"
                f"**Duration:** `{duration_str}`\n\n"
                f"Please select a video format to download.\n"
                f"(Best audio will be merged automatically.)"
            ),
        )
        container.add_item(discord.ui.TextDisplay(meta_text))
        # セレクトを ActionRow に載せる
        select_row = discord.ui.ActionRow()
        select_row.add_item(
            VideoFormatSelect(
                self.cog,
                self.info,
                self.url,
                requester_user_id=self.requester_user_id,
                lang=self.lang,
            )
        )
        container.add_item(select_row)
        # ルートへ追加
        self.add_item(container)


class YtdlpDownloaderCog(commands.Cog):
    """`/download_audio` `/download_video` — file.io 経由の一時メディア共有。"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # 任意 API キー（configs の music.file_io_api_key 等）
        music_cfg = (getattr(bot, "config", None) or {}).get("music") or {}
        api_key = music_cfg.get("file_io_api_key") if isinstance(music_cfg, dict) else None
        # file.io アップローダ
        self.fileio_uploader = FileIoUploader(api_key=api_key)
        self.exception_handler = YTDLPExceptionHandler()
        # SettingsDB
        self.settings_db = resolve_settings_db(bot)
        # 削除予定の UNIX 時刻を key ごとに保持する
        self._fileio_deletion_schedule: Dict[str, float] = {}
        # 稼働中の削除タスクを key ごとに保持する
        self._fileio_deletion_tasks: Dict[str, asyncio.Task[None]] = {}
        # 削除予定の更新を直列化する
        self._fileio_deletion_lock = asyncio.Lock()
        os.makedirs(DOWNLOAD_DIR, exist_ok=True)
        # 起動時に allowlist を読み込み（default.json → json コピー込み）
        load_allowed_extractors()

    def _reject_if_not_allowed(
        self,
        *,
        lang: str,
        url_or_query: Optional[str] = None,
        info: Optional[Dict[str, Any]] = None,
    ) -> Optional[str]:
        """
        allowlist / 成人 IE / プライベート IP を検査し、拒否時はユーザー向け文言を返す。
        許可なら None。
        """
        # URL / クエリの事前検査
        if url_or_query is not None:
            # 許可判定
            ok, reason, _ie = check_download_url_allowed(url_or_query)
            # 拒否なら文言
            if not ok:
                return self.exception_handler.get_allowlist_reject_message(
                    reason, lang=lang
                )
        # extract_info 後の再検査
        if info is not None:
            # info 判定
            ok, reason, _ie = check_extracted_info_allowed(info)
            # 拒否なら文言
            if not ok:
                return self.exception_handler.get_allowlist_reject_message(
                    reason, lang=lang
                )
        # 許可
        return None

    async def cog_load(self) -> None:
        """永続化済みの file.io 削除予定を復元する。"""
        # 保存済みの削除予定を読み込む
        self._fileio_deletion_schedule = self._load_fileio_deletion_schedule()
        # 各予定を現在のイベントループで再開する
        for file_key, delete_at in self._fileio_deletion_schedule.items():
            self._start_fileio_deletion_task(file_key, delete_at)

    def cog_unload(self) -> None:
        """削除予定を保持したままタスクを停止する。"""
        # 実行中タスクだけを停止する
        for task in self._fileio_deletion_tasks.values():
            task.cancel()

    def _load_fileio_deletion_schedule(self) -> Dict[str, float]:
        """削除予定を SettingsDB から読み込み、有効な値だけを返す。"""
        try:
            # namespace から取る
            schedule_data = self.settings_db.load(NS_FILEIO_DELETION_SCHEDULE)
        except Exception:
            # 読み込み不能な予定は安全に無視する
            logger.exception("file.io 削除予定の読み込みに失敗しました。")
            return {}
        # 無ければ空
        if schedule_data is None:
            return {}
        # 辞書以外は予定として扱わない
        if not isinstance(schedule_data, dict):
            logger.error("file.io 削除予定の形式が不正です。")
            return {}
        # 検証済みの削除予定を格納する
        schedule: Dict[str, float] = {}
        # 各 key と削除予定時刻を検証する
        for file_key, delete_at in schedule_data.items():
            # key が文字列以外なら無視する
            if not isinstance(file_key, str):
                continue
            try:
                # 削除予定時刻を浮動小数へ正規化する
                schedule[file_key] = float(delete_at)
            except (TypeError, ValueError):
                # 時刻へ変換できない値は無視する
                logger.warning("file.io 削除予定の時刻が不正です: %s", file_key)
        return schedule

    def _save_fileio_deletion_schedule(self) -> None:
        """削除予定を SettingsDB へ保存する。"""
        try:
            # 最新予定を namespace に書く
            self.settings_db.save(
                NS_FILEIO_DELETION_SCHEDULE,
                self._fileio_deletion_schedule,
            )
        except Exception:
            logger.exception("file.io 削除予定の保存に失敗しました。")

    def _start_fileio_deletion_task(self, file_key: str, delete_at: float) -> None:
        """同じ key の重複実行を避けて削除タスクを開始する。"""
        # すでに実行中なら同じ key を重複予約しない
        existing_task = self._fileio_deletion_tasks.get(file_key)
        if existing_task and not existing_task.done():
            return
        # 指定時刻に実行する削除タスクを開始する
        self._fileio_deletion_tasks[file_key] = asyncio.create_task(
            self._delete_fileio_file_at(file_key, delete_at)
        )

    async def schedule_fileio_deletion(
        self,
        file_key: str,
        *,
        delete_at: Optional[float] = None,
    ) -> None:
        """削除予定を保存して期限後の file.io 削除を開始する。"""
        # 削除予定時刻が無ければ標準の有効期限を使う
        scheduled_time = (
            time.time() + DELETE_DELAY_SECONDS
            if delete_at is None
            else delete_at
        )
        # 予定ファイルと実行タスクを一貫した状態で更新する
        async with self._fileio_deletion_lock:
            # key ごとに削除予定を登録する
            self._fileio_deletion_schedule[file_key] = scheduled_time
            # 再起動後にも復元できるよう予定を保存する
            self._save_fileio_deletion_schedule()
            # 現在のプロセスでも削除タスクを開始する
            self._start_fileio_deletion_task(file_key, scheduled_time)

    async def _delete_fileio_file_at(self, file_key: str, delete_at: float) -> None:
        """保存済みの予定時刻まで待機して file.io ファイルを削除する。"""
        try:
            # 過去の予定は再起動後すぐに実行する
            await asyncio.sleep(max(0.0, delete_at - time.time()))
            # file.io DELETE を実行する
            await self.fileio_uploader.delete_file(file_key)
        except asyncio.CancelledError:
            # 停止時は永続予定を残したまま上げる
            raise
        else:
            # 削除試行が終わった予定を永続ストアから外す
            async with self._fileio_deletion_lock:
                # 完了した key を予定から削除する
                self._fileio_deletion_schedule.pop(file_key, None)
                # 最新の予定を保存する
                self._save_fileio_deletion_schedule()
        finally:
            # 自分自身が登録中ならタスク一覧から外す
            if self._fileio_deletion_tasks.get(file_key) is asyncio.current_task():
                self._fileio_deletion_tasks.pop(file_key, None)

    @app_commands.command(
        name="download_audio",
        description="Downloads audio and shares it via file.io.",
    )
    @app_commands.describe(
        query="YouTube URL or search query.",
        audio_format="Output audio format.",
    )
    @app_commands.choices(
        audio_format=[
            app_commands.Choice(name="MP3", value="mp3"),
            app_commands.Choice(name="M4A", value="m4a"),
            app_commands.Choice(name="Opus", value="opus"),
            app_commands.Choice(name="FLAC", value="flac"),
            app_commands.Choice(name="WAV", value="wav"),
        ]
    )
    async def download_audio(self, interaction: discord.Interaction, query: str, audio_format: str):
        # app → guild → en
        lang = resolve_interaction_lang(interaction)
        # 思考表示で遅延応答
        await interaction.response.defer(thinking=True)
        # 許可リスト / プライベート IP の事前検査
        reject_msg = self._reject_if_not_allowed(lang=lang, url_or_query=query)
        # 拒否時は即エラー表示
        if reject_msg:
            err_view = StatusLayoutView(
                pick_str(
                    lang,
                    ja=f"### ❌ エラー\n{reject_msg}",
                    en=f"### ❌ Error\n{reject_msg}",
                ),
                accent=_ACCENT_ERROR,
            )
            await interaction.followup.send(view=err_view)
            return
        # 一時ファイル用 ID
        unique_id = str(uuid.uuid4())
        output_path = os.path.join(DOWNLOAD_DIR, f"{unique_id}.{audio_format}")
        # yt-dlp 音声抽出オプション
        ydl_opts = apply_youtube_ejs_opts(
            {
                "format": "bestaudio*/best*",
                "outtmpl": os.path.join(DOWNLOAD_DIR, f"{unique_id}.%(ext)s"),
                "postprocessors": [
                    {
                        "key": "FFmpegExtractAudio",
                        "preferredcodec": audio_format,
                        "preferredquality": "192",
                    }
                ],
                "noplaylist": True,
                "default_search": "ytsearch",
                "quiet": True,
                "no_warnings": True,
                "noprogress": True,
            }
        )

        message = None
        # 進捗用 LayoutView（後で差し替え）
        progress = StatusLayoutView(
            pick_str(
                lang,
                ja="### 📥 準備中\n情報を取得しています...",
                en="### 📥 Preparing\nFetching info...",
            )
        )
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                # メタ取得（ダウンロードなし）
                info = await asyncio.to_thread(ydl.extract_info, query, download=False)
                # 検索結果から None でない最初のメディア情報を選ぶ
                info = _select_download_info(info)
                # 有効なメディア情報が無ければダウンロードを開始しない
                if not info:
                    raise yt_dlp.utils.DownloadError(
                        "ダウンロード可能なメディア情報が見つかりませんでした。"
                    )
                # extract 後の IE / URL 再検証
                reject_after = self._reject_if_not_allowed(lang=lang, info=info)
                # 拒否ならエラー表示
                if reject_after:
                    err_view = StatusLayoutView(
                        pick_str(
                            lang,
                            ja=f"### ❌ エラー\n{reject_after}",
                            en=f"### ❌ Error\n{reject_after}",
                        ),
                        accent=_ACCENT_ERROR,
                    )
                    await interaction.followup.send(view=err_view)
                    return
                video_title = info.get("title", "audio")
                # 進捗本文を更新
                progress.update(
                    pick_str(
                        lang,
                        ja=(
                            f"### 📥 ダウンロード中\n"
                            f"**{video_title}** をダウンロード・変換しています..."
                        ),
                        en=(
                            f"### 📥 Downloading\n"
                            f"Downloading & converting **{video_title}**..."
                        ),
                    )
                )
                # 初回送信（V2）
                message = await interaction.followup.send(view=progress)
                # ダウンロード実行
                await asyncio.to_thread(ydl.download, [query])
            # 変換結果が無い
            if not os.path.exists(output_path):
                progress.update(
                    self.exception_handler.get_conversion_error(lang=lang),
                    accent=_ACCENT_ERROR,
                )
                await message.edit(view=progress)
                return
            # アップロード進捗
            progress.update(
                pick_str(
                    lang,
                    ja=(
                        f"### 🔼 アップロード中\n"
                        f"**{video_title}** を file.io にアップロードしています..."
                    ),
                    en=(
                        f"### 🔼 Uploading\n"
                        f"Uploading **{video_title}** to file.io..."
                    ),
                )
            )
            await message.edit(view=progress)
            upload_filename = f"{video_title}.{audio_format}"
            file_key, download_link = await self.fileio_uploader.upload_file(
                output_path, upload_filename
            )
            # アップロード失敗
            if not download_link:
                progress.update(
                    self.exception_handler.get_upload_error(lang=lang),
                    accent=_ACCENT_ERROR,
                )
                await message.edit(view=progress)
                return

            # 完了表示
            minutes = int(DELETE_DELAY_SECONDS / 60)
            ready = DownloadReadyLayoutView(
                title=video_title,
                download_link=download_link,
                expire_minutes=minutes,
                thumbnail_url=info.get("thumbnail"),
                lang=lang,
            )
            await message.edit(view=ready)
            # 期限後削除
            if file_key:
                await self.schedule_fileio_deletion(file_key)
        except Exception as e:
            # エラー文言
            error_msg = self.exception_handler.handle_exception(e, lang=lang)
            err_view = StatusLayoutView(
                pick_str(
                    lang,
                    ja=f"### ❌ エラー\n{error_msg}",
                    en=f"### ❌ Error\n{error_msg}",
                ),
                accent=_ACCENT_ERROR,
            )
            if message:
                await message.edit(view=err_view)
            else:
                await interaction.followup.send(view=err_view)
        finally:
            # UUID 接頭辞が一致する関連一時ファイルをすべて掃除する
            try:
                # 一時ディレクトリ内のファイルを走査する
                for item in os.listdir(DOWNLOAD_DIR):
                    # 今回の UUID で作成したファイルだけを対象にする
                    if item.startswith(unique_id):
                        try:
                            # 対象ファイルを削除する
                            os.remove(os.path.join(DOWNLOAD_DIR, item))
                        except OSError:
                            # 個別ファイルの削除失敗は処理全体を止めない
                            logger.warning(
                                "一時ファイルの削除に失敗しました: %s",
                                item,
                            )
            except OSError:
                # 一時ディレクトリの走査失敗は処理全体を止めない
                logger.exception("一時ファイルの掃除に失敗しました。")

    @app_commands.command(
        name="download_video",
        description="Downloads a video and shares it via file.io.",
    )
    @app_commands.describe(query="URL or search query of the video.")
    async def download_video(self, interaction: discord.Interaction, query: str):
        # app → guild → en
        lang = resolve_interaction_lang(interaction)
        # 思考表示で遅延応答
        await interaction.response.defer(thinking=True)
        # 許可リスト / プライベート IP の事前検査
        reject_msg = self._reject_if_not_allowed(lang=lang, url_or_query=query)
        # 拒否時は即エラー表示
        if reject_msg:
            err_view = StatusLayoutView(
                pick_str(
                    lang,
                    ja=f"### ❌ エラー\n{reject_msg}",
                    en=f"### ❌ Error\n{reject_msg}",
                ),
                accent=_ACCENT_ERROR,
            )
            await interaction.followup.send(view=err_view)
            return
        try:
            # メタ取得用オプション
            ydl_opts = apply_youtube_ejs_opts(
                {"quiet": True, "default_search": "ytsearch", "noplaylist": True, "noprogress": True}
            )
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = await asyncio.to_thread(ydl.extract_info, query, download=False)
                # 検索結果から None でない最初のメディア情報を選ぶ
                info = _select_download_info(info)
                # 有効なメディア情報が無ければ選択 UI を表示しない
                if not info:
                    raise yt_dlp.utils.DownloadError(
                        "ダウンロード可能なメディア情報が見つかりませんでした。"
                    )
            # extract 後の IE / URL 再検証
            reject_after = self._reject_if_not_allowed(lang=lang, info=info)
            # 拒否ならエラー表示
            if reject_after:
                err_view = StatusLayoutView(
                    pick_str(
                        lang,
                        ja=f"### ❌ エラー\n{reject_after}",
                        en=f"### ❌ Error\n{reject_after}",
                    ),
                    accent=_ACCENT_ERROR,
                )
                await interaction.followup.send(view=err_view)
                return
            # 元ページ URL
            video_url = info.get("webpage_url", query)
            # Components V2 の選択 UI
            view = VideoSelectLayoutView(
                self,
                info,
                video_url,
                requester_user_id=interaction.user.id,
                lang=lang,
            )
            await interaction.followup.send(view=view)
        except Exception as e:
            # エラー文言（1 回だけ生成しログ重複を防ぐ）
            error_msg = self.exception_handler.handle_exception(e, lang=lang)
            err_view = StatusLayoutView(
                pick_str(
                    lang,
                    ja=f"### ❌ エラー\n{error_msg}",
                    en=f"### ❌ Error\n{error_msg}",
                ),
                accent=_ACCENT_ERROR,
            )
            await interaction.followup.send(view=err_view)


async def setup(bot: commands.Bot):
    await bot.add_cog(YtdlpDownloaderCog(bot))
