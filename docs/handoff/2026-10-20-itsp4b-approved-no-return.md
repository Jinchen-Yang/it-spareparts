# ITSP-4b 免返批准清单工具交接

决策: D-32

## 范围与基线

- 工作树：`wt-laneA-nrapprove`，分支 `feat/no-return-approval-tool`。
- 叠加在 #423 的 `d9e26d7658967afc767ed61931a5ecb440a3c9a7`；未修改 `maintenance_site_issue_line_correction.py` 或其公开签名，不触碰 #434 工作树。
- 新增只读覆盖报告、批准文件干跑及逐项执行 CLI；没有网页入口、schema 迁移或历史自动回填。说明和模板见 [runbook](../runbooks/itsp4b-no-return-approval-tool.md)。

## 实现结论

SHA256 绑定原文件字节，严格字段白名单及三态。执行复用 #423 唯一命令写路径，预先按相同锁序核对批准前像，锁后复验当前实名账号和范围。原 command 表有 append-only 触发器；批准元数据及首次执行版本追加到已有 operation audit，与事实／事件／revision／原回执同事务提交，不能 UPDATE 原 command。

同单因果链显式引用清单中紧邻前项，仅用数据库原成功回执的版本；缺少回执或后续外部变化即停止，不刷新。撤销用新的反向批准文件，原 after 基线有漂移即冲突。覆盖报告并列原始／有效三态、标准／文字分类及旧义务冻结依据，数据库强制只读一致性快照。

## 验证与状态

本次仅用自建 PostgreSQL 容器 `itsp-laneA-itsp4b`（`127.0.0.1:32887`），容器留运行；pytest 每次建立并清理自己的隔离数据库。每次测试前调用用户指定 `~/workspaces/reviews/lanes-20261020/laneA/gate.sh`；75 时等待60秒再查，不自制 gate，不并行跑测试，不跑完整套件。

最终新工具复验 **49 passed**（批准工具 45、覆盖报告 4），96.77 秒，证据 `/tmp/itsp4b-tools-final.xml`。此前 #423 定向文件 `test_site_issue_line_correction_api.py` 为 **54 passed**；合计103项定向测试。前一合跑100通过／3失败，3个失败是三来源新增快照断言误把既有 `updated_at` 推进当作副作用；已改为要求时间戳推进且其余业务字段保持，最终49项全部通过。既有Starlette/openpyxl弃用警告不在本任务修改。独立需求／技术静态复核均未发现剩余阻断项；同模型角色复核，不宣称异构模型审查。

未做前端构建（无前端变更），未推送、合并、部署或访问生产。状态为本地候选、待业务验收；管理员针对当前 SHA 的人工批准与生产部署授权均未发生。

## 待金臣确认

- 页面入口是否需要以及未来流程。
- 批准人资格、合同／批准材料真实性和存档责任；本轮只校验字段及 SHA，不自动认证批准真实性。

PR 中文草稿按用户指定保存在仓外 `~/workspaces/reviews/lanes-20261020/laneA/pr-itsp4b.md`。回退仅停用工具，保留历史；业务撤销另行形成批准反向项。
