export type HostConfig = {
  port: string;
  token: string;
  host: string;
  apiBase: string;
  wsLogsUrl: string;
};

declare global {
  interface Window {
    momokaHost?: HostConfig;
  }
}

export function getHostConfig(): HostConfig {
  if (window.momokaHost?.token) {
    return window.momokaHost;
  }
  // 開発フォールバック（preload 無し）— 本番では使わない
  const port = "18765";
  const token = "";
  const host = "127.0.0.1";
  return {
    port,
    token,
    host,
    apiBase: `http://${host}:${port}/host-gui/api`,
    wsLogsUrl: `ws://${host}:${port}/host-gui/api/logs?token=`,
  };
}

export async function apiGet<T>(path: string): Promise<T> {
  const cfg = getHostConfig();
  const res = await fetch(`${cfg.apiBase}${path}`, {
    headers: {
      Authorization: `Bearer ${cfg.token}`,
    },
  });
  if (!res.ok) {
    throw new Error(`GET ${path} failed: ${res.status}`);
  }
  return res.json() as Promise<T>;
}

export async function apiPost<T>(path: string): Promise<T> {
  const cfg = getHostConfig();
  const res = await fetch(`${cfg.apiBase}${path}`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${cfg.token}`,
    },
  });
  if (!res.ok) {
    throw new Error(`POST ${path} failed: ${res.status}`);
  }
  return res.json() as Promise<T>;
}

export async function apiPostJson<T>(path: string, body: unknown): Promise<T> {
  const cfg = getHostConfig();
  const res = await fetch(`${cfg.apiBase}${path}`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${cfg.token}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    throw new Error(`POST ${path} failed: ${res.status}`);
  }
  return res.json() as Promise<T>;
}

export async function apiPut<T>(path: string, body: unknown): Promise<T> {
  const cfg = getHostConfig();
  const res = await fetch(`${cfg.apiBase}${path}`, {
    method: "PUT",
    headers: {
      Authorization: `Bearer ${cfg.token}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    throw new Error(`PUT ${path} failed: ${res.status}`);
  }
  return res.json() as Promise<T>;
}
