# MOMOKA/utilities/url_safety.py
# 外部 URL 取得前の SSRF 対策（スキーム制限・DNS 解決後のプライベート IP 拒否・
# 検証済み IP への接続固定・リダイレクト再検証・peer IP 確認）。
from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
from typing import Any, Collection, Optional, Sequence, Set, Tuple, Union
from urllib.parse import urljoin, urlparse

import aiohttp

logger = logging.getLogger(__name__)

# 許可する URL スキーム（http/https のみ）
_ALLOWED_SCHEMES = frozenset({"http", "https"})
# AWS / クラウドメタデータ IPv4（明示拒否）
_METADATA_IPV4 = ipaddress.ip_address("169.254.169.254")
# CGNAT (RFC 6598) 100.64.0.0/10
_CGNAT_NETWORK = ipaddress.ip_network("100.64.0.0/10")
# IPv6 ULA fc00::/7
_IPV6_ULA = ipaddress.ip_network("fc00::/7")
# IPv6 リンクローカル fe80::/10
_IPV6_LINK_LOCAL = ipaddress.ip_network("fe80::/10")


class UnsafeURLError(ValueError):
    """SSRF 判定で拒否された URL を表す例外。"""


def _normalize_ip(addr: ipaddress.IPv4Address | ipaddress.IPv6Address):
    """IPv4 射影 IPv6 なら内側の IPv4 に正規化する。"""
    # IPv6 上の IPv4 射影は内側の IPv4 で判定する
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        # 射影された IPv4 を返す
        return addr.ipv4_mapped
    # それ以外はそのまま
    return addr


def is_blocked_ip(addr: Union[str, ipaddress.IPv4Address, ipaddress.IPv6Address]) -> bool:
    """解決済み IP が SSRF 対象（ループバック等）なら True。"""
    # 文字列なら IP オブジェクトへ変換する
    if isinstance(addr, str):
        # 不正な IP 文字列はブロック扱い
        try:
            ip_obj = ipaddress.ip_address(addr)
        except ValueError:
            # パース不能は安全側で拒否する
            return True
    else:
        # 既に IP オブジェクト
        ip_obj = addr
    # IPv4 射影を正規化する
    ip_obj = _normalize_ip(ip_obj)
    # ループバック（127.0.0.0/8, ::1）
    if ip_obj.is_loopback:
        return True
    # RFC1918 等のプライベート
    if ip_obj.is_private:
        return True
    # リンクローカル（169.254.0.0/16 等。メタデータも含む）
    if ip_obj.is_link_local:
        return True
    # 未指定（0.0.0.0 / ::）
    if ip_obj.is_unspecified:
        return True
    # マルチキャスト
    if ip_obj.is_multicast:
        return True
    # 予約アドレス
    if ip_obj.is_reserved:
        return True
    # クラウドメタデータ IPv4 を明示拒否
    if ip_obj == _METADATA_IPV4:
        return True
    # CGNAT 100.64.0.0/10
    if isinstance(ip_obj, ipaddress.IPv4Address) and ip_obj in _CGNAT_NETWORK:
        return True
    # IPv6 ULA / リンクローカル
    if isinstance(ip_obj, ipaddress.IPv6Address):
        # ULA
        if ip_obj in _IPV6_ULA:
            return True
        # リンクローカル（is_link_local と二重でも安全側）
        if ip_obj in _IPV6_LINK_LOCAL:
            return True
    # 上記以外は許可
    return False


def _resolve_host_ips(hostname: str) -> Set[str]:
    """ホスト名を DNS 解決し、文字列 IP の集合を返す。"""
    # 解決結果の格納先
    resolved: Set[str] = set()
    # getaddrinfo で A/AAAA を取得する（ポートはダミー）
    try:
        # ファミリー不問で解決する
        infos = socket.getaddrinfo(hostname, None, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        # DNS 失敗は拒否する
        raise UnsafeURLError(f"DNS resolution failed for host: {hostname}") from exc
    # 各結果からアドレスを取り出す
    for info in infos:
        # (family, type, proto, canonname, sockaddr)
        sockaddr = info[4]
        # sockaddr[0] が IP 文字列
        if not sockaddr:
            continue
        # IP 文字列を集合へ
        resolved.add(str(sockaddr[0]))
    # 1 件も取れなければ拒否
    if not resolved:
        raise UnsafeURLError(f"No DNS records for host: {hostname}")
    # 解決済み IP 集合を返す
    return resolved


async def _resolve_host_ips_async(hostname: str) -> Set[str]:
    """ホスト名を非同期 DNS 解決し、文字列 IP の集合を返す。"""
    # イベントループ上で getaddrinfo する
    try:
        # ブロッキング DNS を executor 経由で実行する
        infos = await asyncio.get_running_loop().getaddrinfo(
            hostname,
            None,
            type=socket.SOCK_STREAM,
        )
    except socket.gaierror as exc:
        # DNS 失敗は拒否する
        raise UnsafeURLError(f"DNS resolution failed for host: {hostname}") from exc
    # 結果を集める
    resolved: Set[str] = set()
    # 各 sockaddr から IP を取る
    for info in infos:
        # sockaddr
        sockaddr = info[4]
        # 空はスキップ
        if not sockaddr:
            continue
        # IP を追加
        resolved.add(str(sockaddr[0]))
    # 空なら拒否
    if not resolved:
        raise UnsafeURLError(f"No DNS records for host: {hostname}")
    # 返す
    return resolved


def _safe_ips_from_resolved(hostname: str, resolved: Set[str]) -> list[str]:
    """解決済み IP からブロック対象を除き、空なら例外を投げる。"""
    # 許可 IP だけを残す
    safe = [ip for ip in resolved if not is_blocked_ip(ip)]
    # 1 件も無ければ拒否
    if not safe:
        raise UnsafeURLError(f"All resolved IPs blocked for host {hostname}")
    # 許可 IP 一覧を返す
    return safe


def assert_safe_http_url(url: str, *, resolve_dns: bool = True) -> str:
    """http(s) URL を検証し、危険なら UnsafeURLError。問題なければ正規化 URL を返す。"""
    # 空は拒否
    if not url or not str(url).strip():
        raise UnsafeURLError("Empty URL")
    # 前後空白を除く
    cleaned = str(url).strip()
    # URL をパースする
    parsed = urlparse(cleaned)
    # スキームを小文字で見る
    scheme = (parsed.scheme or "").lower()
    # http/https 以外は拒否
    if scheme not in _ALLOWED_SCHEMES:
        raise UnsafeURLError(f"URL scheme not allowed: {scheme or '(none)'}")
    # ホスト必須
    hostname = parsed.hostname
    # ホスト無しは拒否
    if not hostname:
        raise UnsafeURLError("URL missing hostname")
    # ホストが IP リテラルなら即判定
    try:
        # IP リテラルとしてパースを試みる
        literal_ip = ipaddress.ip_address(hostname)
    except ValueError:
        # ホスト名（非 IP）
        literal_ip = None
    # IP リテラルがブロック対象なら拒否
    if literal_ip is not None and is_blocked_ip(literal_ip):
        raise UnsafeURLError(f"Blocked IP literal in URL: {hostname}")
    # DNS 解決して全 IP を検査する
    if resolve_dns:
        # 同期解決し、許可 IP が残るか確認する
        _safe_ips_from_resolved(hostname, _resolve_host_ips(hostname))
    # 検証済み URL を返す
    return cleaned


async def assert_safe_http_url_async(url: str, *, resolve_dns: bool = True) -> str:
    """assert_safe_http_url の非同期版（DNS を async getaddrinfo で行う）。"""
    # 空は拒否
    if not url or not str(url).strip():
        raise UnsafeURLError("Empty URL")
    # 前後空白を除く
    cleaned = str(url).strip()
    # パース
    parsed = urlparse(cleaned)
    # スキーム
    scheme = (parsed.scheme or "").lower()
    # http/https のみ
    if scheme not in _ALLOWED_SCHEMES:
        raise UnsafeURLError(f"URL scheme not allowed: {scheme or '(none)'}")
    # ホスト
    hostname = parsed.hostname
    # 必須
    if not hostname:
        raise UnsafeURLError("URL missing hostname")
    # IP リテラル判定
    try:
        # リテラル IP
        literal_ip = ipaddress.ip_address(hostname)
    except ValueError:
        # ホスト名
        literal_ip = None
    # リテラルが危険なら拒否
    if literal_ip is not None and is_blocked_ip(literal_ip):
        raise UnsafeURLError(f"Blocked IP literal in URL: {hostname}")
    # DNS 解決
    if resolve_dns:
        # 非同期解決し、許可 IP が残るか確認する
        _safe_ips_from_resolved(hostname, await _resolve_host_ips_async(hostname))
    # 返す
    return cleaned


def resolve_safe_connect_ip(url: str) -> Tuple[str, str, str]:
    """
    URL を検証し、(正規化URL, ホスト名, 接続用の許可 IP) を返す。
    DNS リバインディング対策のため、接続はこの IP へ固定する想定。
    """
    # まずスキーム・ホストを検証する（DNS は後で自分で取る）
    cleaned = assert_safe_http_url(url, resolve_dns=False)
    # パース結果を再利用する
    parsed = urlparse(cleaned)
    # ホスト名は検証済み
    hostname = parsed.hostname or ""
    # IP リテラルならその IP を接続先にする
    try:
        # リテラル IP として解釈する
        literal_ip = ipaddress.ip_address(hostname)
    except ValueError:
        # ホスト名なら DNS 解決する
        literal_ip = None
    # リテラルが危険なら拒否（assert 済みだが二重防御）
    if literal_ip is not None:
        # ブロック対象なら拒否
        if is_blocked_ip(literal_ip):
            raise UnsafeURLError(f"Blocked IP literal in URL: {hostname}")
        # リテラル IP を接続先として返す
        return cleaned, hostname, str(literal_ip)
    # DNS 解決して許可 IP だけ残す
    safe_ips = _safe_ips_from_resolved(hostname, _resolve_host_ips(hostname))
    # 先頭の許可 IP を接続先に使う
    return cleaned, hostname, safe_ips[0]


async def resolve_safe_connect_ip_async(url: str) -> Tuple[str, str, str]:
    """resolve_safe_connect_ip の非同期版。"""
    # DNS 無しで基本検証する
    cleaned = await assert_safe_http_url_async(url, resolve_dns=False)
    # パース
    parsed = urlparse(cleaned)
    # ホスト
    hostname = parsed.hostname or ""
    # IP リテラル判定
    try:
        # リテラル IP
        literal_ip = ipaddress.ip_address(hostname)
    except ValueError:
        # ホスト名
        literal_ip = None
    # リテラルならその IP を使う
    if literal_ip is not None:
        # 危険なら拒否
        if is_blocked_ip(literal_ip):
            raise UnsafeURLError(f"Blocked IP literal in URL: {hostname}")
        # 返す
        return cleaned, hostname, str(literal_ip)
    # 非同期 DNS 解決
    safe_ips = _safe_ips_from_resolved(
        hostname, await _resolve_host_ips_async(hostname)
    )
    # 先頭 IP を返す
    return cleaned, hostname, safe_ips[0]


def _peer_ip_from_response(response: aiohttp.ClientResponse) -> Optional[str]:
    """レスポンス接続の peer IP を取り出す。取れなければ None。"""
    # connection が無い場合は確認不能
    connection = getattr(response, "connection", None)
    # 接続オブジェクトが無ければ諦める
    if connection is None:
        return None
    # transport を取る
    transport = getattr(connection, "transport", None)
    # transport 無ければ諦める
    if transport is None:
        return None
    # peername を読む
    peername = transport.get_extra_info("peername")
    # 形式が想定外なら諦める
    if not peername:
        return None
    # (ip, port) または (ip, port, ...)
    if isinstance(peername, tuple) and peername:
        # 先頭が IP 文字列
        return str(peername[0])
    # 不明形式
    return None


def assert_response_peer_safe(
    response: aiohttp.ClientResponse,
    *,
    hostname: Optional[str] = None,
) -> None:
    """接続後 peer IP がブロック対象ならレスポンスを閉じて拒否する。"""
    # peer IP を取得する
    peer_ip = _peer_ip_from_response(response)
    # 取得不能なら（プロキシ等）追加検証はスキップする
    if peer_ip is None:
        # デバッグログだけ残す
        logger.debug(
            "Peer IP unavailable for SSRF check (host=%s)",
            hostname or "?",
        )
        # 接続前 DNS 検査に依存する
        return
    # ブロック対象なら切断して例外
    if is_blocked_ip(peer_ip):
        # レスポンスを閉じる
        response.close()
        # 拒否理由を付ける
        host_part = f" for host {hostname}" if hostname else ""
        # 例外を投げる
        raise UnsafeURLError(f"Blocked peer IP {peer_ip}{host_part}")


def looks_like_http_url(value: str) -> bool:
    """文字列が http(s) URL らしいか（検索クエリと区別する）。"""
    # 空は URL ではない
    if not value or not str(value).strip():
        return False
    # 小文字化して先頭スキームを見る
    lowered = str(value).strip().lower()
    # http/https で始まるものだけ URL 扱い
    return lowered.startswith("http://") or lowered.startswith("https://")


def has_explicit_uri_scheme(value: str) -> bool:
    """`scheme://` 形式の URI スキームを明示しているか。"""
    # 空はスキーム無し
    if not value or not str(value).strip():
        return False
    # 前後空白を除く
    text = str(value).strip()
    # スキーム区切りが無ければ検索語扱い
    if "://" not in text:
        return False
    # 先頭トークンをスキーム候補とする
    scheme = text.split("://", 1)[0].strip().lower()
    # 英数字と +.- のみのスキームなら明示 URI
    return bool(scheme) and all(ch.isalnum() or ch in "+.-" for ch in scheme)


def assert_user_media_query_safe(url_or_query: str) -> str:
    """
    利用者入力の URL/検索語を検証する。
    - 明示スキームがあり http(s) 以外なら拒否
    - http(s) なら SSRF 検査
    - スキーム無し（検索語）はそのまま許可
    """
    # 空は拒否
    if not url_or_query or not str(url_or_query).strip():
        raise UnsafeURLError("Empty URL or query")
    # 正規化
    cleaned = str(url_or_query).strip()
    # 明示スキームがある入力
    if has_explicit_uri_scheme(cleaned):
        # http(s) 以外は即拒否（rtmp 等のすり抜け防止）
        if not looks_like_http_url(cleaned):
            raise UnsafeURLError("Only http/https URLs are allowed")
        # SSRF 検査（DNS 含む）
        return assert_safe_http_url(cleaned)
    # 検索語はそのまま返す
    return cleaned


def collect_info_urls(info: dict) -> list[str]:
    """yt-dlp info dict から検査対象 URL を集める。"""
    # 候補格納
    candidates: list[str] = []
    # 見たいキー
    for key in (
        "webpage_url",
        "original_url",
        "url",
        "thumbnail",
        "manifest_url",
    ):
        # 値を取る
        val = info.get(key)
        # 文字列かつ http(s) のみ
        if isinstance(val, str) and looks_like_http_url(val):
            candidates.append(val)
    # formats / entries も再帰的に見る
    formats = info.get("formats")
    # formats がリストなら各要素の url を見る
    if isinstance(formats, list):
        for fmt in formats:
            # 辞書以外は無視
            if not isinstance(fmt, dict):
                continue
            # format url
            fmt_url = fmt.get("url")
            # http(s) のみ
            if isinstance(fmt_url, str) and looks_like_http_url(fmt_url):
                candidates.append(fmt_url)
            # fragment 等
            frag_url = fmt.get("fragment_base_url")
            # http(s) のみ
            if isinstance(frag_url, str) and looks_like_http_url(frag_url):
                candidates.append(frag_url)
    # プレイリスト entries
    entries = info.get("entries")
    # entries があれば再帰
    if isinstance(entries, list):
        for entry in entries:
            # 辞書以外は無視
            if not isinstance(entry, dict):
                continue
            # 子エントリの URL を追加
            candidates.extend(collect_info_urls(entry))
    # 重複除去しつつ順序維持
    seen: set[str] = set()
    # 結果
    unique: list[str] = []
    # 走査
    for item in candidates:
        # 既出はスキップ
        if item in seen:
            continue
        # 記録
        seen.add(item)
        unique.append(item)
    # 返す
    return unique


def assert_info_urls_safe(info: dict) -> None:
    """yt-dlp info 内の http(s) URL をすべて SSRF 検査する。"""
    # 各候補を検証
    for candidate in collect_info_urls(info):
        # 危険なら例外
        assert_safe_http_url(candidate)


async def get_with_ssrf_protection(
    session: aiohttp.ClientSession,
    url: str,
    *,
    timeout: Optional[aiohttp.ClientTimeout] = None,
    max_redirects: int = 5,
    **request_kwargs: Any,
) -> aiohttp.ClientResponse:
    """
    SSRF ガード付き GET。
    初回 URL と各リダイレクト先を DNS 検証し、接続後 peer IP も再確認する。
    allow_redirects=False で手動追跡する。
    呼び出し側は返却レスポンスを async with / 終了時 close すること。
    """
    # 追跡中の URL（初回は検証済みにする）
    current = await assert_safe_http_url_async(url)
    # リダイレクト回数
    redirects = 0
    # 手動でリダイレクトを追う
    while True:
        # ホスト名を peer 検証メッセージ用に控える
        hostname = urlparse(current).hostname
        # 自動リダイレクトは無効化し、都度検証する
        response = await session.get(
            current,
            allow_redirects=False,
            timeout=timeout,
            **request_kwargs,
        )
        # 接続後 peer IP が内部向けなら拒否する（DNS 再解決 TOCTOU 対策）
        assert_response_peer_safe(response, hostname=hostname)
        # リダイレクト系ステータス以外なら本文レスポンスとして返す
        if response.status not in {301, 302, 303, 307, 308}:
            # 最終レスポンス（呼び出し側が close / async with する）
            return response
        # Location ヘッダを読む
        location = response.headers.get("Location")
        # リダイレクト応答の接続を破棄する（本文は不要）
        response.close()
        # Location 無しは失敗
        if not location:
            raise UnsafeURLError("Redirect without Location header")
        # 相対 Location を絶対 URL にする
        next_url = urljoin(current, location)
        # 回数上限
        redirects += 1
        # 上限超過
        if redirects > max_redirects:
            raise UnsafeURLError("Too many redirects")
        # リダイレクト先を再検証（DNS 含む）
        current = await assert_safe_http_url_async(next_url)
        # ログ（デバッグ）
        logger.debug("SSRF-safe redirect #%s -> %s", redirects, current)


def filter_blocked_resolved_ips(ips: Collection[str]) -> Sequence[str]:
    """IP 文字列リストのうちブロック対象だけを返す（テスト・診断用）。"""
    # ブロック対象のみ抽出
    return [ip for ip in ips if is_blocked_ip(ip)]
