import { useEffect, useRef, useState } from "react";
import { apiGet, getHostConfig } from "../api";

export type LogEntry = {
  name: string;
  level: string;
  message: string;
  category: string;
  id: number;
};

const LEVEL_RANK: Record<string, number> = {
  DEBUG: 10,
  INFO: 20,
  WARNING: 30,
  ERROR: 40,
  CRITICAL: 50,
};

let seq = 0;

export function useLogStream(maxLines = 10000) {
  const [entries, setEntries] = useState<LogEntry[]>([]);
  const [connected, setConnected] = useState(false);
  const [restored, setRestored] = useState(false);
  const wsRef = useRef<WebSocket | null>(null);

  useEffect(() => {
    const cfg = getHostConfig();
    if (!cfg.token) return;
    let closed = false;
    let retry: number | undefined;

    const loadHistory = async () => {
      try {
        const data = await apiGet<{ items: Omit<LogEntry, "id">[] }>(
          `/logs/history?max_lines=${maxLines}`
        );
        if (closed) return;
        const items = (data.items || []).map((row) => ({
          ...row,
          id: ++seq,
        }));
        setEntries(items.slice(-maxLines));
        setRestored(true);
      } catch {
        if (!closed) setRestored(true);
      }
    };

    const connect = () => {
      if (closed) return;
      const ws = new WebSocket(cfg.wsLogsUrl);
      wsRef.current = ws;
      ws.onopen = () => setConnected(true);
      ws.onclose = () => {
        setConnected(false);
        if (!closed) {
          retry = window.setTimeout(connect, 1500);
        }
      };
      ws.onerror = () => {
        try {
          ws.close();
        } catch {
          /* ignore */
        }
      };
      ws.onmessage = (ev) => {
        try {
          const data = JSON.parse(ev.data) as Omit<LogEntry, "id">;
          setEntries((prev) => {
            const next = [...prev, { ...data, id: ++seq }];
            if (next.length > maxLines) {
              return next.slice(next.length - maxLines);
            }
            return next;
          });
        } catch {
          /* ignore */
        }
      };
    };

    // 先に .log 末尾を復元してからライブ接続
    loadHistory().finally(() => {
      if (!closed) connect();
    });

    return () => {
      closed = true;
      if (retry) window.clearTimeout(retry);
      wsRef.current?.close();
    };
  }, [maxLines]);

  // Clear は画面上のみ（ファイルは消さない）
  const clear = () => setEntries([]);

  const filterBy = (category: string, minLevel: string): LogEntry[] => {
    const min = LEVEL_RANK[minLevel] ?? 20;
    return entries.filter((e) => {
      if (category === "error") {
        return (
          e.category === "error" ||
          e.level === "ERROR" ||
          e.level === "CRITICAL"
        );
      }
      if (e.category !== category) return false;
      return (LEVEL_RANK[e.level] ?? 20) >= min;
    });
  };

  return { entries, connected, restored, clear, filterBy };
}
