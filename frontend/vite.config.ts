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
  build: {
    rollupOptions: {
      output: {
        // 分包策略：react 系与 markdown 生态拆稳定 vendor chunk；antd 留给
        // rollup 按使用边界自动切（Table 等重件只随用到它的懒加载页面下载，
        // 首屏比手动整包 antd 小 ~120KB gzip；代价是 entry 含壳层用到的 antd
        // 部分、每次发版会重下，内网小团队场景下取首屏体积优先）。
        // markdown 生态只列语义明确的前缀/包名，不收编通用工具包——
        // 通用包一旦被首屏依赖共享会把整个 markdown chunk 拖进首屏。
        manualChunks(id: string) {
          if (!id.includes("node_modules")) return undefined;
          if (/node_modules\/(react-markdown|remark-|rehype-|micromark|mdast-|hast-|unist-|unified|vfile|property-information|character-entities|decode-named-character-reference|markdown-table|html-url-attributes)/.test(id)) {
            return "vendor-markdown";
          }
          if (/node_modules\/(react|react-dom|react-router|react-router-dom|scheduler)\//.test(id)) {
            return "vendor-react";
          }
          // echarts/zrender 独立成 chunk：体积大（gzip ~200KB 量级）且只有看板类
          // 页面用到，跟随首个引用它的懒加载页面按需下载，绝不进首屏 entry。
          // 当前无生产引用（图表组件仅 chart-demo/测试引用），此规则为集成期预置。
          if (/node_modules\/(echarts|zrender)\//.test(id)) {
            return "vendor-echarts";
          }
          return undefined;
        },
      },
    },
  },
});
