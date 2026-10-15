# 项目免返标注候选：验收与回退

决策: D-32

范围为需求00005 P3-1。项目概览基础信息显示免返标注，原“编辑基本信息”弹窗负责保存、取消和清空。文本只作提醒，返还要求以明细标记和规则为准；不解析文字，不改项目默认免返、行级要求、数量、成本或实际收货。

独立复核和本机完整回归结果见S-A交付报告；全量后端存在环境/时序失败，不能称全绿。候选仍待用户点按验收及 main 精确 SHA 人工审批。批次0的 D-32 文档须先合入 main；本分支不重复创建该决策。没有生产部署授权。

## 读写与兼容

- PATCH 未传标注时保留；显式 null、空串或全空白清空为 NULL。API 与服务均校验原串最多2,000个 Unicode 字符；去除首尾空白，保留内部换行。
- 沿用项目主档页面、动作、利润数据、项目范围和实名权限；可看项目不代表可编辑。主档归档后不可编辑。
- 复用 state→project 锁序、项目 version CAS、`maintenance_project_audit_log` 及 workbook revision。取得项目行锁后刷新已预载的 ORM 实体；真变化增量一次，no-op不新增更新审计或版本。
- 该字段以纯文本展示。保存后统一读回；项目读回失败时清空旧快照，刷新回调意外拒绝时明确提示已保存但刷新失败。版本冲突保留草稿。
- 本批不包含工作簿01或项目导出列，不包含返还规则消费归一、批准清单工具或历史更正。

## 迁移

M1 revision=`f307604fa4d7`，down_revision=`c8f3d1a7e2b9`。只给 `maintenance_project` 加可空 `no_return_note TEXT`，不增加默认值、索引或历史业务回填。

候选发布前在独立PostgreSQL副本执行 upgrade→downgrade到父版本→upgrade和 `alembic check`；并验证有非空标注时downgrade拒绝，列、文字与版本表均保留。检查与DROP在同一事务独占表锁内完成，避免并发写入窗口。

未来获授权发布时，先备份并确认可读，再应用迁移，随后启动同一候选提交的前后端镜像；记录精确SHA、schema revision、发布标签和回退镜像。迁移遇锁超时或任何验证失败即停止，不绕过保护。

## 开发验证入口

后端契约：`cd backend && UV_OFFLINE=1 uv run --extra dev pytest -q tests/test_maintenance_project_no_return_note.py`。包含PATCH/GET/目录、2000/2001字符、清空/未传/no-op、权限/实名/范围/归档、组合bump、审计失败回滚、事实隔离、双Session竞争及迁移保护。

最终回归：`cd backend && UV_OFFLINE=1 uv run --extra dev pytest -q`；`UV_OFFLINE=1 uv run --extra dev alembic heads`。前端依次运行 `npx --no-install tsc`、`npx --no-install vitest run --maxWorkers=2`、`npx --no-install vite build`。Vitest只控制并发，不跳过测试。本批另在冻结源码上运行完整 `npx --no-install vitest run --maxWorkers=1`，真实exit0、100文件/1,188项全部通过；前后源内容指纹一致。先前两worker运行的五失败与真实exit1仍保留在交付报告。

本批定向22项与M1独立迁移演练已在默认本机5433隔离库真实通过。初次5433全量遇共享本机IO等待，工具会话收到中断后返回130，但后续旧日志仍增长，不能据此断言子pytest已停止；该旧运行不作为最终验证证据，完整经过见S-A报告；同一全量改用 `cd backend && PYTEST_DATABASE_BASE_URL=postgresql+psycopg://spareparts:spareparts@127.0.0.1:32771/spareparts_test UV_OFFLINE=1 uv run --extra dev pytest -q`，由现有conftest照常创建随机隔离库。本会话独享PostgreSQL15.19使用动态本机端口32771、tmpfs数据目录，`fsync`、`synchronous_commit`、`full_page_writes`均保持on。没有修改默认5433入口、conftest或测试集合。环境证据为交付目录 `logs/S-A-owned-postgres.json`。

实际退出码及日志见本批S-A交付报告；未执行的命令不能据本runbook宣称通过。

## 用户点按验收

1. 有权账号打开有标注项目和无标注项目，基础信息分别显示多行原文和“未标注”；文字中的HTML按普通文字显示。
2. 从基础信息“编辑免返标注”进入原基本信息弹窗，输入“硬盘免返，其他按合同”保存；刷新、重新打开后相同。取消草稿再打开时恢复服务器值。
3. 清空后保存显示“未标注”；只改项目名称不改标注时，原标注保留。
4. 可查看但无主档编辑权限的账号可读标注，不能编辑。项目归档后不可保存。
5. 两人读同一版本后分别保存，后一人收到冲突，草稿仍在；切换项目时旧弹窗请求不能回写新项目。
6. 保存后网络读回失败时显示“已保存，但页面刷新失败”，不会以旧标注冒充最新值；恢复后重新读取。
7. 标注修改前后核对实际领用、明细应返要求、数量/成本及返还收货事实保持一致。

## 回退

普通回退只回退前后端同候选代码，保留加法列、用户文本和审计；旧代码可忽略新列。不要默认执行schema downgrade。

确需移除列时，须先获得相应操作授权、导出并核实文字及审计留存，再用有版本/原因的受控操作清空必要标注；任何非空值仍会使downgrade拒绝。不得通过改迁移或裸SQL绕过保护。
