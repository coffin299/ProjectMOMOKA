/** Host API セッション（HTTP 直結）。IPC に依存しない。 */

let token = "";
let apiOrigin = "";

/** URL の ?t= / #t= と location.origin から認証を初期化する。 */
export function initSession(): { ok: boolean; reason: string } {
  const u = new URL(window.location.href);
  const fromQuery = u.searchParams.get("t") || u.searchParams.get("token") || "";
  const hash = u.hash.startsWith("#") ? u.hash.slice(1) : u.hash;
  const hashParams = new URLSearchParams(hash);
  const fromHash = hashParams.get("t") || hashParams.get("token") || "";
  token = (fromQuery || fromHash || "").trim();

  // Tauri 同梱アセット (tauri:// / asset) のときは Host API origin を別途渡す
  const fromApi = u.searchParams.get("api") || hashParams.get("api") || "";
  if (fromApi) {
    apiOrigin = fromApi.replace(/\/$/, "");
  } else if (u.protocol.startsWith("http")) {
    apiOrigin = u.origin;
  } else {
    apiOrigin = "";
  }

  if (token) {
    u.searchParams.delete("t");
    u.searchParams.delete("token");
    u.searchParams.delete("api");
    const cleanHash = new URLSearchParams(
      [...hashParams.entries()].filter(([k]) => k !== "t" && k !== "token" && k !== "api")
    ).toString();
    const next = `${u.pathname}${u.search}${cleanHash ? `#${cleanHash}` : ""}`;
    try {
      history.replaceState(null, "", next || u.pathname);
    } catch {
      /* ignore */
    }
  }

  if (!token) return { ok: false, reason: "no-token" };
  if (!apiOrigin) return { ok: false, reason: "no-api-origin" };
  return { ok: true, reason: "ok" };
}

export function getToken(): string {
  return token;
}

export function getApiOrigin(): string {
  return apiOrigin;
}

export function apiUrl(path: string): string {
  const p = path.startsWith("/") ? path : `/${path}`;
  return `${apiOrigin}/host-gui/api${p}`;
}

export function hasSession(): boolean {
  return Boolean(token && apiOrigin);
}
