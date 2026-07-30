# 生产依赖安全门禁

## 适用范围

后端与前端的生产依赖必须在合并前经过漏洞扫描：

- 后端：`uv export --frozen --no-dev --no-hashes --no-emit-project | uvx pip-audit -r /dev/stdin`
- 前端：`npm run audit:prod`

开发依赖不进入生产镜像，按独立维护批次升级；生产依赖的新漏洞必须阻断合并。

## React Router RSC 例外

`GHSA-qwww-vcr4-c8h2` 只影响 React Router 的实验性 RSC Mode。本项目是浏览器端
`BrowserRouter` SPA，不使用 RSC、React Router 服务端包或服务端 Action。

`frontend/scripts/audit-production.mjs` 只在以下条件同时成立时接受这一条告警：

1. 扫描结果没有其他生产依赖漏洞；
2. 漏洞包仅为 `react-router` 与 `react-router-dom`；
3. 唯一 advisory 为 `GHSA-qwww-vcr4-c8h2`；
4. 运行依赖没有 React Router 的 node/dev/serve 包；
5. 前端源码保留 `BrowserRouter`，且未出现 RSC 或服务端处理 API。

任一条件变化都会使 CI 失败。若项目以后引入 SSR/RSC，必须删除此例外并先升级到
官方修复版本。
