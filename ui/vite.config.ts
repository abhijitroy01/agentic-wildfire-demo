import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tsconfigPaths from "vite-tsconfig-paths";

export default defineConfig({
  plugins: [react(), tsconfigPaths()],
  build: {
    outDir: "dist",
    sourcemap: process.env.VITE_BUILD_SOURCEMAP === "true",
  },
  server: {
    port: 3000,
    open: true,
    proxy: {
      "/simulation-api": { target: process.env.SIMULATION_LOCAL_URL || "http://127.0.0.1:8000",
        changeOrigin: true, rewrite: path => path.replace(/^\/simulation-api/, "/api") },
    },
  },
  preview: {
    port: 3000,
  },
});
