// Every page that shows research-plane output carries this banner: nothing in the console is a
// trading decision, a live account view, or a validated result (CLAUDE.md H5 / H10; ADR-0048).
export function SimulatedBanner() {
  return (
    <div
      role="note"
      style={{
        background: "#fff3cd",
        color: "#664d03",
        border: "1px solid #ffe69c",
        borderRadius: 6,
        padding: "8px 12px",
        margin: "8px 0 16px",
        fontSize: 14,
        fontWeight: 600,
      }}
    >
      SIMULATED / NOT_VALIDATED — 只读研究控制台；无下单、无转账、无实盘账户能力。
    </div>
  );
}
