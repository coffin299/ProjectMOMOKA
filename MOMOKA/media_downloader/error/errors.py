# MOMOKA/media_downloader/error/errors.py
from __future__ import annotations

import logging
import re

import yt_dlp

from MOMOKA.utilities.locale import pick_str

logger = logging.getLogger(__name__)

# Discordのメッセージ最大長
DISCORD_MESSAGE_MAX_LENGTH = 1990
# yt-dlp の詳細メッセージに割り当てる最大文字数
YTDLP_ERROR_DETAIL_MAX_LENGTH = 1500
# yt-dlp / ターミナル由来の ANSI エスケープ（色コード等）を除去する
_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*m")


def _strip_ansi_escapes(text: str) -> str:
    """Discord 表示向けに ANSI 色コードを取り除く。"""
    # 色付き ERROR: などをプレーンテキストへ戻す
    return _ANSI_ESCAPE_RE.sub("", text)


def _truncate_ytdlp_error_detail(error_detail: object) -> str:
    """Discord の上限内で yt-dlp のエラー詳細を省略する。"""
    # 例外詳細を安全に文字列へ変換する
    detail = str(error_detail)
    # Discord に色コードが残らないよう除去する
    detail = _strip_ansi_escapes(detail)
    # 上限以下なら元の詳細を返す
    if len(detail) <= YTDLP_ERROR_DETAIL_MAX_LENGTH:
        return detail
    # 省略したことを示す末尾を確保して切り詰める
    suffix = "\n…（エラー詳細は省略されました）"
    # 末尾表示を含めても上限を超えない本文を切り出す
    return f"{detail[:YTDLP_ERROR_DETAIL_MAX_LENGTH - len(suffix)]}{suffix}"


def _classify_ytdlp_download_error(error_text: str) -> str:
    """DownloadError 文言からユーザー向けメッセージ種別を判定する。"""
    # 照合用に小文字化する
    lowered = error_text.lower()
    # 未対応 URL（一覧・タグページ等を含む）
    if "unsupported url" in lowered:
        return "unsupported_url"
    # 選択フォーマットが取得できない場合
    if "requested format is not available" in lowered:
        return "format_unavailable"
    # コンテンツ制限（非公開・年齢制限・地域制限・Instagram 等）
    content_restricted_markers = (
        "isn't available to everyone",
        "isn't available",
        "video unavailable",
        "private video",
        "sign in to confirm",
        "members only",
        "geo restricted",
        "not available in your country",
        "removed by the uploader",
        "login required",
        "account authentication",
        "this content isn't available",
    )
    # いずれかに一致すればコンテンツ制限扱い
    if any(marker in lowered for marker in content_restricted_markers):
        return "content_restricted"
    # 上記以外は汎用 DownloadError 扱い
    return "generic"


def _is_expected_ytdlp_error(e: Exception) -> bool:
    """ユーザー入力・コンテンツ側の問題で起きる想定内エラーか判定する。"""
    # DownloadError は大半が URL / 権限 / コンテンツ制限由来
    if isinstance(e, yt_dlp.utils.DownloadError):
        return True
    # それ以外は想定外
    return False


class YTDLPExceptionHandler:
    """yt-dlpと一時共有（Tunnel）関連のエラーを処理し、ユーザー向けのメッセージを生成するクラス。"""

    def handle_exception(self, e: Exception, *, lang: str = "en") -> str:
        """
        例外オブジェクトを受け取り、種類に応じて適切なエラーメッセージ文字列を返す。

        Args:
            e (Exception): 捕捉された例外オブジェクト。
            lang: UI 言語（ja / en）。

        Returns:
            str: Discordに返信するエラーメッセージ。
        """
        # 想定内（URL / コンテンツ制限）は WARNING、それ以外は ERROR + traceback
        if _is_expected_ytdlp_error(e):
            logger.warning("YTDLP/media_share expected error: %s", e)
        else:
            logger.error(
                "An error occurred in YTDLP/media_share process: %s", e, exc_info=True
            )

        if isinstance(e, yt_dlp.utils.DownloadError):
            # yt-dlp の長い詳細を Discord の表示上限内へ収める
            error_detail = _truncate_ytdlp_error_detail(e)
            # 詳細文言からユーザー向けの原因カテゴリを決める
            error_kind = _classify_ytdlp_download_error(error_detail)
            # 未対応 URL（サイト非対応・一覧ページなど）
            if error_kind == "unsupported_url":
                return pick_str(
                    lang,
                    ja=(
                        f"この URL / サイトには対応していません。"
                        f"動画の直接リンクを指定してください"
                        f"（タグや一覧ページは不可）。\n```{error_detail}```"
                    ),
                    en=(
                        f"This URL / site is not supported. "
                        f"Please use a direct video link "
                        f"(tag or listing pages are not allowed)."
                        f"\n```{error_detail}```"
                    ),
                )
            # 選択した画質・形式が取得できない場合
            if error_kind == "format_unavailable":
                return pick_str(
                    lang,
                    ja=(
                        f"選択したフォーマットを取得できませんでした。"
                        f"別の画質を選ぶか、別の URL を試してください。"
                        f"\n```{error_detail}```"
                    ),
                    en=(
                        f"The selected format is not available. "
                        f"Please try another quality or a different URL."
                        f"\n```{error_detail}```"
                    ),
                )
            # 非公開・年齢制限・地域制限などコンテンツ側の制限
            if error_kind == "content_restricted":
                return pick_str(
                    lang,
                    ja=(
                        f"このコンテンツはダウンロードできません。"
                        f"非公開・制限付き・地域限定の可能性があります。"
                        f"\n```{error_detail}```"
                    ),
                    en=(
                        f"This content cannot be downloaded. "
                        f"It may be private, restricted, or region-locked."
                        f"\n```{error_detail}```"
                    ),
                )
            # その他の DownloadError は従来の汎用メッセージ
            return pick_str(
                lang,
                ja=(
                    f"動画が見つからないか、ダウンロードが許可されていません。"
                    f"検索クエリやURLを確認してください。\n```{error_detail}```"
                ),
                en=(
                    f"Video not found or download is not allowed. "
                    f"Please check the query or URL.\n```{error_detail}```"
                ),
            )
        else:
            # 例外名を含む長い詳細を Discord の表示上限内へ収める
            error_detail = _truncate_ytdlp_error_detail(
                f"{type(e).__name__}: {e}"
            )
            return pick_str(
                lang,
                ja=(
                    f"処理中に予期せぬエラーが発生しました。\n"
                    f"```{error_detail}```"
                ),
                en=(
                    f"An unexpected error occurred during processing.\n"
                    f"```{error_detail}```"
                ),
            )

    def get_merge_error(self, *, lang: str = "en") -> str:
        """動画と音声の結合に失敗した場合のエラーメッセージを返す。"""
        return pick_str(
            lang,
            ja="エラー: 動画と音声の結合に失敗しました。",
            en="Error: Failed to merge video and audio.",
        )

    def get_upload_error(self, *, lang: str = "en") -> str:
        """一時共有リンクの作成に失敗した場合のエラーメッセージを返す。"""
        return pick_str(
            lang,
            ja="エラー: 一時共有リンクの作成に失敗しました（media_share / Tunnel 設定を確認してください）。",
            en="Error: Failed to create a temporary share link (check media_share / Tunnel settings).",
        )

    def get_conversion_error(self, *, lang: str = "en") -> str:
        """ファイル変換に失敗した場合のエラーメッセージを返す。"""
        return pick_str(
            lang,
            ja="エラー: ファイル変換に失敗しました。FFmpegがインストールされていますか？",
            en="Error: File conversion failed. Is FFmpeg installed?",
        )

    def get_unsupported_site_error(self, *, lang: str = "en") -> str:
        """許可リスト外サイトを拒否したときのメッセージ。"""
        return pick_str(
            lang,
            ja=(
                "このサイトからのダウンロードには対応していません。"
                "許可された動画サイトの URL を指定してください。"
            ),
            en=(
                "Downloads from this site are not supported. "
                "Please use a URL from an allowed video site."
            ),
        )

    def get_forbidden_site_error(self, *, lang: str = "en") -> str:
        """成人向けなど禁止エクストラクターを拒否したときのメッセージ。"""
        return pick_str(
            lang,
            ja="このサイトからのダウンロードは禁止されています。",
            en="Downloads from this site are forbidden.",
        )

    def get_unsafe_url_error(self, *, lang: str = "en") -> str:
        """プライベート IP / SSRF 対象 URL を拒否したときのメッセージ。"""
        return pick_str(
            lang,
            ja=(
                "この URL は安全上の理由でダウンロードできません。"
                "（ローカル／プライベートアドレスは不可）"
            ),
            en=(
                "This URL cannot be downloaded for security reasons "
                "(local/private addresses are not allowed)."
            ),
        )

    def get_allowlist_reject_message(
        self,
        reason_code: str,
        *,
        lang: str = "en",
    ) -> str:
        """allowlist / URL 安全性の reason_code からユーザー向け文言を返す。"""
        # 成人・禁止サイト
        if reason_code == "forbidden_site":
            return self.get_forbidden_site_error(lang=lang)
        # プライベート IP 等
        if reason_code == "unsafe_url":
            return self.get_unsafe_url_error(lang=lang)
        # 未対応・Generic・リスト外・IE 無し
        return self.get_unsupported_site_error(lang=lang)
