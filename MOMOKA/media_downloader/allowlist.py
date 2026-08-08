# MOMOKA/media_downloader/allowlist.py
# /download_* 用 yt-dlp エクストラクター許可リストと成人 IE 拒否。
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, FrozenSet, Optional, Set, Tuple

from yt_dlp.extractor import gen_extractors

from MOMOKA.config.loader import configs_dir, ensure_default_configs
from MOMOKA.utilities.url_safety import (
    UnsafeURLError,
    assert_safe_http_url,
    looks_like_http_url,
)

logger = logging.getLogger(__name__)

# ランタイム / デフォルトのファイル名
_ALLOWLIST_RUNTIME_NAME = "media_downloader_allowlist.json"
_ALLOWLIST_DEFAULT_NAME = "media_downloader_allowlist.default.json"

# メモリ上の許可 IE 集合キャッシュ
_cached_allowed: Optional[FrozenSet[str]] = None

# 成人向け IE 名のキーワード（クラス AGE_LIMIT が無い場合のフォールバック）
_ADULT_IE_KEYWORDS = (
    "porn",
    "xxx",
    "xhamster",
    "xvideos",
    "youporn",
    "redtube",
    "spankbang",
    "chaturbate",
    "bongacams",
    "camsoda",
    "eporner",
    "motherless",
    "youjizz",
    "tnaflix",
    "slutload",
    "drtuber",
    "nuvid",
    "tube8",
    "sexu",
    "redgifs",
    "rule34",
    "xanimu",
    "zenporn",
    "thisvid",
    "playvids",
    "peekvids",
    "porntube",
    "pornhub",
    "xnxx",
    "txxx",
    "beeg",
    "eroprofile",
    "goshgay",
    "hellporno",
    "lovehomeporn",
    "moviefap",
    "murrtube",
    "nonktube",
    "nubiles",
    "pornerbros",
    "stripchat",
    "sunporno",
    "toypics",
    "alphaporno",
    "behindkink",
    "empflix",
    "erocast",
    "fourtube",
    "noodlemagazine",
    "pornflip",
    "porntop",
    "pornbox",
    "pornotube",
    "cam4",
    "fux",
    "xxxy",
)


def _ie_class_age_limit(ie: Any) -> Optional[int]:
    """IE クラス自身の AGE_LIMIT / _AGE_LIMIT を返す（無ければ None）。"""
    # クラスオブジェクトを取る
    cls = type(ie)
    # クラス dict 上の属性のみ見る（親の classproperty age_limit は使わない）
    for attr in ("AGE_LIMIT", "_AGE_LIMIT"):
        # クラス変数として定義されているか
        if attr not in vars(cls):
            continue
        # 値を取る
        raw = getattr(cls, attr)
        # None は未設定
        if raw is None:
            continue
        # 整数化を試みる
        try:
            return int(raw)
        except (TypeError, ValueError):
            # 不正値は無視
            continue
    # 未設定
    return None


def is_adult_extractor(ie: Any) -> bool:
    """AGE_LIMIT/_AGE_LIMIT >= 18、または成人向け IE 名なら True。"""
    # クラス属性の年齢制限
    age = _ie_class_age_limit(ie)
    # 18 以上なら成人
    if age is not None and age >= 18:
        return True
    # IE キー名ヒューリスティック（クラス属性が無い成人サイト向け）
    key = str(ie.ie_key()).lower()
    # キーワードのいずれかを含むか
    return any(word in key for word in _ADULT_IE_KEYWORDS)


def load_allowed_extractors(*, force_reload: bool = False) -> FrozenSet[str]:
    """configs の allowlist JSON から許可 IE 名集合を読む。"""
    # キャッシュがあれば再利用
    global _cached_allowed
    # 強制再読込でなければキャッシュ返却
    if _cached_allowed is not None and not force_reload:
        return _cached_allowed
    # default → runtime コピーを保証する
    ensure_default_configs()
    # configs ディレクトリ
    base = configs_dir()
    # ランタイムパス
    runtime_path = base / _ALLOWLIST_RUNTIME_NAME
    # 無ければ default
    path = runtime_path if runtime_path.exists() else base / _ALLOWLIST_DEFAULT_NAME
    # どちらも無ければ空（全て拒否に近い）
    if not path.exists():
        logger.error("media downloader allowlist missing: %s", path)
        _cached_allowed = frozenset()
        return _cached_allowed
    # JSON を読む
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        logger.error("Failed to load media allowlist %s: %s", path, exc)
        _cached_allowed = frozenset()
        return _cached_allowed
    # 形式: {"allowed_extractors": [...]} または素の list
    if isinstance(data, dict):
        raw_list = data.get("allowed_extractors") or data.get("allowlist") or []
    elif isinstance(data, list):
        raw_list = data
    else:
        raw_list = []
    # 文字列 IE 名だけ残す
    names: Set[str] = {
        str(item).strip()
        for item in raw_list
        if isinstance(item, (str, int)) and str(item).strip()
    }
    # YouTube 系は常に許可（欠落対策）
    for ie in gen_extractors():
        # IE キー
        key = ie.ie_key()
        # Youtube 接頭辞は強制追加
        if key.startswith("Youtube"):
            names.add(key)
    # キャッシュして返す
    _cached_allowed = frozenset(names)
    logger.info(
        "Loaded media downloader allowlist (%s IEs) from %s",
        len(_cached_allowed),
        path.name,
    )
    return _cached_allowed


def resolve_extractor_for_url(url_or_query: str) -> Optional[Any]:
    """URL / クエリに最初にマッチする yt-dlp IE インスタンスを返す。"""
    # 空は無し
    if not url_or_query or not str(url_or_query).strip():
        return None
    # 対象文字列
    target = str(url_or_query).strip()
    # 全 IE を順に suitable チェック（yt-dlp と同じ優先順）
    for ie in gen_extractors():
        # マッチしなければ次へ
        try:
            if not ie.suitable(target):
                continue
        except Exception:
            # suitable 失敗はスキップ
            continue
        # Generic は最後に来る想定だが、明示的に記録する
        return ie
    # 見つからない
    return None


def check_download_url_allowed(
    url_or_query: str,
    *,
    allowed: Optional[FrozenSet[str]] = None,
) -> Tuple[bool, str, Optional[str]]:
    """
    /download_* 用の事前チェック。
    戻り値: (ok, reason_code, ie_key)
    reason_code: ok / unsupported_site / forbidden_site / unsafe_url / no_extractor
    """
    # 許可集合（未指定ならロード）
    allow = allowed if allowed is not None else load_allowed_extractors()
    # 検索クエリ（非 URL）は default_search=ytsearch 前提で YouTube 扱い
    if not looks_like_http_url(url_or_query):
        # YouTube 検索は常に許可
        return True, "ok", "Youtube"
    # http(s) URL ならプライベート IP 等を拒否
    try:
        # SSRF / プライベート IP 検査
        assert_safe_http_url(url_or_query)
    except UnsafeURLError:
        # 危険 URL
        return False, "unsafe_url", None
    # マッチ IE を解決
    ie = resolve_extractor_for_url(url_or_query)
    # 無し
    if ie is None:
        return False, "no_extractor", None
    # IE キーと成人・許可リストを共通判定へ
    return _evaluate_ie(ie, allow=allow)


def check_extracted_info_allowed(
    info: Dict[str, Any],
    *,
    allowed: Optional[FrozenSet[str]] = None,
) -> Tuple[bool, str, Optional[str]]:
    """
    extract_info 後の info dict を許可リスト / 成人 IE / URL 安全性で再検証する。
    """
    # 許可集合
    allow = allowed if allowed is not None else load_allowed_extractors()
    # IE 名（extractor_key / ie_key / extractor）
    ie_key_raw = (
        info.get("extractor_key")
        or info.get("ie_key")
        or info.get("extractor")
    )
    # 文字列化
    ie_key = str(ie_key_raw).strip() if ie_key_raw else None
    # webpage_url 等の SSRF 再検査
    try:
        assert_info_url_safe(info)
    except UnsafeURLError:
        return False, "unsafe_url", ie_key
    # キーが無ければ拒否
    if not ie_key:
        return False, "no_extractor", None
    # Generic 拒否
    if ie_key in ("Generic", "GenericOpenGraph"):
        return False, "unsupported_site", ie_key
    # Youtube は常に許可
    if ie_key.startswith("Youtube"):
        return True, "ok", ie_key
    # 成人向けキーワード（IE クラスが取れない場合のフォールバック）
    key_l = ie_key.lower()
    if any(word in key_l for word in _ADULT_IE_KEYWORDS):
        return False, "forbidden_site", ie_key
    # IE クラスを名前から探す
    ie = None
    for candidate in gen_extractors():
        if candidate.ie_key() == ie_key:
            ie = candidate
            break
    # クラス属性の成人チェック（見つかった場合）
    if ie is not None and is_adult_extractor(ie):
        return False, "forbidden_site", ie_key
    # 許可リスト
    if ie_key not in allow:
        return False, "unsupported_site", ie_key
    # OK
    return True, "ok", ie_key


def _evaluate_ie(
    ie: Any,
    *,
    allow: FrozenSet[str],
) -> Tuple[bool, str, Optional[str]]:
    """解決済み IE を許可リスト / 成人属性で判定する。"""
    # IE キー
    ie_key = ie.ie_key()
    # Generic は許可リストに通常含めない
    if ie_key in ("Generic", "GenericOpenGraph"):
        return False, "unsupported_site", ie_key
    # クラス属性での成人 IE 拒否（リストにあっても拒否）
    if is_adult_extractor(ie):
        return False, "forbidden_site", ie_key
    # YouTube 系は常に許可
    if ie_key.startswith("Youtube"):
        return True, "ok", ie_key
    # 許可リスト外
    if ie_key not in allow:
        return False, "unsupported_site", ie_key
    # OK
    return True, "ok", ie_key


def assert_info_url_safe(info: Dict[str, Any]) -> None:
    """extract_info 後の webpage_url / url に対する追加 SSRF チェック。"""
    # 候補 URL を集める
    candidates = []
    # ページ URL
    for key in ("webpage_url", "original_url", "url"):
        # 値を取る
        val = info.get(key)
        # http(s) らしいものだけ
        if isinstance(val, str) and looks_like_http_url(val):
            candidates.append(val)
    # 各候補を検証
    for candidate in candidates:
        # 危険なら例外
        assert_safe_http_url(candidate)


def allowlist_path_for_docs() -> Path:
    """ドキュメント用にランタイム allowlist パスを返す。"""
    # configs 配下のランタイム名
    return configs_dir() / _ALLOWLIST_RUNTIME_NAME
