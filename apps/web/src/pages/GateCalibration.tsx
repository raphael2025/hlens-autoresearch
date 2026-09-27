import type { ReportEnvelope } from "../api";
import { EvidenceOnlyBanner, SimulatedBanner } from "../components/Banner";
import { ReportBrowser } from "../components/ReportBrowser";
import {
  asCalibrationPayload,
  boundsCell,
  ciCell,
  detectorErrorRuns,
  detectorErrorsOf,
  hasDetectorErrors,
  hasPassRateBounds,
  passRate,
  readBounds,
  sealedG5Arms,
  sealedG5Of,
  type Bounds,
  type CandidateEvidence,
  type Rate,
} from "../lib/gateCalibration";

// Payload types and the pure helpers live in src/lib/gateCalibration.ts (tested with node --test).
// The page shows evidence only: no recommended / default / optimal value anywhere.

const ERROR_STYLE = { color: "crimson", fontWeight: 600 } as const;

// A bounds cell: "—" when absent, the pair when present, and a red marker when malformed.
function BoundsTd({ bounds }: { bounds: Bounds }) {
  const text = boundsCell(bounds);
  return (
    <td style={bounds.kind === "absent" ? undefined : ERROR_STYLE} data-bounds={bounds.kind}>
      {text ?? "—"}
    </td>
  );
}

function rateText(rate: Rate | null): string {
  return rate === null ? "—" : `${rate.rate} (${rate.count}/${rate.n})`;
}

function SealedG5Section({ candidate }: { candidate: CandidateEvidence }) {
  const arms = sealedG5Arms(candidate);
  if (arms.length === 0) return null;
  return (
    <>
      <h4>G5（密封样本外）与端到端 G0 – G5</h4>
      <table data-section="sealed-oos-g5">
        <thead>
          <tr>
            <th>arm</th>
            <th>reached</th>
            <th>G5 pass_rate</th>
            <th>G5 95% CI</th>
            <th>G5 inconclusive_rate</th>
            <th>G5 fail_rate</th>
            <th>consumed_without_result</th>
            <th>detector_errors</th>
            <th>G5 pass_rate_bounds</th>
            <th>end-to-end G0 – G5</th>
            <th>end-to-end 95% CI</th>
            <th>end_to_end_bounds</th>
          </tr>
        </thead>
        <tbody>
          {arms.map((arm) => {
            const g5 = sealedG5Of(candidate.pipeline[arm]);
            if (g5 === null) return null;
            const endToEnd = passRate(g5.end_to_end_g0_g5);
            return (
              <tr key={arm}>
                <td>{arm}</td>
                <td>{g5.reached}</td>
                <td>{rateText(g5.pass_rate)}</td>
                <td>{g5.pass_rate !== null ? ciCell(g5.pass_rate) : "—"}</td>
                <td>{rateText(g5.inconclusive_rate)}</td>
                <td>{rateText(g5.fail_rate)}</td>
                <td>{g5.consumed_without_result}</td>
                <td style={g5.detector_errors > 0 ? ERROR_STYLE : undefined}>{g5.detector_errors}</td>
                <BoundsTd bounds={readBounds(g5.pass_rate_bounds)} />
                <td>{endToEnd !== null ? `${endToEnd.label}: ${rateText(endToEnd.rate)}` : "—"}</td>
                <td>{endToEnd !== null ? ciCell(endToEnd.rate) : "—"}</td>
                <BoundsTd bounds={readBounds(g5.end_to_end_bounds)} />
              </tr>
            );
          })}
        </tbody>
      </table>
      <p style={{ color: "#555", fontSize: 13 }}>
        G5 各率以到达 G5 的运行为分母（无运行到达时为 —）；端到端 G0 – G5 以该 arm 全部运行为分母。
        有检测器错误时，点估计不是真实比率：bounds 为全部出错运行分别算作失败 / 通过时的范围（向外取整）。
      </p>
    </>
  );
}

function CandidateSection({ candidate }: { candidate: CandidateEvidence }) {
  const arms = Object.keys(candidate.pipeline);
  const gateIds = Object.keys(candidate.gates).sort();
  const showDetectorErrors = hasDetectorErrors(candidate);
  const showBounds = hasPassRateBounds(candidate);
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
            {showBounds && <th>pass_rate_bounds</th>}
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
                  <td style={detectorErrors !== null ? ERROR_STYLE : undefined}>
                    {detectorErrors ?? 0}
                  </td>
                )}
                {showBounds && <BoundsTd bounds={readBounds(evidence.pass_rate_bounds)} />}
              </tr>
            );
          })}
        </tbody>
      </table>
      {showDetectorErrors && (
        <p style={{ color: "#555", fontSize: 13 }}>
          detector_errors：检测器在该 arm 上抛出异常的运行数（已计入 inconclusive，从不算通过）。
          {showBounds &&
            " 这些 arm 的 rate 只是点估计：pass_rate_bounds 为出错运行全部算失败 / 全部算通过时的通过率范围（向外取整）。"}
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

      <SealedG5Section candidate={candidate} />

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
