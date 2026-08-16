# 给云端 Claude 的执行 Prompt：验收补丁 P1+P2

> 背景：你的「维保前端 2 页重设计」已通过独立验收（后端 11 failed/3501 passed＝环境基线零新增；
> 前端 tsc/build 绿，vitest 581/582 唯一失败为未触碰的 LoginPage 环境用例）。
> 现补两个小改，口径见 `docs/maintenance/REQUIREMENTS.md` #47/#48。

```text
【任务】验收补丁两处，均为小改。读 docs/maintenance/REQUIREMENTS.md #47/#48 后开工。

【P1｜归属挂靠候选按 XSDD 预筛】
- 现状：项目面板「项目基础信息」tab 的挂靠列表拉「全部未归属单前 20 条」，未按 XSDD 预筛。
- 要求（#48）：候选 = 未归属单中「XSDD ∈ 本项目销售单集合」的单，排最前；其余未归属单排在其后。
  多合同项目（如「兵装财务20240701-20250630北京神州新桥 整体维保」）要把该项目名下**全部** XSDD
  都算作本项目的键（项目→合同→XSDD 从台账合同表取）。
- 实现建议：后端 listMaintenanceSourceOrders 增加按 XSDD 集合过滤/排序参数（首选后端做，避免前端
  只拿 20 条漏选）；前端 BasicsTab 传本项目 XSDD 集合。补测试：同 XSDD 候选置顶、多合同项目多键、
  无 XSDD 时回落原列表。
- 无迁移。

【P2｜报销 sheet 备注列（已批准 1 条迁移）】
- 现状：`04_报销订单` sheet 无「备注」可编辑列；`f_project_expense` 无 remark 字段。
- 要求（#47）：
  1. `f_project_expense.remark` 纯加法 nullable（VARCHAR 上限与同表备注列一致即可），
     迁移**追加在迁移链尾**（线性链，只增不改），保持 M0-E 审计口径；
     更新 `docs/releases/v1.23-migration-audit.md` 加一行本修订。
  2. `04_报销订单` sheet 加「备注」列（黄底可编辑），apply 按行回填 remark，导出带已有 remark。
  3. 项目面板「报销」tab 展示备注。
- 测试：sheet 导出含备注列且回填落库、web 展示、迁移 downgrade/upgrade 通过（注意 PG 1600 列槽位守卫）。

【硬规则】
- 不碰其他任何东西；不改既有列语义；不动销售/采购/库存模块与冻结清单。
- 先跑相关测试，再跑一次全量 pytest 与前端 tsc && vite build && vitest。
- 推送到当前分支 claude/web-github-capabilities-x7kfh7，附报告：
  改动文件清单、迁移修订号、测试结果（全量与基线对照）、自行决定。
```
