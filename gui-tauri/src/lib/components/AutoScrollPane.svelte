<script lang="ts">
  import type { Snippet } from "svelte";

  let {
    class: className = "",
    deps,
    direction = "bottom",
    children,
  }: {
    class?: string;
    deps: unknown;
    direction?: "bottom" | "top";
    children: Snippet;
  } = $props();

  let el: HTMLDivElement | undefined = $state();
  let stick = $state(true);

  $effect(() => {
    // deps 変化で自動スクロール
    void deps;
    if (!stick || !el) return;
    const scroll = () => {
      if (!el) return;
      if (direction === "top") el.scrollTop = 0;
      else el.scrollTop = el.scrollHeight;
    };
    scroll();
    const id = window.requestAnimationFrame(scroll);
    return () => window.cancelAnimationFrame(id);
  });

  function onScroll() {
    if (!el) return;
    if (direction === "top") {
      stick = el.scrollTop < 40;
    } else {
      stick = el.scrollHeight - el.scrollTop - el.clientHeight < 48;
    }
  }
</script>

<div class={className} bind:this={el} onscroll={onScroll}>
  {@render children()}
</div>
