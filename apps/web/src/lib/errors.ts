// Error mapping for apps/api answers (apps/api/app.py docstring; apps/api README "错误映射").
// Pure: no React, no fetch — tested by errors.test.ts with `node --test`.
//
// Every error apps/api raises itself has the body `{"detail": "<message>"}` (`ApiError` in the
// OpenAPI document); only FastAPI's own request validation answers 422 with a list `detail`.

/** A non-2xx answer from apps/api, with its status and the server's `detail` message. */
export class ApiRequestError extends Error {
  readonly status: number;
  readonly detail: string;
  readonly path: string;

  constructor(path: string, status: number, detail: string) {
    super(`${path}: HTTP ${status} — ${detail}`);
    this.name = "ApiRequestError";
    this.path = path;
    this.status = status;
    this.detail = detail;
  }
}

/** The `detail` of an error body: the string, the joined validation messages, or the status. */
export function errorDetail(status: number, body: unknown): string {
  if (typeof body === "object" && body !== null && "detail" in body) {
    const detail = (body as { detail: unknown }).detail;
    if (typeof detail === "string" && detail !== "") return detail;
    if (Array.isArray(detail)) {
      const messages = detail
        .map((item) =>
          typeof item === "object" && item !== null && typeof (item as { msg?: unknown }).msg === "string"
            ? (item as { msg: string }).msg
            : null,
        )
        .filter((msg): msg is string => msg !== null);
      if (messages.length > 0) return messages.join("; ");
    }
  }
  return `HTTP ${status}`;
}

// What each status means for this read-only console (never a trading or account state).
const STATUS_LABELS: Record<number, string> = {
  400: "请求无效",
  404: "不存在",
  422: "数据不合法",
  500: "服务端校验失败",
  502: "上游 provider 无法诚实回答",
  503: "后端未配置该数据源",
};

/** One human-readable line for any thrown value (the page's error state shows it verbatim). */
export function describeError(error: unknown): string {
  if (error instanceof ApiRequestError) {
    const label = STATUS_LABELS[error.status] ?? "请求失败";
    return `${label}（HTTP ${error.status}）：${error.detail}`;
  }
  if (error instanceof Error) return `无法连接 API：${error.message}`;
  return `无法连接 API：${String(error)}`;
}
