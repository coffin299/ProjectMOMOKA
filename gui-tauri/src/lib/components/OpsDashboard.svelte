<script lang="ts">
  import type { GuildItem, VcItem } from "$lib/stores/status";
  import type { LogEntry } from "$lib/stores/logs";
  import AutoScrollPane from "./AutoScrollPane.svelte";

  let {
    vc,
    guilds,
    avgLatency,
    llmFeed,
    guildEvents,
    uptime,
    alive,
  }: {
    vc: VcItem[];
    guilds: GuildItem[];
    avgLatency: string;
    llmFeed: LogEntry[];
    guildEvents: LogEntry[];
    uptime: string;
    alive: Record<string, boolean> | undefined;
  } = $props();

  const llmRows = $derived(llmFeed.slice(-80));
  const eventRows = $derived(guildEvents.slice(-80));
  const llmDep = $derived(
    llmRows.length ? `${llmRows[llmRows.length - 1].id}:${llmRows.length}` : "0"
  );
  const eventDep = $derived(
    eventRows.length
      ? `${eventRows[eventRows.length - 1].id}:${eventRows.length}`
      : "0"
  );
  const vcDep = $derived(
    `${vc.length}:${vc.map((v) => `${v.bot_id}-${v.guild_id}`).join(",")}`
  );
  const guildDep = $derived(`${guilds.length}:${guilds[0]?.id ?? ""}`);

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
</script>

<div class="ops-grid">
  <section class="card">
    <h3>Active VC</h3>
    <AutoScrollPane class="card-body" deps={vcDep} direction="top">
      {#if vc.length === 0}
        <div class="muted">No active VC</div>
      {:else}
        {#each vc as row (`${row.bot_id}-${row.guild_id}`)}
          <div class="row">
            <div>
              <span class={row.bot_label === "ARONA" ? "tag-arona" : "tag-plana"}>
                [{row.bot_label || row.bot_id?.toUpperCase() || "?"}]
              </span>
              {row.guild_name}
            </div>
            <div class="muted">
              nowplaying {row.title || "-"}{row.paused ? " (paused)" : ""} · queue
              {row.queue_size}
            </div>
          </div>
        {/each}
      {/if}
    </AutoScrollPane>
  </section>

  <section class="card">
    <h3>Active LLM · avg {avgLatency}</h3>
    <AutoScrollPane class="card-body" deps={llmDep} direction="bottom">
      {#if llmRows.length === 0}
        <div class="muted">No LLM I/O yet this session</div>
      {:else}
        {#each llmRows as e (e.id)}
          <div class="row">{truncate(e.message, 180)}</div>
        {/each}
      {/if}
    </AutoScrollPane>
  </section>

  <section class="card">
    <h3>Joined servers</h3>
    <AutoScrollPane class="card-body" deps={guildDep} direction="top">
      {#if guilds.length === 0}
        <div class="muted">No guilds yet</div>
      {:else}
        {#each guilds as g (g.id)}
          <div class="row">
            <div>{g.name}</div>
            <div class="muted">{g.id} · {formatJoinedAt(g.joined_at)}</div>
          </div>
        {/each}
      {/if}
    </AutoScrollPane>
  </section>

  <section class="card">
    <h3>Join / Leave</h3>
    <AutoScrollPane class="card-body" deps={eventDep} direction="bottom">
      <div class="muted" style="margin-bottom: 8px">
        uptime {uptime} · PLANA
        {alive?.plana ? "up" : alive?.plana === false ? "down" : "-"} · ARONA
        {alive?.arona ? "up" : alive?.arona === false ? "down" : "-"}
      </div>
      {#if eventRows.length === 0}
        <div class="muted">No join/leave events this session</div>
      {:else}
        {#each eventRows as e (e.id)}
          <div class="row">{truncate(e.message, 200)}</div>
        {/each}
      {/if}
    </AutoScrollPane>
  </section>
</div>
