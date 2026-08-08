# MOMOKA/media_downloader/fileio_uploader.py
# file.io への一時アップロードと削除。
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional, Tuple

import aiohttp

logger = logging.getLogger(__name__)

# file.io 本番 API
_FILEIO_BASE = "https://file.io"
# アップロード有効期限（API TimePeriod）
_DEFAULT_EXPIRES = "10m"


class FileIoUploader:
    """file.io にファイルを上げ、key で削除する。"""

    def __init__(self, *, api_key: Optional[str] = None) -> None:
        # 任意の Bearer（未設定なら匿名 POST）
        self._api_key = (api_key or "").strip() or None

    def _headers(self) -> dict:
        """認証ヘッダを組み立てる。"""
        # キーが無ければ空
        if not self._api_key:
            return {}
        # Bearer を付ける
        return {"Authorization": f"Bearer {self._api_key}"}

    async def upload_file(
        self,
        file_path: str,
        file_name: str,
        *,
        expires: str = _DEFAULT_EXPIRES,
        auto_delete: bool = True,
    ) -> Tuple[Optional[str], Optional[str]]:
        """
        ファイルを file.io へアップロードする。
        戻り値: (key, download_link)。失敗時は (None, None)。
        """
        # パスを Path にする
        path = Path(file_path)
        # 存在確認
        if not path.is_file():
            logger.error("file.io upload: file not found: %s", file_path)
            return None, None
        # multipart フォーム用フィールド
        form = aiohttp.FormData()
        # 有効期限
        form.add_field("expires", expires)
        # 念のための自動削除
        form.add_field("autoDelete", "true" if auto_delete else "false")
        # 本体ファイル（maxDownloads は送らない＝無制限）
        form.add_field(
            "file",
            path.read_bytes(),
            filename=file_name or path.name,
            content_type="application/octet-stream",
        )
        try:
            # 短いタイムアウトで POST する
            timeout = aiohttp.ClientTimeout(total=300, connect=30)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(
                    f"{_FILEIO_BASE}/",
                    data=form,
                    headers=self._headers(),
                ) as resp:
                    # 本文を JSON として読む
                    try:
                        data = await resp.json(content_type=None)
                    except Exception:
                        text = await resp.text()
                        logger.error(
                            "file.io upload: non-JSON response %s: %s",
                            resp.status,
                            text[:500],
                        )
                        return None, None
                    # HTTP 失敗
                    if resp.status >= 400 or not isinstance(data, dict):
                        logger.error(
                            "file.io upload failed status=%s body=%s",
                            resp.status,
                            data,
                        )
                        return None, None
                    # success フラグがあれば確認する
                    if data.get("success") is False:
                        logger.error("file.io upload success=false: %s", data)
                        return None, None
                    # key / link を取り出す
                    key = data.get("key") or data.get("id")
                    link = data.get("link")
                    if not key or not link:
                        logger.error("file.io upload missing key/link: %s", data)
                        return None, None
                    # 文字列化して返す
                    return str(key), str(link)
        except Exception as exc:  # noqa: BLE001
            logger.error("file.io upload exception: %s", exc, exc_info=True)
            return None, None

    async def delete_file(self, key: str) -> None:
        """file.io 上のファイルを key で削除する。"""
        # 空キーは無視
        if not key:
            return
        try:
            # 削除用タイムアウト
            timeout = aiohttp.ClientTimeout(total=30, connect=10)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.delete(
                    f"{_FILEIO_BASE}/{key}",
                    headers=self._headers(),
                ) as resp:
                    # 404 は既に消えている扱い
                    if resp.status in (200, 204, 404):
                        logger.info("file.io deleted key=%s status=%s", key, resp.status)
                        return
                    # それ以外はログ
                    text = await resp.text()
                    logger.error(
                        "file.io delete failed key=%s status=%s body=%s",
                        key,
                        resp.status,
                        text[:300],
                    )
        except Exception as exc:  # noqa: BLE001
            logger.error("file.io delete exception key=%s: %s", key, exc, exc_info=True)
