import { useMemo, useState } from "react";
import { apiGet, apiPostJson } from "../api";

type LogHit = {
  line_no: number;
  content_hash: string;
  name?: string;
  level?: string;
  message: string;
  category?: string;
};

type AutoJoinHit = {
  guild_id: string;
  user_id: number;
};

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
  db: {
    auto_join: AutoJoinHit[];
    vc_sessions: VcSessionHit[];
  };
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

export function UserDataPanel() {
  const [userId, setUserId] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<SearchResult | null>(null);
  const [selectedLogs, setSelectedLogs] = useState<Set<string>>(new Set());
  const [selectedAutoJoin, setSelectedAutoJoin] = useState<Set<string>>(
    new Set()
  );
  const [selectedVc, setSelectedVc] = useState<Set<string>>(new Set());
  const [busy, setBusy] = useState(false);

  const logHits = result?.logs ?? [];
  const autoJoinHits = result?.db.auto_join ?? [];
  const vcHits = result?.db.vc_sessions ?? [];

  const allLogsSelected = useMemo(
    () =>
      logHits.length > 0 && logHits.every((r) => selectedLogs.has(logKey(r))),
    [logHits, selectedLogs]
  );
  const allDbSelected = useMemo(() => {
    const ajOk =
      autoJoinHits.length === 0 ||
      autoJoinHits.every((r) => selectedAutoJoin.has(autoJoinKey(r)));
    const vcOk =
      vcHits.length === 0 || vcHits.every((r) => selectedVc.has(vcKey(r)));
    return (
      (autoJoinHits.length > 0 || vcHits.length > 0) && ajOk && vcOk
    );
  }, [autoJoinHits, vcHits, selectedAutoJoin, selectedVc]);

  async function runSearch() {
    const trimmed = userId.trim();
    if (!/^\d+$/.test(trimmed)) {
      setError("Discord ユーザー ID（数字）を入力してください");
      return;
    }
    setLoading(true);
    setError(null);
    try {
      const data = await apiGet<SearchResult>(
        `/privacy/search?user_id=${encodeURIComponent(trimmed)}`
      );
      setResult(data);
      setSelectedLogs(new Set(data.logs.map(logKey)));
      setSelectedAutoJoin(new Set(data.db.auto_join.map(autoJoinKey)));
      setSelectedVc(new Set(data.db.vc_sessions.map(vcKey)));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setResult(null);
    } finally {
      setLoading(false);
    }
  }

  function toggleLog(key: string) {
    setSelectedLogs((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  }

  function toggleAutoJoin(key: string) {
    setSelectedAutoJoin((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  }

  function toggleVc(key: string) {
    setSelectedVc((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  }

  function setAllLogs(on: boolean) {
    setSelectedLogs(on ? new Set(logHits.map(logKey)) : new Set());
  }

  function setAllDb(on: boolean) {
    setSelectedAutoJoin(on ? new Set(autoJoinHits.map(autoJoinKey)) : new Set());
    setSelectedVc(on ? new Set(vcHits.map(vcKey)) : new Set());
  }

  async function refreshAfterMutation() {
    const trimmed = userId.trim();
    if (!/^\d+$/.test(trimmed)) return;
    const data = await apiGet<SearchResult>(
      `/privacy/search?user_id=${encodeURIComponent(trimmed)}`
    );
    setResult(data);
    setSelectedLogs(new Set(data.logs.map(logKey)));
    setSelectedAutoJoin(new Set(data.db.auto_join.map(autoJoinKey)));
    setSelectedVc(new Set(data.db.vc_sessions.map(vcKey)));
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
    setBusy(true);
    setError(null);
    try {
      await apiPostJson("/privacy/logs/mask", { items });
      await refreshAfterMutation();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  async function maskAllLogs() {
    if (logHits.length === 0) return;
    setSelectedLogs(new Set(logHits.map(logKey)));
    const items = logHits.map((r) => ({
      line_no: r.line_no,
      content_hash: r.content_hash,
    }));
    if (
      !window.confirm(
        `ヒットしたログ全 ${items.length} 件をマスクします。よろしいですか？`
      )
    ) {
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await apiPostJson("/privacy/logs/mask", { items });
      await refreshAfterMutation();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
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
    setBusy(true);
    setError(null);
    try {
      await apiPostJson("/privacy/db/delete", {
        user_id: result.user_id,
        auto_join,
        vc_sessions,
      });
      await refreshAfterMutation();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
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
    setBusy(true);
    setError(null);
    try {
      await apiPostJson("/privacy/db/delete", {
        user_id: result.user_id,
        all: true,
      });
      await refreshAfterMutation();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="main privacy-panel">
      <div className="log-toolbar">
        <strong>ログ管理</strong>
        <label className="privacy-search">
          User ID{" "}
          <input
            type="text"
            inputMode="numeric"
            value={userId}
            onChange={(e) => setUserId(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") void runSearch();
            }}
            placeholder="Discord Snowflake"
          />
        </label>
        <button
          type="button"
          className="secondary"
          disabled={loading || busy}
          onClick={() => void runSearch()}
        >
          {loading ? "検索中…" : "検索"}
        </button>
      </div>

      {error ? <div className="privacy-error">{error}</div> : null}

      {!result ? (
        <div className="privacy-empty muted">
          ユーザー ID を検索すると、該当ログと DB データが表示されます。
        </div>
      ) : (
        <div className="privacy-grid">
          <section className="card">
            <h3>
              ログ ({logHits.length})
              <span className="privacy-actions">
                <label>
                  <input
                    type="checkbox"
                    checked={allLogsSelected}
                    onChange={(e) => setAllLogs(e.target.checked)}
                    disabled={logHits.length === 0}
                  />{" "}
                  全選択
                </label>
                <button
                  type="button"
                  className="secondary"
                  disabled={busy || selectedLogs.size === 0}
                  onClick={() => void maskSelectedLogs()}
                >
                  選択をマスク
                </button>
                <button
                  type="button"
                  className="btn-danger"
                  disabled={busy || logHits.length === 0}
                  onClick={() => void maskAllLogs()}
                >
                  全マスク
                </button>
              </span>
            </h3>
            <div className="card-body privacy-list">
              {logHits.length === 0 ? (
                <div className="muted">該当ログなし</div>
              ) : (
                logHits.map((row) => {
                  const key = logKey(row);
                  return (
                    <label className="privacy-row" key={key}>
                      <input
                        type="checkbox"
                        checked={selectedLogs.has(key)}
                        onChange={() => toggleLog(key)}
                      />
                      <span
                        className={`log-line ${lineClass(row.message, row.level)}`}
                      >
                        <span className="muted">#{row.line_no}</span>{" "}
                        {truncate(row.message, 240)}
                      </span>
                    </label>
                  );
                })
              )}
            </div>
          </section>

          <section className="card">
            <h3>
              DB ({autoJoinHits.length + vcHits.length})
              <span className="privacy-actions">
                <label>
                  <input
                    type="checkbox"
                    checked={allDbSelected}
                    onChange={(e) => setAllDb(e.target.checked)}
                    disabled={
                      autoJoinHits.length === 0 && vcHits.length === 0
                    }
                  />{" "}
                  全選択
                </label>
                <button
                  type="button"
                  className="secondary"
                  disabled={
                    busy ||
                    (selectedAutoJoin.size === 0 && selectedVc.size === 0)
                  }
                  onClick={() => void deleteSelectedDb()}
                >
                  選択を削除
                </button>
                <button
                  type="button"
                  className="btn-danger"
                  disabled={
                    busy ||
                    (autoJoinHits.length === 0 && vcHits.length === 0)
                  }
                  onClick={() => void deleteAllDb()}
                >
                  全削除
                </button>
              </span>
            </h3>
            <div className="card-body privacy-list">
              {autoJoinHits.length === 0 && vcHits.length === 0 ? (
                <div className="muted">該当 DB データなし</div>
              ) : (
                <>
                  {autoJoinHits.map((row) => {
                    const key = autoJoinKey(row);
                    return (
                      <label className="privacy-row" key={`aj-${key}`}>
                        <input
                          type="checkbox"
                          checked={selectedAutoJoin.has(key)}
                          onChange={() => toggleAutoJoin(key)}
                        />
                        <span>
                          <strong>autojoin</strong> guild={row.guild_id} user=
                          {row.user_id}
                        </span>
                      </label>
                    );
                  })}
                  {vcHits.map((row) => {
                    const key = vcKey(row);
                    return (
                      <label className="privacy-row" key={`vc-${key}`}>
                        <input
                          type="checkbox"
                          checked={selectedVc.has(key)}
                          onChange={() => toggleVc(key)}
                        />
                        <span>
                          <strong>vc_session</strong> [{row.bot_id}] guild=
                          {row.guild_id}
                          {row.current_match
                            ? ` · current=${row.current_title ?? "-"}`
                            : ""}
                          {row.queue_match_count > 0
                            ? ` · queue×${row.queue_match_count}`
                            : ""}
                        </span>
                      </label>
                    );
                  })}
                </>
              )}
            </div>
          </section>
        </div>
      )}
    </div>
  );
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
