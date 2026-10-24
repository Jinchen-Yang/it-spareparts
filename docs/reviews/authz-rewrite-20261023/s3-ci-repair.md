# S3 首轮 CI 失败与修订边界

基线候选：`2cb6fa28109f0ea91858f3ad5b6430f037f239a0`，tree `f274125e0c9084f2ef54ab1a1060beeb7177b747`。首轮完整 CI [40048631340](https://github.com/Jinchen-Yang/it-spareparts/actions/runs/40048631340) 失败，不能作为 S3 阶段通过。GitHub PR 合成提交 `1f02a3d62d7e08443b50b141c43589664ef33987` 的 tree 已在本地核对与候选相同。

首轮通过部分：prepare（迁移/ORM 一致性、single head、测试收集及依赖审计）、frontend（179 files / 2086 tests、构建、类型、生成物和依赖检查）、后端 shard 0。其余 shard 1/2/3 分别 3/4/1 失败。以下仅解释这些失败和修订，不能用局部通过替代下一次完整 CI。

| 失败项 | 实际原因与修订 | 保留的行为保障 |
| --- | --- | --- |
| collection reminder 三个请求 schema | 精确键集合补入可选 authorization_precondition，默认 None | 原业务/响应键集合仍精确相等；额外六例证明授权参数单独传给真实 API adapter，不混入业务 params/command |
| WBDD receipt schema | 增加稳定 owner_user_id；用户名唯一约束已迁为 canonical/legacy 两个 partial unique index | owner nullable Integer、sys_user.id RESTRICT FK、两索引列和谓词及原业务非空/批次唯一全部精确断言 |
| project create/patch 两个响应 | 精确返回补入 manager_assignment=None；旧 manager_id 不等价于有效 primary_manager 关系 | 原全部字段、审计和乐观并发 409 断言保持；真实关联值另有 canonical 测试 |
| replenishment 白名单 | 从直接 Depends 对象存在性检查改为真实挂载 HTTP 请求 | 每个实际入口在 never-cutover、两种 RBAC 开关下仍拒绝无白名单账号；独立验证 canonical Grant、撤权、暂停与持久切换保护 |
| remediation 锁序 | 源码辅助检查定位到外层 rollback 包装后的真实 _apply_batch | 原 PostgreSQL 双连接交错、批次等待事件、工作簿行锁可获取、最终金额/版本断言不变；静态检查不冒充并发证据 |

本修订只涉及五个旧测试文件及审查记录，没有修改应用代码。各项原样 red、本地整文件 green 与跨作者独审记录见 `s3-artifact-protocol-and-adversarial-constraints.md` 末尾。根代理额外执行完整 remediation + maintenance_ledger 两文件 **66 passed**（`/tmp/itsp-s3-remediation-order-green.log`）。

S4 的设计文档、清单与 wire 合同继续仅作只读准备。本修订的新精确提交必须完整 CI 全绿，才允许 S4 应用/测试实施；不能合并或上线生产。
