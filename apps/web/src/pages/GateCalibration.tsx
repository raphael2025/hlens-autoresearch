import { useEffect, useState } from "react";
import { getReport, listReports, type ReportEnvelope } from "../api";
import { EvidenceOnlyBanner, SimulatedBanner } from "../components/Banner";

// Payload shape written by research/reports/gate_calibration.py (write_gate_calibration_report),
// which wraps research/synthetic_lab/gate_calibration.py's GateCalibrationReport.to_payload() —
// hand-typed here the same way the other report pages type their payload, since /reports/{kind}
// has no per-kind OpenAPI schema (ReportEnvelope.payload is `dict[str, Any]`).
type Interval = {
  method: string;
  alpha: string;
  lower: string;
  upper: string;
};

type Rate = {
  n: number;
  count: number;
  rate: string;
  interval: Interval;
};

// One arm's pipeline-level evidence for one candidate: `false_positive_rate` on the noise arm,
// `power` on a planted-effect arm (research/synthetic_lab/gate_calibration.py's `_pass_key`).
type ArmPipelineEvidence = {
  runs: number;
  false_positive_rate?: Rate;
  power?: Rate;
  inconclusive_rate: Rate;
  failed: number;
  sealed_oos_consumption_rate: Rate;
};

// One gate's evidence within one arm, same `false_positive_rate` / `power` split.
type GateArmEvidence = {
  false_positive_rate?: Rate;
  power?: Rate;
  inconclusive_rate: Rate;
  failed: number;
  not_evaluated: number;
};

type CandidateEvidence = {
  profile: string;
  profile_hash: string;
  pipeline: Record<string, ArmPipelineEvidence>;
  gates: Record<string, Record<string, GateArmEvidence>>;
  runs: unknown[];
  sealed_oos_unsealings: number;
};

type GateCalibrationPayload = {
  kind: string;
  schema_version: string;
  status: string;
  disclaimer: string;
  note: string;
  inputs: {
    detector?: { name?: string };
    interval?: { method?: string; alpha?: string };
    noise_seeds?: unknown[];
    planted_seeds?: unknown[];
    planted_effects?: { arm?: string }[];
  };
  candidates: CandidateEvidence[];
  report_hash: string;
};

function asCalibrationPayload(
  payload: Record<string, unknown> | undefined,
): GateCalibrationPayload | null {
  if (payload === undefined || !Array.isArray(payload.candidates)) return null;
  return payload as unknown as GateCalibrationPayload;
}

// Same split as the writer's `_pass_key`: the noise arm reports a false-positive rate, every
// other (planted-effect) arm reports power. Never both on the same row.
function passRate(evidence: { false_positive_rate?: Rate; power?: Rate }): {
  label: "false_positive_rate" | "power";
  rate: Rate;
} | null {
  if (evidence.false_positive_rate !== undefined) {
    return { label: "false_positive_rate", rate: evidence.false_positive_rate };
  }
  if (evidence.power !== undefined) {
    return { label: "power", rate: evidence.power };
  }
  return null;
}

function ciCell(rate: Rate): string {
  return `[${rate.interval.lower}, ${rate.interval.upper}] (α=${rate.interval.alpha}, ${rate.interval.method})`;
}

function CandidateSection({ candidate }: { candidate: CandidateEvidence }) {
  const arms = Object.keys(candidate.pipeline);
  const gateIds = Object.keys(candidate.gates).sort();

  return (
    <div style={{ marginBottom: 32 }}>
      <h3 style={{ marginBottom: 4 }}>{candidate.profile}</h3>
      <p style={{ marginTop: 0, color: "#555" }}>
        profile_hash <code>{candidate.profile_hash.slice(0, 12)}…</code> · sealed_oos_unsealings{" "}
        {candidate.sealed_oos_unsealings} · runs {candidate.runs.length}
      </p>

      <h4>Pipeline-level (FPR / power)</h4>
      <table>
        <thead>
          <tr>
            <th>arm</th>
            <th>n</th>
            <th>rate</th>
            <th>count</th>
            <th>95% CI</th>
            <th>inconclusive_rate</th>
            <th>failed</th>
            <th>sealed_oos_consumption_rate</th>
          </tr>
        </thead>
        <tbody>
          {arms.map((arm) => {
            const evidence = candidate.pipeline[arm];
            const pass = passRate(evidence);
            return (
              <tr key={arm}>
                <td>{arm}</td>
                <td>{evidence.runs}</td>
                <td>
                  {pass !== null ? `${pass.label}: ${pass.rate.rate}` : "—"}
                </td>
                <td>{pass !== null ? pass.rate.count : "—"}</td>
                <td>{pass !== null ? ciCell(pass.rate) : "—"}</td>
                <td>{evidence.inconclusive_rate.rate}</td>
                <td>{evidence.failed}</td>
                <td>{evidence.sealed_oos_consumption_rate.rate}</td>
              </tr>
            );
          })}
        </tbody>
      </table>

      <h4>Per-gate</h4>
      <table>
        <thead>
          <tr>
            <th>gate_id</th>
            <th>arm</th>
            <th>rate</th>
            <th>95% CI</th>
            <th>inconclusive_rate</th>
            <th>failed</th>
            <th>not_evaluated</th>
          </tr>
        </thead>
        <tbody>
          {gateIds.flatMap((gateId) =>
            arms
              .filter((arm) => candidate.gates[gateId][arm] !== undefined)
              .map((arm) => {
                const evidence = candidate.gates[gateId][arm];
                const pass = passRate(evidence);
                return (
                  <tr key={`${gateId}:${arm}`}>
                    <td>{gateId}</td>
                    <td>{arm}</td>
                    <td>{pass !== null ? `${pass.label}: ${pass.rate.rate}` : "—"}</td>
                    <td>{pass !== null ? ciCell(pass.rate) : "—"}</td>
                    <td>{evidence.inconclusive_rate.rate}</td>
                    <td>{evidence.failed}</td>
                    <td>{evidence.not_evaluated}</td>
                  </tr>
                );
              }),
          )}
        </tbody>
      </table>
    </div>
  );
}

function CalibrationDetail({ envelope }: { envelope: ReportEnvelope }) {
  const report = asCalibrationPayload(envelope.payload);
  if (report === null) {
    return <pre>{JSON.stringify(envelope.payload, null, 2)}</pre>;
  }
  return (
    <>
      <p>
        detector <strong>{report.inputs.detector?.name ?? "—"}</strong> · interval{" "}
        {report.inputs.interval?.method ?? "—"} (α={report.inputs.interval?.alpha ?? "—"}) ·
        noise_seeds {report.inputs.noise_seeds?.length ?? 0} · planted_seeds{" "}
        {report.inputs.planted_seeds?.length ?? 0} · report_hash{" "}
        <code>{report.report_hash.slice(0, 12)}…</code>
      </p>
      <p style={{ color: "#555" }}>{report.note}</p>
      {report.candidates.map((candidate) => (
        <CandidateSection key={candidate.profile_hash} candidate={candidate} />
      ))}
    </>
  );
}

export function GateCalibration() {
  const [reports, setReports] = useState<ReportEnvelope[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [detail, setDetail] = useState<ReportEnvelope | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    listReports("gate_calibration")
      .then(setReports)
      .catch((err) => setError(String(err)));
  }, []);

  useEffect(() => {
    if (selectedId === null) {
      setDetail(null);
      return;
    }
    getReport("gate_calibration", selectedId)
      .then(setDetail)
      .catch((err) => setError(String(err)));
  }, [selectedId]);

  return (
    <section>
      <SimulatedBanner />
      <EvidenceOnlyBanner />
      <h2>Gate Calibration（校准证据）</h2>
      {error && <p style={{ color: "crimson" }}>{error}</p>}
      <div style={{ display: "flex", gap: 24 }}>
        <ul style={{ minWidth: 220 }}>
          {reports.length === 0 && <li>（无校准报告 — 未配置报告目录或目录为空）</li>}
          {reports.map((report) => {
            const payload = asCalibrationPayload(report.payload);
            return (
              <li key={report.id}>
                <button onClick={() => setSelectedId(report.id)}>
                  {payload !== null
                    ? `${payload.inputs.detector?.name ?? report.id} (${payload.candidates.length} candidates)`
                    : report.id}
                </button>
              </li>
            );
          })}
        </ul>
        <div style={{ flex: 1, minWidth: 0 }}>
          {detail === null && <p>选择一份校准报告查看 FPR / power 明细。</p>}
          {detail !== null && <CalibrationDetail envelope={detail} />}
        </div>
      </div>
    </section>
  );
}
