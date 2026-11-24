import { defineConfig } from "vite";
import vue from "@vitejs/plugin-vue";

const nodeEnv =
  (globalThis as { process?: { env?: Record<string, string | undefined> } })
    .process?.env ?? {};

const apiProxyTarget =
  nodeEnv.VITE_API_PROXY || nodeEnv.VITE_API_TARGET || "http://127.0.0.1:8000";

export default defineConfig({
  plugins: [vue()],
  resolve: {
    alias: {
      "@": new URL("./src", import.meta.url).pathname,
    },
  },
  server: {
    host: "0.0.0.0",
    port: 5173,
    proxy: {
      "/api": {
        target: apiProxyTarget,
        changeOrigin: true,
      },
      "/healthz": {
        target: apiProxyTarget,
        changeOrigin: true,
      },
    },
  },
  preview: {
    host: "0.0.0.0",
    port: 4173,
    proxy: {
      "/api": {
        target: apiProxyTarget,
        changeOrigin: true,
      },
      "/healthz": {
        target: apiProxyTarget,
        changeOrigin: true,
      },
    },
  },
});
