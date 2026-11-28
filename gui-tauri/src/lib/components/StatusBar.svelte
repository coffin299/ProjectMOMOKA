<script lang="ts">
  let {
    servers,
    vc,
    llm,
    ping,
    uptime,
    alive,
    linkLabel = "API",
    apiLabel = "ok",
  }: {
    servers: string;
    vc: string;
    llm: string;
    ping: string;
    uptime: string;
    alive: Record<string, boolean> | undefined;
    linkLabel?: string;
    apiLabel?: string;
  } = $props();

  const plana = $derived(alive?.plana);
  const arona = $derived(alive?.arona);
  const apiBad = $derived(apiLabel !== "ok");
</script>

<div class="brand">MOMOKA</div>
<div class="pills">
  <span class="pill">link<strong>{linkLabel}</strong></span>
  <span class="pill" class:bad={apiBad}>api<strong>{apiLabel}</strong></span>
  <span class="pill">Servers<strong>{servers}</strong></span>
  <span class="pill">VC<strong>{vc}</strong></span>
  <span class="pill">LLM<strong>{llm}</strong></span>
  <span class="pill">ping<strong>{ping}</strong></span>
  <span class="pill">uptime<strong>{uptime}</strong></span>
  <span class="pill">
    PLANA<strong>{plana === undefined ? "-" : plana ? "up" : "down"}</strong>
  </span>
  <span class="pill">
    ARONA<strong>{arona === undefined ? "-" : arona ? "up" : "down"}</strong>
  </span>
</div>

<style>
  .pill.bad strong {
    color: #f23f43;
  }
</style>
