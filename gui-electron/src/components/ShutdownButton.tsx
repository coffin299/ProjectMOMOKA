import { useState } from "react";
import { apiPost } from "../api";

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
      // Bot 停止のみ要求。ログ出し切り後に Python / API watchdog がウィンドウを閉じる
      await apiPost("/shutdown");
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
      title="Click twice to confirm. Window closes after shutdown logs finish."
    >
      {busy ? "Shutting down…" : armed ? "Confirm Shutdown" : "Shutdown"}
    </button>
  );
}
