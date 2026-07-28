# MOMOKA/media_downloader/ytdlp_downloader_cog.py
# yt-dlp + Google Drive 共有ダウンローダー（Components V2 UI）。
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
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload

from MOMOKA.media_downloader.error.errors import YTDLPExceptionHandler
from MOMOKA.music.plugins.ytdlp_wrapper import apply_youtube_ejs_opts
from MOMOKA.utilities.locale import pick_str, resolve_interaction_lang

# --- 設定項目 ---
CLIENT_SECRETS_FILE = "client_secrets.json"
TOKEN_FILE = "token.json"
GDRIVE_FOLDER_ID = "1g5KmfB7xVrL-Y59RTf6f2IDbbJsTSFZs"  # ← ここを必ず書き換えてください
DELETE_DELAY_SECONDS = 600
DOWNLOAD_DIR = "temp_media_gdrive"
GDRIVE_DELETION_SCHEDULE_FILE = os.path.join(
    "data",
    "gdrive_deletion_schedule.json",
)
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
        cog_instance: "YtdlpGdriveCog",
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
                        f"**{video_title}** を Google Drive にアップロードしています..."
                    ),
                    en=(
                        f"### 🔼 Uploading\n"
                        f"Uploading **{video_title}** to Google Drive..."
                    ),
                ),
                accent=_ACCENT_PROGRESS,
            )
            await interaction.edit_original_response(content=None, embed=None, view=progress)
            # アップロードファイル名
            upload_filename = f"{video_title}.mp4"
            # GDrive へアップロード
            file_id, download_link = await asyncio.to_thread(
                self.cog.gdrive_uploader.upload_file,
                downloaded_file_path,
                upload_filename,
                GDRIVE_FOLDER_ID,
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
            await self.cog.schedule_gdrive_deletion(file_id)
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
        cog_instance: "YtdlpGdriveCog",
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


class GDriveUploader:
    """Google Drive 認証・アップロード・削除。"""

    def __init__(self, client_secrets_file, token_file):
        self.scopes = ["https://www.googleapis.com/auth/drive"]
        self.client_secrets_file = client_secrets_file
        self.token_file = token_file
        self.service = self._get_drive_service()

    def _get_drive_service(self):
        creds = None
        if os.path.exists(self.token_file):
            creds = Credentials.from_authorized_user_file(self.token_file, self.scopes)
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                try:
                    creds.refresh(Request())
                except Exception as e:
                    logger.error(f"トークンのリフレッシュに失敗しました: {e}")
                    creds = None
            if not creds:
                logger.warning("-" * 60)
                logger.warning("Google Driveの認証が必要です。")
                logger.warning("コンソールに表示されるURLをブラウザで開き、アカウントを認証してください。")
                logger.warning("-" * 60)
                try:
                    flow = InstalledAppFlow.from_client_secrets_file(self.client_secrets_file, self.scopes)
                    creds = flow.run_local_server(port=0)
                except FileNotFoundError:
                    logger.critical(
                        f"エラー: クライアントシークレットファイル '{self.client_secrets_file}' が見つかりません。"
                    )
                    return None
            with open(self.token_file, "w") as token:
                token.write(creds.to_json())
        try:
            return build("drive", "v3", credentials=creds)
        except Exception as e:
            logger.error(f"Google Driveサービスのビルド中にエラー: {e}")
            return None

    def upload_file(self, file_path, file_name, folder_id):
        if not self.service:
            return None, None
        file_metadata = {"name": file_name, "parents": [folder_id]}
        media = MediaFileUpload(file_path, resumable=True)
        file = self.service.files().create(
            body=file_metadata,
            media_body=media,
            fields="id",
        ).execute()
        # 作成直後から失敗時の孤立ファイルを追跡する
        file_id = file.get("id")
        try:
            # 誰でも読み取れる共有権限を付与する
            self.service.permissions().create(
                fileId=file_id,
                body={"type": "anyone", "role": "reader"},
            ).execute()
        except Exception:
            # 権限付与に失敗したファイルを Drive から削除する
            if file_id:
                self.delete_file(file_id)
            raise
        download_link = f"https://drive.google.com/uc?export=download&id={file_id}"
        return file_id, download_link

    def delete_file(self, file_id):
        if not self.service:
            return
        try:
            self.service.files().delete(fileId=file_id).execute()
            logger.info(f"Google Drive上のファイルを削除しました: {file_id}")
        except HttpError as e:
            if e.resp.status == 404:
                logger.warning(f"削除しようとしたファイルが見つかりませんでした: {file_id}")
            else:
                logger.error(f"Google Drive上のファイル削除中にエラーが発生しました: {e}")
        except Exception as e:
            logger.error(f"Google Drive上のファイル削除中に予期せぬエラーが発生しました: {e}")


class YtdlpGdriveCog(commands.Cog):
    """`/download_audio` `/download_video` — Google Drive 経由のメディア共有。"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.gdrive_uploader = GDriveUploader(CLIENT_SECRETS_FILE, TOKEN_FILE)
        self.exception_handler = YTDLPExceptionHandler()
        # 削除予定の UNIX 時刻をファイル ID ごとに保持する
        self._gdrive_deletion_schedule: Dict[str, float] = {}
        # 稼働中の削除タスクをファイル ID ごとに保持する
        self._gdrive_deletion_tasks: Dict[str, asyncio.Task[None]] = {}
        # 削除予定の更新を直列化する
        self._gdrive_deletion_lock = asyncio.Lock()
        os.makedirs(DOWNLOAD_DIR, exist_ok=True)

    async def cog_load(self) -> None:
        """永続化済みの Google Drive 削除予定を復元する。"""
        # 保存済みの削除予定を読み込む
        self._gdrive_deletion_schedule = self._load_gdrive_deletion_schedule()
        # 各予定を現在のイベントループで再開する
        for file_id, delete_at in self._gdrive_deletion_schedule.items():
            self._start_gdrive_deletion_task(file_id, delete_at)

    def cog_unload(self) -> None:
        """再読み込み時に削除予定を保持したままタスクを停止する。"""
        # 再読み込み後に復元できるよう実行中タスクだけを停止する
        for task in self._gdrive_deletion_tasks.values():
            task.cancel()

    def _load_gdrive_deletion_schedule(self) -> Dict[str, float]:
        """削除予定 JSON を読み込み、有効な値だけを返す。"""
        try:
            # JSON ファイルを UTF-8 で開く
            with open(GDRIVE_DELETION_SCHEDULE_FILE, "r", encoding="utf-8") as file:
                schedule_data = json.load(file)
        except FileNotFoundError:
            # 初回起動時は予定なしとして扱う
            return {}
        except (OSError, json.JSONDecodeError):
            # 読み込み不能な予定ファイルは安全に無視する
            logger.exception("Google Drive 削除予定ファイルの読み込みに失敗しました。")
            return {}
        # 辞書以外の JSON は予定として扱わない
        if not isinstance(schedule_data, dict):
            logger.error("Google Drive 削除予定ファイルの形式が不正です。")
            return {}
        # 検証済みの削除予定を格納する
        schedule: Dict[str, float] = {}
        # 各ファイル ID と削除予定時刻を検証する
        for file_id, delete_at in schedule_data.items():
            # ファイル ID が文字列以外なら無視する
            if not isinstance(file_id, str):
                continue
            try:
                # 削除予定時刻を浮動小数へ正規化する
                schedule[file_id] = float(delete_at)
            except (TypeError, ValueError):
                # 時刻へ変換できない値は無視する
                logger.warning(
                    "Google Drive 削除予定の時刻が不正です: %s",
                    file_id,
                )
        return schedule

    def _save_gdrive_deletion_schedule(self) -> None:
        """削除予定を JSON ファイルへ原子的に保存する。"""
        # 中断時の破損を避けるため一時ファイルのパスを作る
        temporary_file = f"{GDRIVE_DELETION_SCHEDULE_FILE}.tmp"
        try:
            # 保存先ディレクトリを取得する
            directory = os.path.dirname(GDRIVE_DELETION_SCHEDULE_FILE)
            # ディレクトリが指定されている場合だけ作成する
            if directory:
                os.makedirs(directory, exist_ok=True)
            # 一時ファイルへ最新予定を書き込む
            with open(temporary_file, "w", encoding="utf-8") as file:
                json.dump(
                    self._gdrive_deletion_schedule,
                    file,
                    ensure_ascii=False,
                    indent=2,
                )
            # 一時ファイルを正式ファイルへ置換する
            os.replace(temporary_file, GDRIVE_DELETION_SCHEDULE_FILE)
        except OSError:
            # 保存失敗は削除処理を止めずに記録する
            logger.exception("Google Drive 削除予定ファイルの保存に失敗しました。")
            try:
                # 残った一時ファイルを掃除する
                os.remove(temporary_file)
            except OSError:
                # 一時ファイルの掃除失敗は追加の例外にしない
                pass

    def _start_gdrive_deletion_task(self, file_id: str, delete_at: float) -> None:
        """同じファイルの重複実行を避けて削除タスクを開始する。"""
        # すでに実行中なら同じファイルを重複予約しない
        existing_task = self._gdrive_deletion_tasks.get(file_id)
        if existing_task and not existing_task.done():
            return
        # 指定時刻に実行する削除タスクを開始する
        self._gdrive_deletion_tasks[file_id] = asyncio.create_task(
            self._delete_gdrive_file_at(file_id, delete_at)
        )

    async def schedule_gdrive_deletion(
        self,
        file_id: str,
        *,
        delete_at: Optional[float] = None,
    ) -> None:
        """削除予定を保存して期限後の Google Drive 削除を開始する。"""
        # 削除予定時刻が無ければ標準の有効期限を使う
        scheduled_time = (
            time.time() + DELETE_DELAY_SECONDS
            if delete_at is None
            else delete_at
        )
        # 予定ファイルと実行タスクを一貫した状態で更新する
        async with self._gdrive_deletion_lock:
            # ファイル ID ごとに削除予定を登録する
            self._gdrive_deletion_schedule[file_id] = scheduled_time
            # 再起動後にも復元できるよう予定を保存する
            self._save_gdrive_deletion_schedule()
            # 現在のプロセスでも削除タスクを開始する
            self._start_gdrive_deletion_task(file_id, scheduled_time)

    async def _delete_gdrive_file_at(self, file_id: str, delete_at: float) -> None:
        """保存済みの予定時刻まで待機して Drive ファイルを削除する。"""
        try:
            # 過去の予定は再起動後すぐに実行する
            await asyncio.sleep(max(0.0, delete_at - time.time()))
            # Google Drive API の同期削除をスレッドで実行する
            await asyncio.to_thread(self.gdrive_uploader.delete_file, file_id)
        except asyncio.CancelledError:
            # 再読み込み時は永続予定を残したまま停止する
            raise
        else:
            # 削除試行が終わった予定を永続ストアから外す
            async with self._gdrive_deletion_lock:
                # 完了したファイル ID を予定から削除する
                self._gdrive_deletion_schedule.pop(file_id, None)
                # 最新の予定を JSON へ保存する
                self._save_gdrive_deletion_schedule()
        finally:
            # 自分自身が登録中ならタスク一覧から外す
            if self._gdrive_deletion_tasks.get(file_id) is asyncio.current_task():
                self._gdrive_deletion_tasks.pop(file_id, None)

    @app_commands.command(
        name="download_audio",
        description="Downloads audio and shares it via Google Drive.",
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
        # GDrive 未初期化なら即エラー
        if not self.gdrive_uploader.service:
            await interaction.response.send_message(
                self.exception_handler.get_gdrive_init_error(lang=lang)
            )
            return
        # 思考表示で遅延応答
        await interaction.response.defer(thinking=True)
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

        temp_original_file_path = None
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
                # 元ファイルパスを控える
                temp_original_file_path = ydl.prepare_filename(info)
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
                        f"**{video_title}** を Google Drive にアップロードしています..."
                    ),
                    en=(
                        f"### 🔼 Uploading\n"
                        f"Uploading **{video_title}** to Google Drive..."
                    ),
                )
            )
            await message.edit(view=progress)
            upload_filename = f"{video_title}.{audio_format}"
            file_id, download_link = await asyncio.to_thread(
                self.gdrive_uploader.upload_file, output_path, upload_filename, GDRIVE_FOLDER_ID
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
            await self.schedule_gdrive_deletion(file_id)
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
        description="Downloads a video and shares it via Google Drive.",
    )
    @app_commands.describe(query="URL or search query of the video.")
    async def download_video(self, interaction: discord.Interaction, query: str):
        # app → guild → en
        lang = resolve_interaction_lang(interaction)
        # GDrive 未初期化なら即エラー
        if not self.gdrive_uploader.service:
            await interaction.response.send_message(
                self.exception_handler.get_gdrive_init_error(lang=lang)
            )
            return
        # 思考表示で遅延応答
        await interaction.response.defer(thinking=True)
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
    await bot.add_cog(YtdlpGdriveCog(bot))
