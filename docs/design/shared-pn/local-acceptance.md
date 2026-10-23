# 双站隔离联网验收

本环境只验证候选应用之间的真实网络、账户授权、数据库事务及浏览器交互。它不读取生产库或生产凭据，不调用 OSS、FC 或模型，也不代表用户验收或生产放行。

## 边界与重建

`scripts/shared-pn-local/configure.py` 接收两个候选 checkout 路径，生成独立数据库名、随机合成账号密码/密钥及短期 localhost TLS 证书。配置和私钥放仓库外，仅当前用户可读，不提交 Git。`service.py` 不加载 `.env`，拒绝非 loopback 或不带专属前缀的数据库，使用真实账号、Grant、Cerbos 及 HTTPS 委托。Shop 的存储仅用本地 MemoryStorage；媒体能力关闭。

准备独占 PostgreSQL 容器或数据库服务，限制为 loopback；本轮使用 PG16、2 CPU、1 GiB，端口 32785。依次执行（各 Python 来自对应仓库已同步的 `.venv`）：

```bash
python3 scripts/shared-pn-local/configure.py /tmp/pn-network-fixture --itsp /path/to/itsp --shop /path/to/shop --postgres-port 32785
/path/to/itsp/backend/.venv/bin/python scripts/shared-pn-local/service.py itsp prepare /tmp/pn-network-fixture/config.json
/path/to/shop/backend/.venv/bin/python scripts/shared-pn-local/service.py shop prepare /tmp/pn-network-fixture/config.json
```

固定版本 Cerbos 使用 ITSP `deploy/cerbos/policies`，只监听 HTTP `127.0.0.1:28492`、gRPC `127.0.0.1:28493`；禁用 telemetry、admin API、explorer 与 payload 日志。服务分别启动：

```bash
/path/to/itsp/backend/.venv/bin/python scripts/shared-pn-local/service.py itsp serve /tmp/pn-network-fixture/config.json
/path/to/shop/backend/.venv/bin/python scripts/shared-pn-local/service.py shop serve /tmp/pn-network-fixture/config.json
/path/to/itsp/backend/.venv/bin/python scripts/shared-pn-local/verify.py /tmp/pn-network-fixture/config.json
```

启动实际页面时，ITSP 前端设置 `NODE_EXTRA_CA_CERTS=/tmp/pn-network-fixture/localhost.pem`、`VITE_API_TARGET=https://127.0.0.1:28481`、`VITE_PORT=28479`；Shop 设置 `VITE_API_PROXY_TARGET=http://127.0.0.1:28482`，监听 `127.0.0.1:28480`。使用合成 `pn-local-editor` 账号，密码只在本地配置中读取。不要复用生产登录信息。固定端口已有服务时先核对进程归属，不自动终止未知进程。

`prepare` 为该合成环境建立本地共享切换记录，既不宣称真实迁移已完成，也不作为上线操作示例。脚本每次配置创建独立数据库；重复 `verify` 创建不同测试 PN。停止时只关闭本次启动的服务，保留数据库和日志用于复核；不要删除不属于本次环境的数据。

## 自动联网检查

`verify.py` 的报告记录于配置目录 `network-report.json`，不包含 token 或密码。

1. 两个应用通过真实本地账号登录。
2. Shop 建立一个权威 PN 与精确绑定的零库存本地档案。
3. 八个并发页面请求独立授权并完成。
4. Shop 写入即能通过 ITSP 读取。
5. 同一幂等键重放原审计及应用版本。
6. 同键不同请求体返回冲突。
7. 同字段并发一成功、一冲突。
8. 不同字段并发均保留。
9. 好坏件通用文案分别维护版本。
10. JSON 自定义参数保留 null、数字、布尔、列表和文本。
11. 两端审计指向同一主账号并区分客户端来源。
12. 当前撤权能阻止原写入回执重放，随后恢复合成授权。
13. 共享写入不改变原本地库存关联。

## 固定构建浏览器证据

2026-10-23 20:36–20:40 UTC，在 Vite preview 提供的生产构建上重新核验：ITSP `45e6959`、Shop `115a1eb`；后续修正测试夹具与库存导入，不改变该页面链路。使用合成 PN `NET-79DA886C`（共享 part 5、Shop UUID `23c91364-2fd6-432e-b75a-de5cc1dd1e54`）。

- ITSP 保存描述，Shop 刷新后读到同值；Shop 再保存，ITSP 刷新后的文本框值相同。
- 两端审计表同时显示这两次修改；主体为同一映射账号，入口分别为 `itsp`、`shop`，来源为 `manual`。
- Shop 390 × 844 移动布局自动进入 `/m/pn/...`，保留商品资料、好坏件和图库入口。
- 最终页面会话 console 检查两站均 0 errors / 0 warnings。开发期 HMR 日志单独保留，不混入固定构建证据。

截图在 PM `output/playwright/`：`itsp-stable-save.png`、`itsp-stable-audit.png`、`shop-stable-audit.png`、`shop-stable-mobile.png`。真实 HTTP 联调报告 `network-report.json` 的 13 项全部通过。

代理浏览器用于开发验证，不替代用户点按验收；生产数据副本守恒和真实 OSS 对象核验也未由本轮合成数据测试代替。图库提供方在该环境关闭，页面明确提示，未调用真实 OSS、FC 或模型。
