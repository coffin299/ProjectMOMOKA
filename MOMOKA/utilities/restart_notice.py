# 管理者による再起動・シャットダウン時に、利用中ユーザーへ表示する共有文言
# 個人識別子の既定値は持たない。utilities_config.support を正とする。

from __future__ import annotations

from typing import Any, Mapping, Optional

from MOMOKA.utilities.support_config import SupportLinks, load_support_links


def format_restart_notice(
    invite_url: str = "",
    developer_user_id: Optional[int] = None,
    developer_display_name: str = "",
) -> str:
    """日英併記の再起動中メッセージを組み立てる。"""
    # invite を正規化する
    invite = (invite_url or "").strip()
    # 表示名を正規化する
    name = (developer_display_name or "").strip()
    # プロフィール URL（uid があるときだけ）
    user_url = (
        f"https://discord.com/users/{int(developer_user_id)}"
        if developer_user_id is not None
        else ""
    )
    # 連絡先リンク断片を組み立てる（欠落分は省略）
    contact_parts_ja: list[str] = []
    # 英語側も同様
    contact_parts_en: list[str] = []
    # 短縮行用
    contact_parts_short: list[str] = []
    # サポート鯖があれば追加する
    if invite:
        # 日本語
        contact_parts_ja.append(f"[サポートサーバー]({invite})")
        # 英語
        contact_parts_en.append(f"[support server]({invite})")
        # 短縮
        contact_parts_short.append(f"[SupportServer]({invite})")
    # 開発者リンクがあれば追加する
    if name and user_url:
        # 日本語
        contact_parts_ja.append(f"[{name}]({user_url})")
        # 英語
        contact_parts_en.append(f"[{name}]({user_url})")
        # 短縮
        contact_parts_short.append(f"[{name}]({user_url})")
    # 基本本文（再起動中）
    lines = [
        "🔄 現在再起動中です…しばらくお待ちください。",
        "Currently restarting… please wait a moment.",
        "",
    ]
    # 連絡先があるときだけ案内を付ける
    if contact_parts_ja:
        # 「A か B」形式（1件ならその1件だけ）
        ja_joined = " か ".join(contact_parts_ja)
        # 英語は or
        en_joined = " or ".join(contact_parts_en)
        # 短縮も or
        short_joined = " or ".join(contact_parts_short)
        # 日英案内を追記する
        lines.extend(
            [
                f"5分以上経っても復帰しない場合は、{ja_joined} へご連絡ください。",
                f"If it does not come back within 5 minutes, contact via the {en_joined}.",
                "",
                short_joined,
            ]
        )
    # 結合して返す
    return "\n".join(lines)


def restart_notice_from_config(config: Optional[Mapping[str, Any]]) -> str:
    """bot.config の support 節から再起動文言を組み立てる。"""
    # SupportLinks を読む
    links = load_support_links(config)
    # 組み立てて返す
    return format_restart_notice(
        invite_url=links.discord_invite_url,
        developer_user_id=links.developer_user_id,
        developer_display_name=links.developer_display_name,
    )


def restart_notice_from_links(links: SupportLinks) -> str:
    """SupportLinks から直接組み立てる。"""
    # format に委譲する
    return format_restart_notice(
        invite_url=links.discord_invite_url,
        developer_user_id=links.developer_user_id,
        developer_display_name=links.developer_display_name,
    )


def shutdown_allowed_user_id(config: Optional[Mapping[str, Any]]) -> Optional[int]:
    """/shutdown を実行できるユーザー ID（support.developer_user_id）。"""
    # SupportLinks から uid を返す
    return load_support_links(config).developer_user_id


# 後方互換: 設定無し時の最小文言（個人リンクなし）
RESTART_NOTICE_TEXT = format_restart_notice()

# 音楽 Now Playing 用も同一文言
RESTART_NOTICE_MUSIC = format_restart_notice()
