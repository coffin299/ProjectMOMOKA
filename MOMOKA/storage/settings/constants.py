"""SettingsDB で使用する namespace 定数と保存範囲を定義する。"""

# デフォルト DB パスをリポジトリルート相対で定義する。
DEFAULT_DB_PATH = "data/momoka.db"

# 正規化スキーマの現行版を定義する（v3: vc_playback_sessions）。
SCHEMA_VERSION = 3

# チャンネル単位の LLM モデル上書き namespace を定義する。
NS_CHANNEL_LLM_MODELS = "channel_llm_models"
# チャンネル単位の画像モデル上書き namespace を定義する。
NS_CHANNEL_IMAGE_MODELS = "channel_image_models"
# Link Fix のギルド設定 namespace を定義する。
NS_LINK_FIX_SETTINGS = "link_fix_settings"
# チャンネル単位の TTS 設定 namespace を定義する。
NS_TTS_SETTINGS = "tts_settings"
# 読み上げのギルド設定 namespace を定義する。
NS_SPEECH_SETTINGS = "speech_settings"
# 読み上げ辞書のギルド設定 namespace を定義する。
NS_SPEECH_DICTIONARY = "speech_dictionary"
# Twitch 通知のギルド設定 namespace を定義する。
NS_TWITCH_SETTINGS = "twitch_settings"
# 地震・津波通知のギルド設定 namespace を定義する。
NS_EARTHQUAKE_CONFIG = "earthquake_tsunami_notification_config"
# ホスト全体のログチャンネル namespace を定義する。
NS_LOGGING_CHANNELS = "logging_channels"
# ホスト全体の応答時間 namespace を定義する。
NS_RESPONSE_TIMES = "response_times"
# ホスト全体のログビューア設定 namespace を定義する。
NS_LOG_VIEWER_CONFIG = "log_viewer_config"
# ホスト全体の一時共有 revoke 予定 namespace（旧 file.io 名を互換維持）。
NS_FILEIO_DELETION_SCHEDULE = "fileio_deletion_schedule"
# 後方互換エイリアス（旧名）
NS_GDRIVE_DELETION_SCHEDULE = NS_FILEIO_DELETION_SCHEDULE

# Web ダッシュボードからギルド単位で変更可能な namespace を集約する。
GUILD_ADMIN_NAMESPACES = frozenset(
    {
        NS_EARTHQUAKE_CONFIG,
        NS_TWITCH_SETTINGS,
        NS_LINK_FIX_SETTINGS,
        NS_SPEECH_SETTINGS,
        NS_SPEECH_DICTIONARY,
    }
)

# ホスト管理者だけが変更する namespace を集約する。
HOST_ONLY_NAMESPACES = frozenset(
    {
        NS_LOGGING_CHANNELS,
        NS_LOG_VIEWER_CONFIG,
        NS_FILEIO_DELETION_SCHEDULE,
        NS_RESPONSE_TIMES,
    }
)
