import { useEffect, useState } from "react";
import { getHealth, getContracts, listReports, REPORT_KINDS, type ReportKind } from "../api";
import { SimulatedBanner } from "../components/Banner";

type Counts = Partial<Record<ReportKind, number>>;

export function Dashboard() {
  const [health, setHealth] = useState<string>("…");
  const [apiVersion, setApiVersion] = useState<string>("");
  const [contractCount, setContractCount] = useState<number | null>(null);
  const [counts, setCounts] = useState<Counts>({});

  useEffect(() => {
    getHealth()
      .then((body) => {
        setHealth(body.status);
        setApiVersion(body.api_version);
      })
      .catch(() => setHealth("down"));
    getContracts()
      .then((list) => setContractCount(list.length))
      .catch(() => setContractCount(null));
    Promise.all(REPORT_KINDS.map((kind) => listReports(kind).then((items) => [kind, items.length] as const)))
      .then((pairs) => setCounts(Object.fromEntries(pairs) as Counts))
      .catch(() => setCounts({}));
  }, []);

  return (
    <section>
      <SimulatedBanner />
      <h2>概览</h2>
      <table>
        <tbody>
          <tr>
            <td>API 状态</td>
            <td>
              {health} {apiVersion && `(v${apiVersion})`}
            </td>
          </tr>
          <tr>
            <td>已注册契约模型数</td>
            <td>{contractCount ?? "—"}</td>
          </tr>
          {REPORT_KINDS.map((kind) => (
            <tr key={kind}>
              <td>{kind}</td>
              <td>{counts[kind] ?? "—"}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}
