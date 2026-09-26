import { useContext, useMemo, useState } from "react";
import { getJob, listJobs, type JobList, type JobStatus, type JobView } from "../api";
import { SimulatedBanner } from "../components/Banner";
import { AsyncView } from "../components/States";
import { JOB_STATUS_LABELS, jobRow, jobRows, statusCounts } from "../lib/jobs";
import { InitialSelectionContext } from "../lib/initialSelection";
import { useApi } from "../lib/useApi";

// Read-only view of the worker's verified results journal (GET /jobs, GET /jobs/{job_id}).
// No submit / retry / cancel control exists here (ADR-0048 read-only console). A tampered or
// unverifiable journal is an explicit 500 from apps/api, shown as the error state — never
// partial data; an unconfigured journal is a 503.

const FILTERS: (JobStatus | "all")[] = ["all", "succeeded", "failed", "interrupted"];

function JobsTable({
  list,
  selectedId,
  onSelect,
}: {
  list: JobList;
  selectedId: string | null;
  onSelect: (jobId: string) => void;
}) {
  const [filter, setFilter] = useState<JobStatus | "all">("all");
  const counts = useMemo(() => statusCounts(list), [list]);
  const rows = useMemo(() => jobRows(list, filter), [list, filter]);

  return (
    <>
      <p style={{ color: "#555", fontSize: 13 }}>
        {list.lines} 行日志 · head_hash <code>{list.head_hash.slice(0, 12)}…</code> · succeeded{" "}
        {counts.succeeded} · failed {counts.failed} · interrupted {counts.interrupted}
      </p>
      <div style={{ display: "flex", gap: 8, marginBottom: 8 }}>
        {FILTERS.map((option) => (
          <button
            key={option}
            onClick={() => setFilter(option)}
            style={{ fontWeight: option === filter ? 700 : 400 }}
            aria-pressed={option === filter}
          >
            {option}
          </button>
        ))}
      </div>
      {rows.length === 0 ? (
        <p style={{ color: "#555" }}>（该状态下没有任务）</p>
      ) : (
        <table>
          <thead>
            <tr>
              <th>job</th>
              <th>name</th>
              <th>status</th>
              <th>attempts</th>
              <th>result / error</th>
              <th>starts / reruns</th>
              <th>lines</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr key={row.jobId}>
                <td>
                  <button
                    onClick={() => onSelect(row.jobId)}
                    style={{ fontWeight: row.jobId === selectedId ? 700 : 400 }}
                    aria-current={row.jobId === selectedId}
                  >
                    <code>{row.shortId}</code>
                  </button>
                </td>
                <td>{row.name}</td>
                <td style={row.status !== "succeeded" ? { color: "crimson" } : undefined}>{row.status}</td>
                <td>{row.attempts}</td>
                <td>
                  <code>{row.outcome}</code>
                </td>
                <td>
                  {row.starts} / {row.reruns}
                </td>
                <td>{row.lines}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </>
  );
}

function JobDetail({ job }: { job: JobView }) {
  const row = jobRow(job);
  return (
    <>
      <h3>
        {job.name} <code>{row.shortId}…</code>
      </h3>
      <p>
        状态：<strong>{JOB_STATUS_LABELS[job.status]}</strong> · attempts {row.attempts} · starts{" "}
        {job.starts} · reruns {job.reruns} · 日志行 {row.lines}
      </p>
      <p style={{ fontSize: 13 }}>
        job_id <code>{job.job_id}</code>
      </p>
      <h4>params</h4>
      <pre>{JSON.stringify(job.params, null, 2)}</pre>
      {job.status === "failed" && (
        <>
          <h4>error</h4>
          <pre style={{ color: "crimson" }}>{job.error ?? "—"}</pre>
        </>
      )}
      {job.status === "succeeded" && (
        <>
          <h4>result</h4>
          <pre>{JSON.stringify(job.result, null, 2)}</pre>
        </>
      )}
      {job.status === "interrupted" && (
        <p style={{ color: "#664d03" }}>
          已开始、尚无结果：可能仍在运行，或进程中途死亡、等待人工审查（未声明幂等的处理器不会自动重跑）。
        </p>
      )}
    </>
  );
}

export function Jobs() {
  const [selectedId, setSelectedId] = useState<string | null>(useContext(InitialSelectionContext));
  const list = useApi(listJobs, []);
  const detail = useApi(selectedId === null ? null : () => getJob(selectedId), [selectedId]);

  return (
    <section>
      <SimulatedBanner />
      <h2>任务（Jobs，只读）</h2>
      <AsyncView
        state={list}
        what="任务列表"
        isEmpty={(data) => data.jobs.length === 0}
        empty="（结果日志中还没有任务）"
      >
        {(data) => <JobsTable list={data} selectedId={selectedId} onSelect={setSelectedId} />}
      </AsyncView>
      <AsyncView state={detail} what="任务详情" idle={<p style={{ color: "#555" }}>选择一个任务查看详情。</p>}>
        {(job) => <JobDetail job={job} />}
      </AsyncView>
    </section>
  );
}
