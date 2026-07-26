# MOMOKA/link_fix/settings_store.py
# ギルド単位の Link Fix 設定の読込・保存。
from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
import tempfile
from typing import Any, Dict, Optional

from MOMOKA.link_fix.presets import (
    get_link_fix_config,
    get_site_meta,
    list_site_ids,
    normalize_domain,
)

logger = logging.getLogger(__name__)


class LinkFixSettingsStore:
    """data/link_fix_settings.json を扱うストア。"""

    def __init__(self, bot_config: Dict[str, Any], project_root: Optional[Path] = None) -> None:
        # bot 全体 config を保持する
        self.bot_config = bot_config
        # プロジェクトルート（未指定なら cwd）
        self.project_root = project_root or Path.cwd()
        # link_fix セクション
        section = get_link_fix_config(bot_config)
        # 相対パス
        rel = str(section.get("settings_path") or "data/link_fix_settings.json")
        # 絶対パスに解決する
        self.path = (self.project_root / rel).resolve()
        # メモリ上の設定
        self._data: Dict[str, Dict[str, Any]] = {}
        # 同時に届く設定更新を直列化する
        self._lock = asyncio.Lock()
        # 初回読込
        self.load()

    def load(self) -> None:
        """ファイルから読み込む。無ければ空。"""
        # 親ディレクトリを用意する
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # 無ければ空 dict
        if not self.path.exists():
            self._data = {}
            return
        # 読み込む
        try:
            with self.path.open("r", encoding="utf-8") as f:
                raw = json.load(f)
            # dict でなければ空
            if not isinstance(raw, dict):
                self._data = {}
                return
            # ギルド id を文字列キーで保持する
            self._data = {str(k): v for k, v in raw.items() if isinstance(v, dict)}
        except (OSError, json.JSONDecodeError) as exc:
            # 壊れていればログして空にする
            logger.error("Failed to load link_fix settings: %s", exc)
            self._data = {}

    async def save(self) -> None:
        """ファイルへ排他的かつ原子的に保存する。"""
        # 親を確保する
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # 更新処理と保存処理が競合しないようにする
        async with self._lock:
            # ロック取得済みの保存処理を呼ぶ
            self._save_locked()

    def _save_locked(self) -> None:
        """ロック取得済みの状態を原子的にファイルへ保存する。"""
        # 一時ファイルのパスを初期化する
        temp_path: Optional[str] = None
        try:
            # 対象と同じディレクトリに一時ファイルを作る
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.path.parent,
                prefix=f"{self.path.name}.",
                suffix=".tmp",
                delete=False,
            ) as temp_file:
                # 完全な JSON を一時ファイルへ書き込む
                json.dump(self._data, temp_file, indent=2, ensure_ascii=False)
                # OS バッファの内容をディスクへ反映する
                temp_file.flush()
                os.fsync(temp_file.fileno())
                # 原子的な置換と後片付けにパスを保存する
                temp_path = temp_file.name
            # 完成済みファイルだけを既存ファイルと原子的に置き換える
            os.replace(temp_path, self.path)
        except OSError as exc:
            # 失敗をログする
            logger.error("Failed to save link_fix settings: %s", exc)
        finally:
            # 置換されなかった一時ファイルを残さない
            if temp_path and os.path.exists(temp_path):
                try:
                    os.unlink(temp_path)
                except OSError as exc:
                    logger.warning("Failed to remove temporary link_fix settings: %s", exc)

    def _guild_key(self, guild_id: int) -> str:
        """ギルド id を辞書キーにする。"""
        return str(guild_id)

    def get_guild(self, guild_id: int) -> Dict[str, Any]:
        """ギルド生設定（無ければ空 dict のコピー）。"""
        # キー
        key = self._guild_key(guild_id)
        # 無ければ空
        entry = self._data.get(key) or {}
        # コピーを返す（呼び出し側の破壊を防ぐ）
        return dict(entry) if isinstance(entry, dict) else {}

    def is_feature_enabled(self, guild_id: int) -> bool:
        """ギルド全体の有効フラグ（未設定時は YAML デフォルト）。"""
        # ギルド設定
        guild = self.get_guild(guild_id)
        # 明示値があればそれを使う
        if "enabled" in guild:
            return bool(guild.get("enabled"))
        # YAML デフォルト
        section = get_link_fix_config(self.bot_config)
        # 未設定時は無効（ユーザーが /linkfix で有効化する想定）
        return bool(section.get("enabled", False))

    async def set_feature_enabled(self, guild_id: int, enabled: bool) -> None:
        """全体 on/off を保存する。"""
        # 更新と保存を一続きの排他処理にする
        async with self._lock:
            # キーを作る
            key = self._guild_key(guild_id)
            # 既存設定を確保する
            entry = self._data.setdefault(key, {})
            # フラグを書く
            entry["enabled"] = bool(enabled)
            # 更新後の状態を保存する
            self._save_locked()

    def get_site(self, guild_id: int, site_id: str) -> Dict[str, Any]:
        """サイト単位のギルド上書き（無ければ空）。"""
        # ギルド
        guild = self.get_guild(guild_id)
        # sites
        sites = guild.get("sites") or {}
        # dict でなければ空
        if not isinstance(sites, dict):
            return {}
        # サイト
        site = sites.get(site_id) or {}
        # dict のみ
        return dict(site) if isinstance(site, dict) else {}

    def get_all_sites_overrides(self, guild_id: int) -> Dict[str, Any]:
        """ギルドの sites 上書き全体。"""
        # ギルド
        guild = self.get_guild(guild_id)
        # sites
        sites = guild.get("sites") or {}
        # dict のみ
        return dict(sites) if isinstance(sites, dict) else {}

    def is_site_enabled(self, guild_id: int, site_id: str) -> bool:
        """サイトが有効か（ギルド上書き → YAML）。"""
        # ギルドサイト
        site = self.get_site(guild_id, site_id)
        # 明示があればそれ
        if "enabled" in site:
            return bool(site.get("enabled"))
        # YAML
        meta = get_site_meta(self.bot_config, site_id)
        return bool(meta.get("enabled", True))

    async def set_site_enabled(
        self, guild_id: int, site_id: str, enabled: bool
    ) -> None:
        """サイト on/off を保存する。"""
        # 更新と保存を一続きの排他処理にする
        async with self._lock:
            # サイト dict を確保する
            site = self._ensure_site(guild_id, site_id)
            # フラグを書く
            site["enabled"] = bool(enabled)
            # 更新後の状態を保存する
            self._save_locked()

    async def set_all_sites_enabled(self, guild_id: int, enabled: bool) -> None:
        """定義済み全サイトの on/off を一括で保存する。"""
        # 更新と保存を一続きの排他処理にする
        async with self._lock:
            # 全サイト id を取る
            for site_id in list_site_ids(self.bot_config):
                # サイト dict を確保する
                site = self._ensure_site(guild_id, site_id)
                # 有効フラグを書く
                site["enabled"] = bool(enabled)
            # まとめて1回だけ保存する
            self._save_locked()

    async def set_fix_domain(
        self, guild_id: int, site_id: str, domain: str
    ) -> bool:
        """Fix 先ドメインを保存する。不正なら False。"""
        # 正規化する
        normalized = normalize_domain(domain)
        # 失敗
        if not normalized:
            return False
        # 更新と保存を一続きの排他処理にする
        async with self._lock:
            # サイトを確保する
            site = self._ensure_site(guild_id, site_id)
            # 正規化済みの値を書く
            site["fix_domain"] = normalized
            # 更新後の状態を保存する
            self._save_locked()
        # 成功
        return True

    async def set_match_domains(
        self, guild_id: int, site_id: str, domains: list[str]
    ) -> bool:
        """Fix 元ドメイン一覧を保存する。"""
        # 正規化リスト
        cleaned: list[str] = []
        # 走査する
        for item in domains:
            normalized = normalize_domain(str(item))
            if normalized and normalized not in cleaned:
                cleaned.append(normalized)
        # 空は拒否
        if not cleaned:
            return False
        # 更新と保存を一続きの排他処理にする
        async with self._lock:
            # サイトを確保する
            site = self._ensure_site(guild_id, site_id)
            # 正規化済み一覧を書く
            site["match_domains"] = cleaned
            # 更新後の状態を保存する
            self._save_locked()
        # 成功
        return True

    async def clear_match_domains(self, guild_id: int, site_id: str) -> None:
        """マッチ元上書きを消し、YAML デフォルトに戻す。"""
        # 更新と保存を一続きの排他処理にする
        async with self._lock:
            # サイトを確保する
            site = self._ensure_site(guild_id, site_id)
            # キーを削除する
            site.pop("match_domains", None)
            # 更新後の状態を保存する
            self._save_locked()

    async def reset_guild(self, guild_id: int) -> None:
        """ギルド設定を全削除してデフォルトに戻す。"""
        # 更新と保存を一続きの排他処理にする
        async with self._lock:
            # 対象ギルドのキーを削除する
            self._data.pop(self._guild_key(guild_id), None)
            # 更新後の状態を保存する
            self._save_locked()

    def count_enabled_sites(self, guild_id: int) -> tuple[int, int]:
        """(有効数, 総数) を返す。"""
        # 全サイト id
        ids = list_site_ids(self.bot_config)
        # 有効数
        enabled = sum(1 for sid in ids if self.is_site_enabled(guild_id, sid))
        # 返す
        return enabled, len(ids)

    def _ensure_site(self, guild_id: int, site_id: str) -> Dict[str, Any]:
        """ギルド sites[site_id] を確保して返す。"""
        # ギルドエントリ
        key = self._guild_key(guild_id)
        entry = self._data.setdefault(key, {})
        # sites
        sites = entry.setdefault("sites", {})
        # dict でなければ作り直す
        if not isinstance(sites, dict):
            sites = {}
            entry["sites"] = sites
        # サイト
        site = sites.setdefault(site_id, {})
        # dict でなければ作り直す
        if not isinstance(site, dict):
            site = {}
            sites[site_id] = site
        # 返す
        return site
