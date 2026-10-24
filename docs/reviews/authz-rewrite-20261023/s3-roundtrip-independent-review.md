# S3 原件工作簿回填独立复审

结论：本次静态复核与独立真实 PostgreSQL/Cerbos 测试未发现阻断；不替代完整 S3 CI、浏览器或部署验收。未修改作者生产代码。

复核范围：`maintenance_roundtrip_authorization.py`，`maintenance_roundtrip.py` 的 canonical 原语接缝，`api/maintenance.py` 的 multipart/worker/清理路径，以及共享 artifact binding 的实际状态核验。

- 首次输入将真实 bytes、签名合同版本、逐行 intent 和完整全局重算对象关系纳入服务端 plan。全局领域锁后 capture 必须匹配新鲜 RR 预检；全部业务更新、重算和原有锁完成后才取当前 actor/PDP。本域 apply 必须同时具有 all_contracts 及本域完整 C/F/K；不能从其他模块拼字段。
- 原件/逻辑回放均重新检查当前 apply。实际成功 batch、稳定 owner、operations、report 和唯一 SysRawFile 的摘要必须匹配已持久 binding，磁盘原件还须实际 hash 相同；历史无 owner/binding 的回执不可认领。该 binding 用于完整历史事实与 bytes 的一致性核验，不把 operation_input 当持久 grant。
- canonical archive 每次独占临时文件，不共享哈希路径。后置异常回滚数据库后，只有确认没有 durable SysRawFile 引用才删除本次 owned file；无法确认 commit 结果时保留 bytes，避免删成功原件。输入临时文件由 worker 完成后的既有清理路径负责。

独立新增 `backend/tests/test_authz_s3_roundtrip_independent.py` 三项真实反例：

1. 成功回执后篡改 SysRawFile 原件 metadata，即使 bytes 未变也拒绝重绑/回放。
2. 第一次上传成功后，第二次上传真正登记 binding 后抛异常；原成功 bytes 与原路径完整保留，第二次新文件删除，业务行/operation/batch 回滚到第一次成功状态。
3. 原语已暂存重算后，独立连接撤掉本域 data_customer，仅改 grant/version、不提升 epoch；最终 PDP 拒绝，事实、operations 和 batch 全 rollback。

原作者 8 + 独立 3 共 **11 passed（25.31s）**：`/tmp/itsp-s3-roundtrip-independent.log`。原作者提供的历史组合 40 passed 仍属作者证据，本复审没有重新计算该组合。
