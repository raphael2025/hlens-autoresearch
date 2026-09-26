import type { ReportEnvelope } from "../api";
import { EvidenceOnlyBanner, SimulatedBanner } from "../components/Banner";
import { ReportBrowser } from "../components/ReportBrowser";
import {
  asCalibrationPayload,
  ciCell,
  detectorErrorRuns,
  detectorErrorsOf,
  hasDetectorErrors,
  passRate,
  type CandidateEvidence,
} from "../lib/gateCalibration";

// Payload types and the pure helpers live in src/lib/gateCalibration.ts (tested with node --test).
// The page shows evidence only: no recommended / default / optimal value anywhere.

function CandidateSection({ candidate }: { candidate: CandidateEvidence }) {
  const arms = Object.keys(candidate.pipeline);
  const gateIds = Object.keys(candidate.gates).sort();
  const showDetectorErrors = hasDetectorErrors(candidate);
  const erroredRuns = detectorErrorRuns(candidate);

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
            {showDetectorErrors && <th>detector_errors</th>}
          </tr>
        </thead>
        <tbody>
          {arms.map((arm) => {
            const evidence = candidate.pipeline[arm];
            const pass = passRate(evidence);
            const detectorErrors = detectorErrorsOf(evidence);
            return (
              <tr key={arm}>
                <td>{arm}</td>
                <td>{evidence.runs}</td>
                <td>{pass !== null ? `${pass.label}: ${pass.rate.rate}` : "—"}</td>
                <td>{pass !== null ? pass.rate.count : "—"}</td>
                <td>{pass !== null ? ciCell(pass.rate) : "—"}</td>
                <td>{evidence.inconclusive_rate.rate}</td>
                <td>{evidence.failed}</td>
                <td>{evidence.sealed_oos_consumption_rate.rate}</td>
                {showDetectorErrors && (
                  <td style={detectorErrors !== null ? { color: "crimson", fontWeight: 600 } : undefined}>
                    {detectorErrors ?? 0}
                  </td>
                )}
              </tr>
            );
          })}
        </tbody>
      </table>
      {showDetectorErrors && (
        <p style={{ color: "#555", fontSize: 13 }}>
          detector_errors：检测器在该 arm 上抛出异常的运行数（已计入 inconclusive，从不算通过）。
        </p>
      )}

      {erroredRuns.length > 0 && (
        <>
          <h4>Detector errors（INCONCLUSIVE 运行，未评估任何 gate）</h4>
          <table>
            <thead>
              <tr>
                <th>arm</th>
                <th>seed</th>
                <th>verdict</th>
                <th>detector_error</th>
              </tr>
            </thead>
            <tbody>
              {erroredRuns.map((run) => (
                <tr key={`${run.arm}:${run.seed}`}>
                  <td>{run.arm}</td>
                  <td>{run.seed}</td>
                  <td>{run.verdict}</td>
                  <td>
                    <code>{run.detector_error}</code>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}

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

function calibrationLabel(report: ReportEnvelope): string {
  const payload = asCalibrationPayload(report.payload);
  return payload !== null
    ? `${payload.inputs.detector?.name ?? report.id} (${payload.candidates.length} candidates)`
    : report.id;
}

export function GateCalibration() {
  return (
    <section>
      <SimulatedBanner />
      <EvidenceOnlyBanner />
      <h2>Gate Calibration（校准证据）</h2>
      <ReportBrowser
        kind="gate_calibration"
        empty="（无校准报告 — 未配置报告目录或目录为空）"
        prompt="选择一份校准报告查看 FPR / power 明细。"
        label={calibrationLabel}
        renderDetail={(detail) => <CalibrationDetail envelope={detail} />}
        listWidth={220}
      />
    </section>
  );
}
