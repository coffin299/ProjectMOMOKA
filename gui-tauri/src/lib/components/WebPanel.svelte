<script lang="ts">
  import { onMount } from "svelte";
  import {
    browserGetUrl,
    browserGoBack,
    browserGoForward,
    browserNavigate,
    browserReload,
    browserSetVisible,
    onBrowserUrl,
    onYoutubeCookieStatus,
    type BrowserBounds,
  } from "$lib/host";

  let { visible }: { visible: boolean } = $props();

  let hostEl: HTMLDivElement | undefined = $state();
  let url = $state("https://www.youtube.com/");
  let status = $state("YouTube かニコニコにログインすると cookie を保存します");

  function measureBounds(): BrowserBounds | null {
    if (!hostEl) return null;
    const rect = hostEl.getBoundingClientRect();
    // 論理ピクセルで Rust 側へ渡す
    return {
      x: rect.left,
      y: rect.top,
      width: Math.max(1, rect.width),
      height: Math.max(1, rect.height),
    };
  }

  async function syncVisibility() {
    const bounds = measureBounds();
    if (visible) {
      if (!bounds) return;
      await browserSetVisible(true, bounds);
      try {
        url = await browserGetUrl();
      } catch {
        /* 未作成直後 */
      }
    } else {
      await browserSetVisible(false, bounds ?? undefined);
    }
  }

  $effect(() => {
    void visible;
    void syncVisibility();
  });

  onMount(() => {
    let offs: Array<() => void> = [];
    void (async () => {
      offs.push(
        await onYoutubeCookieStatus((data) => {
          if (data?.message) status = data.message;
        })
      );
      offs.push(
        await onBrowserUrl((next) => {
          url = next;
        })
      );
    })();

    const onResize = () => {
      if (visible) void syncVisibility();
    };
    window.addEventListener("resize", onResize);

    return () => {
      window.removeEventListener("resize", onResize);
      for (const off of offs) off();
      void browserSetVisible(false);
    };
  });
</script>

<section class={`web-panel${visible ? "" : " hidden"}`} aria-hidden={!visible}>
  <div class="log-toolbar">
    <button type="button" class="tool-btn" onclick={() => void browserGoBack()}>
      戻る
    </button>
    <button type="button" class="tool-btn" onclick={() => void browserGoForward()}>
      進む
    </button>
    <button type="button" class="tool-btn" onclick={() => void browserReload()}>
      再読込
    </button>
    <button
      type="button"
      class="tool-btn"
      onclick={() => void browserNavigate("youtube")}
    >
      YouTube
    </button>
    <button
      type="button"
      class="tool-btn"
      onclick={() => void browserNavigate("niconico")}
    >
      ニコニコ
    </button>
    <span class="muted web-url">{url}</span>
    <span class="muted web-status">{status}</span>
  </div>
  <!-- 実ブラウザ面は Rust 子 WebView。ここは位置合わせ用ホスト -->
  <div class="web-view-host" bind:this={hostEl}></div>
</section>
