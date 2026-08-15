const { app, BrowserWindow, shell, ipcMain, net } = require("electron");
const path = require("path");

/** @type {BrowserWindow | null} */
let mainWindow = null;

/** Bearer は main process のみ保持（renderer へ渡さない） */
const GUI_PORT = process.env.MOMOKA_HOST_GUI_PORT || "18765";
const GUI_HOST = process.env.MOMOKA_HOST_GUI_HOST || "127.0.0.1";
const GUI_TOKEN = process.env.MOMOKA_HOST_GUI_TOKEN || "";
const API_BASE = `http://${GUI_HOST}:${GUI_PORT}/host-gui/api`;

/** 外部ブラウザで開いてよいスキーム */
const ALLOWED_EXTERNAL_PROTOCOLS = new Set(["https:"]);

/**
 * window.open 由来 URL を検証し、許可時のみ openExternal する。
 * @param {string} rawUrl
 * @returns {boolean}
 */
function openExternalSafe(rawUrl) {
  let parsed;
  try {
    parsed = new URL(rawUrl);
  } catch {
    return false;
  }
  if (!ALLOWED_EXTERNAL_PROTOCOLS.has(parsed.protocol)) {
    return false;
  }
  if (!parsed.hostname) {
    return false;
  }
  shell.openExternal(parsed.toString());
  return true;
}

/**
 * Host GUI API を main から代理呼び出しする（token はここだけ）。
 * @param {{ method?: string, path: string, body?: unknown, headers?: Record<string,string> }} opts
 */
async function proxyApi(opts) {
  const method = String(opts.method || "GET").toUpperCase();
  const apiPath = String(opts.path || "");
  if (!apiPath.startsWith("/")) {
    throw new Error("invalid_path");
  }
  const headers = Object.assign({}, opts.headers || {});
  if (GUI_TOKEN) {
    headers.Authorization = `Bearer ${GUI_TOKEN}`;
  }
  /** @type {RequestInit} */
  const init = { method, headers };
  if (opts.body !== undefined && method !== "GET" && method !== "HEAD") {
    headers["Content-Type"] = headers["Content-Type"] || "application/json";
    init.body =
      typeof opts.body === "string" ? opts.body : JSON.stringify(opts.body);
  }
  const res = await net.fetch(`${API_BASE}${apiPath}`, init);
  const text = await res.text();
  let json = null;
  try {
    json = text ? JSON.parse(text) : null;
  } catch {
    json = null;
  }
  return {
    ok: res.ok,
    status: res.status,
    json,
    text,
  };
}

/** SSE 購読の AbortController */
let sseAbort = null;

function stopLogSse() {
  if (sseAbort) {
    try {
      sseAbort.abort();
    } catch {
      /* ignore */
    }
    sseAbort = null;
  }
}

/**
 * ログ SSE を main で購読し、renderer へ転送する。
 * @param {Electron.WebContents} sender
 */
async function startLogSse(sender) {
  stopLogSse();
  if (!GUI_TOKEN) {
    throw new Error("missing_token");
  }
  const ac = new AbortController();
  sseAbort = ac;
  const res = await net.fetch(`${API_BASE}/logs/stream`, {
    headers: {
      Authorization: `Bearer ${GUI_TOKEN}`,
      Accept: "text/event-stream",
    },
    signal: ac.signal,
  });
  if (!res.ok || !res.body) {
    throw new Error(`SSE HTTP ${res.status}`);
  }
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buf = "";
  (async () => {
    try {
      while (!ac.signal.aborted) {
        const { done, value } = await reader.read();
        if (done) break;
        buf += decoder.decode(value, { stream: true });
        const parts = buf.split("\n\n");
        buf = parts.pop() ?? "";
        for (const frame of parts) {
          const dataLines = frame
            .split("\n")
            .filter((ln) => ln.startsWith("data:"))
            .map((ln) => ln.slice(5).trimStart());
          if (!dataLines.length) continue;
          try {
            const data = JSON.parse(dataLines.join("\n"));
            if (!sender.isDestroyed()) {
              sender.send("momoka:sse-log", data);
            }
          } catch {
            /* ignore bad frame */
          }
        }
      }
    } catch {
      /* aborted or network */
    } finally {
      if (!sender.isDestroyed()) {
        sender.send("momoka:sse-end");
      }
    }
  })();
  return { ok: true };
}

function registerIpc() {
  ipcMain.handle("momoka:api", async (_event, opts) => proxyApi(opts || {}));
  ipcMain.handle("momoka:has-auth", async () => Boolean(GUI_TOKEN));
  ipcMain.handle("momoka:sse-start", async (event) =>
    startLogSse(event.sender)
  );
  ipcMain.handle("momoka:sse-stop", async () => {
    stopLogSse();
    return { ok: true };
  });
}

function createWindow() {
  const win = new BrowserWindow({
    width: 1280,
    height: 800,
    backgroundColor: "#1E1F22",
    webPreferences: {
      preload: path.join(__dirname, "preload.cjs"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
    },
    show: false,
  });
  mainWindow = win;

  win.once("ready-to-show", () => {
    if (!win.isDestroyed()) win.show();
  });

  win.on("closed", () => {
    stopLogSse();
    if (mainWindow === win) mainWindow = null;
  });

  win.webContents.setWindowOpenHandler(({ url }) => {
    openExternalSafe(url);
    return { action: "deny" };
  });

  const useDev = process.env.MOMOKA_GUI_DEV === "1";
  const bust = Date.now();

  const load = async () => {
    try {
      if (win.isDestroyed()) return;
      await win.webContents.session.clearCache();
    } catch {
      /* ignore */
    }
    if (win.isDestroyed()) return;
    try {
      if (useDev) {
        await win.loadURL(`http://127.0.0.1:5173/?v=${bust}`);
      } else {
        await win.loadURL(`http://${GUI_HOST}:${GUI_PORT}/?v=${bust}`);
      }
    } catch (err) {
      console.error("Failed to load Host GUI URL:", err);
    }
  };

  void load();
}

app.whenReady().then(() => {
  registerIpc();
  createWindow();
  app.on("activate", () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow();
  });
});

app.on("window-all-closed", () => {
  stopLogSse();
});
