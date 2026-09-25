import { useEffect, useState } from "react";
import { getLifecycleTransitions } from "../api";
import { SimulatedBanner } from "../components/Banner";

export function Lifecycle() {
  const [transitions, setTransitions] = useState<Array<{ from: string; to: string }>>([]);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    getLifecycleTransitions()
      .then(setTransitions)
      .catch((err) => setError(String(err)));
  }, []);

  return (
    <section>
      <SimulatedBanner />
      <h2>生命周期转移（Lifecycle Transitions）</h2>
      {error && <p style={{ color: "crimson" }}>{error}</p>}
      <table>
        <thead>
          <tr>
            <th>from</th>
            <th>to</th>
          </tr>
        </thead>
        <tbody>
          {transitions.map((t) => (
            <tr key={`${t.from}->${t.to}`}>
              <td>{t.from}</td>
              <td>{t.to}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}
