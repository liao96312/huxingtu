import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    // Allow Cloudflare quick-tunnel hostnames so the panorama viewer is reachable
    // from a trycloudflare.com address (Vite blocks unknown Host headers by default).
    allowedHosts: [".trycloudflare.com"],
    proxy: { "/api": "http://127.0.0.1:8000", "/artifacts": "http://127.0.0.1:8000" },
  },
  preview: { proxy: { "/api": "http://127.0.0.1:8000", "/artifacts": "http://127.0.0.1:8000" } },
});
