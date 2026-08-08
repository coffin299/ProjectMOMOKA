import { useEffect, useRef, useState } from "react";
import type { LogEntry } from "../hooks/useLogStream";

type Props = {
  title: string;
  entries: LogEntry[];
  level: string;
  onLevelChange: (level: string) => void;
  autoScroll: boolean;
  onAutoScrollChange: (v: boolean) => void;
  onClear: () => void;
  /** WS ライブ接続中なら true（ポーリングのみのときは false） */
  live?: boolean;
};

const LEVELS = ["DEBUG", "INFO", "WARNING", "ERROR"];

export function LogPanel({
  title,
  entries,
  level,
  onLevelChange,
  autoScroll,
  onAutoScrollChange,
  onClear,
  live = false,
}: Props) {
  const ref = useRef<HTMLDivElement>(null);
  const [stick, setStick] = useState(true);

  useEffect(() => {
    if (!autoScroll || !stick) return;
    const el = ref.current;
    if (!el) return;
    const scroll = () => {
      el.scrollTop = el.scrollHeight;
    };
    scroll();
    const id = window.requestAnimationFrame(scroll);
    return () => window.cancelAnimationFrame(id);
  }, [entries, autoScroll, stick]);

  const onScroll = () => {
    const el = ref.current;
    if (!el) return;
    const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 40;
    setStick(atBottom);
  };

  return (
    <div className="main" style={{ display: "flex", flexDirection: "column" }}>
      <div className="log-toolbar">
        <strong>{title}</strong>
        <span
          title={
            live
              ? "WebSocket ライブ接続中"
              : "履歴ポーリング中（WS 未接続でも更新されます）"
          }
          style={{ opacity: 0.75, fontSize: "0.85em" }}
        >
          {live ? "● live" : "○ poll"}
        </span>
        <label>
          Level{" "}
          <select
            value={level}
            onChange={(e) => onLevelChange(e.target.value)}
          >
            {LEVELS.map((l) => (
              <option key={l} value={l}>
                {l}
              </option>
            ))}
          </select>
        </label>
        <label>
          <input
            type="checkbox"
            checked={autoScroll}
            onChange={(e) => onAutoScrollChange(e.target.checked)}
          />{" "}
          Auto-scroll
        </label>
        <button type="button" className="secondary" onClick={onClear}>
          Clear
        </button>
      </div>
      <div className="log-panel" ref={ref} onScroll={onScroll}>
        {entries.map((e) => (
          <p key={e.id} className={`log-line ${lineClass(e)}`}>
            {renderMessage(e.message)}
          </p>
        ))}
      </div>
    </div>
  );
}

function lineClass(e: LogEntry): string {
  if (e.message.includes("[USER_INPUT]")) return "user-input";
  if (e.message.includes("[LLM_RESPONSE]")) return "llm-response";
  if (e.message.includes("[GUILD_EVENT]")) return "guild-event";
  const lv = e.level.toLowerCase();
  if (lv === "critical") return "level-error";
  return `level-${lv}`;
}

function renderMessage(message: string) {
  const parts = message.split(/(\[PLANA\]|\[ARONA\])/g);
  return parts.map((p, i) => {
    if (p === "[PLANA]")
      return (
        <span key={i} className="tag-plana">
          {p}
        </span>
      );
    if (p === "[ARONA]")
      return (
        <span key={i} className="tag-arona">
          {p}
        </span>
      );
    return <span key={i}>{p}</span>;
  });
}
