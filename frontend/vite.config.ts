import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// MUSHA_API points the dev proxy at another API port, e.g. MUSHA_API=8101 npm run dev -- --port 5181
const api = `http://127.0.0.1:${process.env.MUSHA_API || "8000"}`;

export default defineConfig({
  plugins: [react()],
  // ws: the shop call is a WebSocket under /api
  server: { port: 5174, proxy: { "/api": { target: api, ws: true } } },
  // AG Grid is one chunk of about 810 kB that only the Week and Books tabs load; every other chunk stays small.
  build: { chunkSizeWarningLimit: 900 },
});
