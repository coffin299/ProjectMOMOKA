# MOMOKA/utilities/url_safety.py
# 外部 URL 取得前の SSRF 対策（スキーム制限・DNS 解決後のプライベート IP 拒否・リダイレクト再検証）。
from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
from typing import Any, Collection, Optional, Sequence, Set, Union
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
        # 同期解決
        for ip_str in _resolve_host_ips(hostname):
            # いずれかがブロック対象なら拒否（DNS リバインディング軽減）
            if is_blocked_ip(ip_str):
                raise UnsafeURLError(
                    f"Blocked resolved IP {ip_str} for host {hostname}"
                )
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
        # 非同期解決
        for ip_str in await _resolve_host_ips_async(hostname):
            # ブロック IP なら拒否
            if is_blocked_ip(ip_str):
                raise UnsafeURLError(
                    f"Blocked resolved IP {ip_str} for host {hostname}"
                )
    # 返す
    return cleaned


def looks_like_http_url(value: str) -> bool:
    """文字列が http(s) URL らしいか（検索クエリと区別する）。"""
    # 空は URL ではない
    if not value or not str(value).strip():
        return False
    # 小文字化して先頭スキームを見る
    lowered = str(value).strip().lower()
    # http/https で始まるものだけ URL 扱い
    return lowered.startswith("http://") or lowered.startswith("https://")


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
    初回 URL と各リダイレクト先を再検証し、allow_redirects=False で手動追跡する。
    呼び出し側は返却レスポンスを async with / 終了時 close すること。
    """
    # 追跡中の URL（初回は検証済みにする）
    current = await assert_safe_http_url_async(url)
    # リダイレクト回数
    redirects = 0
    # 手動でリダイレクトを追う
    while True:
        # 自動リダイレクトは無効化し、都度検証する
        response = await session.get(
            current,
            allow_redirects=False,
            timeout=timeout,
            **request_kwargs,
        )
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
