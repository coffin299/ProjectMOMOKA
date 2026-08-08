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

/** 履歴ポーリング間隔（SSE 不通時でも必ず追従） */
const HISTORY_POLL_MS = 1000;
/** SSE 再接続間隔 */
const LIVE_RETRY_MS = 1500;

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

function parseSseChunk(
  chunk: string,
  onData: (payload: Omit<LogEntry, "id">) => void
): string {
  const parts = chunk.split("\n\n");
  const rest = parts.pop() ?? "";
  for (const frame of parts) {
    const dataLines = frame
      .split("\n")
      .filter((ln) => ln.startsWith("data:"))
      .map((ln) => ln.slice(5).trimStart());
    if (!dataLines.length) continue;
    try {
      const data = JSON.parse(dataLines.join("\n")) as Omit<LogEntry, "id">;
      if (data && typeof data.message === "string") onData(data);
    } catch {
      /* ignore */
    }
  }
  return rest;
}

export function useLogStream(maxLines = 10000) {
  const [entries, setEntries] = useState<LogEntry[]>([]);
  const [connected, setConnected] = useState(false);
  const [restored, setRestored] = useState(false);
  const abortRef = useRef<AbortController | null>(null);
  const liveRef = useRef(false);

  useEffect(() => {
    const cfg = getHostConfig();
    if (!cfg.token) return;
    let closed = false;
    let retry: number | undefined;
    let pollTimer: number | undefined;

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

    const connectSse = async () => {
      if (closed) return;
      const live = getHostConfig();
      if (!live.token) return;

      try {
        abortRef.current?.abort();
      } catch {
        /* ignore */
      }
      const ac = new AbortController();
      abortRef.current = ac;

      try {
        // Bearer 付き SSE のみ（WS は localhost onopen 競合で死にやすいので使わない）
        const res = await fetch(`${live.apiBase}/logs/stream`, {
          headers: {
            Authorization: `Bearer ${live.token}`,
            Accept: "text/event-stream",
          },
          signal: ac.signal,
          cache: "no-store",
        });
        if (!res.ok || !res.body) {
          throw new Error(`SSE HTTP ${res.status}`);
        }
        liveRef.current = true;
        setConnected(true);
        const reader = res.body.getReader();
        const decoder = new TextDecoder();
        let buf = "";
        while (!closed) {
          const { done, value } = await reader.read();
          if (done) break;
          buf += decoder.decode(value, { stream: true });
          buf = parseSseChunk(buf, (row) => {
            setEntries((prev) => appendFresh(prev, [row], maxLines));
          });
        }
      } catch {
        if (ac.signal.aborted || closed) return;
      } finally {
        liveRef.current = false;
        setConnected(false);
      }
      if (!closed) {
        retry = window.setTimeout(() => {
          void connectSse();
        }, LIVE_RETRY_MS);
      }
    };

    loadHistory("replace").finally(() => {
      if (closed) return;
      void connectSse();
      // WS に依存せず、ファイル履歴ポーリングで必ず追従する
      pollTimer = window.setInterval(() => {
        if (closed) return;
        void loadHistory("append", liveRef.current ? 500 : maxLines);
      }, HISTORY_POLL_MS);
    });

    return () => {
      closed = true;
      if (retry) window.clearTimeout(retry);
      if (pollTimer) window.clearInterval(pollTimer);
      try {
        abortRef.current?.abort();
      } catch {
        /* ignore */
      }
    };
  }, [maxLines]);

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
