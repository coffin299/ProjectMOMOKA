<script lang="ts">
  import type { NavId } from "$lib/nav";

  const ITEMS: { id: NavId; label: string }[] = [
    { id: "overview", label: "Overview" },
    { id: "web", label: "Web" },
    { id: "general", label: "一般" },
    { id: "llm", label: "LLM" },
    { id: "tts", label: "TTS+Music" },
    { id: "error", label: "エラー" },
    { id: "privacy", label: "ログ管理" },
  ];

  let {
    active,
    onSelect,
  }: {
    active: NavId;
    onSelect: (id: NavId) => void;
  } = $props();
</script>

<nav class="sidebar">
  {#each ITEMS as item (item.id)}
    <button
      type="button"
      class="nav-btn"
      class:active={active === item.id}
      onclick={() => onSelect(item.id)}
    >
      {item.label}
    </button>
  {/each}
</nav>
