# ツール実行中に Discord 本文下へ出すステータス文言の生成。
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Tuple

# 検索ツールの既定ステータス文言（言語コード -> 文言）
_DEFAULT_SEARCH_STATUS: Dict[str, str] = {
    "ja": "🔍 検索中....",
    "en": "🔍 Searching....",
    "ko": "🔍 검색 중....",
    "vi": "🔍 Đang tìm kiếm....",
    "zh-CN": "🔍 搜索中....",
    "zh-TW": "🔍 搜尋中....",
}

# ツール名ごとの既定文言テーブル
_DEFAULT_TOOL_STATUS: Dict[str, Dict[str, str]] = {
    "search": _DEFAULT_SEARCH_STATUS,
}

# クエリ表示の最大文字数（長すぎる検索語で本文を圧迫しないため）
_MAX_QUERY_CHARS = 80


def _pick_lang_text(table: Dict[str, str], lang: Optional[str]) -> Optional[str]:
    """言語コードに合う文言を返す（無ければ英語→先頭要素）。"""
    # テーブルが空なら文言なし
    if not table:
        return None
    # 言語未指定は英語扱い
    lang_norm = (lang or "en").strip()
    # 完全一致を優先する
    if lang_norm in table:
        return table[lang_norm]
    # zh 系などは接頭辞で照合する
    base = lang_norm.split("-", 1)[0]
    # 接頭辞が一致するキーを探す
    for key, text in table.items():
        # 先頭言語部分が同じなら採用する
        if key.split("-", 1)[0] == base:
            return text
    # 英語を既定とし、それも無ければ最初の文言を使う
    return table.get("en") or next(iter(table.values()))


def _status_table(llm_config: Dict[str, Any], tool_name: str) -> Dict[str, str]:
    """設定の tool_status を既定値へ上書きした文言テーブルを返す。"""
    # 既定テーブルをコピーして土台にする
    table = dict(_DEFAULT_TOOL_STATUS.get(tool_name, {}))
    # 設定側の tool_status セクションを読む
    cfg = (llm_config or {}).get("tool_status") or {}
    # ツール別の上書き文言を取得する
    override = cfg.get(tool_name) if isinstance(cfg, dict) else None
    # 辞書なら言語ごとに上書きする
    if isinstance(override, dict):
        table.update({str(k): str(v) for k, v in override.items() if v})
    return table


def tool_status_enabled(llm_config: Dict[str, Any]) -> bool:
    """ツール実行中ステータス表示が有効か（未設定時は有効）。"""
    # 設定セクションを取得する
    cfg = (llm_config or {}).get("tool_status") or {}
    # enabled 未指定なら True
    return bool(cfg.get("enabled", True)) if isinstance(cfg, dict) else True


def format_tool_status(
    llm_config: Dict[str, Any],
    calls: Iterable[Tuple[str, Dict[str, Any]]],
    lang: Optional[str],
) -> str:
    """ツール呼び出し一覧からステータス行（複数行）を組み立てる。"""
    # 表示が無効なら空文字
    if not tool_status_enabled(llm_config):
        return ""
    # 設定でクエリ表示を切り替える（既定は表示）
    cfg = (llm_config or {}).get("tool_status") or {}
    show_query = bool(cfg.get("show_query", True)) if isinstance(cfg, dict) else True
    # 出力行と重複判定用の集合
    lines: List[str] = []
    seen = set()
    for name, args in calls:
        # ツールごとの文言を言語に合わせて選ぶ
        text = _pick_lang_text(_status_table(llm_config, name), lang)
        # 文言の無いツールは表示しない
        if not text:
            continue
        # 検索語があれば末尾に添える
        query = str((args or {}).get("query") or "").strip() if show_query else ""
        # 長い検索語は省略する
        if len(query) > _MAX_QUERY_CHARS:
            query = query[: _MAX_QUERY_CHARS - 1] + "…"
        # クエリ有無で行を組み立てる
        line = f"-# {text} `{query.replace('`', '')}`" if query else f"-# {text}"
        # 同一行の重複を除く
        if line in seen:
            continue
        seen.add(line)
        lines.append(line)
    # 改行区切りで返す
    return "\n".join(lines)
