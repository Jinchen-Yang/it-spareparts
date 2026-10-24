# S3 全项目批量传输实现与复核

范围为实际五个 HTTP 入口：options、preview、apply、receipt-rulings、download。现有销售/收款适配器和事实写入规则继续复用；新增受控 facade 负责统一授权、稳定身份、原件与回执。本文是本地开发证据，尚不表示 S3 完整 CI 或生产验收通过。

- 预览先从原始 XLSX 解析订单与 raw ID，仅查询实际合同、来源挂靠、项目编号和身份关系。一次一致读内按全部真实目标授权后才运行原预览计划；新建、无法定位的输入显式要求 all_projects。计划读取限制到这些项目/合同，销售 ORM 只投影计划需要的金额、税率、状态与身份列。
- preview、apply、receipt_ruling 使用本域独立 F 权限，不附送导出或其他模块能力。导出使用独立 resource 与原有表单、字段白名单；SQL 范围先于聚合，完整文件在一致读内生成，释放连接后由共享下载门检查当前读取者再发布。
- 预览原件使用本次命令独占路径，实际 SysImportBatch 保留稳定 owner；不可变输入摘要、真实 apply 回执各有独立 sidecar。历史无 owner/sidecar 的批次不凭同名账号接管。成功重放仍须当前 apply 与实际选择范围，未选中的项目不附加到提交范围。
- apply 保留原批次行锁、选择幂等锁与业务域锁。最终阶段检查原/现父合同、项目授权关系和当前主体，才允许事务提交。ruling 对实际旧收款记录和锁后合同归属核验，避免 precondition 只与旧上下文自比较。业务版本自身合法变化与授权关系变化分别处理。
- `operation_input` 仅用于当前命令解析，不保存为可重放授权。事实、回执和审计同事务，发布后异常会回滚；预览原件仅清理本次拥有且未持久化的文件。
- 表单/字段展示注册表移到纯 service 模块，受控 facade 不反向依赖 API。options 数据响应带实际 `authorization_context`，与前端 ContextTransport 协议一致。

作者实际 PostgreSQL/Cerbos 测试：最新作者 12、独立 7 与原业务 99 共 **118 passed**（`/tmp/itsp-s3-transfer-projected-complete.log`，72.96s）。独立测试包括本轮其他领域 2 条，因此不能将 118 全算作本模块新增测试；此前其他重叠组合不相加。独立审查文件为 `s3-batch-transfer-independent-review.md` 与 `s3-artifact-protocol-and-adversarial-constraints.md`。

跨模型审查另以真实 SQL 列投影与两连接锁冲突复现两个阻断，现已修复并原样通过：

- APPLY 按所选 row_key 在 SQL 内提取实际 operation/public row/对应 contract state，构成最小业务计划；不再物化未选项目的 report_json。业务原语使用同一投影、批次最小 ORM（raiseload），只用 JSONB patch 更新结果而保留完整原始输入。实际其他批次的幂等命中会先锁其批次，独立一致读授权其实际范围，再读取回执。
- 最终关系锁遵循 project → contract。等待合同锁后若发现新父项目不在已锁闭集中，立即拒绝当前命令，不持合同锁再追新项目锁。原 actor 最终锁和关系 OCC 保留。

输入/回执完整性仍在 PostgreSQL 对完整冻结 JSON 计算不可逆摘要；投影不缩小 sidecar 覆盖。原 options DTO、未选项目误拒绝和裁定后移父 OCC 三项亦有修复及真实反例。

数据库升级/降级与 ORM 漂移另有 20 条通过，含真实 SysImportBatch / RoundtripOperation owner 即使无 sidecar 仍拒绝有损降级；未执行生产迁移。

跨模型复核最终原样 19 passed（`/tmp/itsp-s3-transfer-final-independent.log`）；冻结后将 SQL 断言扩到正式 APPLY 与 replay，tail 4 passed（`/tmp/itsp-s3-transfer-frozen-tail.log`）。两项真实红均已闭合。
