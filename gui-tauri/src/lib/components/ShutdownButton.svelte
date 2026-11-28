<script lang="ts">
  import { apiPost } from "$lib/api";

  let armed = $state(false);
  let busy = $state(false);

  async function onClick() {
    if (!armed) {
      armed = true;
      window.setTimeout(() => {
        armed = false;
      }, 4000);
      return;
    }
    busy = true;
    try {
      await apiPost("/shutdown");
    } catch {
      busy = false;
      armed = false;
    }
  }
</script>

<button
  type="button"
  class="btn-danger"
  disabled={busy}
  onclick={onClick}
  title="Click twice to confirm. Window closes after shutdown logs finish."
>
  {busy ? "Shutting down…" : armed ? "Confirm Shutdown" : "Shutdown"}
</button>
