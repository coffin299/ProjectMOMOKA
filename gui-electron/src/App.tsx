import { useMemo, useState } from "react";
import { Sidebar, type NavId } from "./components/Sidebar";
import { StatusBar } from "./components/StatusBar";
import { ShutdownButton } from "./components/ShutdownButton";
import { OpsDashboard } from "./components/OpsDashboard";
import { LogPanel } from "./components/LogPanel";
import { useStatus } from "./hooks/useStatus";
import { useLogStream } from "./hooks/useLogStream";
import "./styles/discord-theme.css";

const LOG_TITLES: Record<Exclude<NavId, "overview">, string> = {
  general: "一般ログ",
  llm: "LLMログ",
  tts: "TTS+Musicログ",
  error: "エラーログ",
};

export default function App() {
  const [nav, setNav] = useState<NavId>("overview");
  const { status, vc, guilds, format } = useStatus(1000);
  const { entries, clear, filterBy } = useLogStream(10000);
  const [levels, setLevels] = useState({
    general: "INFO",
    llm: "INFO",
    tts: "INFO",
    error: "WARNING",
  });
  const [autoScroll, setAutoScroll] = useState(true);

  const llmFeed = useMemo(
    () =>
      entries.filter(
        (e) =>
          e.message.includes("[USER_INPUT]") ||
          e.message.includes("[LLM_RESPONSE]")
      ),
    [entries]
  );
  const guildEvents = useMemo(
    () =>
      entries.filter(
        (e) =>
          e.message.includes("[GUILD_EVENT]") ||
          /Joined guild|Left guild/i.test(e.message)
      ),
    [entries]
  );

  const logCategory =
    nav === "overview" ? null : (nav as Exclude<NavId, "overview">);
  const logEntries = logCategory
    ? filterBy(logCategory === "tts" ? "tts" : logCategory, levels[logCategory])
    : [];

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
        ) : (
          <LogPanel
            title={LOG_TITLES[nav]}
            entries={logEntries}
            level={levels[nav]}
            onLevelChange={(lv) =>
              setLevels((prev) => ({ ...prev, [nav]: lv }))
            }
            autoScroll={autoScroll}
            onAutoScrollChange={setAutoScroll}
            onClear={clear}
          />
        )}
      </div>
    </div>
  );
}
