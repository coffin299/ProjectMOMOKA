<script lang="ts">
  import { onDestroy, onMount } from "svelte";
  import Sidebar from "$lib/components/Sidebar.svelte";
  import type { NavId } from "$lib/nav";
  import StatusBar from "$lib/components/StatusBar.svelte";
  import ShutdownButton from "$lib/components/ShutdownButton.svelte";
  import OpsDashboard from "$lib/components/OpsDashboard.svelte";
  import LogPanel from "$lib/components/LogPanel.svelte";
  import UserDataPanel from "$lib/components/UserDataPanel.svelte";
  import WebPanel from "$lib/components/WebPanel.svelte";
  import {
    buildFormat,
    emptyFormat,
    type GuildItem,
    type SnapshotPayload,
    type StatusFormat,
    type StatusPayload,
    type VcItem,
  } from "$lib/stores/status";
  import {
    createLogStore,
    filterLogEntries,
    takeLastMatching,
    type LogEntry,
  } from "$lib/stores/logs";
  import { getConnInfo, getSnapshot } from "$lib/host";
  import "$lib/styles/discord-theme.css";

  type LogNavId = Exclude<NavId, "overview" | "privacy" | "web">;

  const FEED_ROWS = 80;
  const EMPTY_ENTRIES: LogEntry[] = [];
  const POLL_MS = 1000;

  const isLlmIo = (e: LogEntry) =>
    e.message.includes("[USER_INPUT]") || e.message.includes("[LLM_RESPONSE]");
  const isGuildEvent = (e: LogEntry) =>
    e.message.includes("[GUILD_EVENT]") || /Joined guild|Left guild/i.test(e.message);

  const LOG_TITLES: Record<LogNavId, string> = {
    general: "一般ログ",
    llm: "LLMログ",
    tts: "TTS+Musicログ",
    error: "エラーログ",
  };

  let nav = $state<NavId>("overview");
  let levels = $state({
    general: "INFO",
    llm: "INFO",
    tts: "INFO",
    error: "WARNING",
  });
  let autoScroll = $state(true);
  let linkLabel = $state("checking…");
  let apiLabel = $state("…");
  let status = $state<StatusPayload | null>(null);
  let vc = $state<VcItem[]>([]);
  let guilds = $state<GuildItem[]>([]);
  let avgLatency = $state<number | null>(null);
  let format = $state<StatusFormat>(emptyFormat);

  const logStore = createLogStore(5000);
  let pollTimer: number | undefined;
  let cancelled = false;

  function briefError(err: unknown): string {
    const raw = err instanceof Error ? err.message : String(err ?? "error");
    const text = raw.replace(/^[^:]+:\s*/, "").trim() || raw;
    return text.length > 48 ? `${text.slice(0, 48)}…` : text;
  }

  async function tickSnapshot() {
    try {
      const snap = await getSnapshot<SnapshotPayload>();
      status = snap.status ?? null;
      vc = snap.vc?.items ?? [];
      guilds = snap.guilds?.items ?? [];
      avgLatency = snap.llm?.average_seconds ?? null;
      format = buildFormat(status, avgLatency);
      apiLabel = "ok";
    } catch (e) {
      apiLabel = briefError(e);
      console.error("momoka_snapshot failed", e);
    }
  }

  onMount(() => {
    void (async () => {
      try {
        const info = await getConnInfo();
        linkLabel = info.hasAuth ? `ok :${info.port}` : `no-token :${info.port}`;
      } catch (e) {
        linkLabel = "ipc-fail";
        apiLabel = "ipc-fail";
        console.error("momoka_conn_info failed", e);
      }
    })();

    const loop = async () => {
      if (cancelled) return;
      await tickSnapshot();
      if (!cancelled) {
        pollTimer = window.setTimeout(loop, POLL_MS);
      }
    };
    void loop();
  });

  onDestroy(() => {
    cancelled = true;
    if (pollTimer !== undefined) window.clearTimeout(pollTimer);
    logStore.destroy();
  });

  const logSnap = $derived($logStore);

  const isOverview = $derived(nav === "overview");
  const llmFeed = $derived(
    isOverview ? takeLastMatching(logSnap.entries, FEED_ROWS, isLlmIo) : EMPTY_ENTRIES
  );
  const guildEvents = $derived(
    isOverview
      ? takeLastMatching(logSnap.entries, FEED_ROWS, isGuildEvent)
      : EMPTY_ENTRIES
  );

  const isLogNav = $derived(
    nav === "general" || nav === "llm" || nav === "tts" || nav === "error"
  );
  const logCategory = $derived(isLogNav ? (nav as LogNavId) : null);
  const logLevel = $derived(logCategory ? levels[logCategory] : "INFO");
  const logEntries = $derived(
    logCategory
      ? filterLogEntries(logSnap.entries, logCategory, logLevel)
      : EMPTY_ENTRIES
  );
</script>

<div class="app">
  <header class="topbar">
    <StatusBar
      servers={format.servers}
      vc={format.vc}
      llm={format.llm}
      ping={format.ping}
      uptime={format.uptime}
      alive={status?.alive}
      {linkLabel}
      {apiLabel}
    />
    <ShutdownButton />
  </header>
  <div class="body">
    <Sidebar active={nav} onSelect={(id) => (nav = id)} />
    <div class="content-slot">
      {#if nav === "overview"}
        <OpsDashboard
          {vc}
          {guilds}
          avgLatency={format.avgLatency}
          {llmFeed}
          {guildEvents}
          uptime={format.uptime}
          alive={status?.alive}
        />
      {:else if nav === "privacy"}
        <UserDataPanel />
      {:else if nav !== "web"}
        <LogPanel
          title={LOG_TITLES[nav as LogNavId]}
          entries={logEntries}
          level={levels[nav as LogNavId]}
          onLevelChange={(lv) => {
            levels = { ...levels, [nav as LogNavId]: lv };
          }}
          {autoScroll}
          onAutoScrollChange={(v) => (autoScroll = v)}
          onClear={() => logStore.clear()}
          live={logSnap.connected}
        />
      {/if}
      <WebPanel visible={nav === "web"} />
    </div>
  </div>
</div>
