# ITSP-4a：免返标注只读工作簿与可选导出

决策: D-32

- 更新：2026-10-21，laneA2 实现续接；分支 `feat/no-return-note-workbook-export`，接手 head 为 `aba2390f0e0afd0601107415b224ee299c265212`。
- 基线：本轮先 `git fetch origin`，再普通 `git merge origin/main`，合入 `d41e3d60f6cb0c6c4013c490232fa06bb864d092`；新的受测代码 head（合并提交）为 `d671592c2928a7921afc4e9ea47d0b1722d6ced2`，无冲突，无 rebase、无历史改写。保留 main 的 #432 行级筛选导出、字段保护、数量归一化及审计；相对 main 无前端差异。
- PR：[#424](https://github.com/Jinchen-Yang/it-spareparts/pull/424) 只读核对时为 OPEN、base 为 main。本交接更新仅改文档；最终交付 head 和普通推送结果记录于 `/home/cloudlay/workspaces/reviews/laneA2-20261021/pr424-body-draft.md` 与本轮最终报告。**CI：以新 head 为准**，不沿用旧 head 的结果，不推断新 CI 已通过。本轮不在 GitHub 评论、批准、合并或修改 PR 状态/正文。
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

## 2026-10-21 合并后验证

仅使用用户指定 PostgreSQL 15 容器 `laneA2-pg-424impl`，`127.0.0.1:33000`。pytest 按仓库现有夹具逐进程建立独占数据库和合成数据；本轮始终单 pytest 进程、三个命令串行，两个主工作簿文件均跑全文件，未跑全量后端。未使用旧 32870 端口、旧 gate 或其他容器，未调整数据库配置。

命令在本 worktree 的 `backend/` 执行；完整输出、JUnit 和墙钟记录均在 `/home/cloudlay/workspaces/reviews/laneA2-20261021/`：

```bash
export PYTEST_DATABASE_BASE_URL='postgresql+psycopg://spareparts:spareparts@127.0.0.1:33000/spareparts_test'
export DATABASE_URL="$PYTEST_DATABASE_BASE_URL"

uv run --extra dev pytest -q \
  tests/test_maintenance_project_export.py \
  tests/test_maintenance_no_return_note_workbook.py \
  tests/test_maintenance_project_note_workbook.py \
  --tb=short --junitxml=/home/cloudlay/workspaces/reviews/laneA2-20261021/pytest-notes-export.xml

uv run --extra dev pytest -q \
  tests/test_maintenance_project_master_workbook.py \
  tests/test_maintenance_project_master_v2_editable.py \
  --tb=short --junitxml=/home/cloudlay/workspaces/reviews/laneA2-20261021/pytest-master-full.xml

uv run --extra dev pytest -q \
  tests/test_maintenance_row_filters.py \
  tests/test_maintenance_row_filter_operators.py \
  tests/test_maintenance_row_workbench_review.py \
  tests/test_maintenance_legacy_row_filters.py \
  tests/test_maintenance_demand_export_ordering.py \
  --tb=short --junitxml=/home/cloudlay/workspaces/reviews/laneA2-20261021/pytest-row-export.xml

uv run alembic upgrade head && uv run alembic check && uv run alembic heads
```

仓库根目录执行 `git diff --check`。测试执行日志另由 `/usr/bin/time` 记录墙钟耗时；pytest 表内耗时取实际汇总行，不把历史 127 项子集或历史红绿灯计作本轮证据。

| 命令组（上方命令，均全文件） | 通过 | 失败 / 错误 / 跳过 | pytest 耗时（秒） | 墙钟耗时（秒） |
| --- | ---: | --- | ---: | ---: |
| `pytest-notes-export`：项目导出 + 免返工作簿 + 项目备注（3 个全文件） | 115 | 0 / 0 / 0 | 423.14 | 436.68 |
| `pytest-master-full`：主工作簿 + V2 可编辑工作簿（2 个全文件） | 146 | 0 / 0 / 0 | 584.92 | 593.69 |
| `pytest-row-export`：#432 行级筛选/导出 + 跨出口排序（5 个全文件） | 216 | 0 / 0 / 0 | 482.07 | 491.39 |

合计 **477 passed、0 failed、0 errors、0 skipped**，三组退出码均为 0。

| 整文件 | 通过数 |
| --- | ---: |
| `test_maintenance_project_export.py` | 26 |
| `test_maintenance_no_return_note_workbook.py` | 50 |
| `test_maintenance_project_note_workbook.py` | 39 |
| `test_maintenance_project_master_workbook.py` | 46 |
| `test_maintenance_project_master_v2_editable.py` | 100 |
| `test_maintenance_row_filters.py` | 23 |
| `test_maintenance_row_filter_operators.py` | 58 |
| `test_maintenance_row_workbench_review.py` | 96 |
| `test_maintenance_legacy_row_filters.py` | 22 |
| `test_maintenance_demand_export_ordering.py` | 17 |

| 其他检查命令 | 实际结果 | 墙钟耗时（秒） |
| --- | --- | ---: |
| `uv run alembic upgrade head` | 退出 0，专用库升级至 `d8f6a2c4b9e7` | 7.62 |
| `uv run alembic check` | 退出 0，`No new upgrade operations detected.` | 3.71 |
| `uv run alembic heads` | 退出 0，唯一 `d8f6a2c4b9e7 (head)` | 0.81 |
| `git diff --check` | 退出 0，无空白错误 | 0.01 |

告警：pytest 输出 Starlette/httpx 与 openpyxl 弃用提示；Alembic check 输出 SQLAlchemy `dialect_options` SAWarning，但上述命令均以 0 退出，未检测到待生成迁移。

补充类型核对：调用本路径 `_build_xlsx` 后重新读取实际 XLSX，Decimal/int/float 为 `n`，date/带时区 datetime 为 `d`，None 留空，bool 为 `b`，公式/错误样式及首部空白文本为 `s`，原有撇号精确保留。**14 项通过、0 失败，0.110 秒**；日志 `xlsx-scalar-check.log`。此为内存序列化核对，不计入 pytest 数量。

复跑命令（`backend/`，同一测试库环境）：`PYTHONPATH=. uv run python /home/cloudlay/workspaces/reviews/laneA2-20261021/xlsx-scalar-check.py`；脚本及逐例结果均落在证据目录。

关键测试：

- a：`test_export_preserves_text_verbatim_without_formulas`、`test_export_xlsx_uses_current_contract_and_latest_confirmed_collection`、`test_export_enforces_cell_length_after_control_character_cleanup`，覆盖原文、类型及长度边界。
- b：`test_download_displays_no_return_note_as_readonly_plain_text`，V1/V2 共 30 个参数用例，核对完整标签、空值、只读底色、显式文本和项目备注可编辑样式。
- c：`test_validate_and_apply_ignore_no_return_note_without_master_audit[old_label-v2]`、`test_readonly_no_return_note_does_not_block_project_note_writeback[old_label]`，核对旧标签回传忽略与同次项目备注回写。由新模板改旧标签并保留签名构造兼容文件，不声称历史二进制实测。
- #432：`test_xlsx_writes_numbers_as_numeric_cells_and_text_as_text`、`test_csv_keeps_ordinary_signed_values_and_escapes_only_formulas`，加上筛选、权限和跨出口排序的整文件回归。

同模型独立代理只读复核需求符合度、实现质量、main 合并保留、修改范围、三组原始测试/迁移证据及两份文档，未发现阻塞项；不是异构模型审查，也不替代用户实际验收或 main 人工审批。最终文档提交只更新交接记录，受测实现保持不变。

本轮建议 PR 正文完整草稿：`/home/cloudlay/workspaces/reviews/laneA2-20261021/pr424-body-draft.md`。仅写本地文件，不在 GitHub 修改 PR。

## 审查回应与范围外排查

已消除本次共享项目导出的撇号约定，更新原有撇号断言，保留字符清洗、长度上限和数值类型。01 文案统一为 D-32 完整提醒，新旧标签忽略路径由公共 API 契约测试守护。

以下为用户明确限定不改的出口/helper；相对本轮合并的 main 均无差异，前端同样无差异。范围外遗留约定继续等金臣裁定，本轮不顺手修改：

| 位置 | 出口/复用影响 |
| --- | --- |
| `backend/app/api/profit.py:139` | 利润 CSV 维度 |
| `backend/app/api/imports.py:66` | 导入错误 CSV |
| `backend/app/api/maintenance.py:229`（`_safe`） | 项目汇总、明细与看板 CSV |
| `backend/app/services/maintenance_export.py:123` | 维保订单 XLSX（后续已有 `write_string`） |
| `backend/app/services/maintenance_project_workbook_v2.py:280` | V2 工作簿文本及 canonical/digest/metadata |
| `backend/app/services/maintenance_manager_workbook_v3.py:230` | 管理 V3 工作簿文本及 canonical |
| `backend/app/services/replenishment.py:2305` | 补货复核/筛选/WBDD 子集，维保补货证据复用 |
| `backend/app/services/maintenance_workbook_export.py:107,448` | 合同/ZIP 工作簿及 ZIP CSV 清单；项目 V3 与维保 API 复用 |
| `backend/app/services/maintenance_roundtrip.py:466`（`_safe`） | 往返模板字符/长度校验，保留 main 原实现 |

待金臣确认：无新增业务待决项。代码与定向验证已就绪，用户实际验收仍待安排。

本 PR 相对 main 无 schema 或迁移差异、无历史数据更正；main 已有迁移仅在本轮指定测试库验证，不代表生产已迁移。回退仅撤回本次展示与选列代码，不清理主档字段或业务数据。本轮未部署、未操作生产、未启动或删除其他容器；开发验证与 CI 不代表用户实际验收或 main 人工审批。
