# S-B 批次 1：领用事实编辑原语

候选分支：`refactor/req00006-site-issue-facts`。状态：本地开发候选；本轮证据见 S-B-6 报告，用户点按、提交、审批和部署均未完成。

决策: D-33

正式决策文件由批次 0 落地，本分支不重复创建。实施契约来自调研目录 P1 边界 §2.3 与 P2 边界。

## 公共原语与调用者责任

`maintenance_site_issue_facts.apply_locked_site_issue_edits` 接受已锁定项目、领用单和按稳定行 ID 定位的局部补丁，返回变动行、单/行版本及前后事实。未出现的行不动，不授予创建、删行、软作废或复活语义。

调用者仍负责实名与项目权限、DATA_CHANGE → state → project → issue → line 锁序、锁后乐观版本检查、幂等命令回执、workbook revision bump 和唯一提交/失败整体回滚。改日期/单号时应提前锁住本单全部有效行，供成本重取价使用。原语不取得上层锁、不 commit、不重复 bump。

原语处理事实字段赋值、PN 更换时旧人工价失效、数量金额同步与成本重取价、P3 返还要求更正、P3 快照之外的事实审计和 CAS 推进；不重复推进 P3 已推进的版本。原生发货来源仍走原有受控流程。

## 旧路径兼容与 S-A 交接

V1、V2、人工整单旧路径直接调用同一个公共 `apply_locked_site_issue_edits`，私有兼容 Adapter 已移除。`LegacySiteIssueEditContext` 显式标明旧协议：赋值阶段共享同一实现，收口阶段传入原始的单内 P3 快照（含外层软作废行），每单只记录一次 corrections；调用者不再直接调用 P3。

旧调用者保留原来的批量成本解析、CREATE/缺行软作废、外层审计、CAS、bump 和提交时点。V1 不额外获得备注/SN 的版本、事实审计或重价；V2 保留单头版本与 UPDATE 审计；人工整单保留行事实预先推进版本、成本变化推进版本、P3、单头再推进版本的顺序，以及改单号前按旧号取价的既有行为。默认局部编辑仍按新号 flush 后取价，两种协议分别有契约测试。

后续 S-A 局部命令使用默认协议，不传 `legacy_context`。该上下文只用于已经验证的旧完整命令，不接受来自 HTTP 的任意协议选择；不会开放原生发货 Beta 或绕过其原有受控入口。

S-A owner 文件 `maintenance_manual_site_issue.py` 只改公共函数导入、既有行赋值、P3 收口、单头赋值接线，删除已迁走的金额同步及 P3 透传辅助函数。创建、缺行软作废、成本、状态、回执和 bump 编排保留。`maintenance_project_operations.py` 未修改。

## 验证和验收

```bash
cd backend
uv run --extra dev pytest -q tests/test_maintenance_site_issue_facts.py
uv run --extra dev pytest -q
uv run --extra dev alembic heads
cd ../frontend
npx tsc && npx vitest run && npx vite build
```

实际命令退出码、独立复核与文件清单见调研目录 `R/logs/S-B-6.report.md`，不以历史 S-B 报告代替本轮结果。

共享测试 PostgreSQL 同时运行多个隔离库时，DROP 仍需等待集群检查点。清理前 client 为零、没有锁阻塞且等待 CheckpointStart/CheckpointDone 时，先检查集群 I/O 与其他测试负载。使用本任务独立的本机 PostgreSQL 实例，通过现有 `PYTEST_DATABASE_BASE_URL` 指向它；保留真实 PostgreSQL、fsync、同步提交和 conftest 建库/清理协议。不要扩大清理超时、FORCE DROP 或终止其他会话。S-B-6 使用相同本机缓存 PostgreSQL 15 镜像和独立临时数据目录，旧 20 项原样对照与候选 25 项均实际 exit 0；容器资源在验证后移除，后续需重新创建本机测试实例。

候选点按回归：旧项目总表 V1/V2 修改领用的数量/SN/备注/单号/日期、人工整单编辑及缺行作废、旧基线并发冲突、原始/展示号往返；核对数量、成本、版本、审计及每事务 revision 推进保持原契约。新网页局部编辑属于后续 S-A 批次，本分支不新增 UI，也不将自动化称为用户验收。

## 迁移和回滚

无新增 Alembic revision、DDL 或数据回填，既有唯一 head `f307604fa4d7`（随 main 合入 #371）。独立于 P2-1/P2-2，未堆叠分支。

应用回退恢复先前已批准的同 commit 构建。原语不引入持久化新形状，不用数据回填撤销编辑；已发生的人工作业只按既有实名更正流程处理。后续 S-A 接入后应同时回退依赖调用点，不能撤去仍被导入的原语。

main 合并须当前候选 SHA 的管理员人工 APPROVED；部署另行授权。本次不执行这些动作。
