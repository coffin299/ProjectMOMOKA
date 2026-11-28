<script lang="ts">
  import type { LogEntry } from "$lib/stores/logs";

  let {
    title,
    entries,
    level,
    onLevelChange,
    autoScroll,
    onAutoScrollChange,
    onClear,
    live = false,
  }: {
    title: string;
    entries: LogEntry[];
    level: string;
    onLevelChange: (level: string) => void;
    autoScroll: boolean;
    onAutoScrollChange: (v: boolean) => void;
    onClear: () => void;
    live?: boolean;
  } = $props();

  const LEVELS = ["DEBUG", "INFO", "WARNING", "ERROR"];
  const RENDER_LIMIT = 1500;

  let panelEl: HTMLDivElement | undefined = $state();
  let stick = $state(true);

  const hiddenCount = $derived(Math.max(0, entries.length - RENDER_LIMIT));
  const visible = $derived(hiddenCount > 0 ? entries.slice(hiddenCount) : entries);

  $effect(() => {
    void entries;
    if (!autoScroll || !stick || !panelEl) return;
    const scroll = () => {
      if (panelEl) panelEl.scrollTop = panelEl.scrollHeight;
    };
    scroll();
    const id = window.requestAnimationFrame(scroll);
    return () => window.cancelAnimationFrame(id);
  });

  function onScroll() {
    if (!panelEl) return;
    stick = panelEl.scrollHeight - panelEl.scrollTop - panelEl.clientHeight < 40;
  }

  function lineClass(e: LogEntry): string {
    if (e.message.includes("[USER_INPUT]")) return "user-input";
    if (e.message.includes("[LLM_RESPONSE]")) return "llm-response";
    if (e.message.includes("[GUILD_EVENT]")) return "guild-event";
    const lv = e.level.toLowerCase();
    if (lv === "critical") return "level-error";
    return `level-${lv}`;
  }

  function splitTags(message: string): { text: string; tag?: "PLANA" | "ARONA" }[] {
    const parts = message.split(/(\[PLANA\]|\[ARONA\])/g);
    return parts.filter(Boolean).map((p) => {
      if (p === "[PLANA]") return { text: p, tag: "PLANA" as const };
      if (p === "[ARONA]") return { text: p, tag: "ARONA" as const };
      return { text: p };
    });
  }
</script>

<div class="main" style="display: flex; flex-direction: column">
  <div class="log-toolbar">
    <strong>{title}</strong>
    <span
      title={live
        ? "SSE ライブ接続中"
        : "履歴ポーリング中（WS 未接続でも更新されます）"}
      style="opacity: 0.75; font-size: 0.85em"
    >
      {live ? "● live" : "○ poll"}
    </span>
    <label>
      Level
      <select value={level} onchange={(e) => onLevelChange(e.currentTarget.value)}>
        {#each LEVELS as l}
          <option value={l}>{l}</option>
        {/each}
      </select>
    </label>
    <label>
      <input
        type="checkbox"
        checked={autoScroll}
        onchange={(e) => onAutoScrollChange(e.currentTarget.checked)}
      />
      Auto-scroll
    </label>
    <button type="button" class="secondary" onclick={onClear}>Clear</button>
  </div>
  <div class="log-panel" bind:this={panelEl} onscroll={onScroll}>
    {#if hiddenCount > 0}
      <p class="log-line level-debug">
        … 古い {hiddenCount} 行は省略中（全文は data/momoka_gui.log）
      </p>
    {/if}
    {#each visible as e (e.id)}
      <p class={`log-line ${lineClass(e)}`}>
        {#each splitTags(e.message) as part, i (i)}
          {#if part.tag === "PLANA"}
            <span class="tag-plana">{part.text}</span>
          {:else if part.tag === "ARONA"}
            <span class="tag-arona">{part.text}</span>
          {:else}
            <span>{part.text}</span>
          {/if}
        {/each}
      </p>
    {/each}
  </div>
</div>
