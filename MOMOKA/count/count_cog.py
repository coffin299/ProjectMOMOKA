# MOMOKA/count/count_cog.py
# 掲載サイトへサーバー数（count）とコマンド一覧を定期投稿する Cog。
from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

import aiohttp
from discord.ext import commands, tasks

from MOMOKA.count.providers import (
    PROVIDERS,
    SITE_MIN_INTERVAL_SECONDS,
    RateLimitedError,
    app_commands_to_payload,
    is_placeholder_token,
    post_discordbotlist_commands,
    resolve_bot_id,
)

logger = logging.getLogger(__name__)


class CountCog(commands.Cog):
    """guild 数を top.gg 等の掲載サイトへ投稿する。"""

    def __init__(self, bot: commands.Bot) -> None:
        # Bot 参照
        self.bot = bot
        # マージ済み設定
        self.bot_config: Dict[str, Any] = getattr(bot, "config", None) or {}
        # count セクション
        self.section: Dict[str, Any] = self.bot_config.get("count") or {}
        # dict でなければ空にする
        if not isinstance(self.section, dict):
            self.section = {}
        # HTTP セッション（遅延生成）
        self._session: Optional[aiohttp.ClientSession] = None
        # サイトごとの最終成功時刻（monotonic）
        self._last_success_at: Dict[str, float] = {}
        # 間隔（分）を読む
        interval = float(self.section.get("interval_minutes") or 30)
        # 最低 5 分にクランプする（レート制限・過負荷防止）
        if interval < 5:
            interval = 5.0
        # ループ間隔を動的に差し替える
        self.post_counts.change_interval(minutes=interval)
        # 有効ならループ開始は on_ready 側で行う

    def _sites(self) -> Dict[str, Dict[str, Any]]:
        """sites 辞書を返す。"""
        # sites を取る
        sites = self.section.get("sites") or {}
        # dict のみ
        if not isinstance(sites, dict):
            return {}
        # サイト id → 設定
        result: Dict[str, Dict[str, Any]] = {}
        for site_id, cfg in sites.items():
            if isinstance(cfg, dict):
                result[str(site_id)] = cfg
        return result

    async def _get_session(self) -> aiohttp.ClientSession:
        """共有 ClientSession を返す。"""
        # 未作成 or 閉じ済みなら作る
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        # 返す
        return self._session

    def _in_cooldown(self, site_id: str) -> bool:
        """サイト固有の最短間隔内なら True。"""
        # 既定の最短間隔（秒）
        min_seconds = float(SITE_MIN_INTERVAL_SECONDS.get(site_id, 0.0))
        # サイト設定で上書きできる
        cfg = self._sites().get(site_id) or {}
        raw = cfg.get("min_interval_minutes")
        if raw is not None:
            try:
                min_seconds = max(min_seconds, float(raw) * 60.0)
            except (TypeError, ValueError):
                pass
        # 制限なし
        if min_seconds <= 0:
            return False
        # 最終成功が無ければ制限外
        last = self._last_success_at.get(site_id)
        if last is None:
            return False
        # 経過秒を見る
        return (time.monotonic() - last) < min_seconds

    async def cog_unload(self) -> None:
        """Cog アンロード時にループ停止とセッション閉鎖。"""
        # ループ停止
        if self.post_counts.is_running():
            self.post_counts.cancel()
        # セッション閉鎖
        if self._session is not None and not self._session.closed:
            await self._session.close()

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        """Ready 後に投稿ループを開始する。"""
        # 全体オフなら何もしない
        if not bool(self.section.get("enabled", True)):
            logger.info("[count] disabled in config; skipping poster loop")
            return
        # 有効サイトが無ければ開始しない
        if not self._enabled_sites():
            logger.info("[count] no enabled sites with tokens; skipping poster loop")
            return
        # 既に動いていれば再スタートしない
        if self.post_counts.is_running():
            return
        # ループ開始
        self.post_counts.start()
        logger.info("[count] poster loop started")

    def _enabled_sites(self) -> Dict[str, Dict[str, Any]]:
        """有効かつトークン設定済みのサイトだけ返す。"""
        # 結果
        enabled: Dict[str, Dict[str, Any]] = {}
        # 走査する
        for site_id, cfg in self._sites().items():
            # enabled でなければスキップ
            if not bool(cfg.get("enabled", False)):
                continue
            # 未実装プロバイダはスキップ
            if site_id not in PROVIDERS:
                logger.warning("[count] unknown site provider skipped: %s", site_id)
                continue
            # トークン
            token = str(cfg.get("token") or "")
            # プレースホルダならスキップ
            if is_placeholder_token(token):
                logger.debug("[count] site %s has no token; skipped", site_id)
                continue
            # 採用
            enabled[site_id] = cfg
        # 返す
        return enabled

    def _collect_command_payloads(self) -> List[Dict[str, Any]]:
        """CommandTree 上のアプリコマンドを Discord API 形式へ変換する。"""
        # tree が無ければ空
        tree = getattr(self.bot, "tree", None)
        if tree is None:
            return []
        # 登録済みトップレベルコマンドを取る
        commands_list = tree.get_commands()
        # Discord API 形式へ変換する（to_dict に tree が必要）
        return app_commands_to_payload(commands_list, tree)

    async def _post_discordbotlist_commands(
        self,
        session: aiohttp.ClientSession,
        cfg: Dict[str, Any],
    ) -> None:
        """discordbotlist へスラッシュコマンド一覧を投稿する。"""
        # サイト設定でオフなら送らない（既定はオン）
        if not bool(cfg.get("post_commands", True)):
            return
        # Bot 未ログインなら送れない
        if self.bot.user is None:
            return
        # コマンド配列を組み立てる
        payloads = self._collect_command_payloads()
        # 未登録ならスキップ（空配列で消すのは明示設定時のみにする）
        if not payloads:
            logger.info("[count] discordbotlist commands skipped: no app commands on tree")
            return
        # bot_id / token
        bot_id = resolve_bot_id(cfg, self.bot.user.id)
        token = str(cfg.get("token") or "").strip()
        # POST する
        await post_discordbotlist_commands(session, bot_id, payloads, token)
        # 成功ログ
        logger.info(
            "[count] posted commands to discordbotlist: count=%s bot_id=%s",
            len(payloads),
            bot_id,
        )

    @tasks.loop(minutes=30)
    async def post_counts(self) -> None:
        """有効な掲載サイトへ server_count（と DBL コマンド一覧）を送る。"""
        # 全体オフ
        if not bool(self.section.get("enabled", True)):
            return
        # Bot 未ログイン
        if self.bot.user is None:
            return
        # サーバー数
        server_count = len(self.bot.guilds)
        # セッション
        session = await self._get_session()
        # 有効サイト
        sites = self._enabled_sites()
        # 無ければ終了
        if not sites:
            return
        # 成功したサイト名（まとめて1行ログするため）
        posted_sites: list[str] = []
        # ログ用 bot_id（最後に成功したものを残す）
        last_bot_id: Optional[int] = None
        # 各サイトへ投稿
        for site_id, cfg in sites.items():
            # プロセス内クールダウン中なら送らない
            if self._in_cooldown(site_id):
                logger.debug("[count] skip %s: site cooldown", site_id)
                continue
            # プロバイダ関数
            post_fn = PROVIDERS[site_id]
            # bot_id 解決
            bot_id = resolve_bot_id(cfg, self.bot.user.id)
            # token
            token = str(cfg.get("token") or "").strip()
            # 投稿を試みる
            try:
                await post_fn(session, bot_id, server_count, token)
                # 成功時刻を記録する
                self._last_success_at[site_id] = time.monotonic()
                # 成功サイトを溜める（個別ログは出さない）
                posted_sites.append(site_id)
                # サマリ用に保持する
                last_bot_id = bot_id
            except RateLimitedError as exc:
                # 再起動直後などで起きうる想定内エラー
                logger.info("[count] rate limited for %s: %s", site_id, exc)
            except Exception as exc:
                # サイト単位で失敗しても他は続ける
                logger.warning("[count] post failed for %s: %s", site_id, exc)
            # Discord Bot List だけコマンド一覧も送る
            if site_id == "discordbotlist":
                try:
                    await self._post_discordbotlist_commands(session, cfg)
                except RateLimitedError as exc:
                    logger.info(
                        "[count] discordbotlist commands rate limited: %s",
                        exc,
                    )
                except Exception as exc:
                    # コマンド投稿失敗でも stats 成功分は残す
                    logger.warning(
                        "[count] discordbotlist commands post failed: %s",
                        exc,
                    )
        # 1件以上成功したらまとめて1行
        if posted_sites:
            logger.info(
                "[count] posted to all bot list sites: "
                "server_count=%s sites=%s bot_id=%s",
                server_count,
                ",".join(posted_sites),
                last_bot_id,
            )

    @post_counts.before_loop
    async def before_post_counts(self) -> None:
        """初回投稿前に ready を待つ。"""
        # ready 待ち
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot) -> None:
    """Cog をロードする。"""
    # 追加する
    await bot.add_cog(CountCog(bot))
