# 项目总表同号领用单合并与安全改名

- 基线：`origin/main` / `ae32737cb6b5e4a7c125a3192688cc63fc229117`。
- 分支：`fix/workbook-intra-batch-issue-no`；继续 PR #417，指向 main，不合并、不部署。
- 决策: D-0042。金臣 2026-10-19 11:01Z 批准同号既有单自动合并，取代本 PR 初版 `3c72466` 的同号拒绝规则；详见 `docs/decisions/0020-项目总表同号领用单合并.md`。
- 无 schema 变更、无迁移、未改 secrets/env；仅指定 worktree、本任务合成测试库。前端补合单预览与确认凭据透传。

## 复现与根因

基线 HTTP 契约测试将两张不同既有领用单的签名行同时改成数据库不存在的 `WBDD-20261013-0005`：validate 200，apply 的单头 flush 抛真实 PostgreSQL `UniqueViolation / uq_maintenance_site_issue_project_no`。parse 的 `chosen_headers` 仅逐单查询数据库原号，没有比较批内其他单的最终号；apply 只检查同一 ID 的重复行，批量 UPDATE 最终撞唯一键。失败回滚后目标号仍不存在，与用户给出的故障形状一致；未读取生产原文件或数据库。

核查路径：旧 parse 会拒绝普通互换/链式改名，不能称其为正常 validate 200 的本次根因；绕过校验的计划可在 UPDATE 排序中碰撞。地址补丁的 `setdefault` 只补原单头，本身不绕过旧检查。普通无 ID 手填单号表示同头新明细；签名改名 A→B 加新 B 明细必须按最终归属追加。空号/WBDD 输入则逐行生成独立 LY-AUTO 头。`apply_locked_site_issue_edits` 的 legacy 单头更新不自行 flush/bump，外层负责；稍后的行更正会 flush，故防护覆盖整个 V2 apply（含新建、回执与 commit）。

## 合单契约

- 预检按全项目最终单号归组，只合并既有 issue_id。保留当前持有目标号且未改走的头，否则按 `created_at, issue_id` 升序。目标号当前持有者即使没有编辑行也进入计划。
- 单头默认保留单值；工作簿显式日期/地址覆盖。读取签名基线区分“原导出值”和“用户修改”，也收集普通新增明细的显式值及地址别名操作，避免旧地址警告吞掉合单冲突。被 D-02 忽略的作废明细不能间接改头。预检列出全部不同值及来源，显式意图矛盾时中文 422 拒绝。
- 作废来源/目标、未知/不兼容状态、跨项目、重复发货明细身份等拒绝。confirmed/corrected 可共存；纯 draft 可合并，但不得存在确认返还义务。原生 site_issue_v2 保留单不能接收非原生来源明细，以免破坏后续发货身份编辑协议。
- 自动 LY-AUTO 新头始终独立；若与既有改名目标冲突仍 `duplicate_issue_no` / 422。普通手填目标号的新增明细追加到合并后的保留单，行号接在全部移动明细之后。
- apply 在既有项目→需求→领用头/明细锁序内锁项目全部单头与全部明细，再锁引用并重算计划。校验提供 HMAC 凭据，绑定文件 hash、项目、操作者及完整计划；merge apply 必须带 `site_issue_merge_token`。集合、单头、明细或引用快照变化，或缺少/篡改凭据，返回 `stale_site_issue_merge` / 409，提示重新校验；强制接管不跳过此保护。非合单请求保持原协议。
- 改名仍两阶段：所有改号头先用 Core SQL 移至 UUID 暂号，再设置最终号；ORM 强引用保留原头，审计不读取暂号。被吸收头改为 `WB-MERGED-<uuid>`（42 字符），软作废；display_no 终身保留。所有明细（含 inactive）保持 ID，按来源 issue_id、原行号、明细 ID 稳定追加到保留单最大 line_no 后。
- 每条移动行和各参与头写 MERGE 审计，包含原号/来源 IDs/保留单/移动行 IDs 与数量/冲突/引用迁移证据。推进明细和单头版本、返还水位，沿用成本重算和工作簿 revision 协议。同文件重放返回首次完整回执；重新导出的文件可无变更回传。
- 整个 V2 apply 的 IntegrityError rollback 后 warning 记录原异常：项目内单号唯一约束→`duplicate_issue_no`，其他约束→`apply_conflict`，中文 HTTP 422，失败 session 可继续查询。

## 引用清单与历史处理

检索 models 和全部 alembic migration 的外键/通用引用并核对数据库触发器：

| 引用 | 处理 |
| --- | --- |
| maintenance_site_issue_line.issue_id | 全部改挂保留单，ID 不变，追加 line_no、版本和审计 |
| maintenance_return_obligation.issue_id | 改挂保留单，obligation_id 不变；坏件返还行指向 obligation 的 FK 因而保持有效 |
| 返还事件 | 先消费未消费旧事件；追加保留单纠正事件、吸收单空作废事件并消费，水位超过全部旧事件/义务；旧事件身份/payload 不改，迟到旧事件不会复活义务 |
| maintenance_site_issue_command / 历史事件 | append-only 历史保留原 ID 和 payload，软作废单及 MERGE 审计维持可追溯性 |
| warehouse document links | 原 active→superseded（版本+1），追加指向保留单的新 active 链接，保留 supersedes 关系与仓库审计 |
| warehouse open ambiguities | DB 禁止改 candidates/evidence；旧 open→resolved，追加指向保留单的新 open 候选，记录双向接替关系；已 resolved 历史不改 |
| display_no | 保留各头原展示号，不回收不重分配 |
| 行级 return requirements / return links / 成本及人工价证据 | 稳定行 ID 不变；归属随行迁移，沿用更正、定价与查询投影 |
| 其他 facts / derived 展示 | 经稳定明细→单头 join 读取新归属；历史审计/冻结 manifest/回执 JSON 不改写 |
| 附件 | 当前 BusinessFileLink 仅关联验收交付物，无领用单附件 FK；没有需要迁移的领用附件表 |

纯工作簿无发货明细的单不凭空生成 legacy 返还事件；混合明细的 legacy payload 仅包含真实 delivery_line_id 行。库存和真实收货事实未修改。

## Validate 形状与前端

向后兼容新增 `summary.site_issue_merges` 数量、顶层 `site_issue_merge_count`、结构化 `site_issue_merges` 数组及确认 token；generic changes 同时提供中文说明。例（ID、行号为示例，省略快照及其他单头字段）：

```json
{
  "valid": true,
  "summary": {"site_issue_merges": 1},
  "site_issue_merge_count": 1,
  "site_issue_merges": [{
    "target_issue_no": "WBDD-20261013-0005",
    "survivor_issue_id": "issue-a",
    "sources": [
      {"issue_id": "issue-a", "issue_no": "A", "line_count": 1, "rows": [12]},
      {"issue_id": "issue-b", "issue_no": "B", "line_count": 1, "rows": [30]}
    ],
    "total_line_count": 2,
    "rows": [12, 30],
    "header_conflicts": [{"field": "site_location", "survivor_value": "甲地址",
      "discarded": [{"issue_id": "issue-b", "issue_no": "B", "value": "乙地址"}],
      "final_value": "甲地址"}],
    "message": "领用单 A、B 将合并为 WBDD-20261013-0005（共 2 行）"
  }],
  "site_issue_merge_token": "<signed-preview-token>"
}
```

页面显示目标号、保留单、各来源 ID/原号/明细数/行号和冲突决议；只在应用已预览合单时透传 token。4xx 示例：`{"detail":{"code":"site_issue_merge_conflict","message":"06_领用返还 第 12、30 行领用单号 T 无法合并：多行显式填写了不同领用日期；请修改后重新校验","issues":[],"rows":[{"row_no":12,"issue_no":"T"},{"row_no":30,"issue_no":"T"}]}}`。

## 验证与交付

- 后端 **935 passed、0 failed、0 skipped**：47 个 master/workbook/site_issue 文件 + 8 个 bad_return/warehouse 文件，共 55 个文件。除新契约文件外的排序列表按 `[0::2]`、`[1::2]` 分组，分别 **394 passed / 339.26s**、**489 passed / 418.33s**；最终新契约文件 **52 passed / 81.65s**，三个进程退出码均为 0。仅既有 TestClient/openpyxl 弃用告警。
- 新合单预期先在旧拒绝行为上得到两项红灯（validate 422），再实现并验证。52 个契约覆盖二/三单、持号者优先、显式/默认冲突、软作废、全部引用、旧事件防复活、互换/链式、token 漂移/非 ASCII、真实 IntegrityError 回滚及重导出 0 changes 的干净回传。flush 故障注入是同事务 Core 竞争行，证明真实约束异常和整本回滚，不宣称并发压力测试。
- 使用本任务专用 `postgres:15` 容器 `itsp-wbmerge-pytest-1792407853`，随机 localhost 端口 32866；pytest 再为每进程创建独立数据库，未使用共享 5433。测试后仅移除本任务容器，未更改其他容器。
- 前端相关 Vitest **114 passed**（WorkbookRoundTrip/API/Blob 33 + 项目面板 81），`npm run build`（tsc + vite）退出 0，仅已有大 chunk 提示。合成数据实浏览器验证 320/390 宽度无横向溢出、预览可滚动、Enter 确认正确带 token。
- 独立后端边界复核 **3 passed**，发现的无 ID 地址意图、inactive 签名行及合单+链式+新增明细组合已收编到52项契约。非 ASCII token 使用 bytes 恒时比较，validate/apply 的操作者选路一致。

复跑选集（先启动自己的临时 PostgreSQL 15，仅命令级设置 `PYTEST_DATABASE_BASE_URL`，不要改 env 文件或使用共享端口）：

```bash
cd backend
uv run --extra dev python - <<'PYTEST'
from pathlib import Path
import subprocess, sys
files = sorted(str(p) for p in Path('tests').glob('test_*.py')
               if any(term in p.name for term in ('master', 'workbook', 'site_issue',
                                                  'bad_return', 'return_obligation', 'warehouse')))
subprocess.run([sys.executable, '-m', 'pytest', '-q', '--tb=short', *files], check=True)
PYTEST
```

独立复核为同模型不同代理，不宣称异构审查。开发验证不代表用户点按验收、main 人工审批或生产部署。回退可撤回代码；已成功的业务合单不通过代码回退自动拆单，应依据 MERGE 审计另行处理，不伪改历史。
