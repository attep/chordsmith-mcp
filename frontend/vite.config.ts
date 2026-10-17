/// <reference types="vitest/config" />
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

export default defineConfig({
  plugins: [react()],
  // host binds 0.0.0.0 so the dev container is reachable through the published port
  server: { port: 5173, strictPort: true, host: true },
  test: { environment: "jsdom" },
});
