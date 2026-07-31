const { app, BrowserWindow, shell } = require("electron");
const path = require("path");

/** @type {BrowserWindow | null} */
let mainWindow = null;

function createWindow() {
  const port = process.env.MOMOKA_HOST_GUI_PORT || "18765";
  const host = process.env.MOMOKA_HOST_GUI_HOST || "127.0.0.1";

  mainWindow = new BrowserWindow({
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

  mainWindow.once("ready-to-show", () => {
    if (mainWindow) mainWindow.show();
  });

  // 閉じても Bot は継続（ウィンドウだけ消す）
  mainWindow.on("closed", () => {
    mainWindow = null;
  });

  // 外部リンクは OS ブラウザへ
  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    shell.openExternal(url);
    return { action: "deny" };
  });

  const useDev = process.env.MOMOKA_GUI_DEV === "1";
  if (useDev) {
    // Vite 開発サーバ（同一マシン loopback）
    mainWindow.loadURL("http://127.0.0.1:5173");
  } else {
    // FastAPI が dist を配信（同一オリジン + Bearer）
    mainWindow.loadURL(`http://${host}:${port}/`);
  }
}

app.whenReady().then(() => {
  createWindow();
  app.on("activate", () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow();
  });
});

app.on("window-all-closed", () => {
  // Bot 継続のためウィンドウだけ閉じる場合は quit しない。
  // プロセス全体の終了は Python 側 stop_host_gui / taskkill に任せる。
});
