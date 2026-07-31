const { contextBridge } = require("electron");

const port = process.env.MOMOKA_HOST_GUI_PORT || "18765";
const token = process.env.MOMOKA_HOST_GUI_TOKEN || "";
const host = process.env.MOMOKA_HOST_GUI_HOST || "127.0.0.1";

contextBridge.exposeInMainWorld("momokaHost", {
  port,
  token,
  host,
  apiBase: `http://${host}:${port}/host-gui/api`,
  wsLogsUrl: `ws://${host}:${port}/host-gui/api/logs?token=${encodeURIComponent(token)}`,
});
