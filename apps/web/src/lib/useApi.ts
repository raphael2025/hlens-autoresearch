// One small hook for every page's request: explicit loading / error / ok states, so no page
// silently swallows an error or shows an empty table while a request is still in flight.
// (The state type and its pure helpers live in ./loadState.ts, tested with `node --test`.)
import { createContext, useContext, useEffect, useState, type DependencyList } from "react";
import { describeError } from "./errors";
import type { LoadState } from "./loadState";

/**
 * Component-test seam (apps/web README "组件测试"): a provider may hand `useApi` the *initial*
 * state of a request, so a server render (`renderToStaticMarkup`, which never runs effects) can
 * show the loaded / empty / error states (src/components/render.test-util.tsx). The console never
 * provides it: without a provider every request starts `loading` and is fetched by the effect,
 * exactly as before.
 */
export type ApiSeed = (load: () => Promise<unknown>) => LoadState<unknown> | undefined;
export const ApiSeedContext = createContext<ApiSeed | null>(null);

/**
 * Runs `load` whenever `deps` change (`null` = nothing to load: the `idle` state, e.g. no report
 * selected yet). A response that arrives after the deps changed or the page unmounted is dropped.
 */
export function useApi<T>(load: (() => Promise<T>) | null, deps: DependencyList): LoadState<T> {
  const seed = useContext(ApiSeedContext);
  const [state, setState] = useState<LoadState<T>>(() => {
    if (load === null) return { status: "idle" };
    return (seed?.(load) as LoadState<T> | undefined) ?? { status: "loading" };
  });

  useEffect(() => {
    if (load === null) {
      setState({ status: "idle" });
      return;
    }
    let current = true;
    setState({ status: "loading" });
    load().then(
      (data) => {
        if (current) setState({ status: "ok", data });
      },
      (error: unknown) => {
        if (current) setState({ status: "error", error: describeError(error) });
      },
    );
    return () => {
      current = false;
    };
    // `load` is a fresh closure every render; `deps` says when it actually changes.
  }, deps);

  return state;
}

/** Turns one request into its settled state, so several can run together without one failure
 * hiding the others (the Dashboard's per-kind report counts). */
export function settle<T>(promise: Promise<T>): Promise<LoadState<T>> {
  return promise.then(
    (data): LoadState<T> => ({ status: "ok", data }),
    (error: unknown): LoadState<T> => ({ status: "error", error: describeError(error) }),
  );
}
