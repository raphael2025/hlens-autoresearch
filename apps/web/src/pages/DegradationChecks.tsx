import type { ReportEnvelope } from "../api";
import { SimulatedBanner } from "../components/Banner";
import { ReportBrowser } from "../components/ReportBrowser";
import type { AuthorityView, DegradationCheckPayload, EvidenceStrength } from "../lib/degradationCheck";
import {
  asDegradationCheckPayload,
  authorityOf,
  checkStatus,
  checkSummary,
  degradationLabel,
  directionText,
  evidenceStrength,
  evidenceStrengthLabel,
  evidenceStrengthText,
  metricRows,
  metricStatus,
  statusText,
} from "../lib/degradationCheck";

// Phase 11 degradation checks (apps/worker/degradation.py, ADR-0049): recent metrics vs the
// validation baseline, with each threshold's source. Evidence only — a check never changes
// lifecycle state (ACTIVE -> DEGRADED is a Control Plane transition). Payload types and the pure
// helpers live in src/lib/degradationCheck.ts.

const STATUS_COLOR = { breached: "#b91c1c", missing: "#92400e", within: "#166534" } as const;

type JsonRecord = Record<string, unknown>;

function isRecord(value: unknown): value is JsonRecord {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function fieldText(value: unknown): string | null {
  if (typeof value === "string" || typeof value === "number" || typeof value === "boolean") {
    return String(value);
  }
  return null;
}

function EvidenceField({ label, value }: { label: string; value: unknown }) {
  const text = fieldText(value);
  if (text === null) return null;
  return (
    <p style={{ margin: "4px 0", overflowWrap: "anywhere" }}>
      <strong>{label}:</strong> <code>{text}</code>
    </p>
  );
}

function EvidenceMap({ label, value }: { label: string; value: unknown }) {
  if (!isRecord(value)) return null;
  const entries = Object.entries(value);
  if (entries.length === 0) return <p>{label}: none</p>;
  return (
    <div>
      <strong>{label}</strong>
      <ul>
        {entries.map(([key, entry]) => (
          <li key={key} style={{ overflowWrap: "anywhere" }}>
            <code>{key}</code>: <code>{fieldText(entry) ?? "(unsupported value; see raw evidence)"}</code>
          </li>
        ))}
      </ul>
    </div>
  );
}

function ObservationManifest({ value }: { value: unknown }) {
  if (!isRecord(value)) return null;
  const sources = Array.isArray(value.sources) ? value.sources : [];
  const metrics = isRecord(value.metrics) ? Object.entries(value.metrics) : [];
  return (
    <section aria-label="Recent observation manifest">
      <h4>Recent observation manifest（近期观测清单）</h4>
      <EvidenceField label="format" value={value.format} />
      <EvidenceField label="subject" value={value.subject} />
      <EvidenceField label="profile_ref" value={value.profile_ref} />
      <EvidenceField label="profile_hash" value={value.profile_hash} />
      <EvidenceField label="observation_set_id" value={value.observation_set_id} />
      <EvidenceField label="method_id" value={value.method_id} />
      {isRecord(value.window) && (
        <>
          <EvidenceField label="window.label" value={value.window.label} />
          <EvidenceField label="window.start" value={value.window.start} />
          <EvidenceField label="window.end" value={value.window.end} />
        </>
      )}
      <h5>Sources（来源声明）</h5>
      {sources.length === 0 ? (
        <p>Manifest contains no readable source entries.</p>
      ) : (
        <table>
          <thead>
            <tr>
              <th>source_id</th>
              <th>source_hash</th>
              <th>event_time</th>
              <th>observed_time</th>
            </tr>
          </thead>
          <tbody>
            {sources.map((source, index) => {
              if (!isRecord(source)) {
                return (
                  <tr key={index}>
                    <td colSpan={4}>Unreadable source entry（see raw evidence）</td>
                  </tr>
                );
              }
              return (
                <tr key={`${fieldText(source.source_id) ?? "source"}-${index}`}>
                  <td><code>{fieldText(source.source_id) ?? "—"}</code></td>
                  <td style={{ overflowWrap: "anywhere" }}>
                    <code>{fieldText(source.source_hash) ?? "—"}</code>
                  </td>
                  <td>
                    <code>{fieldText(source.event_time) ?? "—"}</code>
                  </td>
                  <td>
                    <code>{fieldText(source.observed_time) ?? "—"}</code>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
      <h5>Metrics（清单指标）</h5>
      {metrics.length === 0 ? (
        <p>Manifest contains no readable metrics.</p>
      ) : (
        <table>
          <thead>
            <tr>
              <th>metric</th>
              <th>value</th>
            </tr>
          </thead>
          <tbody>
            {metrics.map(([metric, metricValue]) => (
              <tr key={metric}>
                <td>
                  <code>{metric}</code>
                </td>
                <td>
                  <code>{fieldText(metricValue) ?? "(unsupported value; see raw evidence)"}</code>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}

const STRENGTH_STYLE: Record<EvidenceStrength, { background: string; border: string; color: string }> = {
  authority_resolved: { background: "#ecfdf5", border: "#166534", color: "#166534" },
  authority_unreadable: { background: "#fef3c7", border: "#92400e", color: "#92400e" },
  caller_declared: { background: "#f3f4f6", border: "#4b5563", color: "#374151" },
};

function EvidenceStrengthBadge({ check }: { check: DegradationCheckPayload }) {
  const strength = evidenceStrength(check);
  const style = STRENGTH_STYLE[strength];
  return (
    <div
      role="note"
      data-evidence-strength={strength}
      style={{ background: style.background, border: `1px solid ${style.border}`, padding: "6px 12px", margin: "8px 0" }}
    >
      <strong style={{ color: style.color }}>证据强度：{evidenceStrengthLabel(strength)}</strong>
      <p style={{ margin: "4px 0 0", fontSize: 13 }}>{evidenceStrengthText(check)}</p>
    </div>
  );
}

function shortHash(value: string | null): string {
  return value === null ? "—" : value.length > 16 ? `${value.slice(0, 16)}…` : value;
}

function AuthorityRow({ label, value, full }: { label: string; value: string | null; full?: string | null }) {
  return (
    <tr>
      <th style={{ textAlign: "left", fontWeight: 500 }}>{label}</th>
      <td style={{ overflowWrap: "anywhere" }} title={full ?? undefined}>
        <code>{value ?? "—"}</code>
      </td>
    </tr>
  );
}

function AuthorityPanel({ authority }: { authority: AuthorityView }) {
  const head =
    authority.headRecordCount === null
      ? null
      : `${authority.headRecordCount} records · ${shortHash(authority.headLastRecordHash)}`;
  return (
    <section aria-label="Authority provenance" data-authority-format={authority.format ?? ""}>
      <h4>Authority provenance（权威来源记录，ADR-0098）</h4>
      {authority.problems.length > 0 && (
        <div
          role="note"
          data-authority-problems={authority.problems.length}
          style={{ background: "#fef3c7", border: "1px solid #92400e", color: "#92400e", padding: "6px 12px" }}
        >
          <strong>无法作为权威证据读取：</strong>
          <ul style={{ margin: "4px 0 0" }}>
            {authority.problems.map((problem, index) => (
              <li key={index}>{problem}</li>
            ))}
          </ul>
        </div>
      )}
      <table>
        <tbody>
          <AuthorityRow label="provenance format" value={authority.format} />
          <AuthorityRow label="lifecycle head" value={head} full={authority.headLastRecordHash} />
          <AuthorityRow
            label="lifecycle anchor"
            value={
              authority.anchor === "present"
                ? "present（已用外部 anchor 核验）"
                : authority.anchor === "absent"
                  ? "absent（未核验：无法检测尾部回滚）"
                  : authority.anchor
            }
          />
          <AuthorityRow label="lifecycle history_hash" value={shortHash(authority.historyHash)} full={authority.historyHash} />
          <AuthorityRow
            label="replayed records"
            value={authority.recordHashCount === null ? null : String(authority.recordHashCount)}
          />
          <AuthorityRow label="source dataset_id" value={authority.datasetId} />
          <AuthorityRow label="source manifest_hash" value={shortHash(authority.manifestHash)} full={authority.manifestHash} />
          <AuthorityRow label="source data_type / snapshot" value={
            authority.dataType === null && authority.snapshotId === null
              ? null
              : `${authority.dataType ?? "—"} / ${authority.snapshotId ?? "—"}`
          } />
          <AuthorityRow label="baseline run_id" value={authority.baselineRunId} />
          <AuthorityRow
            label="baseline manifest_hash"
            value={shortHash(authority.baselineManifestHash)}
            full={authority.baselineManifestHash}
          />
          <AuthorityRow label="baseline_set_hash" value={shortHash(authority.baselineSetHash)} full={authority.baselineSetHash} />
          <AuthorityRow label="metric registry" value={authority.metricRegistry} />
          <AuthorityRow label="as_of" value={authority.asOf} />
          <AuthorityRow
            label="window"
            value={
              authority.windowStart === null && authority.windowEnd === null
                ? null
                : `[${authority.windowStart ?? "—"}, ${authority.windowEnd ?? "—"})`
            }
          />
          <AuthorityRow
            label="recent_manifest_hash"
            value={shortHash(authority.recentManifestHash)}
            full={authority.recentManifestHash}
          />
        </tbody>
      </table>
      <h5>Metric definitions（闭集 metric 定义）</h5>
      {authority.definitions.length === 0 ? (
        <p>没有可读取的 metric 定义。</p>
      ) : (
        <table>
          <thead>
            <tr>
              <th>metric</th>
              <th>definition（id@version）</th>
              <th>gate</th>
              <th>window_scope</th>
            </tr>
          </thead>
          <tbody>
            {authority.definitions.map((row, index) => (
              <tr key={`${row.definition}-${row.gateId ?? ""}-${index}`}>
                <td><code>{row.metric}</code></td>
                <td><code>{row.definition}</code></td>
                <td><code>{row.gateId ?? "—"}</code></td>
                <td>{row.windowScope ?? "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}

function DegradationProvenance({ check }: { check: DegradationCheckPayload | null }) {
  if (check === null || check.schema_version !== "1.1.0" || !isRecord(check.evidence)) return null;
  const evidence = check.evidence;
  const authority = authorityOf(check);
  const resolved = evidenceStrength(check) === "authority_resolved";
  return (
    <section
      aria-label="Hash-bound provenance"
      style={{ marginTop: 20, borderTop: "1px solid #ccc", paddingTop: 12 }}
    >
      <h3>Hash-bound provenance（哈希绑定的来源记录）</h3>
      <div
        role="note"
        style={{ background: "#eff6ff", border: "1px solid #1d4ed8", padding: "8px 12px" }}
      >
        {resolved
          ? "生命周期与近期指标经 ADR-0098 authority 解析（见下方 Authority provenance），全部内容随报告参与哈希绑定；authority 解析说明输入的来源，不等于验证通过，也不改变生命周期。"
          : "这些内容由调用方声明，并随报告内容参与哈希绑定；哈希绑定只证明内容一致性，不认证外部来源、source ID 或指标聚合的真实性。"}
      </div>
      {authority !== null && <AuthorityPanel authority={authority} />}
      <h4>Profile freeze（Profile 冻结登记）</h4>
      <EvidenceField label="profile_ref" value={evidence.profile_ref} />
      <EvidenceField label="profile_hash" value={evidence.profile_hash} />
      <EvidenceField label="freeze_record_id" value={evidence.profile_freeze_id} />
      <EvidenceField
        label="calibration_report_hash"
        value={evidence.profile_freeze_calibration_report_hash}
      />
      <EvidenceField label="anchor_snapshot.length" value={evidence.profile_freeze_anchor_length} />
      <EvidenceField
        label="anchor_snapshot.head_hash"
        value={evidence.profile_freeze_anchor_head_hash}
      />

      <h4>Baseline and lifecycle（基线与生命周期）</h4>
      <EvidenceField label="validation_report_hash" value={evidence.validation_report_hash} />
      <EvidenceMap label="baseline_gate_ids" value={evidence.baseline_gate_ids} />
      <EvidenceField label="lifecycle_history_hash" value={evidence.lifecycle_history_hash} />
      <EvidenceField label="lifecycle_scope" value={evidence.lifecycle_scope} />

      <h4>Recent observation binding（近期观测绑定）</h4>
      <EvidenceField label="window_start" value={evidence.window_start} />
      <EvidenceField label="window_end" value={evidence.window_end} />
      <EvidenceField label="metric_method_id" value={evidence.metric_method_id} />
      <EvidenceField label="recent_metrics_scope" value={evidence.recent_metrics_scope} />
      <EvidenceField label="observation_set_id" value={evidence.recent_observation_set_id} />
      <EvidenceField label="observation_set_hash" value={evidence.recent_observation_set_hash} />
      <ObservationManifest value={evidence.recent_observation_manifest} />

      <details>
        <summary>完整 evidence JSON（包括未知的扩展字段）</summary>
        <pre style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>
          {JSON.stringify(evidence, null, 2)}
        </pre>
      </details>
    </section>
  );
}

function CheckDetail({ envelope }: { envelope: ReportEnvelope }) {
  const check = asDegradationCheckPayload(envelope.payload);
  if (check === null) {
    return <pre>{JSON.stringify(envelope.payload, null, 2)}</pre>;
  }
  const status = checkStatus(check);
  return (
    <>
      <p>
        subject <strong>{check.subject}</strong> · window {check.window} · check_hash{" "}
        <code>{`${check.check_hash.slice(0, 12)}…`}</code>
      </p>
      <EvidenceStrengthBadge check={check} />
      {status === "insufficient_evidence" ? (
        <div
          role="note"
          data-check-status="insufficient_evidence"
          style={{ background: "#fef3c7", border: "1px solid #92400e", color: "#92400e", padding: "8px 12px" }}
        >
          <strong>INSUFFICIENT EVIDENCE（证据不足）</strong>
          <p style={{ margin: "4px 0 0" }}>{checkSummary(check)}</p>
          <p style={{ margin: "4px 0 0", fontSize: 13 }}>
            没有任何指标的近期值可与基线比较：这次检查既不能说明退化，也不能说明健康（degraded = false 只表示没有
            breach）。
          </p>
        </div>
      ) : (
        <p>
          <strong>{checkSummary(check)}</strong>
        </p>
      )}
      <p style={{ color: "#555", fontSize: 13 }}>
        {check.status} — {check.note}
      </p>
      <table>
        <thead>
          <tr>
            <th>metric</th>
            <th>status</th>
            <th>direction</th>
            <th>baseline</th>
            <th>recent</th>
            <th>decline</th>
            <th>max_decline</th>
            <th>threshold source</th>
          </tr>
        </thead>
        <tbody>
          {metricRows(check).map((metric) => {
            const status = metricStatus(metric);
            return (
              <tr key={metric.metric}>
                <td>{metric.metric}</td>
                <td style={{ color: STATUS_COLOR[status], fontWeight: 600 }}>{statusText(status)}</td>
                <td>{directionText(metric.direction)}</td>
                <td>{metric.baseline}</td>
                <td>{metric.recent ?? "—"}</td>
                <td>{metric.decline ?? "—"}</td>
                <td>{metric.max_decline}</td>
                <td>
                  <code>{metric.threshold_source}</code>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
      <DegradationProvenance check={check} />
    </>
  );
}

function checkLabel(report: ReportEnvelope): string {
  const check = asDegradationCheckPayload(report.payload);
  return check !== null ? degradationLabel(check) : report.id;
}

export function DegradationChecks() {
  return (
    <section>
      <SimulatedBanner />
      <h2>Degradation Checks（退化检查）</h2>
      <p style={{ color: "#555", fontSize: 14 }}>
        近期指标与验证基线的对比；阈值只来自所列来源（Validation Profile 或显式映射）。只作证据：不改变生命周期状态，
        缺少近期值表示证据不足而不是健康（全部指标都缺少时整次检查显示为 INSUFFICIENT EVIDENCE）；每份检查标出证据强度：带
        ADR-0098 authority 记录的为权威解析，其余（含所有旧报告）为调用方声明；没有任何下单、账户或转账能力（H10）。
      </p>
      <ReportBrowser
        kind="degradation_check"
        empty="（无退化检查报告 — 未配置报告目录或目录为空）"
        prompt="选择一份检查查看每个指标。"
        label={checkLabel}
        renderDetail={(detail) => <CheckDetail envelope={detail} />}
        listWidth={300}
      />
    </section>
  );
}
