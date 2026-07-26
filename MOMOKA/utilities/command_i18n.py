# スラッシュコマンド description の Discord localizations 用 Translator。
# コマンド name は翻訳せず、description / パラメータ説明のみ多言語を返す（en はデフォルトへフォールバック）。
from __future__ import annotations

from typing import Any, Dict, Optional

import discord
from discord import app_commands
from discord.app_commands import (
    TranslationContextLocation,
    TranslationContextTypes,
    locale_str,
)

# 対応する内部言語コード（ドキュメント用）
# en / ja / ko / vi / zh-CN / zh-TW / es / fr / de / pt-BR / ru / th / id

# name 系 location（常に翻訳しない）
_NAME_LOCATIONS = frozenset(
    {
        TranslationContextLocation.command_name,
        TranslationContextLocation.group_name,
        TranslationContextLocation.parameter_name,
        TranslationContextLocation.choice_name,
    }
)

# description 系 location（カタログ参照）
_DESC_LOCATIONS = frozenset(
    {
        TranslationContextLocation.command_description,
        TranslationContextLocation.group_description,
    }
)


def locale_to_lang(locale: Any) -> str:
    """Discord Locale を内部言語コードへ変換する。"""
    # 未指定は英語へフォールバックする
    if locale is None:
        return "en"
    # Locale enum 等は value を優先する
    raw = getattr(locale, "value", None)
    # value が無ければ文字列化する
    key = str(raw if raw is not None else locale).strip().lower()
    # 空文字は未取得扱いとする
    if not key:
        return "en"
    # 日本語
    if key.startswith("ja"):
        return "ja"
    # 韓国語
    if key.startswith("ko"):
        return "ko"
    # ベトナム語
    if key.startswith("vi"):
        return "vi"
    # 中国語（繁体: TW / HK / Hant）
    if key in ("zh-tw", "zh-hk", "zh-hant") or key.startswith("zh-tw") or key.startswith("zh-hk"):
        return "zh-TW"
    # 中国語（簡体: CN / Hans / その他 zh）
    if key.startswith("zh"):
        return "zh-CN"
    # スペイン語（es-ES / es-419 等）
    if key.startswith("es"):
        return "es"
    # フランス語
    if key.startswith("fr"):
        return "fr"
    # ドイツ語
    if key.startswith("de"):
        return "de"
    # ポルトガル語（pt-BR / pt-PT → カタログは pt-BR）
    if key.startswith("pt"):
        return "pt-BR"
    # ロシア語
    if key.startswith("ru"):
        return "ru"
    # タイ語
    if key.startswith("th"):
        return "th"
    # インドネシア語
    if key.startswith("id"):
        return "id"
    # 英語（en-US / en-GB 等）
    if key.startswith("en"):
        return "en"
    # 未対応 locale は英語フォールバック
    return "en"


def _qualified_name_from_data(data: Any) -> str:
    """Command / Group から qualified_name を取得する。"""
    # qualified_name があればそれを使う
    qname = getattr(data, "qualified_name", None)
    # 取れればそのまま返す
    if qname:
        return str(qname)
    # 無ければ name に落とす
    name = getattr(data, "name", None)
    # name も無ければ空文字
    return str(name) if name else ""


class CommandDescriptionTranslator(app_commands.Translator):
    """YAML カタログに基づき description のみローカライズする Translator。"""

    def __init__(self, config: Optional[Dict[str, Any]] = None) -> None:
        # マージ済み bot.config 全体を保持する
        self._config = config or {}
        # コマンド辞書（qualified_name → エントリ）
        self._commands: Dict[str, Any] = {}
        # フォールバック言語（通常 en）
        self._fallback = "en"

    async def load(self) -> None:
        """set_translator 時にカタログをメモリへ載せる。"""
        # commands_i18n セクションを取り出す
        section = self._config.get("commands_i18n") or {}
        # fallback 言語を読む（未設定なら en）
        self._fallback = str(section.get("fallback") or "en").strip().lower() or "en"
        # commands マップを読む
        commands = section.get("commands") or {}
        # dict でなければ空にする
        self._commands = commands if isinstance(commands, dict) else {}

    async def unload(self) -> None:
        """Translator 解除時にカタログを捨てる。"""
        # 参照を空にして解放する
        self._commands = {}

    def _lookup_command_description(self, qname: str, lang: str) -> Optional[str]:
        """コマンド／Group の description を言語で返す。"""
        # カタログエントリを取得する
        entry = self._commands.get(qname)
        # 無ければ翻訳なし
        if not isinstance(entry, dict):
            return None
        # description マップを取る
        desc_map = entry.get("description") or {}
        # dict でなければ翻訳なし
        if not isinstance(desc_map, dict):
            return None
        # 対象言語の文字列を返す（無ければ None）
        text = desc_map.get(lang)
        # 空文字は未設定扱い
        if text is None or str(text).strip() == "":
            return None
        # 翻訳文を返す
        return str(text)

    def _lookup_option_description(
        self, qname: str, option_name: str, lang: str
    ) -> Optional[str]:
        """パラメータ説明を言語で返す。"""
        # カタログエントリを取得する
        entry = self._commands.get(qname)
        # 無ければ翻訳なし
        if not isinstance(entry, dict):
            return None
        # options マップを取る
        options = entry.get("options") or {}
        # dict でなければ翻訳なし
        if not isinstance(options, dict):
            return None
        # オプションエントリを取る
        opt_entry = options.get(option_name)
        # 無ければ翻訳なし
        if not isinstance(opt_entry, dict):
            return None
        # description マップを取る
        desc_map = opt_entry.get("description") or {}
        # dict でなければ翻訳なし
        if not isinstance(desc_map, dict):
            return None
        # 対象言語の文字列を返す
        text = desc_map.get(lang)
        # 空文字は未設定扱い
        if text is None or str(text).strip() == "":
            return None
        # 翻訳文を返す
        return str(text)

    async def translate(
        self,
        string: locale_str,
        locale: discord.Locale,
        context: TranslationContextTypes,
    ) -> Optional[str]:
        """Discord sync 時に呼ばれ、locale 向け翻訳または None を返す。"""
        # name 系は常に英語のまま（翻訳しない）
        if context.location in _NAME_LOCATIONS:
            return None
        # Discord locale → 内部言語
        lang = locale_to_lang(locale)
        # フォールバック言語（en）はデフォルト文字列を使うため None
        if lang == self._fallback:
            return None
        # コマンド／Group の description
        if context.location in _DESC_LOCATIONS:
            # data から qualified_name を取る
            qname = _qualified_name_from_data(context.data)
            # カタログを参照する
            return self._lookup_command_description(qname, lang)
        # パラメータの description
        if context.location == TranslationContextLocation.parameter_description:
            # Parameter オブジェクト
            param = context.data
            # 親コマンドを取得する
            parent = getattr(param, "command", None)
            # 親の qualified_name
            qname = _qualified_name_from_data(parent) if parent is not None else ""
            # パラメータ名
            opt_name = str(getattr(param, "name", "") or "")
            # カタログを参照する
            return self._lookup_option_description(qname, opt_name, lang)
        # その他 location は翻訳しない
        return None
