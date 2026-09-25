import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The console only talks to apps/api through the OpenAPI-generated types (apps/web README).
export default defineConfig({
  plugins: [react()],
  server: { proxy: { "/api": { target: "http://127.0.0.1:8000", rewrite: (p) => p.replace(/^\/api/, "") } } },
});
