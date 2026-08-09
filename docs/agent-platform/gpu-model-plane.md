# 私网 GPU 模型平面设计与发布门禁

> 对应 #217 的 AI-P1-01。本文件只定义目标架构和验收门禁，不代表 GPU0 或生产 Tailnet 已完成变更。

## 1. 解决的问题与依赖

GPU0 只承担模型推理，生产端继续掌握身份、权限、任务、数据和审计。它依赖 #219 的
Capability/Egress Policy 和 #223 的 Durable Task；下游被 Text2SQL 规划、补库解释和模板清洗
提案调用。

```mermaid
flowchart LR
  B["浏览器"] --> P["生产控制面"]
  P --> C["Capability / Task / Evidence"]
  C --> G["Tailnet 单端口 Agent Gateway"]
  G --> V["127.0.0.1 vLLM"]
  C --> R["生产侧只读工具网关"]
  R --> D["业务数据库与 Artifact Store"]
```

模型平面永远不持有生产数据库、对象存储、用户 Token、SSH 或业务 API 凭据，也不挂载生产
文件目录。业务数据只能由生产侧先做权限交集、字段最小化和预算裁剪，再作为当前 Task 的
短生命周期上下文发给模型。

## 2. 网络边界

- 生产主机加入现有 Tailnet，使用独立机器身份标签；不开 Funnel、出口节点、子网路由或
  Tailscale Serve 公网入口。
- GPU 节点使用独立服务标签。Tailnet Grants 只允许 `production -> gpu-gateway:tcp/9443`；
  不授予 `gpu -> production` 新建连接，也不授予 GPU 访问数据库、SSH 或 Artifact 端口。
- Agent Gateway 只绑定 Tailscale 地址的单端口；vLLM 只绑定 `127.0.0.1`。主机防火墙同时
  拒绝公网和 LAN 到 Gateway/vLLM/分布式通信端口，避免仅依赖一层 ACL。
- `private` Provider 不是自由文本标签。生产配置必须把规范化 Gateway origin 放入精确
  allowlist，带 userinfo、query、fragment、重定向或未列入 origin 的地址一律拒绝。
- 初次接管只使用专用 SSH Key，并在带外核对主机指纹。口令不进入命令行、环境变量、仓库、
  Issue、日志或自动化。

Tailscale 官方对新配置推荐使用 deny-by-default 的 Grants；标签先由 `tagOwners` 明确授权，
再按来源、目标和端口授予最小访问：

- https://tailscale.com/docs/features/access-control/grants
- https://tailscale.com/docs/reference/syntax/grants

## 3. Gateway 协议

生产侧只调用项目自有、窄化的 Gateway，不把 vLLM OpenAI-compatible API 直接暴露给生产网段。

允许的请求字段固定为：

```text
request_id, task_id, step_id, model_profile
messages[], allowed_tool_schemas[], max_input_tokens, max_output_tokens
deadline, response_schema_version
```

禁止任意 URL、文件路径、模型路径、LoRA、chat template、正则 structured output、插件、MCP、
代码解释器和服务端工具选择参数。Gateway 重建 vLLM 请求，不透明透传未知字段。

每个请求同时满足：

1. 双向 TLS 机器身份；
2. 生产签发的短期 JWT，固定 `iss/aud/sub/task_id/step_id/scope/iat/nbf/exp/jti`；
3. `jti` 防重放，Task/Step 与生产账本一致；
4. 请求体字节、消息数、上下文 Token、输出 Token、并发和 wall-clock 预算；
5. 响应只返回结构化 completion、usage/usage_unknown、模型快照和安全错误码。

日志只记录 ID、模型 profile、Token/字节计数、排队与执行耗时、状态码；不记录 prompt、tool
结果、模型正文、Authorization、证书内容或客户字段。

## 4. vLLM 隔离

- 使用非 root、只读根文件系统、最小 Linux capabilities、私有 cache/model 目录；不挂 Docker
  socket、宿主 SSH、生产目录或通用共享盘。
- 固定镜像 digest、vLLM 版本、模型 revision 和 tokenizer/chat-template hash；上线前逐条核对该
  版本的 GitHub Security Advisories，不允许浮动 `latest`。
- 保持 `trust_remote_code=false`，不加载运行时插件，不启用 demo tools、MCP、browser、Python
  code interpreter 或 gRPC；不接受调用方提供 chat template。
- vLLM 仍配置独立 API key，但其只作为纵深防御。官方说明 API key 只覆盖部分路径，因此
  `127.0.0.1` 绑定、Gateway allowlist 和防火墙不可省略。
- 限制单请求 prompt、batch、并行序列、最大模型长度和并发；GPU OOM 后由 supervisor 拉起，
  不在生产 API 线程内无限重试。

参考：

- https://docs.vllm.ai/en/latest/usage/security/
- https://docs.vllm.ai/en/latest/serving/online_serving/openai_compatible_server/
- https://github.com/vllm-project/vllm/security/advisories

## 5. 数据出境和模型选择

模型 profile 由服务端版本化，至少包含 provider origin、trust zone、模型/revision、上下文上限、
支持的结构化输出、允许 sensitivity 和保留策略。Task 同时保存静态 Capability fingerprint 与
运行时 Provider policy fingerprint，恢复时不一致即暂停并重新授权。

在读取 GPU 硬件、驱动、显存、磁盘和现有运行时之前不指定模型。候选模型必须通过同一组
中文业务黄金样本：工具选择、Typed Query IR、补库解释、模板映射、JSON schema 遵循、提示
注入拒绝、长表格上下文、延迟和显存峰值。准确率、规则越权率和结构化输出成功率优先于参数量。

## 6. 故障与降级

- 生产侧 Provider client 使用连接/首 Token/总时限、并发舱壁和指数退避；Task 模式关闭 SDK
  隐式重试，由 #223 账本统一计数。
- 连续失败、超时或队列饱和触发 circuit breaker。业务 Web、导入导出和人工流程不得依赖 GPU
  健康；Agent Task 明确进入 `paused_recoverable` 或稳定失败。
- 是否允许切换到获批外部 Provider 由 sensitivity/egress policy 决定；不得为了可用性把私密
  文件或成本数据自动发往公网。
- 回滚先关闭 private Provider profile，再排空/取消任务；保留 Task/Evidence，不删除模型和日志
  来伪造成功。

## 7. 分阶段发布

### Stage 0：只读盘点

核对 SSH 主机指纹、OS/内核、GPU 型号与显存、驱动/CUDA、磁盘、Tailscale 版本与节点身份、
监听端口、容器运行时和现有工作负载。只读，不安装、不重启、不改 ACL。

### Stage 1：离线基准

在 GPU0 本地回环运行固定版本模型和 Gateway，使用脱敏/合成黄金集，验证准确率、结构化输出、
并发、OOM 恢复和恶意请求；不连接生产。

### Stage 2：Tailnet canary

生产以专用标签加入 Tailnet；先启用单个 canary 账号、低敏 Capability 和小并发。验证 Grants、
GPU 无法反向连接生产、Token 重放拒绝、断网/重启恢复和 24 小时观测。

### Stage 3：受控开放

按 sensitivity 分级开放；每个模型/profile 变更重新跑安全 advisory、黄金集和压力门禁。生产合并、
部署和业务验收仍是三个独立状态。

## 8. 验收门禁

- 公网/LAN 无法访问 Gateway 和 vLLM；只有生产机器身份可访问 Gateway 单端口。
- GPU 对生产 API、数据库、SSH、Artifact 端口的主动连接探针全部失败。
- 无凭据、错 audience/scope/task、过期、未来时间、重复 `jti`、超大/未知字段请求全部在推理前拒绝。
- Gateway 重定向、DNS/Host 混淆和错误标记 private 的公网 origin 均 fail closed。
- vLLM 非 `/v1` 路由、插件、工具、gRPC 和 remote code 不可从 Gateway 到达。
- 断开 Tailnet、停止 vLLM、GPU OOM、超时和 429 时，主业务 SLO 不受拖垮，Task 状态可解释、可恢复。
- 日志/指标不含测试哨兵 prompt、客户字段、Token、密钥或原始模型输出。
- 固定镜像/模型 SBOM、许可证、安全公告检查、黄金集和负载报告齐备后，才可进入生产 canary。
