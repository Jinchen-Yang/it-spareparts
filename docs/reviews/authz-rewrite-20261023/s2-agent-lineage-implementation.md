# S2 Agent / provenance 实现与独立 B1 证据

本包作者为 authz_phase_review；Agent 的根代理交叉审查、修复及跨模型复验另见 `s2-root-cross-review.md` 与 `s2-f3-implementation-and-cross-review.md`，不将作者测试当作独立复审。已纳入 S2 分支，最终阶段 CI 待完成；未部署、未调用外部 LLM。

## Agent 交付边界

- `authz/provenance.py` 的 `AccessCollector` 从共享授权服务实际返回的不可变 `ResourceAccess` 收集依赖；保存 principal、epoch、catalog、resource、actions、Grant version。校验行为本身不重复污染 collector。对象授权来源留给 S3，不以 epoch 伪造关系 revision。
- Agent worker 在真正执行工具的线程安装 ContextVar；生成器每次推进安装、yield 前恢复，兼容 StreamingResponse 跨复制上下文推进。每次工具、外部 LLM 发送与返回 chunk、发布、队列消费、保存、历史回放、文件读取和衍生均重验。数据库锁不跨 LLM 网络等待。
- 两个无状态 chat 在新权威下仅接受 user 输入；sessions 继续使用 `{message}`，由服务端加载可信助手历史。旧无来源助手记录与文件保守失效；null owner 会话不自动按用户名认领。
- ChatSession 稳定 owner ID、ChatMessage JSONB 来源与外部文件 metadata 由服务器保存；新产物发布前调用持久 artifact 哨兵。撤配置不能恢复旧 Agent / 文件旁路。文件下载按 64 KiB 分块重验，生成文件继承已有会话及基文件来源。
- 固定业务工具接入 B1/B3 受保护服务；零授权回调不能代表安全空依赖。S2 未迁移的三个 maintenance 工具显式 unavailable。仅真正 legacy 实例保留原行为。

## 实测

- `test_authz_s2_agent_provenance.py`：23 项真实 PostgreSQL / Cerbos 测试通过。外部 LLM transport 使用合成 provider；数据库、PDP、HTTP、worker、文件与队列为真实路径。
- 新测试与 6 个既有 Agent / chat_store 测试文件组合：67 passed、1 skipped；日志 `/tmp/itsp-authz-s2-agent-combined.log`。另跑 chat hub 既有用例，见 `/tmp/itsp-authz-s2-agent-hub.log`。
- 覆盖 epoch / Grant version 撤销、未知历史与跨用户文件、配置撤回、checkpoint 拒绝覆盖、发送前 / chunk 后 / 工具间 / 队列消费重验、vision 返回重验、真实 HTTP 会话保存回放与文件上传下载、跨 Context 推进、真实 inventory / part 工具来源收集。
- 原 `test_agent_runtime.py` 只有 5 个测试增加标准 db fixture；原断言保留，因为运行时现需读取持久 cutover 下界。

## B1 独立审查

B1 / core 由 root 著作，本代理另写 `test_authz_s2_b1_adversarial.py` 独立验证，15 项全部通过，日志 `/tmp/itsp-authz-s2-b1-concurrency.log`。

覆盖销售动作隔离及拒绝前无事实 SQL、非 boss 显式采购分析授权、供应商过滤侧信道、财务衍生字段与 CSV 投影、库存写后投影与真实操作者审计、设置审计身份单独授权、PDP 故障不写。两连接并发用例持有库存 / 设置领域锁，写请求等待时另一事务撤 epoch 可提交，放锁后请求 401 且业务和审计不变，证明不存在提前 actor SHARE 阻挡撤权及锁后旧身份提交。

此结论限于上述攻击面；不代替全模块覆盖、迁移差分、独立 Agent 复审、完整 CI 或用户验收。
