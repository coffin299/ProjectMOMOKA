import type { GuildItem, VcItem } from "../hooks/useStatus";
import type { LogEntry } from "../hooks/useLogStream";

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
  return (
    <div className="ops-grid">
      <section className="card">
        <h3>Active VC</h3>
        <div className="card-body">
          {vc.length === 0 ? (
            <div className="muted">No active VC</div>
          ) : (
            vc.map((row) => (
              <div className="row" key={`${row.bot_id}-${row.guild_id}`}>
                <div>{row.guild_name}</div>
                <div className="muted">
                  nowplaying {row.title || "-"}
                  {row.paused ? " (paused)" : ""}
                  {` · queue ${row.queue_size}`}
                </div>
              </div>
            ))
          )}
        </div>
      </section>

      <section className="card">
        <h3>Active LLM · avg {avgLatency}</h3>
        <div className="card-body">
          {llmFeed.length === 0 ? (
            <div className="muted">No LLM I/O yet this session</div>
          ) : (
            llmFeed
              .slice(-80)
              .map((e) => (
                <div className="row" key={e.id}>
                  {truncate(e.message, 180)}
                </div>
              ))
          )}
        </div>
      </section>

      <section className="card">
        <h3>Joined servers</h3>
        <div className="card-body">
          {guilds.length === 0 ? (
            <div className="muted">No guilds yet</div>
          ) : (
            guilds.map((g) => (
              <div className="row" key={g.id}>
                <div>{g.name}</div>
                <div className="muted">{g.id}</div>
              </div>
            ))
          )}
        </div>
      </section>

      <section className="card">
        <h3>Join / Leave</h3>
        <div className="card-body">
          <div className="muted" style={{ marginBottom: 8 }}>
            uptime {uptime} · PLANA{" "}
            {alive?.plana ? "up" : alive?.plana === false ? "down" : "-"} · ARONA{" "}
            {alive?.arona ? "up" : alive?.arona === false ? "down" : "-"}
          </div>
          {guildEvents.length === 0 ? (
            <div className="muted">No join/leave events this session</div>
          ) : (
            guildEvents
              .slice(-80)
              .map((e) => (
                <div className="row" key={e.id}>
                  {truncate(e.message, 200)}
                </div>
              ))
          )}
        </div>
      </section>
    </div>
  );
}

function truncate(s: string, n: number): string {
  if (s.length <= n) return s;
  return `${s.slice(0, n)}…`;
}
