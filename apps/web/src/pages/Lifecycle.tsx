import { getLifecycleTransitions } from "../api";
import { SimulatedBanner } from "../components/Banner";
import { AsyncView } from "../components/States";
import { useApi } from "../lib/useApi";

export function Lifecycle() {
  const transitions = useApi(getLifecycleTransitions, []);

  return (
    <section>
      <SimulatedBanner />
      <h2>生命周期转移（Lifecycle Transitions）</h2>
      <AsyncView
        state={transitions}
        what="转移表"
        isEmpty={(list) => list.length === 0}
        empty="（API 未返回任何允许的转移）"
      >
        {(list) => (
          <table>
            <thead>
              <tr>
                <th>from</th>
                <th>to</th>
              </tr>
            </thead>
            <tbody>
              {list.map((t) => (
                <tr key={`${t.from}->${t.to}`}>
                  <td>{t.from}</td>
                  <td>{t.to}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </AsyncView>
    </section>
  );
}
