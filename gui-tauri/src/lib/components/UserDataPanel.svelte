<script lang="ts">
  import { apiGet, apiPostJson } from "$lib/api";

  type LogHit = {
    line_no: number;
    content_hash: string;
    name?: string;
    level?: string;
    message: string;
    category?: string;
  };
  type AutoJoinHit = { guild_id: string; user_id: number };
  type VcSessionHit = {
    bot_id: string;
    guild_id: number;
    current_match: boolean;
    current_title: string | null;
    queue_match_count: number;
    queue_titles: string[];
  };
  type SearchResult = {
    user_id: number;
    logs: LogHit[];
    db: { auto_join: AutoJoinHit[]; vc_sessions: VcSessionHit[] };
  };

  function logKey(row: LogHit): string {
    return `${row.line_no}:${row.content_hash}`;
  }
  function autoJoinKey(row: AutoJoinHit): string {
    return `${row.guild_id}:${row.user_id}`;
  }
  function vcKey(row: VcSessionHit): string {
    return `${row.bot_id}:${row.guild_id}`;
  }

  let userId = $state("");
  let loading = $state(false);
  let error = $state<string | null>(null);
  let result = $state<SearchResult | null>(null);
  let selectedLogs = $state(new Set<string>());
  let selectedAutoJoin = $state(new Set<string>());
  let selectedVc = $state(new Set<string>());
  let busy = $state(false);

  const logHits = $derived(result?.logs ?? []);
  const autoJoinHits = $derived(result?.db.auto_join ?? []);
  const vcHits = $derived(result?.db.vc_sessions ?? []);
  const allLogsSelected = $derived(
    logHits.length > 0 && logHits.every((r) => selectedLogs.has(logKey(r)))
  );
  const allDbSelected = $derived(
    (() => {
      const ajOk =
        autoJoinHits.length === 0 ||
        autoJoinHits.every((r) => selectedAutoJoin.has(autoJoinKey(r)));
      const vcOk =
        vcHits.length === 0 || vcHits.every((r) => selectedVc.has(vcKey(r)));
      return (autoJoinHits.length > 0 || vcHits.length > 0) && ajOk && vcOk;
    })()
  );

  async function runSearch() {
    const trimmed = userId.trim();
    if (!/^\d+$/.test(trimmed)) {
      error = "Discord ユーザー ID（数字）を入力してください";
      return;
    }
    loading = true;
    error = null;
    try {
      const data = await apiGet<SearchResult>(
        `/privacy/search?user_id=${encodeURIComponent(trimmed)}`
      );
      result = data;
      selectedLogs = new Set(data.logs.map(logKey));
      selectedAutoJoin = new Set(data.db.auto_join.map(autoJoinKey));
      selectedVc = new Set(data.db.vc_sessions.map(vcKey));
    } catch (e) {
      error = e instanceof Error ? e.message : String(e);
      result = null;
    } finally {
      loading = false;
    }
  }

  function toggleIn(set: Set<string>, key: string): Set<string> {
    const next = new Set(set);
    if (next.has(key)) next.delete(key);
    else next.add(key);
    return next;
  }

  async function refreshAfterMutation() {
    const trimmed = userId.trim();
    if (!/^\d+$/.test(trimmed)) return;
    const data = await apiGet<SearchResult>(
      `/privacy/search?user_id=${encodeURIComponent(trimmed)}`
    );
    result = data;
    selectedLogs = new Set(data.logs.map(logKey));
    selectedAutoJoin = new Set(data.db.auto_join.map(autoJoinKey));
    selectedVc = new Set(data.db.vc_sessions.map(vcKey));
  }

  async function maskSelectedLogs() {
    const items = logHits
      .filter((r) => selectedLogs.has(logKey(r)))
      .map((r) => ({ line_no: r.line_no, content_hash: r.content_hash }));
    if (items.length === 0) return;
    if (
      !window.confirm(
        `${items.length} 件のログ行を日時のみ残してマスクします。よろしいですか？`
      )
    ) {
      return;
    }
    busy = true;
    error = null;
    try {
      await apiPostJson("/privacy/logs/mask", { items });
      await refreshAfterMutation();
    } catch (e) {
      error = e instanceof Error ? e.message : String(e);
    } finally {
      busy = false;
    }
  }

  async function maskAllLogs() {
    if (logHits.length === 0) return;
    selectedLogs = new Set(logHits.map(logKey));
    const items = logHits.map((r) => ({
      line_no: r.line_no,
      content_hash: r.content_hash,
    }));
    if (
      !window.confirm(`ヒットしたログ全 ${items.length} 件をマスクします。よろしいですか？`)
    ) {
      return;
    }
    busy = true;
    error = null;
    try {
      await apiPostJson("/privacy/logs/mask", { items });
      await refreshAfterMutation();
    } catch (e) {
      error = e instanceof Error ? e.message : String(e);
    } finally {
      busy = false;
    }
  }

  async function deleteSelectedDb() {
    if (!result) return;
    const auto_join = autoJoinHits.filter((r) =>
      selectedAutoJoin.has(autoJoinKey(r))
    );
    const vc_sessions = vcHits.filter((r) => selectedVc.has(vcKey(r)));
    if (auto_join.length === 0 && vc_sessions.length === 0) return;
    if (
      !window.confirm(
        `DB ${auto_join.length + vc_sessions.length} 件分を削除します。よろしいですか？`
      )
    ) {
      return;
    }
    busy = true;
    error = null;
    try {
      await apiPostJson("/privacy/db/delete", {
        user_id: result.user_id,
        auto_join,
        vc_sessions,
      });
      await refreshAfterMutation();
    } catch (e) {
      error = e instanceof Error ? e.message : String(e);
    } finally {
      busy = false;
    }
  }

  async function deleteAllDb() {
    if (!result) return;
    if (autoJoinHits.length === 0 && vcHits.length === 0) return;
    if (
      !window.confirm(
        `ユーザー ${result.user_id} の DB ヒットをすべて削除します。よろしいですか？`
      )
    ) {
      return;
    }
    busy = true;
    error = null;
    try {
      await apiPostJson("/privacy/db/delete", {
        user_id: result.user_id,
        all: true,
      });
      await refreshAfterMutation();
    } catch (e) {
      error = e instanceof Error ? e.message : String(e);
    } finally {
      busy = false;
    }
  }

  function lineClass(message: string, level?: string): string {
    if (message.includes("[USER_INPUT]")) return "user-input";
    if (message.includes("[LLM_RESPONSE]")) return "llm-response";
    if (message.includes("[GUILD_EVENT]")) return "guild-event";
    const lv = (level || "info").toLowerCase();
    if (lv === "critical") return "level-error";
    return `level-${lv}`;
  }

  function truncate(text: string, max: number): string {
    if (text.length <= max) return text;
    return `${text.slice(0, max)}…`;
  }
</script>

<div class="main privacy-panel">
  <div class="log-toolbar">
    <strong>ログ管理</strong>
    <label class="privacy-search">
      User ID
      <input
        type="text"
        inputmode="numeric"
        bind:value={userId}
        onkeydown={(e) => {
          if (e.key === "Enter") void runSearch();
        }}
        placeholder="Discord Snowflake"
      />
    </label>
    <button
      type="button"
      class="secondary"
      disabled={loading || busy}
      onclick={() => void runSearch()}
    >
      {loading ? "検索中…" : "検索"}
    </button>
  </div>

  {#if error}
    <div class="privacy-error">{error}</div>
  {/if}

  {#if !result}
    <div class="privacy-empty muted">
      ユーザー ID を検索すると、該当ログと DB データが表示されます。
    </div>
  {:else}
    <div class="privacy-grid">
      <section class="card">
        <h3>
          ログ ({logHits.length})
          <span class="privacy-actions">
            <label>
              <input
                type="checkbox"
                checked={allLogsSelected}
                onchange={(e) => {
                  selectedLogs = e.currentTarget.checked
                    ? new Set(logHits.map(logKey))
                    : new Set();
                }}
                disabled={logHits.length === 0}
              />
              全選択
            </label>
            <button
              type="button"
              class="secondary"
              disabled={busy || selectedLogs.size === 0}
              onclick={() => void maskSelectedLogs()}
            >
              選択をマスク
            </button>
            <button
              type="button"
              class="btn-danger"
              disabled={busy || logHits.length === 0}
              onclick={() => void maskAllLogs()}
            >
              全マスク
            </button>
          </span>
        </h3>
        <div class="card-body privacy-list">
          {#if logHits.length === 0}
            <div class="muted">該当ログなし</div>
          {:else}
            {#each logHits as row (logKey(row))}
              <label class="privacy-row">
                <input
                  type="checkbox"
                  checked={selectedLogs.has(logKey(row))}
                  onchange={() => {
                    selectedLogs = toggleIn(selectedLogs, logKey(row));
                  }}
                />
                <span class={`log-line ${lineClass(row.message, row.level)}`}>
                  <span class="muted">#{row.line_no}</span>
                  {truncate(row.message, 240)}
                </span>
              </label>
            {/each}
          {/if}
        </div>
      </section>

      <section class="card">
        <h3>
          DB ({autoJoinHits.length + vcHits.length})
          <span class="privacy-actions">
            <label>
              <input
                type="checkbox"
                checked={allDbSelected}
                onchange={(e) => {
                  if (e.currentTarget.checked) {
                    selectedAutoJoin = new Set(autoJoinHits.map(autoJoinKey));
                    selectedVc = new Set(vcHits.map(vcKey));
                  } else {
                    selectedAutoJoin = new Set();
                    selectedVc = new Set();
                  }
                }}
                disabled={autoJoinHits.length === 0 && vcHits.length === 0}
              />
              全選択
            </label>
            <button
              type="button"
              class="secondary"
              disabled={busy || (selectedAutoJoin.size === 0 && selectedVc.size === 0)}
              onclick={() => void deleteSelectedDb()}
            >
              選択を削除
            </button>
            <button
              type="button"
              class="btn-danger"
              disabled={busy || (autoJoinHits.length === 0 && vcHits.length === 0)}
              onclick={() => void deleteAllDb()}
            >
              全削除
            </button>
          </span>
        </h3>
        <div class="card-body privacy-list">
          {#if autoJoinHits.length === 0 && vcHits.length === 0}
            <div class="muted">該当 DB データなし</div>
          {:else}
            {#each autoJoinHits as row (`aj-${autoJoinKey(row)}`)}
              <label class="privacy-row">
                <input
                  type="checkbox"
                  checked={selectedAutoJoin.has(autoJoinKey(row))}
                  onchange={() => {
                    selectedAutoJoin = toggleIn(selectedAutoJoin, autoJoinKey(row));
                  }}
                />
                <span>
                  <strong>autojoin</strong> guild={row.guild_id} user={row.user_id}
                </span>
              </label>
            {/each}
            {#each vcHits as row (`vc-${vcKey(row)}`)}
              <label class="privacy-row">
                <input
                  type="checkbox"
                  checked={selectedVc.has(vcKey(row))}
                  onchange={() => {
                    selectedVc = toggleIn(selectedVc, vcKey(row));
                  }}
                />
                <span>
                  <strong>vc_session</strong> [{row.bot_id}] guild={row.guild_id}
                  {row.current_match ? ` · current=${row.current_title ?? "-"}` : ""}
                  {row.queue_match_count > 0 ? ` · queue×${row.queue_match_count}` : ""}
                </span>
              </label>
            {/each}
          {/if}
        </div>
      </section>
    </div>
  {/if}
</div>
