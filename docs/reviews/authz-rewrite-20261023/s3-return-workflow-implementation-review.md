# S3 返还链实现与验证

范围：`maintenance_bad_returns.py` 10 个入口、`maintenance_bad_salvage.py` 3 个入口、`maintenance_return_receipts.py` 7 个入口；与执行 registry 的 method/path 集合精确相等。四资源分别为 return_obligation、return_categories、salvage、return_receipt（均带 maintenance_ 前缀）。未处理 return-receipt-import 后台六入口，也未进入 S4。

实现通过 `maintenance_return_authorization.py` 复用 ContextBatch、真实项目关系证据、Cerbos 与稳定产物协议。每个入口仅消费自身登记的动作；授权视图只返回 locator/context，普通读取在同一 RR/read-only 事务校验 precondition 并物化事实。salvage 整块成本与毛利要求本资源 C/F；其他三资源不借用它的字段。响应禁止缓存。

写入先预检，再按 data-change advisory → 幂等键（坏件命令）→ 实际全部旧/新项目 → 对象行锁，捕获锁后完整上下文。领域原语取得其 stock/capacity 等剩余锁并暂存业务修改，最后锁当前 actor、重新裁决完整原目标集合及 precondition，才提交。写入自行造成的 version/父关系变更不使成功回执误报冲突；实际出现闭包外的新父项目整体回滚。未改变既有批量原子语义、容量校验和追加式历史守卫。

BadReturnCommand 在首次 INSERT 存稳定 owner 与原始请求/结果合同，历史 JSON 回执由私有 return_command sidecar 的完整 manifest 校验；重放必须有当前同动作授权、真实子对象/全部父项目、稳定 owner 和匹配的内容 hash。Salvage 与 Receipt 重放从当前事实行重读，不制造第二份事实 sidecar；未知历史 owner 不凭用户名或幂等键认领。原文件哈希/幂等键业务唯一性未按账号任意拆分。

返件 audit 保留逐条过滤：当前 receipt 准入后，同一 RR 内对各条 before/after 涉及的全部父项目使用同一 `maintenance_return_receipt.read_audit` 裁决。仅返回授权记录，不返回被过滤数量、作者、时间或其他统计。转移必须覆盖实际旧、新项目；请求携带的 project_id 不能替代存储的父关系。

验证：

- 原业务 9 文件 110 passed：`/tmp/itsp-s3-return-legacy-first.log`。
- 新 19 + 原 110 组合 129 passed：`/tmp/itsp-s3-return-combined.log`，122.18 s。
- 新例含 exact 20 入口全拒绝及零 payload/零业务写、独立动作与整块字段、description 输入替换 409、历史审计过滤、稳定账号改名/旧名复用/历史 NULL owner、三 owner 实际降级阻断、真实 project 锁等待后的撤权与移父、领域暂存后 grant 撤销、真实 manifest 已发布后异常全回滚、历史回执 hash 篡改拒绝。
- 最终补齐坏件幂等锁与原领域锁序一致，19 例重跑通过：`/tmp/itsp-s3-return-final.log`，49.08 s。三角独立复核由 authz_phase_review 继续，当前不将它标为已完成。

覆盖证据属于本地隔离 PostgreSQL/Cerbos。未提交、未部署，不代表用户验收或最终完整 CI 通过。
