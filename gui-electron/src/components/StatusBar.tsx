type Props = {
  servers: string;
  vc: string;
  llm: string;
  ping: string;
  uptime: string;
  alive: Record<string, boolean> | undefined;
};

export function StatusBar({
  servers,
  vc,
  llm,
  ping,
  uptime,
  alive,
}: Props) {
  const plana = alive?.plana;
  const arona = alive?.arona;
  return (
    <>
      <div className="brand">MOMOKA</div>
      <div className="pills">
        <span className="pill">
          Servers<strong>{servers}</strong>
        </span>
        <span className="pill">
          VC<strong>{vc}</strong>
        </span>
        <span className="pill">
          LLM<strong>{llm}</strong>
        </span>
        <span className="pill">
          ping<strong>{ping}</strong>
        </span>
        <span className="pill">
          uptime<strong>{uptime}</strong>
        </span>
        <span className="pill">
          PLANA
          <strong>{plana === undefined ? "-" : plana ? "up" : "down"}</strong>
        </span>
        <span className="pill">
          ARONA
          <strong>{arona === undefined ? "-" : arona ? "up" : "down"}</strong>
        </span>
      </div>
    </>
  );
}
