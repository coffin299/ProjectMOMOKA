import { useState } from "react";
import { apiPost, getHostConfig } from "../api";

export function ShutdownButton() {
  const [armed, setArmed] = useState(false);
  const [busy, setBusy] = useState(false);

  const onClick = async () => {
    if (!armed) {
      setArmed(true);
      window.setTimeout(() => setArmed(false), 4000);
      return;
    }
    setBusy(true);
    try {
      await apiPost("/shutdown");
      // Bot 停止要求後にウィンドウ自身も閉じる（Python 側 taskkill の保険）
      const quit = getHostConfig().quitApp;
      if (quit) {
        await quit();
      }
    } catch {
      setBusy(false);
      setArmed(false);
    }
  };

  return (
    <button
      type="button"
      className="btn-danger"
      disabled={busy}
      onClick={onClick}
      title="Click twice to confirm shutdown"
    >
      {busy ? "Shutting down…" : armed ? "Confirm Shutdown" : "Shutdown"}
    </button>
  );
}
