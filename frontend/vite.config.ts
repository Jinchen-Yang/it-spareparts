import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// VITE_PORT / VITE_API_TARGET 允许多实例并行开发（如 worktree 验证）覆盖默认值
export default defineConfig({
  plugins: [react()],
  server: {
    port: Number(process.env.VITE_PORT) || 5173,
    proxy: {
      "/api": {
        target: process.env.VITE_API_TARGET || "http://localhost:8000",
        changeOrigin: true,
      },
    },
  },
});
