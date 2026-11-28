/** Host GUI スナップショット型と表示フォーマット。 */

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

export type SnapshotPayload = {
  status: StatusPayload;
  vc: { items: VcItem[] };
  guilds: { items: GuildItem[] };
  llm: { average_seconds: number | null };
};

export type StatusFormat = {
  servers: string;
  vc: string;
  llm: string;
  ping: string;
  uptime: string;
  avgLatency: string;
};

function dash(v: number | null | undefined, digits = 0): string {
  if (v === null || v === undefined || Number.isNaN(v)) return "-";
  return digits > 0 ? v.toFixed(digits) : String(Math.round(v));
}

export const emptyFormat: StatusFormat = {
  servers: "-",
  vc: "-",
  llm: "-",
  ping: "-",
  uptime: "-",
  avgLatency: "-",
};

/** status + 平均応答から StatusBar / Overview 用文字列を作る。 */
export function buildFormat(
  status: StatusPayload | null,
  avgLatency: number | null
): StatusFormat {
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
