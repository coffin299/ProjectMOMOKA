type NavId = "overview" | "general" | "llm" | "tts" | "error";

const ITEMS: { id: NavId; label: string }[] = [
  { id: "overview", label: "Overview" },
  { id: "general", label: "一般" },
  { id: "llm", label: "LLM" },
  { id: "tts", label: "TTS+Music" },
  { id: "error", label: "エラー" },
];

type Props = {
  active: NavId;
  onSelect: (id: NavId) => void;
};

export type { NavId };

export function Sidebar({ active, onSelect }: Props) {
  return (
    <nav className="sidebar">
      {ITEMS.map((item) => (
        <button
          key={item.id}
          type="button"
          className={`nav-btn${active === item.id ? " active" : ""}`}
          onClick={() => onSelect(item.id)}
        >
          {item.label}
        </button>
      ))}
    </nav>
  );
}
