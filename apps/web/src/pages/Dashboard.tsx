import { getContracts, getHealth, listReports, REPORT_KINDS, type ReportListing } from "../api";
import { SimulatedBanner } from "../components/Banner";
import { AsyncView } from "../components/States";
import { listingSummary, type LoadState } from "../lib/loadState";
import { settle, useApi } from "../lib/useApi";

// Every cell shows its own loading / error state: a failing endpoint is named, never shown as
// "down" or "—" without a reason, and one failing report kind does not hide the others.
export function Dashboard() {
  const health = useApi(getHealth, []);
  const contracts = useApi(getContracts, []);
  const listings = useApi(
    () => Promise.all(REPORT_KINDS.map((kind) => settle(listReports(kind)))),
    [],
  );

  return (
    <section>
      <SimulatedBanner />
      <h2>概览</h2>
      <table>
        <tbody>
          <tr>
            <td>API 状态</td>
            <td>
              <AsyncView state={health} what="健康检查">
                {(body) => `${body.status} (v${body.api_version})`}
              </AsyncView>
            </td>
          </tr>
          <tr>
            <td>已注册契约模型数</td>
            <td>
              <AsyncView state={contracts} what="契约清单">
                {(list) => list.length}
              </AsyncView>
            </td>
          </tr>
          {REPORT_KINDS.map((kind, index) => {
            const cell: LoadState<ReportListing> =
              listings.status === "ok" ? listings.data[index] : listings;
            return (
              <tr key={kind}>
                <td>{kind}</td>
                <td style={cell.status === "error" ? { color: "crimson" } : undefined}>
                  {listingSummary(cell)}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </section>
  );
}
