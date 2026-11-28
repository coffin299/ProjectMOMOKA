import { readable } from "svelte/store";
import { apiGet } from "$lib/api";

export type StatusPayload = {
  servers: number | null;
  vc: number | null;
  llm: number | null;
  ping_ms: number | null;
  uptime_seconds: number | null;
  alive: Record<string, boolean>;
  version: string;
};

export type VcItem = {
  guild_id: number;
  guild_name: string;
  title: string | null;
  paused: boolean;
  queue_size: number;
  bot_id?: string;
  bot_label?: string;
  bot_display?: string;
};

export type GuildItem = {
  id: string;
  name: string;
  joined_at?: string | null;
};

function dash(v: number | null | undefined, digits = 0): string {
  if (v === null || v === undefined || Number.isNaN(v)) return "-";
  return digits > 0 ? v.toFixed(digits) : String(Math.round(v));
}

export type StatusState = {
  status: StatusPayload | null;
  vc: VcItem[];
  guilds: GuildItem[];
  avgLatency: number | null;
  format: {
    servers: string;
    vc: string;
    llm: string;
    ping: string;
    uptime: string;
    avgLatency: string;
  };
};

const emptyFormat = {
  servers: "-",
  vc: "-",
  llm: "-",
  ping: "-",
  uptime: "-",
  avgLatency: "-",
};

function buildFormat(
  status: StatusPayload | null,
  avgLatency: number | null
): StatusState["format"] {
  return {
    servers: dash(status?.servers ?? null),
    vc: dash(status?.vc ?? null),
    llm: dash(status?.llm ?? null),
    ping: status?.ping_ms != null ? `${dash(status.ping_ms)}ms` : "-",
    uptime:
      status?.uptime_seconds != null
        ? `${Math.floor(status.uptime_seconds / 3600)}h${Math.floor(
            (status.uptime_seconds % 3600) / 60
          )}m`
        : "-",
    avgLatency: avgLatency != null ? `${avgLatency.toFixed(1)}s` : "-",
  };
}

/** ステータスを 1 秒間隔でポーリングする store */
export function createStatusStore(pollMs = 1000) {
  return readable<StatusState>(
    {
      status: null,
      vc: [],
      guilds: [],
      avgLatency: null,
      format: emptyFormat,
    },
    (set) => {
      let cancelled = false;
      let inFlight = false;
      let generation = 0;
      let timer: number | undefined;
      let lastKey = "";

      const commit = (
        status: StatusPayload | null,
        vc: VcItem[],
        guilds: GuildItem[],
        avgLatency: number | null
      ) => {
        const key = JSON.stringify([status, vc, guilds, avgLatency]);
        if (key === lastKey) return;
        lastKey = key;
        set({
          status,
          vc,
          guilds,
          avgLatency,
          format: buildFormat(status, avgLatency),
        });
      };

      const tick = async () => {
        if (cancelled || inFlight) return;
        // バックグラウンドでもポーリングは継続する。
        // Tauri WebView2 は起動直後 document.hidden=true のことがあり、
        // ここで return すると visibilitychange が来ず永続的に空表示になる。
        inFlight = true;
        const myGen = ++generation;
        try {
          // 1 本失敗で全部落ちないよう個別に取る
          const [sRes, vRes, gRes, lRes] = await Promise.allSettled([
            apiGet<StatusPayload>("/status"),
            apiGet<{ items: VcItem[] }>("/vc"),
            apiGet<{ items: GuildItem[] }>("/guilds"),
            apiGet<{ average_seconds: number | null }>("/llm/stats"),
          ]);
          if (cancelled || myGen !== generation) return;
          const s = sRes.status === "fulfilled" ? sRes.value : null;
          const v = vRes.status === "fulfilled" ? vRes.value.items || [] : [];
          const g = gRes.status === "fulfilled" ? gRes.value.items || [] : [];
          const l =
            lRes.status === "fulfilled" ? lRes.value.average_seconds : null;
          commit(s, v, g, l);
        } catch {
          if (cancelled || myGen !== generation) return;
          commit(null, [], [], null);
        } finally {
          inFlight = false;
          if (!cancelled) {
            timer = window.setTimeout(tick, pollMs);
          }
        }
      };

      void tick();

      return () => {
        cancelled = true;
        if (timer !== undefined) window.clearTimeout(timer);
      };
    }
  );
}
