import { writable } from "svelte/store";
import { apiGet, hostHasAuth } from "$lib/api";
import { apiUrl, getToken } from "$lib/session";

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

const HISTORY_POLL_MS = 1000;
const HISTORY_POLL_LIVE_MS = 30000;
const HISTORY_APPEND_LINES = 1000;
const HISTORY_APPEND_LINES_LIVE = 300;
const LIVE_RETRY_MS = 1500;
const LIVE_FLUSH_MS = 100;

let seq = 0;

function mapRows(rows: Omit<LogEntry, "id">[] | undefined): LogEntry[] {
  return (rows || []).map((row) => ({
    ...row,
    id: ++seq,
  }));
}

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
    if ((row as { type?: string }).type === "log_masked") continue;
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

export type LogState = {
  entries: LogEntry[];
  connected: boolean;
  restored: boolean;
};

/** ログ SSE（EventSource）+ 履歴補完 store */
export function createLogStore(maxLines = 10000) {
  const { subscribe, set, update } = writable<LogState>({
    entries: [],
    connected: false,
    restored: false,
  });

  let closed = false;
  let retry: number | undefined;
  let pollTimer: number | undefined;
  let flushTimer: number | undefined;
  let pending: Omit<LogEntry, "id">[] = [];
  let live = false;
  let es: EventSource | null = null;

  const flushPending = () => {
    flushTimer = undefined;
    if (closed || !pending.length) return;
    const batch = pending;
    pending = [];
    update((s) => ({
      ...s,
      entries: appendFresh(s.entries, batch, maxLines),
    }));
  };

  const pushLive = (row: Omit<LogEntry, "id">) => {
    pending.push(row);
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
      update((s) => ({
        ...s,
        entries:
          mode === "replace"
            ? mapRows(items).slice(-maxLines)
            : appendFresh(s.entries, items, maxLines),
        restored: true,
      }));
    } catch (e) {
      console.error("log history fetch failed", e);
      if (!closed) {
        update((s) => ({ ...s, restored: true }));
      }
    }
  };

  const teardownSse = () => {
    if (es) {
      try {
        es.close();
      } catch {
        /* ignore */
      }
      es = null;
    }
    live = false;
    update((s) => ({ ...s, connected: false }));
  };

  const connectSse = () => {
    if (closed || !hostHasAuth()) return;
    teardownSse();
    const url = `${apiUrl("/logs/stream")}?token=${encodeURIComponent(getToken())}`;
    try {
      es = new EventSource(url);
      es.onopen = () => {
        live = true;
        update((s) => ({ ...s, connected: true }));
      };
      es.onmessage = (ev) => {
        try {
          const row = JSON.parse(ev.data) as Omit<LogEntry, "id"> & {
            type?: string;
          };
          if (row && typeof row.message === "string") {
            pushLive(row);
          }
        } catch {
          /* ignore bad frame */
        }
      };
      es.onerror = () => {
        live = false;
        update((s) => ({ ...s, connected: false }));
        teardownSse();
        if (!closed) {
          retry = window.setTimeout(() => connectSse(), LIVE_RETRY_MS);
        }
      };
    } catch (e) {
      console.error("EventSource failed", e);
      if (!closed) {
        retry = window.setTimeout(() => connectSse(), LIVE_RETRY_MS);
      }
    }
  };

  void (async () => {
    if (!hostHasAuth() || closed) {
      update((s) => ({ ...s, restored: true }));
      return;
    }
    await loadHistory("replace");
    if (closed) return;
    connectSse();
    const schedulePoll = () => {
      if (closed) return;
      const isLive = live;
      pollTimer = window.setTimeout(async () => {
        if (closed) return;
        await loadHistory(
          "append",
          Math.min(
            maxLines,
            live ? HISTORY_APPEND_LINES_LIVE : HISTORY_APPEND_LINES
          )
        );
        schedulePoll();
      }, isLive ? HISTORY_POLL_LIVE_MS : HISTORY_POLL_MS);
    };
    schedulePoll();
  })();

  return {
    subscribe,
    clear: () => update((s) => ({ ...s, entries: [] })),
    destroy: () => {
      closed = true;
      if (retry) window.clearTimeout(retry);
      if (pollTimer) window.clearTimeout(pollTimer);
      if (flushTimer) window.clearTimeout(flushTimer);
      pending = [];
      teardownSse();
      set({ entries: [], connected: false, restored: true });
    },
  };
}

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
