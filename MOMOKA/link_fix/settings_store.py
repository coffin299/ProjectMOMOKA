# MOMOKA/link_fix/settings_store.py
# ギルド単位の Link Fix 設定の読込・保存（SQLite）。
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, Dict, Optional

from MOMOKA.link_fix.presets import (
    get_link_fix_config,
    get_site_meta,
    list_site_ids,
    normalize_domain,
)
from MOMOKA.storage import NS_LINK_FIX_SETTINGS, resolve_settings_db

logger = logging.getLogger(__name__)


class LinkFixSettingsStore:
    """data/momoka.db の link_fix_settings を扱うストア。"""

    def __init__(self, bot_config: Dict[str, Any], project_root: Optional[Path] = None, bot: Any = None) -> None:
        # bot 全体 config を保持する
        self.bot_config = bot_config
        # プロジェクトルート（未指定なら cwd）— YAML settings_path 互換のため残す
        self.project_root = project_root or Path.cwd()
        # SettingsDB（bot 注入 or デフォルト）
        self.settings_db = resolve_settings_db(bot)
        # メモリ上の設定
        self._data: Dict[str, Dict[str, Any]] = {}
        # 同時に届く設定更新を直列化する
        self._lock = asyncio.Lock()
        # 初回読込
        self.load()

    def load(self) -> None:
        """DB から読み込む。無ければ空。"""
        try:
            # namespace から生データを取る
            raw = self.settings_db.load(NS_LINK_FIX_SETTINGS)
            # 無ければ空 dict
            if raw is None:
                self._data = {}
                return
            # dict でなければ空
            if not isinstance(raw, dict):
                self._data = {}
                return
            # ギルド id を文字列キーで保持する
            self._data = {str(k): v for k, v in raw.items() if isinstance(v, dict)}
        except Exception as exc:  # noqa: BLE001
            # 壊れていればログして空にする
            logger.error("Failed to load link_fix settings: %s", exc)
            self._data = {}

    async def save(self) -> None:
        """DB へ排他的に保存する。"""
        # 更新処理と保存処理が競合しないようにする
        async with self._lock:
            # ロック取得済みの保存処理を呼ぶ
            await self._save_locked()

    async def _save_locked(self) -> None:
        """ロック取得済みの状態を DB へ保存する。"""
        try:
            # メモリ上の全設定を namespace に書く
            await self.settings_db.save_async(NS_LINK_FIX_SETTINGS, self._data)
        except Exception as exc:  # noqa: BLE001
            # 失敗をログする
            logger.error("Failed to save link_fix settings: %s", exc)

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
            await self._save_locked()

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
            await self._save_locked()

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
            await self._save_locked()

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
            await self._save_locked()
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
            await self._save_locked()
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
            await self._save_locked()

    async def reset_guild(self, guild_id: int) -> None:
        """ギルド設定を全削除してデフォルトに戻す。"""
        # 更新と保存を一続きの排他処理にする
        async with self._lock:
            # 対象ギルドのキーを削除する
            self._data.pop(self._guild_key(guild_id), None)
            # 更新後の状態を保存する
            await self._save_locked()

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
