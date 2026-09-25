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

// Gate Calibration reports are Phase 9 synthetic-truth evidence for D-09 (ADR-0007 two-step
// freeze) — never a Profile decision by themselves (research/synthetic_lab/gate_calibration.py's
// DISCLAIMER). This banner is deliberately louder (red, not the amber SimulatedBanner) so the
// page cannot be read as "here are the numbers to use".
export function EvidenceOnlyBanner() {
  return (
    <div
      role="note"
      style={{
        background: "#f8d7da",
        color: "#58151c",
        border: "1px solid #f1aeb5",
        borderRadius: 6,
        padding: "8px 12px",
        margin: "8px 0 16px",
        fontSize: 14,
        fontWeight: 700,
      }}
    >
      EVIDENCE ONLY — NOT A PROFILE DECISION — 这些 FPR / power 数字是 Phase 9 校准证据，不是 Validation
      Profile 的数值来源；Profile 数值只能由 Raphael 依两步冻结流程（ADR-0007）事后设定。本页不给出、也不暗示任何推荐值。
    </div>
  );
}
