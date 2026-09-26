// The list + detail layout every report page shares: GET /reports/{kind} (with its invalid files
// shown as a warning) on the left, GET /reports/{kind}/{id} of the selected report on the right,
// each with explicit loading / empty / error states (src/components/States.tsx).
import { useState, type ReactNode } from "react";
import { getReport, listReports, type ReportEnvelope, type ReportKind } from "../api";
import { useApi } from "../lib/useApi";
import { AsyncView, InvalidReports } from "./States";

export function ReportBrowser({
  kind,
  empty,
  prompt,
  label,
  renderDetail,
  listWidth = 240,
}: {
  kind: ReportKind;
  /** Shown when the listing has no well-formed report. */
  empty: ReactNode;
  /** Shown in the detail pane before a report is selected. */
  prompt: ReactNode;
  /** The list button's text for one report (default: its id). */
  label?: (report: ReportEnvelope) => ReactNode;
  renderDetail: (report: ReportEnvelope) => ReactNode;
  listWidth?: number;
}) {
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const listing = useApi(() => listReports(kind), [kind]);
  const detail = useApi(
    selectedId === null ? null : () => getReport(kind, selectedId),
    [kind, selectedId],
  );

  return (
    <>
      {listing.status === "ok" && <InvalidReports invalid={listing.data.invalid} />}
      <div style={{ display: "flex", gap: 24 }}>
        <div style={{ minWidth: listWidth }}>
          <AsyncView
            state={listing}
            what="报告列表"
            isEmpty={(data) => data.reports.length === 0}
            empty={empty}
          >
            {(data) => (
              <ul>
                {data.reports.map((report) => (
                  <li key={report.id}>
                    <button
                      onClick={() => setSelectedId(report.id)}
                      style={{ fontWeight: report.id === selectedId ? 700 : 400 }}
                      aria-current={report.id === selectedId}
                    >
                      {label ? label(report) : report.id}
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </AsyncView>
        </div>
        <div style={{ flex: 1, minWidth: 0 }}>
          <AsyncView state={detail} what="报告详情" idle={<p>{prompt}</p>}>
            {renderDetail}
          </AsyncView>
        </div>
      </div>
    </>
  );
}
