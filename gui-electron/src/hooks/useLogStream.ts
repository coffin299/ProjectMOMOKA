import { useCallback, useEffect, useRef, useState } from "react";
import { apiGet, getHostConfig, hostHasAuth } from "../api";

export type LogEntry = {
  name: string;
  level: string;
  message: string;
  category: string;
  id: number;
  event_id?: number;
  content_hash?: string;
  masked?: boolean;
};

const LEVEL_RANK: Record<string, number> = {
  DEBUG: 10,
  INFO: 20,
  WARNING: 30,
  ERROR: 40,
  CRITICAL: 50,
};

/** 履歴ポーリング間隔（SSE 不通時） */
const HISTORY_POLL_MS = 1000;
/** 履歴ポーリング間隔（SSE 接続中は取りこぼし補完のみ） */
const HISTORY_POLL_LIVE_MS = 10000;
/** 追従ポーリング 1 回で取る行数（SSE 不通時） */
const HISTORY_APPEND_LINES = 1000;
/** 追従ポーリング 1 回で取る行数（SSE 接続中） */
const HISTORY_APPEND_LINES_LIVE = 300;
/** SSE 再接続間隔 */
const LIVE_RETRY_MS = 1500;
/** ライブログをまとめて state へ反映する間隔 */
const LIVE_FLUSH_MS = 100;

let seq = 0;

function mapRows(
  rows: Omit<LogEntry, "id">[] | undefined
): LogEntry[] {
  return (rows || []).map((row) => ({
    ...row,
    id: ++seq,
  }));
}

/** 末尾へ未所持イベントだけ足す（event_id / content_hash 優先、無ければ文言） */
function appendFresh(
  prev: LogEntry[],
  incoming: Omit<LogEntry, "id">[],
  maxLines: number
): LogEntry[] {
  if (!incoming.length) return prev;
  const seenEventIds = new Set(
    prev.map((e) => e.event_id).filter((v): v is number => typeof v === "number")
  );
  const seenHashes = new Set(
    prev
      .map((e) => e.content_hash)
      .filter((v): v is string => typeof v === "string" && v.length > 0)
  );
  const seenMessages = new Set(prev.map((e) => e.message));
  const fresh: LogEntry[] = [];
  for (const row of incoming) {
    if (!row || typeof row.message !== "string") continue;
    // マスク通知は一覧へ載せない
    if ((row as { type?: string }).type === "log_masked") {
      continue;
    }
    if (typeof row.event_id === "number") {
      if (seenEventIds.has(row.event_id)) continue;
      seenEventIds.add(row.event_id);
    } else if (typeof row.content_hash === "string" && row.content_hash) {
      if (seenHashes.has(row.content_hash)) continue;
      seenHashes.add(row.content_hash);
    } else if (seenMessages.has(row.message)) {
      continue;
    }
    seenMessages.add(row.message);
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
  const liveRef = useRef(false);
  const unsubRef = useRef<(() => void) | null>(null);

  useEffect(() => {
    let closed = false;
    let retry: number | undefined;
    let pollTimer: number | undefined;
    let flushTimer: number | undefined;
    // 1 行ごとに 10000 件走査しないよう、ライブ行は溜めてまとめて反映する
    let pending: Omit<LogEntry, "id">[] = [];

    const flushPending = () => {
      flushTimer = undefined;
      if (closed || !pending.length) return;
      const batch = pending;
      pending = [];
      setEntries((prev) => appendFresh(prev, batch, maxLines));
    };

    const pushLive = (row: Omit<LogEntry, "id">) => {
      pending.push(row);
      // 未反映が上限を超えたら古いものを捨てる（state 側も maxLines で切る）
      if (pending.length > maxLines) {
        pending = pending.slice(pending.length - maxLines);
      }
      if (flushTimer === undefined) {
        flushTimer = window.setTimeout(flushPending, LIVE_FLUSH_MS);
      }
    };

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

    const teardownSse = () => {
      if (unsubRef.current) {
        try {
          unsubRef.current();
        } catch {
          /* ignore */
        }
        unsubRef.current = null;
      }
      const cfg = getHostConfig();
      if (cfg.stopLogSse) {
        void cfg.stopLogSse().catch(() => undefined);
      }
      liveRef.current = false;
      setConnected(false);
    };

    const connectSse = async () => {
      if (closed) return;
      const ok = await hostHasAuth();
      if (!ok || closed) return;

      const live = getHostConfig();
      teardownSse();

      // Electron: main が SSE を購読し IPC で転送（token 非公開）
      if (live.startLogSse && live.onLogSse) {
        try {
          const offData = live.onLogSse((raw) => {
            const row = raw as Omit<LogEntry, "id"> & { type?: string };
            if (row && typeof row.message === "string") {
              pushLive(row);
            }
          });
          const offEnd = live.onLogSseEnd
            ? live.onLogSseEnd(() => {
                liveRef.current = false;
                setConnected(false);
                if (!closed) {
                  retry = window.setTimeout(() => {
                    void connectSse();
                  }, LIVE_RETRY_MS);
                }
              })
            : () => undefined;
          unsubRef.current = () => {
            offData();
            offEnd();
          };
          await live.startLogSse();
          if (closed) {
            teardownSse();
            return;
          }
          liveRef.current = true;
          setConnected(true);
        } catch {
          liveRef.current = false;
          setConnected(false);
          if (!closed) {
            retry = window.setTimeout(() => {
              void connectSse();
            }, LIVE_RETRY_MS);
          }
        }
        return;
      }

      // preload 無しフォールバック（通常は未使用）
      if (!live.token) return;
      try {
        const ac = new AbortController();
        unsubRef.current = () => ac.abort();
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
          const parts = buf.split("\n\n");
          buf = parts.pop() ?? "";
          for (const frame of parts) {
            const dataLines = frame
              .split("\n")
              .filter((ln) => ln.startsWith("data:"))
              .map((ln) => ln.slice(5).trimStart());
            if (!dataLines.length) continue;
            try {
              const data = JSON.parse(dataLines.join("\n")) as Omit<
                LogEntry,
                "id"
              >;
              if (data && typeof data.message === "string") {
                pushLive(data);
              }
            } catch {
              /* ignore */
            }
          }
        }
      } catch {
        /* reconnect below */
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

    void (async () => {
      const ok = await hostHasAuth();
      if (!ok || closed) {
        setRestored(true);
        return;
      }
      await loadHistory("replace");
      if (closed) return;
      void connectSse();
      // 前回の応答を待ってから次を予約する（Bot ハング時に要求が積み上がらない）
      const schedulePoll = () => {
        if (closed) return;
        const live = liveRef.current;
        pollTimer = window.setTimeout(
          async () => {
            if (closed) return;
            await loadHistory(
              "append",
              Math.min(
                maxLines,
                liveRef.current
                  ? HISTORY_APPEND_LINES_LIVE
                  : HISTORY_APPEND_LINES
              )
            );
            schedulePoll();
          },
          live ? HISTORY_POLL_LIVE_MS : HISTORY_POLL_MS
        );
      };
      schedulePoll();
    })();

    return () => {
      closed = true;
      if (retry) window.clearTimeout(retry);
      if (pollTimer) window.clearTimeout(pollTimer);
      if (flushTimer) window.clearTimeout(flushTimer);
      pending = [];
      teardownSse();
    };
  }, [maxLines]);

  const clear = useCallback(() => setEntries([]), []);

  return { entries, connected, restored, clear };
}

/** カテゴリ / 最低レベルでログを絞り込む（呼び出し側で useMemo する前提） */
export function filterLogEntries(
  entries: LogEntry[],
  category: string,
  minLevel: string
): LogEntry[] {
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
}

/** 末尾から走査して条件一致を最大 limit 件返す（古い→新しい順） */
export function takeLastMatching(
  entries: LogEntry[],
  limit: number,
  predicate: (e: LogEntry) => boolean
): LogEntry[] {
  const out: LogEntry[] = [];
  for (let i = entries.length - 1; i >= 0 && out.length < limit; i -= 1) {
    if (predicate(entries[i])) out.push(entries[i]);
  }
  return out.reverse();
}
