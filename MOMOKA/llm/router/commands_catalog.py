# commands_i18n から言語切片を取り出すカタログ。
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# 言語フォールバック順
_LANG_FALLBACK = ("ja", "en")


def _pick_localized(value: Any, lang: str) -> str:
    """dict なら lang → en → ja → 先頭値、str ならそのまま。"""
    # 文字列はそのまま
    if isinstance(value, str):
        return value.strip()
    # dict 以外は空
    if not isinstance(value, dict):
        return ""
    # 優先順で探す
    for key in (lang, "en", "ja"):
        hit = value.get(key)
        if isinstance(hit, str) and hit.strip():
            return hit.strip()
    # どれか最初の文字列
    for v in value.values():
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def load_commands_catalog(bot: Any, lang: str) -> List[Dict[str, str]]:
    """commands_i18n 設定から {name, description} のリストを返す。"""
    # マージ済み config
    cfg = getattr(bot, "config", {}) or {}
    # カタログ本体
    raw = cfg.get("commands_i18n") or cfg.get("commands") or {}
    # よくあるネスト: commands: { play: { description: {ja:..} } }
    commands_block = raw.get("commands") if isinstance(raw, dict) else None
    if not isinstance(commands_block, dict):
        # トップがそのままコマンド map の場合
        commands_block = raw if isinstance(raw, dict) else {}
    # 結果リスト
    out: List[Dict[str, str]] = []
    # 各コマンドを走査
    for name, meta in commands_block.items():
        # メタが無い場合はスキップ
        if not isinstance(meta, dict):
            continue
        # description を言語切片で取る
        desc = _pick_localized(meta.get("description"), lang)
        # name が無いエントリはキーを名前にする
        cmd_name = str(meta.get("name") or name)
        out.append({"name": cmd_name, "description": desc})
    # 名前順で安定化
    out.sort(key=lambda x: x["name"].lower())
    return out


def format_catalog_for_prompt(catalog: List[Dict[str, str]], *, limit: int = 80) -> str:
    """LLM に渡す短いカタログ文字列。"""
    # 行を集める
    lines: List[str] = []
    for item in catalog[:limit]:
        # 説明付き行
        desc = item.get("description") or ""
        if desc:
            lines.append(f"- /{item['name']}: {desc}")
        else:
            lines.append(f"- /{item['name']}")
    # 結合
    return "\n".join(lines)


def find_command_help(catalog: List[Dict[str, str]], query: str) -> Optional[Dict[str, str]]:
    """名前部分一致でヘルプ項目を探す。"""
    # 正規化
    q = (query or "").strip().lstrip("/").lower()
    if not q:
        return None
    # 完全一致
    for item in catalog:
        if item["name"].lower() == q:
            return item
    # 部分一致
    for item in catalog:
        if q in item["name"].lower():
            return item
    return None
