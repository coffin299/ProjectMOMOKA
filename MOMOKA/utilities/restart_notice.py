# 管理者による再起動・シャットダウン時に、利用中ユーザーへ表示する共有文言

from __future__ import annotations

from typing import Any, Mapping, Optional

# /shutdown を実行できる Discord ユーザー ID（ハードコード・config の developer と同値）
SHUTDOWN_USER_ID = 270446628622696449

# サポート鯖 invite の既定値（utilities_config.support.discord_invite_url）
DEFAULT_SUPPORT_INVITE_URL = "https://discord.com/invite/H79HKKqx3s"

# 開発者 Discord ユーザー ID の既定値
DEFAULT_DEVELOPER_USER_ID = 270446628622696449

# 開発者表示名の既定値
DEFAULT_DEVELOPER_DISPLAY_NAME = "coffin299"


def format_restart_notice(
    invite_url: str = DEFAULT_SUPPORT_INVITE_URL,
    developer_user_id: int = DEFAULT_DEVELOPER_USER_ID,
    developer_display_name: str = DEFAULT_DEVELOPER_DISPLAY_NAME,
) -> str:
    """日英併記の再起動中メッセージを組み立てる。"""
    # invite が空なら既定 URL を使う。
    invite = (invite_url or DEFAULT_SUPPORT_INVITE_URL).strip()
    # 表示名が空なら既定名を使う。
    name = (developer_display_name or DEFAULT_DEVELOPER_DISPLAY_NAME).strip()
    # ユーザープロフィール URL を組み立てる。
    user_url = f"https://discord.com/users/{int(developer_user_id)}"
    # LLM / Music 共通の本文を返す。
    return (
        "🔄 現在再起動中です…しばらくお待ちください。\n"
        "Currently restarting… please wait a moment.\n\n"
        f"5分以上経っても復帰しない場合は、[サポートサーバー]({invite}) か "
        f"[{name}]({user_url}) へご連絡ください。\n"
        f"If it does not come back within 5 minutes, contact via the "
        f"[support server]({invite}) or [{name}]({user_url}).\n\n"
        f"[SupportServer]({invite}) or [{name}]({user_url})"
    )


def restart_notice_from_config(config: Optional[Mapping[str, Any]]) -> str:
    """bot.config の support 節から再起動文言を組み立てる。"""
    # config が無ければ既定文言。
    if not config:
        # 既定組み立て。
        return format_restart_notice()
    # support 節を取る。
    support = config.get("support") if isinstance(config, Mapping) else None
    # support が dict でなければ既定。
    if not isinstance(support, Mapping):
        # 既定組み立て。
        return format_restart_notice()
    # invite URL。
    invite_url = support.get("discord_invite_url") or DEFAULT_SUPPORT_INVITE_URL
    # 表示名。
    display_name = support.get("developer_display_name") or DEFAULT_DEVELOPER_DISPLAY_NAME
    # user id（文字列でも受け付ける）。
    raw_uid = support.get("developer_user_id", DEFAULT_DEVELOPER_USER_ID)
    try:
        # 整数化する。
        user_id = int(raw_uid)
    except (TypeError, ValueError):
        # 壊れていれば既定 ID。
        user_id = DEFAULT_DEVELOPER_USER_ID
    # 組み立てて返す。
    return format_restart_notice(
        invite_url=str(invite_url),
        developer_user_id=user_id,
        developer_display_name=str(display_name),
    )


# 後方互換: モジュール定数（既定 config 相当）
RESTART_NOTICE_TEXT = format_restart_notice()

# 音楽 Now Playing 用も同一文言
RESTART_NOTICE_MUSIC = format_restart_notice()
