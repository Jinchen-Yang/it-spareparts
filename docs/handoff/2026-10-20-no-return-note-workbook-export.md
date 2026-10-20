# ITSP-4a：免返标注只读工作簿与可选导出

决策: D-32

- 分支：`feat/no-return-note-workbook-export`；原功能提交 `51ee6c7`，本次审查修复提交 `7adff98`。
- 基线：本轮再次 `git fetch origin`，正常合并 `origin/main` `1a743c1ca9aa51ad44e222b165654718bfbe326a`，合并提交 `e20a0af`，无冲突。保留 lane B ITSP-6a 字段保护、数量归一化及审计，也包含 main 已有 #426 前端改动；本 PR 相对 main 无前端差异。
- PR：[#424](https://github.com/Jinchen-Yang/it-spareparts/pull/424) 已存在、OPEN、base 为 main；本轮只读核对的远端 head 仍为 `51ee6c7`。本次修复及合并仅本地提交，未推送；CI 在推送后运行，不能用原 head 的 CI 代表本次修复。
- 范围：补齐 D-32 第 5 款；免返标注仍只作提醒，不新增默认免返开关、不改计算或返还状态。

## 实施与兼容方案

V2 `01_项目概览` 在项目备注后显示「免返标注（仅网页维护，此处只读；仅作提醒，返还要求以明细标记和规则为准）」，采用只读底色、多行纯文本及已有导出字符清洗。NULL 导出为空。项目备注仍可回写，不开启整张表保护。

V1 的对应基础信息表名为 `01_项目基础信息`，同样补只读行；其 `02_概览数据` 是合同和经营指标，不重复项目主档文本。独立 V3 工作簿不在本次范围。

V2 01 没有整表只读哈希，解析仅取项目备注及合同额的明确标签；V1 不解析 01/02。新增行沿现有只读概览字段约定静默忽略，不增写入入口、警告协议或签名字段，不升模板版本。新旧标签均不参与免返标注回写：旧标签「免返标注（仅网页维护，此处只读）」下修改值、旧模板缺行、原样回传、修改或清空免返单元格都不能改变标注；项目备注版本冲突保护保持原状。

项目列表导出注册 `no_return_note`，中文标题「免返标注」，与项目备注同为默认不选。共享 `_safe_excel_value` 只清理不可表示的控制字符并执行 32767 字符上限检查；`_build_xlsx` 在创建 `WriteOnlyCell` 后，把所有字符串显式设为 `data_type="s"`。以 `= + - @ \t \r \n` 开头的文本原样导出，不加撇号、不裁掉首部空白，`=SUM(A1)` / `=HYPERLINK(...)` 始终为字面文本而非公式，`#N/A` 也保持文本。数值保留数值类型，字符串 `"-1"` 仍为文本；bool/date/None 沿用原处理。该共享写入同时覆盖项目名、两种备注及其他文本列，也覆盖项目批量移交复用的项目导出。前端选列组件从服务端读取字段，无需修改前端。

## 公共 API 验收场景

使用合成项目及有权限的实名账号，通过现有下载、validate、apply、项目详情和导出 options/POST API 验证：

1. V1/V2 下载：NULL、中文换行、历史非法控制字符及全部上述前缀；非空免返标注与 V2 项目备注精确值往返且 `data_type == "s"`，免返行只读样式、项目备注仍可编辑。
2. 上传：免返行修改、清空、删除、原样回传均保留值及项目版本，不新增主档字段审计；同次修改项目备注正常生效。
3. 兼容：从新模板删除新增行或恢复旧标签且保留原签名，验证缺行/旧标签均被忽略（不宣称历史二进制实测）；旧标签也不阻碍同次项目备注回写；网页后来更新标注后上传旧文件不覆盖新值；既有项目备注 CAS 继续回归。
4. 导出：服务端提供可选列，默认不选。公开 API 的项目名、项目备注、免返标注三列覆盖 `-48V`、`-48V 电源免返`、`+5V`、`+5V 板卡免返`、`- 硬盘免返`、`"-1"`、换行/制表/回车开头、`@备注`、SUM/HYPERLINK 和 `#N/A`，逐格断言原值及 `s`。同表负数冲销报销 `Decimal("-48.00")` 为 `n`、文本 `"-1"` 为 `s`、整数版本为 `n`、日期为 `d`。另验证 32767 原样保留、清理控制字符后判长、32768 返回 422 而不静默截断。

## 验证与交付记录

沿用本任务 PostgreSQL 15 容器 `itsp-laneA-itsp4a-20261020-nrwb`，localhost 端口 32870；仅通过命令级 `PYTEST_DATABASE_BASE_URL` 指向它，每个 pytest 进程再建立独立数据库。上次会话记录：初始化出现 `DataFileImmediateSync` 等待，随后仅在此临时容器设置 `fsync=off` 并 reload；真实事务/锁仍保留，不以此验证崩溃耐久性。本次续接未调整容器配置，未新增或删除容器。

本轮每次测试前均运行用户指定的 `bash ~/workspaces/reviews/lanes-20261020/laneA/gate.sh`，未自写或修改 gate。所有执行时 gate 均返回 0（load1 < 10、MemAvailable ≥ 4G、至多 1 个其他真实测试进程）；本任务始终单测试进程，未运行完整后端、Vitest 或构建。

TDD 证据：

- 导出修复前：**16 failed / 7 passed，50.23 秒**，直接复现多余撇号和 `#N/A` 被识别为 `e`；XML `/tmp/itsp4a-export-red-20261020.xml`。
- 导出修复后：**26 passed，41.76 秒**；XML `/tmp/itsp4a-export-green-20261020.xml`。
- 01 新文案修复前：**1 failed，12.61 秒**，因下载仍为旧标签；XML `/tmp/itsp4a-label-red-20261020.xml`。
- 01 修复后：**50 passed，73.31 秒**，包含 V1/V2 旧标签上传忽略；XML `/tmp/itsp4a-label-green-20261020.xml`。

合并后代码候选 `e20a0af` 的定向回归命令如下，只选择三个指定文件及 12 个触及 01 的主工作簿测试：

```bash
cd backend
bash ~/workspaces/reviews/lanes-20261020/laneA/gate.sh &&
PYTEST_DATABASE_BASE_URL='postgresql+psycopg://spareparts:spareparts@127.0.0.1:32870/spareparts_test' \
  uv run --extra dev pytest -q \
  tests/test_maintenance_project_export.py \
  tests/test_maintenance_no_return_note_workbook.py \
  tests/test_maintenance_project_note_workbook.py \
  tests/test_maintenance_project_master_workbook.py::test_v2_validate_contract_total_uses_authenticated_identity \
  tests/test_maintenance_project_master_workbook.py::test_v2_overview_respects_explicit_empty_salesperson_override \
  tests/test_maintenance_project_master_workbook.py::test_master_workbook_has_all_six_sheets \
  tests/test_maintenance_project_master_v2_editable.py::test_v2_overview_strict_cost_keeps_real_zero_and_rejects_dirty_fact \
  tests/test_maintenance_project_master_v2_editable.py::test_v2_overview_active_manual_override_uses_net_quantity \
  tests/test_maintenance_project_master_v2_editable.py::test_v24_overview_masks_known_contract_subtotal_when_incomplete \
  tests/test_maintenance_project_master_v2_editable.py::test_v24_overview_unique_contract_total_roundtrips_to_inc_tax_with_audit \
  tests/test_maintenance_project_master_v2_editable.py::test_v24_overview_accepts_zero_as_a_real_contract_total \
  tests/test_maintenance_project_master_v2_editable.py::test_v24_unique_missing_contract_amount_stays_editable \
  tests/test_maintenance_project_master_v2_editable.py::test_v24_stale_overview_rejects_contract_added_after_validation \
  tests/test_maintenance_project_master_v2_editable.py::test_v24_overview_rejects_project_total_when_multiple_contracts_are_current \
  tests/test_maintenance_project_master_v2_editable.py::test_v24_overview_shared_contract_now_editable_with_cas \
  --tb=short --junitxml=/tmp/itsp4a-postmerge-20261020.xml
```

合并后结果：**127 passed、0 failed、0 errors、0 skipped，186.92 秒，退出码 0**。JUnit 分文件计数为：项目导出 26、免返工作簿 50、项目备注工作簿 39、主工作簿 01 相关 3、V2 可编辑工作簿 01 相关 9。前述两轮绿灯均包含在本轮，不重复计数。仅既有 Starlette/httpx 与 openpyxl 弃用告警；`git diff --check` 通过。原功能提交曾有 220 passed 的历史记录，不计作本次修复后的验证。

同模型独立代理分别核查需求契约与实现质量，并复核 main 合并的 lane B 保留情况及两份文档，未发现阻塞项，不宣称异构模型审查。中文 PR 正文草稿位于 `/home/cloudlay/workspaces/reviews/lanes-20261020/laneA/pr-itsp4a.md`。

## 审查回应与范围外排查

已消除本次共享项目导出的撇号约定，更新原有撇号断言，保留字符清洗、长度上限和数值类型。01 文案统一为 D-32 完整提醒，新旧标签忽略路径由公共 API 契约测试守护。

全 backend 搜索 `_safe_excel`、CSV/XLSX helper、公式前缀与撇号拼接后，另有以下 **8 个文件、9 处** 沿用撇号约定，均不复用本次共享函数，按本轮授权仅报告、未修改：

| 位置 | 出口/复用影响 |
| --- | --- |
| `backend/app/api/profit.py:139` | 利润 CSV 维度 |
| `backend/app/api/imports.py:66` | 导入错误 CSV |
| `backend/app/api/maintenance.py:224` | 项目汇总、明细与看板 CSV |
| `backend/app/services/maintenance_export.py:123` | 维保订单 XLSX（后续已有 `write_string`） |
| `backend/app/services/maintenance_project_workbook_v2.py:280` | V2 工作簿文本及 canonical/digest/metadata |
| `backend/app/services/maintenance_manager_workbook_v3.py:230` | 管理 V3 工作簿文本及 canonical |
| `backend/app/services/replenishment.py:2305` | 补货复核/筛选/WBDD 子集，维保补货证据复用 |
| `backend/app/services/maintenance_workbook_export.py:107,448` | 合同/ZIP 工作簿及 ZIP CSV 清单；项目 V3 与维保 API 复用 |

待金臣确认：无新增业务待决项。代码与定向验证已就绪，用户实际验收仍待安排。

无 schema 变更、无数据迁移。回退仅撤回本次展示与选列代码，不清理主档字段或业务数据。开发验证与 CI 不代表用户实际验收、main 人工审批或生产部署。
