import type { GuildItem, VcItem } from "../hooks/useStatus";
import type { LogEntry } from "../hooks/useLogStream";
import { AutoScrollPane } from "./AutoScrollPane";

type Props = {
  vc: VcItem[];
  guilds: GuildItem[];
  avgLatency: string;
  llmFeed: LogEntry[];
  guildEvents: LogEntry[];
  uptime: string;
  alive: Record<string, boolean> | undefined;
};

export function OpsDashboard({
  vc,
  guilds,
  avgLatency,
  llmFeed,
  guildEvents,
  uptime,
  alive,
}: Props) {
  // 末尾80件（新しいものが下）
  const llmRows = llmFeed.slice(-80);
  const eventRows = guildEvents.slice(-80);
  // 末尾IDで更新検知
  const llmDep = llmRows.length
    ? `${llmRows[llmRows.length - 1].id}:${llmRows.length}`
    : "0";
  const eventDep = eventRows.length
    ? `${eventRows[eventRows.length - 1].id}:${eventRows.length}`
    : "0";
  const vcDep = `${vc.length}:${vc.map((v) => `${v.bot_id}-${v.guild_id}`).join(",")}`;
  const guildDep = `${guilds.length}:${guilds[0]?.id ?? ""}`;

  return (
    <div className="ops-grid">
      <section className="card">
        <h3>Active VC</h3>
        <AutoScrollPane className="card-body" deps={vcDep} direction="top">
          {vc.length === 0 ? (
            <div className="muted">No active VC</div>
          ) : (
            vc.map((row) => (
              <div className="row" key={`${row.bot_id}-${row.guild_id}`}>
                <div>
                  <span
                    className={
                      row.bot_label === "ARONA" ? "tag-arona" : "tag-plana"
                    }
                  >
                    [{row.bot_label || row.bot_id?.toUpperCase() || "?"}]
                  </span>{" "}
                  {row.guild_name}
                </div>
                <div className="muted">
                  nowplaying {row.title || "-"}
                  {row.paused ? " (paused)" : ""}
                  {` · queue ${row.queue_size}`}
                </div>
              </div>
            ))
          )}
        </AutoScrollPane>
      </section>

      <section className="card">
        <h3>Active LLM · avg {avgLatency}</h3>
        <AutoScrollPane className="card-body" deps={llmDep} direction="bottom">
          {llmRows.length === 0 ? (
            <div className="muted">No LLM I/O yet this session</div>
          ) : (
            llmRows.map((e) => (
              <div className="row" key={e.id}>
                {truncate(e.message, 180)}
              </div>
            ))
          )}
        </AutoScrollPane>
      </section>

      <section className="card">
        <h3>Joined servers</h3>
        <AutoScrollPane className="card-body" deps={guildDep} direction="top">
          {guilds.length === 0 ? (
            <div className="muted">No guilds yet</div>
          ) : (
            guilds.map((g) => (
              <div className="row" key={g.id}>
                <div>{g.name}</div>
                <div className="muted">
                  {g.id}
                  {" · "}
                  {formatJoinedAt(g.joined_at)}
                </div>
              </div>
            ))
          )}
        </AutoScrollPane>
      </section>

      <section className="card">
        <h3>Join / Leave</h3>
        <AutoScrollPane
          className="card-body"
          deps={eventDep}
          direction="bottom"
        >
          <div className="muted" style={{ marginBottom: 8 }}>
            uptime {uptime} · PLANA{" "}
            {alive?.plana ? "up" : alive?.plana === false ? "down" : "-"} · ARONA{" "}
            {alive?.arona ? "up" : alive?.arona === false ? "down" : "-"}
          </div>
          {eventRows.length === 0 ? (
            <div className="muted">No join/leave events this session</div>
          ) : (
            eventRows.map((e) => (
              <div className="row" key={e.id}>
                {truncate(e.message, 200)}
              </div>
            ))
          )}
        </AutoScrollPane>
      </section>
    </div>
  );
}

function truncate(s: string, n: number): string {
  if (s.length <= n) return s;
  return `${s.slice(0, n)}…`;
}

function formatJoinedAt(iso: string | null | undefined): string {
  if (!iso) return "joined: unknown";
  try {
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return `joined: ${iso}`;
    const y = d.getFullYear();
    const m = String(d.getMonth() + 1).padStart(2, "0");
    const day = String(d.getDate()).padStart(2, "0");
    const hh = String(d.getHours()).padStart(2, "0");
    const mm = String(d.getMinutes()).padStart(2, "0");
    return `joined: ${y}-${m}-${day} ${hh}:${mm}`;
  } catch {
    return `joined: ${iso}`;
  }
}
