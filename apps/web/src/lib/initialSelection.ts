// Component-test seam (apps/web README "组件测试"), like `ApiSeedContext` in ./useApi.ts: the id a
// list page starts with selected (ReportBrowser's report, the Jobs page's job) or, for Knowledge
// Search, the terms it starts with already submitted — so a server render can show a detail pane.
// The console never provides it: the default `null` means "nothing selected yet", as before.
import { createContext } from "react";

export const InitialSelectionContext = createContext<string | null>(null);
