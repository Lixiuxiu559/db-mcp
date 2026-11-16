import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
export default defineConfig({
  plugins: [react()],
  base: "./",
  server: { proxy: { "/api": "http://127.0.0.1:8807" } },
  build: { outDir: "../src/db_mcp/web/static", emptyOutDir: true },
});
