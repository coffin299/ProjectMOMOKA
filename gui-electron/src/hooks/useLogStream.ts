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

/** 履歴ポーリング間隔（WS が死んでも追従する） */
const HISTORY_POLL_MS = 2000;
/** WS 再接続間隔 */
const WS_RETRY_MS = 2000;

let seq = 0;

function mapRows(
  rows: Omit<LogEntry, "id">[] | undefined
): LogEntry[] {
  return (rows || []).map((row) => ({
    ...row,
    id: ++seq,
  }));
}

/** 末尾へ未所持メッセージだけ足す（同一文言の完全重複は捨てる） */
function appendFresh(
  prev: LogEntry[],
  incoming: Omit<LogEntry, "id">[],
  maxLines: number
): LogEntry[] {
  if (!incoming.length) return prev;
  const seen = new Set(prev.map((e) => e.message));
  const fresh: LogEntry[] = [];
  for (const row of incoming) {
    if (!row || typeof row.message !== "string") continue;
    if (seen.has(row.message)) continue;
    seen.add(row.message);
    fresh.push({ ...row, id: ++seq });
  }
  if (!fresh.length) return prev;
  const next = [...prev, ...fresh];
  if (next.length > maxLines) {
    return next.slice(next.length - maxLines);
  }
  return next;
}

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
    let pollTimer: number | undefined;
    let authTimer: number | undefined;

    const loadHistory = async (
      mode: "replace" | "append",
      lineLimit = maxLines
    ) => {
      try {
        const data = await apiGet<{ items: Omit<LogEntry, "id">[] }>(
          `/logs/history?max_lines=${lineLimit}`
        );
        if (closed) return;
        const items = data.items || [];
        if (mode === "replace") {
          setEntries(mapRows(items).slice(-maxLines));
        } else {
          setEntries((prev) => appendFresh(prev, items, maxLines));
        }
        setRestored(true);
      } catch {
        if (!closed) setRestored(true);
      }
    };

    const connect = () => {
      if (closed) return;
      // 毎回最新 config を読む
      const live = getHostConfig();
      if (!live.token) return;
      // 古いソケットを捨てる
      try {
        wsRef.current?.close();
      } catch {
        /* ignore */
      }
      // クエリにも subprotocol にも token を載せない（接続直後メッセージで認証）
      const ws = new WebSocket(live.wsLogsUrl);
      wsRef.current = ws;
      let authed = false;

      ws.onopen = () => {
        // 必須: 初回メッセージ認証
        try {
          ws.send(JSON.stringify({ type: "auth", token: live.token }));
        } catch {
          /* ignore */
        }
        // auth_ok が来なければ未接続扱いのまま再接続へ
        if (authTimer) window.clearTimeout(authTimer);
        authTimer = window.setTimeout(() => {
          if (!authed && ws.readyState === WebSocket.OPEN) {
            try {
              ws.close();
            } catch {
              /* ignore */
            }
          }
        }, 5000);
      };
      ws.onclose = () => {
        authed = false;
        setConnected(false);
        if (authTimer) window.clearTimeout(authTimer);
        if (!closed) {
          // 再起動直後は API 起動待ちがあるので少し長めにリトライ
          retry = window.setTimeout(connect, WS_RETRY_MS);
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
          const data = JSON.parse(ev.data) as Record<string, unknown>;
          if (!data || typeof data !== "object") return;
          // 認証 ACK
          if (data.type === "auth_ok") {
            authed = true;
            setConnected(true);
            if (authTimer) window.clearTimeout(authTimer);
            return;
          }
          // ログ行以外は無視
          if (typeof data.message !== "string") return;
          const row = data as unknown as Omit<LogEntry, "id">;
          setEntries((prev) => appendFresh(prev, [row], maxLines));
        } catch {
          /* ignore */
        }
      };
    };

    // 先に .log 末尾を復元してからライブ接続
    loadHistory("replace").finally(() => {
      if (closed) return;
      connect();
      // WS 不通時の保険: ファイル履歴を定期マージ（live 中は末尾だけ）
      pollTimer = window.setInterval(() => {
        if (closed) return;
        const liveNow = wsRef.current?.readyState === WebSocket.OPEN;
        void loadHistory("append", liveNow ? 400 : maxLines);
      }, HISTORY_POLL_MS);
    });

    return () => {
      closed = true;
      if (retry) window.clearTimeout(retry);
      if (pollTimer) window.clearInterval(pollTimer);
      if (authTimer) window.clearTimeout(authTimer);
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
