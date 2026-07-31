import { useEffect, useState } from "react";
import { apiGet } from "../api";

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

export function useStatus(pollMs = 1000) {
  const [status, setStatus] = useState<StatusPayload | null>(null);
  const [vc, setVc] = useState<VcItem[]>([]);
  const [guilds, setGuilds] = useState<GuildItem[]>([]);
  const [avgLatency, setAvgLatency] = useState<number | null>(null);

  useEffect(() => {
    let cancelled = false;
    const tick = async () => {
      try {
        const [s, v, g, l] = await Promise.all([
          apiGet<StatusPayload>("/status"),
          apiGet<{ items: VcItem[] }>("/vc"),
          apiGet<{ items: GuildItem[] }>("/guilds"),
          apiGet<{ average_seconds: number | null }>("/llm/stats"),
        ]);
        if (cancelled) return;
        setStatus(s);
        setVc(v.items || []);
        setGuilds(g.items || []);
        setAvgLatency(l.average_seconds);
      } catch {
        // API 未起動時は無視
      }
    };
    tick();
    const id = window.setInterval(tick, pollMs);
    return () => {
      cancelled = true;
      window.clearInterval(id);
    };
  }, [pollMs]);

  return {
    status,
    vc,
    guilds,
    avgLatency,
    format: {
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
      avgLatency:
        avgLatency != null ? `${avgLatency.toFixed(1)}s` : "-",
    },
  };
}
