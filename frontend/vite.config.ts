import react from "@vitejs/plugin-react";
import { fileURLToPath } from "node:url";
import { defineConfig, loadEnv } from "vite";

export default defineConfig(({ mode }) => {
  // The dev server signs gateway calls with the first key in backend/.env, so the browser never holds one.
  // ponytail: dev server only. A production build needs its own proxy, or sign-in again.
  const keys = loadEnv(mode, fileURLToPath(new URL("../backend", import.meta.url)), "").GATEWAY_KEYS;
  if (!keys) throw new Error("Set GATEWAY_KEYS in backend/.env");
  const gateway = { target: "http://127.0.0.1:8000", headers: { "x-api-key": keys.split(",")[0].split(":")[0].trim() } };

  return {
    plugins: [react()],
    server: { proxy: { "/v1": gateway, "/usage": gateway } },
  };
});
