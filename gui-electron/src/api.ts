export type HostConfig = {
  port: string;
  host: string;
  apiBase: string;
  /** 互換用。常に空（token は main のみ） */
  token: string;
  hasAuth?: () => Promise<boolean>;
  apiRequest?: (opts: {
    method?: string;
    path: string;
    body?: unknown;
    headers?: Record<string, string>;
  }) => Promise<{ ok: boolean; status: number; json: unknown; text: string }>;
  startLogSse?: () => Promise<{ ok: boolean }>;
  stopLogSse?: () => Promise<{ ok: boolean }>;
  onLogSse?: (handler: (data: unknown) => void) => () => void;
  onLogSseEnd?: (handler: () => void) => () => void;
};

declare global {
  interface Window {
    momokaHost?: HostConfig;
  }
}

export function getHostConfig(): HostConfig {
  if (window.momokaHost) {
    return window.momokaHost;
  }
  // 開発フォールバック（preload 無し）— 本番では使わない
  const port = "18765";
  const host = "127.0.0.1";
  return {
    port,
    host,
    token: "",
    apiBase: `http://${host}:${port}/host-gui/api`,
  };
}

export async function hostHasAuth(): Promise<boolean> {
  const cfg = getHostConfig();
  if (cfg.hasAuth) {
    return Boolean(await cfg.hasAuth());
  }
  return Boolean(cfg.token);
}

async function viaIpc<T>(
  method: string,
  path: string,
  body?: unknown
): Promise<T> {
  const cfg = getHostConfig();
  if (!cfg.apiRequest) {
    throw new Error("Host GUI IPC is unavailable");
  }
  const res = await cfg.apiRequest({ method, path, body });
  if (!res.ok) {
    throw new Error(`${method} ${path} failed: ${res.status}`);
  }
  return res.json as T;
}

export async function apiGet<T>(path: string): Promise<T> {
  const cfg = getHostConfig();
  if (cfg.apiRequest) {
    return viaIpc<T>("GET", path);
  }
  // preload 無しフォールバック（通常は到達しない）
  const res = await fetch(`${cfg.apiBase}${path}`, {
    headers: cfg.token ? { Authorization: `Bearer ${cfg.token}` } : {},
  });
  if (!res.ok) {
    throw new Error(`GET ${path} failed: ${res.status}`);
  }
  return res.json() as Promise<T>;
}

export async function apiPost<T>(path: string): Promise<T> {
  const cfg = getHostConfig();
  if (cfg.apiRequest) {
    return viaIpc<T>("POST", path);
  }
  const res = await fetch(`${cfg.apiBase}${path}`, {
    method: "POST",
    headers: cfg.token ? { Authorization: `Bearer ${cfg.token}` } : {},
  });
  if (!res.ok) {
    throw new Error(`POST ${path} failed: ${res.status}`);
  }
  return res.json() as Promise<T>;
}

export async function apiPostJson<T>(path: string, body: unknown): Promise<T> {
  const cfg = getHostConfig();
  if (cfg.apiRequest) {
    return viaIpc<T>("POST", path, body);
  }
  const res = await fetch(`${cfg.apiBase}${path}`, {
    method: "POST",
    headers: {
      ...(cfg.token ? { Authorization: `Bearer ${cfg.token}` } : {}),
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
  if (cfg.apiRequest) {
    return viaIpc<T>("PUT", path, body);
  }
  const res = await fetch(`${cfg.apiBase}${path}`, {
    method: "PUT",
    headers: {
      ...(cfg.token ? { Authorization: `Bearer ${cfg.token}` } : {}),
      "Content-Type": "application/json",
    },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    throw new Error(`PUT ${path} failed: ${res.status}`);
  }
  return res.json() as Promise<T>;
}
