import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// Served by FastAPI under /app (see ab_dashboard/main.py). In development, API calls are
// proxied to the backend on :8000, so the browser sees one origin either way.
const API = "http://127.0.0.1:8000";
const apiPaths = ["/dunning", "/cash", "/tasks", "/experiments", "/prompts", "/health", "/auth", "/admin"];

export default defineConfig({
  base: "/app/",
  plugins: [react(), tailwindcss()],
  server: {
    proxy: Object.fromEntries(apiPaths.map((p) => [p, API])),
  },
  build: { outDir: "dist", sourcemap: false },
});
