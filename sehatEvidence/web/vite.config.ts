import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// https://vite.dev/config/
export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    proxy: {
      // Dev mode: python -m api.server runs separately on :8000; this
      // proxy is what lets the frontend call same-origin /api/* paths
      // during `npm run dev` without a CORS round-trip. In production
      // the built assets are served BY api/server.py itself, so /api/*
      // is already same-origin and this proxy is unused.
      "/api": {
        target: "http://127.0.0.1:8000",
        changeOrigin: true,
      },
    },
  },
  build: {
    outDir: "dist",
  },
});
