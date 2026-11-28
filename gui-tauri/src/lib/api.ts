import { apiRequest, hostHasAuth } from "./host";

export { hostHasAuth };

async function viaIpc<T>(method: string, path: string, body?: unknown): Promise<T> {
  const res = await apiRequest({ method, path, body });
  if (!res?.ok) {
    throw new Error(`${method} ${path} failed: ${res?.status ?? "?"}`);
  }
  // data 優先、旧 json フィールドも許容
  return (res.data ?? res.json) as T;
}

export async function apiGet<T>(path: string): Promise<T> {
  return viaIpc<T>("GET", path);
}

export async function apiPost<T>(path: string): Promise<T> {
  return viaIpc<T>("POST", path);
}

export async function apiPostJson<T>(path: string, body: unknown): Promise<T> {
  return viaIpc<T>("POST", path, body);
}

export async function apiPut<T>(path: string, body: unknown): Promise<T> {
  return viaIpc<T>("PUT", path, body);
}
