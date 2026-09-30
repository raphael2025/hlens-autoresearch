// The list + detail layout every report page shares: GET /reports/{kind} (with its invalid files
// shown as a warning) on the left, GET /reports/{kind}/{id} of the selected report on the right,
// each with explicit loading / empty / error states (src/components/States.tsx).
import { useContext, useState, type CSSProperties, type ReactNode } from "react";
import { getReport, listReports, type ReportEnvelope, type ReportKind } from "../api";
import { InitialSelectionContext } from "../lib/initialSelection";
import { inspectReportDTO } from "../lib/reportDto";
import { useApi } from "../lib/useApi";
import { AsyncView, InvalidReports } from "./States";
import "./ReportBrowser.css";

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
  const [selectedId, setSelectedId] = useState<string | null>(useContext(InitialSelectionContext));
  const listing = useApi(() => listReports(kind), [kind]);
  const detail = useApi(
    selectedId === null ? null : () => getReport(kind, selectedId),
    [kind, selectedId],
  );

  return (
    <>
      {listing.status === "ok" && <InvalidReports invalid={listing.data.invalid} />}
      <div className="report-browser" style={{ "--report-list-width": `${listWidth}px` } as CSSProperties}>
        <div className="report-browser__list">
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
        <div className="report-browser__detail">
          <AsyncView state={detail} what="报告详情" idle={<p>{prompt}</p>}>
            {(report) => {
              const dto = inspectReportDTO(report);
              if (dto.status !== "supported") {
                const message = dto.status === "unknown-version"
                  ? `未知 DTO 版本 ${dto.version}；仅以只读原始 JSON 展示。`
                  : `报告 DTO 无效（${dto.version}）：${dto.reason}；仅以只读原始 JSON 展示。`;
                return <><p role="status">{message}</p><pre>{JSON.stringify(report.payload, null, 2)}</pre></>;
              }
              return renderDetail(report);
            }}
          </AsyncView>
        </div>
      </div>
    </>
  );
}
