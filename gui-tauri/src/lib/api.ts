import { apiUrl, getToken, hasSession } from "./session";

export { hasSession as hostHasAuth };

async function viaHttp<T>(method: string, path: string, body?: unknown): Promise<T> {
  if (!hasSession()) {
    throw new Error("no-session");
  }
  const headers: Record<string, string> = {
    Authorization: `Bearer ${getToken()}`,
    Accept: "application/json",
  };
  const init: RequestInit = { method, headers, cache: "no-store" };
  if (body !== undefined && method !== "GET" && method !== "HEAD") {
    headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(body);
  }
  const res = await fetch(apiUrl(path), init);
  const text = await res.text();
  if (!res.ok) {
    const brief = text.slice(0, 120);
    throw new Error(`http ${res.status}: ${brief || res.statusText}`);
  }
  if (!text) return null as T;
  try {
    return JSON.parse(text) as T;
  } catch {
    throw new Error("json-parse");
  }
}

export async function apiGet<T>(path: string): Promise<T> {
  return viaHttp<T>("GET", path);
}

export async function apiPost<T>(path: string): Promise<T> {
  return viaHttp<T>("POST", path);
}

export async function apiPostJson<T>(path: string, body: unknown): Promise<T> {
  return viaHttp<T>("POST", path, body);
}

export async function apiPut<T>(path: string, body: unknown): Promise<T> {
  return viaHttp<T>("PUT", path, body);
}
