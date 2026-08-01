# bot.config からサポート／リポジトリ関連リンクを読む共有ヘルパ。
# 個人識別子のコード既定値は持たない（utilities_config 等の YAML が正）。

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional


@dataclass(frozen=True)
class SupportLinks:
    """サポート誘導・リポジトリ URL 一式。"""

    # サポート鯖 invite
    discord_invite_url: str
    # 開発者 Discord ユーザー ID（未設定は None）
    developer_user_id: Optional[int]
    # 開発者表示名
    developer_display_name: str
    # X (Twitter) プロフィール URL
    x_url: str
    # Discord 上の ID / 表示用ハンドル
    discord_id: str
    # ドキュメント／サイト URL
    docs_url: str
    # GitHub リポジトリ URL（slash_commands.updates_repository_url）
    repository_url: str

    @property
    def issues_url(self) -> str:
        """Issues URL をリポジトリから派生する。空なら空文字。"""
        # リポジトリが無ければ Issues も作れない
        base = (self.repository_url or "").rstrip("/")
        # 空なら空を返す
        if not base:
            # 呼び出し側でボタン非表示などに使う
            return ""
        # /issues を付与する
        return f"{base}/issues"

    @property
    def developer_profile_url(self) -> str:
        """開発者プロフィール URL。uid が無ければ空文字。"""
        # uid が無ければリンク不可
        if self.developer_user_id is None:
            # 空文字
            return ""
        # Discord ユーザー URL を組み立てる
        return f"https://discord.com/users/{int(self.developer_user_id)}"


def _as_mapping(value: Any) -> Mapping[str, Any]:
    """dict 相当でなければ空 dict を返す。"""
    # Mapping ならそのまま
    if isinstance(value, Mapping):
        # 呼び出し側で .get する
        return value
    # それ以外は空
    return {}


def _str_field(raw: Any) -> str:
    """設定値を strip 済み文字列にする。None は空。"""
    # None は空
    if raw is None:
        # 空文字
        return ""
    # 文字列化して前後空白を除く
    return str(raw).strip()


def _parse_user_id(raw: Any) -> Optional[int]:
    """developer_user_id を int 化する。失敗時は None。"""
    # 未設定
    if raw is None or raw == "":
        # None
        return None
    try:
        # 整数化
        return int(raw)
    except (TypeError, ValueError):
        # 壊れていれば未設定扱い
        return None


def load_support_links(config: Optional[Mapping[str, Any]]) -> SupportLinks:
    """マージ済み bot.config から SupportLinks を構築する。"""
    # config が無ければ空扱い
    root = config if isinstance(config, Mapping) else {}
    # support 節
    support = _as_mapping(root.get("support"))
    # slash_commands 節
    slash = _as_mapping(root.get("slash_commands"))
    # リポジトリ（ネスト優先、後方互換でトップレベルも見る）
    repo = _str_field(slash.get("updates_repository_url"))
    # ネストに無ければトップレベル
    if not repo:
        # 旧配置フォールバック
        repo = _str_field(root.get("updates_repository_url"))
    # SupportLinks を返す
    return SupportLinks(
        discord_invite_url=_str_field(support.get("discord_invite_url")),
        developer_user_id=_parse_user_id(support.get("developer_user_id")),
        developer_display_name=_str_field(support.get("developer_display_name")),
        x_url=_str_field(support.get("x_url")),
        discord_id=_str_field(support.get("discord_id")),
        docs_url=_str_field(support.get("docs_url")),
        repository_url=repo,
    )


def support_links_from_bot(bot: Any) -> SupportLinks:
    """commands.Bot の config 属性から読む。"""
    # bot.config を渡す
    return load_support_links(getattr(bot, "config", None))
