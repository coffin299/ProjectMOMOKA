# MOMOKA/link_fix/presets.py
# サイト別宗派プリセットとギルド上書きの解決。
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

# ドメインとして許容する簡易パターン（ホスト名のみ）
_DOMAIN_RE = re.compile(
    r"^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,}(?::\d{1,5})?$",
    re.IGNORECASE,
)

# ダッシュボード / 設定 UI から指定されても拒否する危険ホスト（SSRF・内部誘導）
_BLOCKED_HOST_SUFFIXES = (
    ".localhost",
    ".local",
    ".internal",
    ".lan",
)
_BLOCKED_HOSTS = frozenset(
    {
        "localhost",
        "metadata.google.internal",
        "metadata",
    }
)
_PRIVATE_IP_RE = re.compile(
    r"^(?:"
    r"127\.\d{1,3}\.\d{1,3}\.\d{1,3}|"
    r"10\.\d{1,3}\.\d{1,3}\.\d{1,3}|"
    r"192\.168\.\d{1,3}\.\d{1,3}|"
    r"169\.254\.\d{1,3}\.\d{1,3}|"
    r"172\.(?:1[6-9]|2\d|3[0-1])\.\d{1,3}\.\d{1,3}"
    r")(?::\d{1,5})?$"
)


def _is_blocked_host(host: str) -> bool:
    """内部向け・メタデータ・プライベート IP ホストなら True。"""
    # ポートを除いたホスト名
    name = host.split(":")[0].lower()
    # 明示拒否リスト
    if name in _BLOCKED_HOSTS:
        return True
    # 危険なサフィックス
    if any(name.endswith(suffix) for suffix in _BLOCKED_HOST_SUFFIXES):
        return True
    # プライベート IP リテラル
    if _PRIVATE_IP_RE.match(host):
        return True
    return False


def normalize_domain(raw: str) -> Optional[str]:
    """入力文字列をホスト名に正規化する。不正・危険なら None。"""
    # 前後空白を除去する
    text = (raw or "").strip()
    # 空は拒否する
    if not text:
        return None
    # スキームを落とす
    text = re.sub(r"^https?://", "", text, flags=re.IGNORECASE)
    # パス以降を落とす
    text = text.split("/")[0]
    # www. は残してもよいが小文字化する
    text = text.lower().rstrip(".")
    # パターン不一致なら拒否する
    if not _DOMAIN_RE.match(text):
        return None
    # 内部ホストは拒否する（将来の Web 設定 UI 向け）
    if _is_blocked_host(text):
        return None
    # 正規化済みドメインを返す
    return text


def parse_domain_list(raw: str) -> Tuple[List[str], Optional[str]]:
    """カンマ／空白区切りのドメイン列をパースする。失敗時は ([], エラー文)。"""
    # 区切りで分割する
    parts = re.split(r"[\s,]+", (raw or "").strip())
    # 結果リスト
    domains: List[str] = []
    # 各要素を正規化する
    for part in parts:
        # 空トークンはスキップする
        if not part:
            continue
        # 正規化する
        normalized = normalize_domain(part)
        # 不正ならエラーを返す
        if normalized is None:
            return [], f"Invalid domain: {part}"
        # 重複を避ける
        if normalized not in domains:
            domains.append(normalized)
    # 1件も無ければエラー
    if not domains:
        return [], "At least one domain is required."
    # 成功
    return domains, None


def get_link_fix_config(bot_config: Dict[str, Any]) -> Dict[str, Any]:
    """bot.config から link_fix セクションを返す。"""
    # トップレベル link_fix を取る
    section = bot_config.get("link_fix") or {}
    # dict でなければ空にする
    if not isinstance(section, dict):
        return {}
    # 返す
    return section


def get_sites_config(bot_config: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """サイト定義 dict を返す。"""
    # link_fix セクション
    section = get_link_fix_config(bot_config)
    # sites を取る
    sites = section.get("sites") or {}
    # dict でなければ空
    if not isinstance(sites, dict):
        return {}
    # サイト id -> 定義
    result: Dict[str, Dict[str, Any]] = {}
    # 各サイトを走査する
    for site_id, meta in sites.items():
        # dict のみ採用する
        if isinstance(meta, dict):
            result[str(site_id)] = meta
    # 返す
    return result


def list_site_ids(bot_config: Dict[str, Any]) -> List[str]:
    """設定に定義されたサイト id の順序付きリスト。"""
    # sites のキー順（YAML 順）を保つ
    return list(get_sites_config(bot_config).keys())


def get_site_meta(bot_config: Dict[str, Any], site_id: str) -> Dict[str, Any]:
    """単一サイトの YAML 定義を返す。無ければ空。"""
    # sites から取得する
    return get_sites_config(bot_config).get(site_id) or {}


def get_fixer_presets(bot_config: Dict[str, Any], site_id: str) -> List[str]:
    """サイトの宗派プリセット一覧。"""
    # メタを取る
    meta = get_site_meta(bot_config, site_id)
    # fixer_presets を読む
    presets = meta.get("fixer_presets") or []
    # 文字列だけ残す
    return [str(p).lower() for p in presets if p]


def supports_translation(bot_config: Dict[str, Any], site_id: str, fix_domain: str) -> bool:
    """指定 Fix 先が言語サフィックス翻訳に対応するか。"""
    # Twitter 以外は当面ボタン対象外（計画どおり）
    if site_id != "twitter":
        return False
    # メタの translation_domains を見る
    meta = get_site_meta(bot_config, site_id)
    # 許可リスト
    allowed = meta.get("translation_domains") or []
    # 小文字比較する
    domain = (fix_domain or "").lower()
    # リスト内なら対応
    return domain in {str(d).lower() for d in allowed}


def resolve_fix_domain(
    bot_config: Dict[str, Any],
    site_id: str,
    guild_site: Optional[Dict[str, Any]] = None,
) -> str:
    """ギルド上書き → YAML default → 空文字 の順で Fix 先を決める。"""
    # ギルド上書きがあれば優先する
    if isinstance(guild_site, dict):
        override = guild_site.get("fix_domain")
        # 文字列があれば正規化して返す
        if isinstance(override, str) and override.strip():
            normalized = normalize_domain(override)
            if normalized:
                return normalized
    # YAML デフォルトを使う
    meta = get_site_meta(bot_config, site_id)
    # default_fix_domain
    default = meta.get("default_fix_domain") or ""
    # 正規化して返す（失敗時は生文字列の小文字）
    return normalize_domain(str(default)) or str(default).lower()


def resolve_match_domains(
    bot_config: Dict[str, Any],
    site_id: str,
    guild_site: Optional[Dict[str, Any]] = None,
) -> List[str]:
    """ギルド上書き → YAML match_domains の順でマッチ元を決める。"""
    # ギルド上書き
    if isinstance(guild_site, dict):
        override = guild_site.get("match_domains")
        # リストなら正規化して返す
        if isinstance(override, list) and override:
            cleaned: List[str] = []
            for item in override:
                normalized = normalize_domain(str(item))
                if normalized and normalized not in cleaned:
                    cleaned.append(normalized)
            if cleaned:
                return cleaned
    # YAML デフォルト
    meta = get_site_meta(bot_config, site_id)
    # match_domains
    defaults = meta.get("match_domains") or []
    # 正規化リスト
    result: List[str] = []
    for item in defaults:
        normalized = normalize_domain(str(item))
        if normalized and normalized not in result:
            result.append(normalized)
    # 返す
    return result


def resolve_rewrite_mode(bot_config: Dict[str, Any], site_id: str) -> str:
    """書き換えモード（domain / vx_prefix）。"""
    # メタから読む
    meta = get_site_meta(bot_config, site_id)
    # デフォルトは domain 差し替え
    return str(meta.get("rewrite_mode") or "domain")
