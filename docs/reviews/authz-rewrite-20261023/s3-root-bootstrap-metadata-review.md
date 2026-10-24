# S3 root bootstrap metadata 收口

状态：作者真实 PostgreSQL/Cerbos 回归通过；独立原样复跑已通过，等待最终精确提交 CI。生产未变更。

最后按调用顺序检查入口 resolver、artifact domain state 与正式读取，发现三处权限拒绝虽返回 403，但在 PDP 前已把受保护的业务字段读入进程。三条真实 HTTP + cursor.description 反例首轮均失败：`/tmp/itsp-s3-root-bootstrap-red.log`。没有把已通过 PDP 后的完整性校验计作漏洞。

| 入口 | 原读取 | 最终修复 |
| --- | --- | --- |
| migration-runs GET/read 及其授权视图 | 完整 run.preview/manifest、event.payload、候选计划和基线 | resolver 只取 run ID/version/status/stable actors；源项目从 JSON 内投影 ID，并与真实 cutover plan 的 ID 比较；event 完整性在数据库计算全事件摘要，返回 actor/content hash |
| roundtrip-import 重放 | `_input_plan` 在授权前加载旧批次 report 与操作 result | 此资源唯一的 all_contracts/apply 合同先经同一 context/PDP 判定；只在通过后读取真实完整旧回执，再解析最终实际输入上下文；最终领域锁、actor、原件/回执验证保持 |
| acceptance-files GET 与附件 DELETE 的 resolver | 关联 deliverable 的提交/审批/拒绝理由等完整字段 | join 显式投影 deliverable ID 与 project ID，raiseload 防意外加载；完整二进制及实际业务记录仍只在已授权正式路径读取 |

新增拒绝 SQL 反例保存在三个原作者测试文件。原样修后与本模块已有授权/文件/独立重放测试合计 **33 passed**（`/tmp/itsp-s3-root-bootstrap-fix.log`，58.41s）。旧业务独立组合仍在执行，其结果另记；重复组合不相加为新增覆盖。

这些修改不改变历史业务规则、项目数据、生产配置或身份策略；输入/回执完整性继续覆盖实际完整内容，SQL 投影只限制授权引导阶段可物化的内容。

独立根包组合 **43 passed**（`/tmp/itsp-s3-root-bootstrap-independent-final.log`）。旧业务首次组合 307 passed / 1 failed；失败是旧取消测试直接构造 SimpleNamespace 缺真实 UserContext 字段，改用真实上下文并保持原断言，独立单例通过。随后整文件运行揭出另一既有测试竞态：busy request 未抵达互斥锁，50ms 定时器已释放 worker，实得 200。增加 100ms 调度延迟原样确定复现（`/tmp/itsp-s3-roundtrip-busy-race-red.log`）；修成等待实际 busy 请求完成再 finally 释放 worker，保留所有 429/零请求体/Retry-After/取消/清理/锁断言及原 worker 2s 上限，没有修改生产代码或放宽 timeout。最后旧 security 全文件 **74 passed**（`/tmp/itsp-s3-roundtrip-security-final.log`），独立两修例 **2 passed**（`/tmp/itsp-s3-roundtrip-fixtures-independent-final.log`）。这些组合存在重叠，不相加为总数。
