const { app, BrowserWindow, shell, ipcMain, net, session } = require("electron");
const path = require("path");

// ログ表示主体の UI なので GPU プロセスを起こさずソフトウェア描画にする（常駐メモリ削減）
app.disableHardwareAcceleration();
// 先読み用の予備 renderer プロセスを作らせない
app.commandLine.appendSwitch("disable-features", "SpareRendererForSitePerProcess");
// renderer の V8 ヒープ上限（ログ保持が暴走しても OS 全体を圧迫しない）
app.commandLine.appendSwitch("js-flags", "--max-old-space-size=256");

/** @type {BrowserWindow | null} */
let mainWindow = null;

/** Bearer は main process のみ保持（renderer へ渡さない） */
const GUI_PORT = process.env.MOMOKA_HOST_GUI_PORT || "18765";
const GUI_HOST = process.env.MOMOKA_HOST_GUI_HOST || "127.0.0.1";
const GUI_TOKEN = process.env.MOMOKA_HOST_GUI_TOKEN || "";
const API_BASE = `http://${GUI_HOST}:${GUI_PORT}/host-gui/api`;

/** Web タブ専用の永続セッション（システムの Chrome / Firefox とは別） */
const YT_PARTITION = "persist:momoka-youtube";
/** ログイン済みとみなす YouTube cookie 名 */
const YT_AUTH_COOKIES = new Set([
  "LOGIN_INFO",
  "SAPISID",
  "SID",
  "__Secure-1PSID",
  "__Secure-3PSID",
  "__Secure-1PAPISID",
  "__Secure-3PAPISID",
]);
/** ログイン済みとみなすニコニコ cookie 名 */
const NICO_AUTH_COOKIES = new Set(["user_session"]);
/** cookie 変化の連続通知をまとめる */
const exportTimers = { youtube: null, niconico: null };
/** YouTube / ニコニコの定期読み直し */
let ytReloadTimer = null;
/** 自動入力に使う。renderer へは渡さない。 */
let ytLogin = {
  email: "",
  password: "",
  reloadMinutes: 60,
  niconicoEmail: "",
  niconicoPassword: "",
};

/** 外部ブラウザで開いてよいスキーム */
const ALLOWED_EXTERNAL_PROTOCOLS = new Set(["https:"]);

/** Host API 生存監視用 */
let apiSeenOk = false;
let apiFailStreak = 0;
/** @type {ReturnType<typeof setInterval> | null} */
let apiWatchTimer = null;
/** 前回の生存確認が未完了なら次を撃たない（ハング時の Promise 蓄積防止） */
let apiWatchInFlight = false;

/** 生存確認 1 回あたりのタイムアウト */
const API_WATCH_TIMEOUT_MS = 3000;
/** renderer 代理 API 呼び出しのタイムアウト */
const PROXY_API_TIMEOUT_MS = 15000;

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
  // パスワードを含む経路は renderer の汎用 IPC から呼ばせない
  if (apiPath === "/youtube-browser-login" || apiPath.startsWith("/youtube-browser-login?")) {
    throw new Error("forbidden");
  }
  const headers = Object.assign({}, opts.headers || {});
  if (GUI_TOKEN) {
    headers.Authorization = `Bearer ${GUI_TOKEN}`;
  }
  /** @type {RequestInit} */
  const init = {
    method,
    headers,
    // Bot ハング時に IPC 応答待ちが溜まり続けないよう打ち切る
    signal: AbortSignal.timeout(PROXY_API_TIMEOUT_MS),
  };
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

function quitHostGui() {
  stopLogSse();
  for (const site of Object.keys(exportTimers)) {
    if (exportTimers[site]) {
      clearTimeout(exportTimers[site]);
      exportTimers[site] = null;
    }
  }
  if (ytReloadTimer) {
    clearInterval(ytReloadTimer);
    ytReloadTimer = null;
  }
  if (apiWatchTimer) {
    clearInterval(apiWatchTimer);
    apiWatchTimer = null;
  }
  app.quit();
}

/**
 * Bot / Host API 停止後にウィンドウが残らないよう生存監視する。
 */
function startApiWatchdog() {
  if (apiWatchTimer) return;
  apiWatchTimer = setInterval(async () => {
    // 前回分が終わっていなければスキップ（応答待ちの多重化を防ぐ）
    if (apiWatchInFlight) return;
    apiWatchInFlight = true;
    try {
      const headers = {};
      if (GUI_TOKEN) {
        headers.Authorization = `Bearer ${GUI_TOKEN}`;
      }
      const res = await net.fetch(`${API_BASE}/status`, {
        headers,
        signal: AbortSignal.timeout(API_WATCH_TIMEOUT_MS),
      });
      // 本文は使わないので破棄し、接続とバッファを即解放する
      void res.body?.cancel().catch(() => undefined);
      if (res.ok) {
        apiSeenOk = true;
        apiFailStreak = 0;
        return;
      }
      apiFailStreak += 1;
    } catch {
      apiFailStreak += 1;
    } finally {
      apiWatchInFlight = false;
    }
    // 一度成功したあと連続失敗なら Bot 側停止とみなして終了
    if (apiSeenOk && apiFailStreak >= 3) {
      quitHostGui();
    }
  }, 1000);
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
      // 意図的な停止 / 差し替え済みの古いストリームは通知しない
      // （新しい購読の終了と誤認され再接続ループになるため）
      const isCurrent = sseAbort === ac;
      if (isCurrent) sseAbort = null;
      if (isCurrent && !sender.isDestroyed()) {
        sender.send("momoka:sse-end");
      }
    }
  })();
  return { ok: true };
}

/**
 * ホストが許可サフィックスか。
 * @param {string} domain
 * @param {string[]} suffixes
 */
function hostIn(domain, suffixes) {
  const host = String(domain || "").replace(/^\./, "").toLowerCase();
  return suffixes.some((suffix) => host === suffix || host.endsWith(`.${suffix}`));
}

/**
 * YouTube / Google 系のホストか。
 * @param {string} domain
 */
function ytDomainOk(domain) {
  return hostIn(domain, [
    "youtube.com",
    "youtu.be",
    "google.com",
    "gstatic.com",
    "googleusercontent.com",
    "ggpht.com",
    "recaptcha.net",
  ]);
}

/**
 * ニコニコ動画のホストか。
 * @param {string} domain
 */
function nicoDomainOk(domain) {
  return hostIn(domain, ["nicovideo.jp", "nico.ms"]);
}

/**
 * ログイン確認（Turnstile）のホストか。cookie の保存対象にはしない。
 * @param {string} domain
 */
function loginChallengeOk(domain) {
  return hostIn(domain, ["challenges.cloudflare.com"]);
}

/**
 * Web タブで開いてよい URL か。
 * @param {string} rawUrl
 */
function isAllowedWebUrl(rawUrl) {
  let parsed;
  try {
    parsed = new URL(rawUrl);
  } catch {
    return false;
  }
  if (parsed.protocol !== "https:") return false;
  return ytDomainOk(parsed.hostname) || nicoDomainOk(parsed.hostname) || loginChallengeOk(parsed.hostname);
}

/**
 * Host API を main 自身から呼ぶ（renderer 向け IPC の拒否リストを通さない）。
 * @param {string} method
 * @param {string} apiPath
 * @param {unknown} [body]
 */
async function mainApi(method, apiPath, body) {
  const headers = {};
  if (GUI_TOKEN) headers.Authorization = `Bearer ${GUI_TOKEN}`;
  /** @type {RequestInit} */
  const init = { method, headers, signal: AbortSignal.timeout(PROXY_API_TIMEOUT_MS) };
  if (body !== undefined && method !== "GET" && method !== "HEAD") {
    headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(body);
  }
  const res = await net.fetch(`${API_BASE}${apiPath}`, init);
  const text = await res.text();
  let json = null;
  try {
    json = text ? JSON.parse(text) : null;
  } catch {
    json = null;
  }
  return { ok: res.ok, status: res.status, json };
}

function sendYtStatus(payload) {
  const win = mainWindow;
  if (!win || win.isDestroyed()) return;
  win.webContents.send("momoka:youtube-cookie-status", payload);
}

async function loadYtLogin() {
  try {
    const res = await mainApi("GET", "/youtube-browser-login");
    if (!res.ok || !res.json) return;
    const email = String(res.json.email || "");
    const password = String(res.json.password || "");
    const reloadMinutes = Number(res.json.reload_minutes) || 60;
    ytLogin = {
      email,
      password,
      reloadMinutes,
      niconicoEmail: String(res.json.niconico_email || ""),
      niconicoPassword: String(res.json.niconico_password || ""),
    };
  } catch (err) {
    console.error("YouTube login config unavailable:", err);
  }
}

const SITE_EXPORT = {
  youtube: {
    domainOk: ytDomainOk,
    auth: YT_AUTH_COOKIES,
    path: "/youtube-cookies",
    loggedOut: "YouTube にログインすると cookie を保存します",
    saved: "YouTube の cookie を保存しました",
    fresh: "YouTube の cookie は最新です",
    failed: "YouTube の cookie 保存に失敗しました",
  },
  niconico: {
    domainOk: nicoDomainOk,
    auth: NICO_AUTH_COOKIES,
    path: "/niconico-cookies",
    loggedOut: "ニコニコ動画にログインすると cookie を保存します",
    saved: "ニコニコの cookie を保存しました",
    fresh: "ニコニコの cookie は最新です",
    failed: "ニコニコの cookie 保存に失敗しました",
  },
};

/**
 * 指定サイトの cookie を API へ送る。
 * @param {"youtube" | "niconico"} site
 * @param {{ quietIfLoggedOut?: boolean }} [opts]
 */
async function exportSiteCookies(site, opts) {
  const spec = SITE_EXPORT[site];
  const quietIfLoggedOut = Boolean(opts && opts.quietIfLoggedOut);
  const ses = session.fromPartition(YT_PARTITION);
  const all = await ses.cookies.get({});
  const cookies = all
    .filter((cookie) => spec.domainOk(cookie.domain))
    .map((cookie) => ({
      name: cookie.name,
      value: cookie.value,
      domain: cookie.domain,
      path: cookie.path,
      secure: cookie.secure,
      expires: cookie.session ? -1 : cookie.expirationDate,
    }));
  if (!cookies.some((cookie) => spec.auth.has(cookie.name))) {
    if (!quietIfLoggedOut) sendYtStatus({ ok: false, message: spec.loggedOut });
    return;
  }
  const res = await mainApi("POST", spec.path, { cookies });
  if (!res.ok) {
    sendYtStatus({ ok: false, message: spec.failed });
    return;
  }
  const changed = Array.isArray(res.json?.changed) ? res.json.changed : [];
  if (res.json?.error === "not_logged_in") {
    if (!quietIfLoggedOut) sendYtStatus({ ok: false, message: spec.loggedOut });
    return;
  }
  if (!changed.length && res.json?.ok) {
    sendYtStatus({ ok: true, message: spec.fresh });
    return;
  }
  sendYtStatus({
    ok: Boolean(res.json?.ok),
    message: res.json?.ok ? spec.saved : spec.failed,
  });
}

/**
 * @param {"youtube" | "niconico"} site
 */
function scheduleSiteExport(site) {
  if (exportTimers[site]) clearTimeout(exportTimers[site]);
  exportTimers[site] = setTimeout(() => {
    exportTimers[site] = null;
    void exportSiteCookies(site).catch((err) => {
      console.error(`${site} cookie export failed:`, err && err.message ? err.message : "failed");
      sendYtStatus({ ok: false, message: SITE_EXPORT[site].failed });
    });
  }, 2000);
}

function bindYtSession() {
  const ses = session.fromPartition(YT_PARTITION);
  ses.cookies.on("changed", (_event, cookie) => {
    if (!cookie) return;
    if (ytDomainOk(cookie.domain)) scheduleSiteExport("youtube");
    if (nicoDomainOk(cookie.domain)) scheduleSiteExport("niconico");
  });
}

/**
 * ニコニコのログイン欄へ config のアカウントを入れる。確認画面はタブで続ける。
 * @param {Electron.WebContents} contents
 * @param {string} url
 */
async function maybeAutofillNiconico(contents, url) {
  if (!ytLogin.niconicoEmail || !ytLogin.niconicoPassword) return;
  if (!url.includes("account.nicovideo.jp")) return;
  const email = JSON.stringify(ytLogin.niconicoEmail);
  const password = JSON.stringify(ytLogin.niconicoPassword);
  const script = `(() => {
    if (window.__momokaNicoWatch) return "watching";
    window.__momokaNicoWatch = 1;
    const setValue = (el, value) => {
      const proto = HTMLInputElement.prototype;
      const desc = Object.getOwnPropertyDescriptor(proto, "value");
      if (el._valueTracker) el._valueTracker.setValue("");
      if (desc && desc.set) desc.set.call(el, value);
      else el.value = value;
      el.dispatchEvent(new Event("input", { bubbles: true }));
      el.dispatchEvent(new Event("change", { bubbles: true }));
    };
    const tryFill = () => {
      const mail = document.querySelector('input[name="mailOrTel"], input[autocomplete="username"]');
      const pass = document.querySelector('input[name="password"], input[autocomplete="current-password"]');
      if (!mail || !pass) return false;
      if (!pass.dataset.momokaFilled) {
        pass.dataset.momokaFilled = "1";
        setValue(mail, ${email});
        setValue(pass, ${password});
      }
      const form = pass.closest("form");
      const button = form && form.querySelector('button[type="submit"]');
      if (!button || button.disabled || button.dataset.momokaClicked) return false;
      button.dataset.momokaClicked = "1";
      button.click();
      return true;
    };
    if (tryFill()) return "submit";
    const obs = new MutationObserver(() => {
      if (tryFill()) obs.disconnect();
    });
    obs.observe(document.documentElement, {
      childList: true,
      subtree: true,
      attributes: true,
      attributeFilter: ["disabled"],
    });
    setTimeout(() => obs.disconnect(), 20000);
    return "wait";
  })()`;
  try {
    await contents.executeJavaScript(script, true);
  } catch {
    console.error("Niconico login autofill failed");
  }
}

async function maybeAutofill(contents) {
  let url = "";
  try {
    url = contents.getURL();
  } catch {
    return;
  }
  if (url.includes("account.nicovideo.jp")) {
    await maybeAutofillNiconico(contents, url);
    return;
  }
  if (!ytLogin.email || !ytLogin.password) return;
  if (!url.includes("accounts.google.com")) return;
  const email = JSON.stringify(ytLogin.email);
  const password = JSON.stringify(ytLogin.password);
  const script = `(() => {
    const setValue = (el, value) => {
      const proto = Object.getPrototypeOf(el);
      const desc = Object.getOwnPropertyDescriptor(proto, "value");
      if (desc && desc.set) desc.set.call(el, value);
      else el.value = value;
      el.dispatchEvent(new Event("input", { bubbles: true }));
      el.dispatchEvent(new Event("change", { bubbles: true }));
    };
    const emailBox = document.querySelector("#identifierId");
    if (emailBox && !emailBox.dataset.momokaFilled) {
      emailBox.dataset.momokaFilled = "1";
      setValue(emailBox, ${email});
      const next = document.querySelector("#identifierNext button, #identifierNext");
      if (next) next.click();
      return "email";
    }
    const pass = document.querySelector('input[name="Passwd"]');
    if (pass && !pass.dataset.momokaFilled) {
      pass.dataset.momokaFilled = "1";
      setValue(pass, ${password});
      const next = document.querySelector("#passwordNext button, #passwordNext");
      if (next) next.click();
      return "password";
    }
    return "none";
  })()`;
  try {
    await contents.executeJavaScript(script, true);
  } catch {
    console.error("YouTube login autofill failed");
  }
}

/**
 * Web タブの webview を YouTube / Google / ニコニコに制限する。
 * @param {Electron.WebContents} contents
 */
function bindWebview(contents) {
  contents.on("will-navigate", (event, url) => {
    if (!isAllowedWebUrl(url)) event.preventDefault();
  });
  contents.on("will-redirect", (event, url) => {
    if (!isAllowedWebUrl(url)) event.preventDefault();
  });
  contents.setWindowOpenHandler(({ url }) => {
    if (isAllowedWebUrl(url)) {
      void contents.loadURL(url);
    }
    return { action: "deny" };
  });
  contents.on("did-finish-load", () => {
    void maybeAutofill(contents);
  });
}

function startYtReloadLoop() {
  if (ytReloadTimer) clearInterval(ytReloadTimer);
  const minutes = Math.max(15, Number(ytLogin.reloadMinutes) || 60);
  ytReloadTimer = setInterval(() => {
    const win = mainWindow;
    if (!win || win.isDestroyed()) return;
    win.webContents.send("momoka:youtube-reload");
    scheduleSiteExport("youtube");
    scheduleSiteExport("niconico");
  }, minutes * 60 * 1000);
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
  ipcMain.handle("momoka:quit", async () => {
    quitHostGui();
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
      // Web タブの <webview>
      webviewTag: true,
      // 入力欄はほぼ無いので辞書をロードしない
      spellcheck: false,
    },
    show: false,
  });
  mainWindow = win;

  win.once("ready-to-show", () => {
    if (!win.isDestroyed()) win.show();
    startApiWatchdog();
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
  bindYtSession();
  app.on("web-contents-created", (_event, contents) => {
    if (contents.getType() === "webview") bindWebview(contents);
  });
  void loadYtLogin().finally(() => startYtReloadLoop());
  createWindow();
  // 以前のログインが残っていれば、タブを開く前でも cookie を書き出す
  setTimeout(() => {
    void exportSiteCookies("youtube", { quietIfLoggedOut: true }).catch(() => undefined);
    void exportSiteCookies("niconico", { quietIfLoggedOut: true }).catch(() => undefined);
  }, 8000);
  app.on("activate", () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow();
  });
});

app.on("window-all-closed", () => {
  stopLogSse();
  // macOS 以外はウィンドウ無しで常駐させない（シャットダウン残留防止）
  if (process.platform !== "darwin") {
    quitHostGui();
  }
});
