import { invoke } from "@tauri-apps/api/core";
import { listen, type UnlistenFn } from "@tauri-apps/api/event";
import { apiGet } from "./api";
import { getApiOrigin, hasSession } from "./session";
import type { SnapshotPayload } from "./stores/status";

/** Tauri シェル内かどうか（Web タブ IPC 用）。 */
export function isTauriShell(): boolean {
  return typeof window !== "undefined" && "__TAURI_INTERNALS__" in window;
}

export type ConnInfo = {
  host: string;
  port: string;
  hasAuth: boolean;
  apiBase: string;
};

export async function getConnInfo(): Promise<ConnInfo> {
  if (!hasSession()) {
    throw new Error("no-session");
  }
  try {
    const u = new URL(getApiOrigin());
    return {
      host: u.hostname,
      port: u.port || (u.protocol === "https:" ? "443" : "80"),
      hasAuth: true,
      apiBase: `${getApiOrigin()}/host-gui/api`,
    };
  } catch {
    throw new Error("bad-api-origin");
  }
}

/** Host API /snapshot（HTTP）。 */
export async function getSnapshot(): Promise<SnapshotPayload> {
  return apiGet<SnapshotPayload>("/snapshot");
}

export async function quitApp(): Promise<void> {
  if (!isTauriShell()) return;
  try {
    await invoke("momoka_quit");
  } catch {
    /* ignore */
  }
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
  if (!isTauriShell()) return;
  await invoke("browser_set_visible", { visible, bounds: bounds ?? null });
}

export async function browserNavigate(target: string): Promise<void> {
  if (!isTauriShell()) return;
  await invoke("browser_navigate", { target });
}

export async function browserReload(): Promise<void> {
  if (!isTauriShell()) return;
  await invoke("browser_reload");
}

export async function browserGoBack(): Promise<void> {
  if (!isTauriShell()) return;
  await invoke("browser_go_back");
}

export async function browserGoForward(): Promise<void> {
  if (!isTauriShell()) return;
  await invoke("browser_go_forward");
}

export async function browserGetUrl(): Promise<string> {
  if (!isTauriShell()) return "";
  return invoke<string>("browser_get_url");
}

export function onYoutubeCookieStatus(
  handler: (data: { ok?: boolean; message?: string }) => void
): Promise<UnlistenFn> {
  if (!isTauriShell()) return Promise.resolve(() => undefined);
  return listen("momoka:youtube-cookie-status", (event) =>
    handler(event.payload as { ok?: boolean; message?: string })
  );
}

export function onYoutubeReload(handler: () => void): Promise<UnlistenFn> {
  if (!isTauriShell()) return Promise.resolve(() => undefined);
  return listen("momoka:youtube-reload", () => handler());
}

export function onBrowserUrl(handler: (url: string) => void): Promise<UnlistenFn> {
  if (!isTauriShell()) return Promise.resolve(() => undefined);
  return listen("momoka:browser-url", (event) => {
    const payload = event.payload as { url?: string };
    if (payload?.url) handler(payload.url);
  });
}
