import { useMemo, useState } from "react";
import { Sidebar, type NavId } from "./components/Sidebar";
import { StatusBar } from "./components/StatusBar";
import { ShutdownButton } from "./components/ShutdownButton";
import { OpsDashboard } from "./components/OpsDashboard";
import { LogPanel } from "./components/LogPanel";
import { UserDataPanel } from "./components/UserDataPanel";
import { useStatus } from "./hooks/useStatus";
import {
  filterLogEntries,
  takeLastMatching,
  useLogStream,
  type LogEntry,
} from "./hooks/useLogStream";
import "./styles/discord-theme.css";

type LogNavId = Exclude<NavId, "overview" | "privacy">;

/** 概要カードに出す末尾件数 */
const FEED_ROWS = 80;
/** 参照を固定した空配列（非表示時の不要な再描画を避ける） */
const EMPTY_ENTRIES: LogEntry[] = [];

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

export default function App() {
  const [nav, setNav] = useState<NavId>("overview");
  const { status, vc, guilds, format } = useStatus(1000);
  const { entries, clear, connected } = useLogStream(10000);
  const [levels, setLevels] = useState({
    general: "INFO",
    llm: "INFO",
    tts: "INFO",
    error: "WARNING",
  });
  const [autoScroll, setAutoScroll] = useState(true);

  const isOverview = nav === "overview";
  // 概要表示中のみ、末尾から必要件数だけ拾う
  const llmFeed = useMemo(
    () => (isOverview ? takeLastMatching(entries, FEED_ROWS, isLlmIo) : EMPTY_ENTRIES),
    [entries, isOverview]
  );
  const guildEvents = useMemo(
    () =>
      isOverview ? takeLastMatching(entries, FEED_ROWS, isGuildEvent) : EMPTY_ENTRIES,
    [entries, isOverview]
  );

  const isLogNav =
    nav === "general" || nav === "llm" || nav === "tts" || nav === "error";
  const logCategory = isLogNav ? (nav as LogNavId) : null;
  const logLevel = logCategory ? levels[logCategory] : "INFO";
  // ステータス更新（毎秒）の再描画で 10000 件を再フィルタしない
  const logEntries = useMemo(
    () =>
      logCategory ? filterLogEntries(entries, logCategory, logLevel) : EMPTY_ENTRIES,
    [entries, logCategory, logLevel]
  );

  return (
    <div className="app">
      <header className="topbar">
        <StatusBar
          servers={format.servers}
          vc={format.vc}
          llm={format.llm}
          ping={format.ping}
          uptime={format.uptime}
          alive={status?.alive}
        />
        <ShutdownButton />
      </header>
      <div className="body">
        <Sidebar active={nav} onSelect={setNav} />
        {nav === "overview" ? (
          <OpsDashboard
            vc={vc}
            guilds={guilds}
            avgLatency={format.avgLatency}
            llmFeed={llmFeed}
            guildEvents={guildEvents}
            uptime={format.uptime}
            alive={status?.alive}
          />
        ) : nav === "privacy" ? (
          <UserDataPanel />
        ) : (
          <LogPanel
            title={LOG_TITLES[nav as LogNavId]}
            entries={logEntries}
            level={levels[nav as LogNavId]}
            onLevelChange={(lv) =>
              setLevels((prev) => ({ ...prev, [nav as LogNavId]: lv }))
            }
            autoScroll={autoScroll}
            onAutoScrollChange={setAutoScroll}
            onClear={clear}
            live={connected}
          />
        )}
      </div>
    </div>
  );
}
