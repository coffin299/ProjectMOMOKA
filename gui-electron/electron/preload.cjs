const { contextBridge, ipcRenderer } = require("electron");

const port = process.env.MOMOKA_HOST_GUI_PORT || "18765";
const host = process.env.MOMOKA_HOST_GUI_HOST || "127.0.0.1";

// token は renderer に渡さない。API / SSE は main 経由 IPC。
contextBridge.exposeInMainWorld("momokaHost", {
  port,
  host,
  apiBase: `http://${host}:${port}/host-gui/api`,
  /** @deprecated token は非公開。hasAuth / apiRequest を使う */
  token: "",
  hasAuth: () => ipcRenderer.invoke("momoka:has-auth"),
  apiRequest: (opts) => ipcRenderer.invoke("momoka:api", opts),
  startLogSse: () => ipcRenderer.invoke("momoka:sse-start"),
  stopLogSse: () => ipcRenderer.invoke("momoka:sse-stop"),
  quitApp: () => ipcRenderer.invoke("momoka:quit"),
  onLogSse: (handler) => {
    const listener = (_event, data) => handler(data);
    ipcRenderer.on("momoka:sse-log", listener);
    return () => ipcRenderer.removeListener("momoka:sse-log", listener);
  },
  onLogSseEnd: (handler) => {
    const listener = () => handler();
    ipcRenderer.on("momoka:sse-end", listener);
    return () => ipcRenderer.removeListener("momoka:sse-end", listener);
  },
});
