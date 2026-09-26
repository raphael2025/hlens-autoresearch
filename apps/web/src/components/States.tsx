// Shared loading / empty / error / invalid-file states (src/lib/useApi.ts): every page renders
// its requests through these, so an in-flight request, a failure and "nothing there" always look
// different and no error is swallowed.
import type { ReactNode } from "react";
import type { InvalidReport } from "../api";
import { invalidWarnings, type LoadState } from "../lib/loadState";

export function Loading({ what = "数据" }: { what?: string }) {
  return (
    <p role="status" style={{ color: "#555" }}>
      加载{what}中…
    </p>
  );
}

export function ErrorState({ message }: { message: string }) {
  return (
    <p role="alert" style={{ color: "crimson", whiteSpace: "pre-wrap" }}>
      {message}
    </p>
  );
}

export function Empty({ children }: { children: ReactNode }) {
  return <p style={{ color: "#555" }}>{children}</p>;
}

/** Report files apps/api found but could not serve (`ReportListing.invalid`), as a warning. */
export function InvalidReports({ invalid }: { invalid: readonly InvalidReport[] }) {
  if (invalid.length === 0) return null;
  return (
    <div
      role="alert"
      style={{
        background: "#fff3cd",
        color: "#664d03",
        border: "1px solid #ffe69c",
        borderRadius: 6,
        padding: "8px 12px",
        margin: "8px 0",
        fontSize: 14,
      }}
    >
      <strong>{invalid.length} 个报告文件无效，未展示：</strong>
      <ul style={{ margin: "4px 0 0" }}>
        {invalidWarnings(invalid).map((line) => (
          <li key={line}>
            <code>{line}</code>
          </li>
        ))}
      </ul>
    </div>
  );
}

/**
 * Renders one request's state: loading, error, empty (when `isEmpty(data)`), or `children(data)`.
 * `idle` (nothing requested yet) renders `idle`.
 */
export function AsyncView<T>({
  state,
  what,
  isEmpty,
  empty,
  idle = null,
  children,
}: {
  state: LoadState<T>;
  what?: string;
  isEmpty?: (data: T) => boolean;
  empty?: ReactNode;
  idle?: ReactNode;
  children: (data: T) => ReactNode;
}) {
  switch (state.status) {
    case "idle":
      return <>{idle}</>;
    case "loading":
      return <Loading what={what} />;
    case "error":
      return <ErrorState message={state.error} />;
    case "ok":
      if (isEmpty !== undefined && isEmpty(state.data)) return <Empty>{empty ?? "（无数据）"}</Empty>;
      return <>{children(state.data)}</>;
  }
}
