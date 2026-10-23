# S0 四分片 CI：原流程保持与业务测试复核

状态：冻结实现的根代理复核通过；异模型实现者测试、独立执行攻击复审与根代理原流程/业务保持比较完成。独立复审见 [ci-sharding-independent-review.md](ci-sharding-independent-review.md)。四片完整执行及精确提交远端 CI 尚待验证；不能据此宣布 S0 退出。

## 比较结果

- 前端 job 与 `1be345dd2e6396714de692ac5765a6920d7a272b` 的源码逐字相同；构建、行为测试、生产依赖漏洞检查均保留。
- 后端原有 PostgreSQL 15、uv 0.11.31、固定真实 Cerbos 安装、迁移 upgrade/check、单头校验、全部 pytest 与依赖漏洞检查均保留。uv run 增加 `--frozen`，禁止本次 CI 临时改锁。没有更改分支保护所需检查名称。
- 三个执行层依赖清楚：准备迁移/完整清单/漏洞检查 → 四个独立 runner 与数据库 → 原 required 名称汇总。汇总 `if: always()`，检查 needs 成功、当前 run/attempt/commit、全部四片、执行事件与清单 exactly-once；不因上游失败而自身被跳过。
- 单个分片仅执行分配的完整文件，文件内原顺序不变；每片仍收集完整项目，检查分母一致。未引入仓库明令不支持的 pytest-xdist；并行发生在独立 CI runner，不共享数据库。
- 独立程序对比真实清单与磁盘：7014 个节点、388 个测试文件，四片 1754/1754/1753/1753，所有现行测试文件均出现。此项是收集证据，不是执行通过证据。
- 唯一既有业务测试变动是六个上传类型的参数化 `ids`。根代理用 Python AST 去掉新增 `ids` 后与 HEAD 相等，参数字节、用例及断言均未变。Excel/Word 的 ZIP 内时间戳不再进入节点名称，修复跨进程清单不稳定。
- 7 个既有条件跳过逐节点和精确理由登记；未新增权限跳过许可。完整旧套件的跳过数量已知，精确跳过集合还由串行复跑核实；新 CI 会拒绝未登记 skip、xfail/xpass、丢失事件、失败 teardown、提前结束及实际进程非零退出。

## 审查边界

原始 pytest 也会遵循 conftest 和收集配置，因此不能把原本已有的任意测试隐藏机制描述为分片新引入的漏洞。本次额外固定既有默认发现合同，识别额外位置参数、缩窄发现配置、遗漏默认测试文件和前后 Item 变化，便于发现意外漏跑；不声称抵抗可任意改写进程的恶意 Python 插件。

变更不会部署应用或修改生产。是否放行迁移下一阶段仍以精确提交的全部 CI、独立复审与剩余发现闭环为准。

## 冻结文件 SHA-256

| 文件 | SHA-256 |
|---|---|
| `.github/workflows/ci.yml` | `341cea12bef94c3a3a455e1d3d7fba62694d1ec2c196a606111bc57a6a4755bd` |
| `scripts/pytest_shard.py` | `611c799fcd4dd4dcffa9a2df0eb6da4eee1198fe8c325d975d3d6542181bf332` |
| `scripts/pytest_shard_plugin.py` | `517c0503696791d274573f835b8f758fb51879d5f2b99ee93dbb34a5cb3151c4` |
| `scripts/pytest_shard_skips.json` | `27efd1ba4eb25756df9b02464f0fd270e381ea4307914584f9141795f83e8ffd` |
| `backend/tests/test_ci_pytest_shards.py` | `5bc3ae0dd9360658e68c517ee7fee008fa96ab2b84703c85d318937dc02c255d` |
| `backend/tests/test_maintenance_collection_evidence.py` | `e81e3a1f816738136592714d8dee6bca0d850c3cfc9f2901108e8027c40933b7` |
