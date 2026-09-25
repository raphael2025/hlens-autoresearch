import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The console only talks to apps/api through the OpenAPI-generated types (apps/web README).
export default defineConfig({
  plugins: [react()],
  server: { proxy: { "/api": { target: "http://127.0.0.1:8000", rewrite: (p) => p.replace(/^\/api/, "") } } },
  build: {
    rollupOptions: {
      output: {
        // Pages are lazy-loaded (src/App.tsx), which already keeps ECharts out of the entry chunk
        // (apps/web README "Code splitting"). Even the tree-shaken subset ECharts pages import
        // (src/lib/echarts.ts) is still one ~550 kB shared chunk by default because zrender (the
        // renderer ECharts sits on) and echarts/core bundle together; splitting zrender into its
        // own chunk brings every individual chunk under the 500 kB build warning.
        manualChunks(id: string) {
          if (id.includes("node_modules/zrender/")) return "zrender";
          if (id.includes("node_modules/echarts/")) return "echarts";
          return undefined;
        },
      },
    },
  },
});
