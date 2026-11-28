import { invoke } from "@tauri-apps/api/core";
import { listen, type UnlistenFn } from "@tauri-apps/api/event";

export type ApiResponse = {
  ok: boolean;
  status: number;
  data: unknown;
  text: string;
  /** 旧フィールド互換（リネーム前バイナリ向け） */
  json?: unknown;
};

export type ConnInfo = {
  host: string;
  port: string;
  hasAuth: boolean;
  apiBase: string;
};

export async function hostHasAuth(): Promise<boolean> {
  return Boolean(await invoke<boolean>("momoka_has_auth"));
}

export async function getConnInfo(): Promise<ConnInfo> {
  return invoke<ConnInfo>("momoka_conn_info");
}

/** Host API /snapshot の JSON 本体をそのまま取る。 */
export async function getSnapshot<T = unknown>(): Promise<T> {
  return invoke<T>("momoka_snapshot");
}

export async function apiRequest(opts: {
  method?: string;
  path: string;
  body?: unknown;
}): Promise<ApiResponse> {
  // 引数はフラットに渡す（ネスト args は WebView によって落ちることがある）
  const args: Record<string, unknown> = {
    method: opts.method ?? "GET",
    path: opts.path,
  };
  // null は送らず省略（Option はキー無しで None）
  if (opts.body !== undefined && opts.body !== null) {
    args.body = opts.body;
  }
  return invoke<ApiResponse>("momoka_api", args);
}

export async function startLogSse(): Promise<void> {
  await invoke("momoka_sse_start");
}

export async function stopLogSse(): Promise<void> {
  await invoke("momoka_sse_stop");
}

export async function quitApp(): Promise<void> {
  await invoke("momoka_quit");
}

export function onLogSse(handler: (data: unknown) => void): Promise<UnlistenFn> {
  return listen("momoka:sse-log", (event) => handler(event.payload));
}

export function onLogSseEnd(handler: () => void): Promise<UnlistenFn> {
  return listen("momoka:sse-end", () => handler());
}

export function onYoutubeCookieStatus(
  handler: (data: { ok?: boolean; message?: string }) => void
): Promise<UnlistenFn> {
  return listen("momoka:youtube-cookie-status", (event) =>
    handler(event.payload as { ok?: boolean; message?: string })
  );
}

export function onYoutubeReload(handler: () => void): Promise<UnlistenFn> {
  return listen("momoka:youtube-reload", () => handler());
}

export function onBrowserUrl(handler: (url: string) => void): Promise<UnlistenFn> {
  return listen("momoka:browser-url", (event) => {
    const payload = event.payload as { url?: string };
    if (payload?.url) handler(payload.url);
  });
}

export type BrowserBounds = {
  x: number;
  y: number;
  width: number;
  height: number;
};

export async function browserSetVisible(
  visible: boolean,
  bounds?: BrowserBounds
): Promise<void> {
  await invoke("browser_set_visible", { visible, bounds: bounds ?? null });
}

export async function browserNavigate(target: string): Promise<void> {
  await invoke("browser_navigate", { target });
}

export async function browserReload(): Promise<void> {
  await invoke("browser_reload");
}

export async function browserGoBack(): Promise<void> {
  await invoke("browser_go_back");
}

export async function browserGoForward(): Promise<void> {
  await invoke("browser_go_forward");
}

export async function browserGetUrl(): Promise<string> {
  return invoke<string>("browser_get_url");
}
